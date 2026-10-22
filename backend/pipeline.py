"""Public workflow entry points and version dispatch for persisted OCR runs."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pymupdf as fitz

from .compile_service import cache_candidate_render, generate_snapshot_outputs
from .latex_export import GENERATOR_VERSION
from .model_client import create_model_client
from .models import RunCreate, WorkflowError
from .responses_client import ModelServiceError
from .storage import Storage
from .importers import IMAGE_LOCK, prepare_page
from .workflow_context import WorkflowContext, _RunContext, _Paused, _Unavailable, _SourceUnavailable, _safe_error
from .workflow_legacy import LegacyWorkflow
from .workflow_content import ContentWorkflow
from .workflow_output import OutputWorkflow


# Existing preview routes use the importer-owned shared image lock.
_prepare_page = prepare_page


class BookProcessor(LegacyWorkflow, ContentWorkflow, OutputWorkflow, WorkflowContext):
    """Compose isolated stage owners around one storage instance and run context.

    V1 owns its complete historical execution path. V2 content recovery calls
    the output owner only to retain or restore local layout/render candidates;
    the dispatcher orders content work before output and snapshot generation.
    """

    def __init__(self, storage: Storage):
        self.storage = storage

    async def process(
        self, book_id: str, settings: dict[str, Any], api_key: str,
        pause_requested: asyncio.Event | None = None,
        records: list[dict[str, Any]] | None = None,
        pending_pages: list[int] | None = None,
        manually_saved_pages: set[int] | None = None,
    ) -> None:
        """The old entry starts the same run; revisions protect concurrent manual edits."""
        book = self.storage.get_book(book_id)
        if book is None:
            raise KeyError("book")
        numbers = pending_pages if pending_pages is not None else (
            [record["number"] for record in records] if records is not None else None
        )
        run = self.storage.create_run(
            book_id, RunCreate(pages=list(numbers) if numbers is not None else None,
                               expected_arrangement_revision=book.arrangement_revision,
                               client_request_id=str(uuid4())),
            settings_snapshot=settings, generator_version=GENERATOR_VERSION,
        )
        await self.process_run(book_id, run.run_id, api_key, pause_requested)

    async def process_run(
        self, book_id: str, run_id: str, api_key: str, pause_event: asyncio.Event | None = None,
    ) -> None:
        """The sole dispatcher keeps historical V1 stage data out of V2."""
        run = self.storage.get_run(book_id, run_id)
        if run is None:
            raise KeyError("run")
        if run.workflow_version == 2:
            context = _RunContext(run, self.storage.get_run_book(book_id, run_id),
                                  self.storage.books_root / book_id, pause_event)
            await self._process_run_v2(context, api_key)
        else:
            await self._process_run_v1(book_id, run_id, api_key, pause_event)

    async def _process_run_v2(self, context: _RunContext, api_key: str) -> None:
        """Finish basic responsibilities before ordered recovery and local output."""
        run = context.run
        if run.status == "finished":
            return
        if self._pause_requested(context):
            self.storage.set_run_status(run.book_id, run.run_id, "paused", run.error)
            return
        context.configuration_error = next((task.stage_data.get("configuration_error") for task in run.tasks
                                            if task.stage_data.get("configuration_error")), None)
        self._restore_configuration_error(context)
        self.storage.set_run_status(run.book_id, run.run_id, "running", context.configuration_error)
        # A persisted response is reparsed locally. A sent request without its
        # body is consumed unknown; an unused reservation remains reusable.
        for attempt in self.storage.get_attempts(run.run_id):
            if attempt.sent_at is not None and attempt.settled_at is None:
                response = self.storage.get_recognition_response(attempt.attempt_id)
                saved = bool(response and response.body_storage == "saved")
                unknown = not saved or response.completion_status == "unknown"
                self.storage.finish_run_attempt(
                    attempt.attempt_id, state="unknown" if unknown else "succeeded",
                    usage=response.usage if response else attempt.usage,
                    provider_request_id=response.provider_request_id if response else attempt.provider_request_id,
                    error=attempt.error or ("上次已发送请求没有已保存的完成响应；不自动重发" if unknown else None),
                )
        try:
            for task in run.tasks:
                for value in task.stage_data.get("inherited_errors", []):
                    error = WorkflowError.model_validate(value)
                    if error not in self._task(context, task.page_id).errors:
                        self._update(context, task.page_id, workflow_error=error)
                await self._reuse_inherited_render_v2(context, task.page_id)
            pending = [task.page_id for task in self.storage.get_run_tasks(run.run_id) if task.outcome is None]
            if any(self._task(context, page_id).stage_data.get("basic_recognition_complete") for page_id in pending):
                context.first_recognized.set()

            async def worker(first: bool) -> None:
                if not first:
                    await context.first_recognized.wait()
                client = None
                try:
                    while pending:
                        self._checkpoint(context, source_only=True)
                        page_id = pending.pop(0)
                        if client is None and not context.configuration_error:
                            if not api_key or not run.settings_snapshot.get("extraction_model"):
                                context.configuration_error = "模型密钥或页面识别模型未配置；保留已取得内容及源页"
                            else:
                                try:
                                    client = create_model_client(run.settings_snapshot, api_key)
                                except ModelServiceError as exc:
                                    context.configuration_error = _safe_error(exc)
                            if context.configuration_error:
                                self._update(context, page_id, stage_data={"configuration_error": context.configuration_error})
                                context.first_recognized.set()
                        try:
                            await self._basic_page_v2(context, page_id, client)
                        except _Paused:
                            return
                        except _SourceUnavailable as exc:
                            self._error_v2(context, page_id, "prepare", "unreadable_source", _safe_error(exc))
                            self._update(context, page_id, stage_data={"v2_source_unavailable": True,
                                         "basic_recognition_unavailable": True, "basic_review_unavailable": True})
                        except (_Unavailable, OSError, ValueError, KeyError, TypeError, fitz.FileDataError) as exc:
                            task = self._task(context, page_id)
                            if not isinstance(exc, _Unavailable) or not exc.recorded:
                                self._error_v2(context, page_id, task.stage, "invalid_structure", _safe_error(exc))
                            self._update(context, page_id, stage_data={
                                "v2_analysis_unavailable": page_id not in context.prepared_pages,
                                "basic_recognition_unavailable": not task.stage_data.get("basic_recognition_complete", False),
                                "basic_review_unavailable": not task.stage_data.get("basic_review_complete", False)})
                except _Paused:
                    return
                finally:
                    if first:
                        context.first_recognized.set()

            concurrency = max(1, int(run.settings_snapshot.get("processing_concurrency", 2)))
            if pending:
                async with asyncio.TaskGroup() as group:
                    for index in range(min(concurrency, len(pending))):
                        group.create_task(worker(index == 0))
            self._checkpoint(context, source_only=True)
            # Frozen order controls use of the remaining shared budget, rather
            # than whichever basic worker finishes first. Rendering is serial.
            client = None
            if not context.configuration_error and api_key and run.settings_snapshot.get("extraction_model"):
                try:
                    client = create_model_client(run.settings_snapshot, api_key)
                except ModelServiceError as exc:
                    context.configuration_error = _safe_error(exc)
            for page_id in run.page_ids:
                self._checkpoint(context, source_only=True)
                task = self._task(context, page_id)
                if task.outcome is not None:
                    continue
                if not task.stage_data.get("v2_source_unavailable") and not task.stage_data.get("v2_analysis_unavailable"):
                    try:
                        await self._recover_page_v2(context, page_id, client)
                    except (_Unavailable, OSError, ValueError, KeyError, TypeError) as exc:
                        if not isinstance(exc, _Unavailable) or not exc.recorded:
                            self._error_v2(context, page_id, "recover", "invalid_structure", _safe_error(exc), phase="recovery")
                        recovery = self._task(context, page_id).stage_data.get("v2_recovery", {})
                        if recovery.get("base_revision_id") and not recovery.get("done"):
                            base = self._revision(recovery["base_revision_id"])
                            restored = self._save_content_v2(context, page_id, base.page_content)
                            if base.page_layout is not None:
                                restored = self._save_layout_v2(context, page_id, base.page_layout, restored)
                                cached = self._load_render_v2(context, base)
                                if cached:
                                    try:
                                        await cache_candidate_render(context.book, restored, context.book_dir, result=cached)
                                    except (OSError, ValueError, KeyError, TypeError) as bind_error:
                                        self._error_v2(context, page_id, "render", "render", _safe_error(bind_error), phase="output")
                await self._finish_page_v2(context, page_id)
            self._checkpoint(context, source_only=True)
            snapshot = self.storage.create_output_snapshot(run.book_id, run.run_id)
            for page_id in run.page_ids:
                self._update(context, page_id, stage="export", state="running")
            snapshot = await generate_snapshot_outputs(self.storage, snapshot, context.book_dir)
            # An active export settles its formats before acknowledging pause.
            for page_id in run.page_ids:
                self._update(context, page_id, state="finished", completed_stage="export")
            if self._pause_requested(context):
                self.storage.set_run_status(run.book_id, run.run_id, "paused", context.configuration_error)
                return
            failures = [value.error for value in snapshot.formats.values() if value.status == "failed" and value.error]
            self.storage.set_run_status(run.book_id, run.run_id, "finished",
                                        context.configuration_error or ("部分格式生成失败：" + failures[0] if failures else None))
        except _Paused:
            self.storage.set_run_status(run.book_id, run.run_id, "paused", context.configuration_error)
        except asyncio.CancelledError:
            self.storage.set_run_status(run.book_id, run.run_id, "interrupted", "执行已中断；已保存阶段复用，已发送未知请求不重发")
            raise
        except Exception:
            self.storage.set_run_status(run.book_id, run.run_id, "interrupted", "技术步骤中断；内容、候选及响应保持已保存状态")
            raise


WorkflowProcessor = BookProcessor
