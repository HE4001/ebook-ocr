"""The fixed, unattended OCR workflow, with persisted request and compile limits."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymupdf as fitz
from PIL import Image

from .compile_service import (
    CandidateRenderResult, cache_candidate_render, generate_manifest_outputs, generate_snapshot_outputs,
    load_candidate_render, preserve_source_page, preserve_source_regions, render_candidate,
)
from .content_contract import UnresolvedSpan, ContentIssue, ContentParseResult, PageContent, PageRegion
from .importers import IMAGE_LOCK, prepare_analyzed_page, prepare_page
from .latex_diagnostics import local_geometry_adjustment
from .latex_export import GENERATOR_VERSION, FidelityLayoutError, generate_source_fidelity_latex
from .layout_contract import CropMapping, LayoutObservation, PageLayout, PageLinePlacement, PageSourceMetadata, SourceFidelityLayout
from .layout_solver import derive_layout, solve_layout, summarize_book_styles, to_source_fidelity_layout
from .model_client import create_model_client
from .models import Assessment, Book, PageOutcome, PageTask, Revision, Run, RunCreate, StructuredPageResult, WorkflowError
from .prompts import (
    WORKFLOW_RECOGNITION_PROMPT, WORKFLOW_REPAIR_PROMPT, WORKFLOW_REVIEW_PROMPT, page_context,
)
from .quality_service import (
    RULE_VERSION, apply_repair, assess, improves, issue, now, passed,
    preservation_regions, rebind_assessment, render_score, validate_recognition,
    content_conclusion, content_improves, local_content_candidate,
    recovery_targets, renew_content, reviewed_content, source_coverage_content,
)
from .responses_client import (
    CONFIGURATION_STATUS, ContentClientV2, ModelRequestResult, ModelServiceError, RequestDiagnostic, _saved_http_status,
)
from .source_analysis import SourceAnalysis, analyze_source, persist_source_region, prepare_recognition_inputs, source_reading_order
from .workflow_model_contract import ContentReview
from .storage import RequestBudgetExceeded, Storage


# Existing preview routes use the importer-owned shared image lock.
_prepare_page = prepare_page


class _Paused(Exception):
    pass


class _ConfigurationStopped(Exception):
    pass


class _Unavailable(Exception):
    """A settled or unrepeatable operation has no usable persisted result."""

    def __init__(self, message: str, *, recorded: bool = False, category: str | None = None):
        super().__init__(message)
        self.recorded = recorded
        self.category = category


class _SourceUnavailable(Exception):
    """Source preparation actually failed to read the frozen file/page."""


@dataclass
class _RunContext:
    run: Run
    book: Book
    book_dir: Path
    pause_event: asyncio.Event | None
    first_recognized: asyncio.Event = field(default_factory=asyncio.Event)
    configuration_error: str | None = None
    style_samples: list[SourceFidelityLayout] = field(default_factory=list)
    style_summary: dict[str, Any] = field(default_factory=dict)
    prepared_pages: dict[str, tuple[dict[str, Any], Path, PageSourceMetadata, SourceAnalysis]] = field(default_factory=dict)


class BookProcessor:
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

    def _error_v2(self, context: _RunContext, page_id: str, stage: str, category: str, message: str,
                  *, phase: str = "initial", attempt_id: str | None = None, field_path: str | None = None,
                  block_id: str | None = None, bbox=None) -> WorkflowError:
        error = WorkflowError(stage=stage, category=category, message=message[:2_000], phase=phase,
                              attempt_id=attempt_id, field_path=field_path, block_id=block_id, source_bbox=bbox)
        if error not in self._task(context, page_id).errors:
            self._update(context, page_id, workflow_error=error)
        return error

    def _restore_configuration_error(self, context: _RunContext) -> None:
        if context.configuration_error:
            return
        for attempt in self.storage.get_attempts(context.run.run_id):
            if _saved_http_status(attempt.error) in CONFIGURATION_STATUS:
                context.configuration_error = attempt.error
            elif context.run.workflow_version == 2:
                response = self.storage.get_recognition_response(attempt.attempt_id)
                error = next((item for item in response.parse_errors if item.category == "service_configuration"), None) if response else None
                if error:
                    context.configuration_error = error.message
            if context.configuration_error:
                self._update(context, attempt.page_id, stage_data={"configuration_error": context.configuration_error})
                return

    def _unknown_consumption_v2(self, context: _RunContext, page_id: str) -> bool:
        attempts = self.storage.get_attempts(context.run.run_id, page_id)
        identities = {item.attempt_id for item in attempts}
        return (any(item.state == "unknown" for item in attempts)
                or any(error.category == "unknown_consumption" and error.attempt_id in identities
                       for error in self._task(context, page_id).errors))

    def _operation_v2(self, context: _RunContext, page_id: str, name: str) -> dict[str, Any]:
        return dict(self._task(context, page_id).stage_data.get("v2_operations", {}).get(name, {}))

    def _store_operation_v2(self, context: _RunContext, page_id: str, name: str, operation: dict[str, Any]) -> None:
        operations = dict(self._task(context, page_id).stage_data.get("v2_operations", {}))
        operations[name] = operation
        self._update(context, page_id, stage_data={"v2_operations": operations})

    async def _prepare_v2(self, context: _RunContext, page_id: str):
        self._checkpoint(context, source_only=True)
        if page_id in context.prepared_pages:
            return context.prepared_pages[page_id]
        record = self.storage.get_run_page_record(context.run.run_id, page_id)
        prepared = self._task(context, page_id).stage_data.get("prepared")
        if prepared is None:
            self._update(context, page_id, stage="prepare", state="running")
            try:
                width, height, image_name, metadata, analysis = await asyncio.to_thread(
                    prepare_analyzed_page, context.book_dir, record, max_crops=3)
            except (OSError, fitz.FileDataError) as exc:
                # Preparation includes analysis/cache writes. Only an actual
                # failed read of the original frozen source proves unreadable.
                failure = await asyncio.to_thread(self._source_read_failure_v2, context, page_id)
                if failure is not None:
                    raise _SourceUnavailable(failure) from exc
                raise
            prepared = {"width": width, "height": height, "image_name": image_name,
                        "metadata": metadata.model_dump(mode="json")}
            self._update(context, page_id, completed_stage="prepare", stage_data={"prepared": prepared})
        else:
            metadata = PageSourceMetadata.model_validate(prepared["metadata"])
            analysis = await asyncio.to_thread(analyze_source, context.book_dir / prepared["image_name"], metadata,
                                               book_dir=context.book_dir, max_crops=3)
        record.update(width=prepared["width"], height=prepared["height"], image_name=prepared["image_name"])
        context.prepared_pages[page_id] = record, context.book_dir / prepared["image_name"], metadata, analysis
        return context.prepared_pages[page_id]

    def _source_read_failure_v2(self, context: _RunContext, page_id: str) -> str | None:
        """Read the original on an error path; cache/output I/O is not evidence."""
        record = self.storage.get_run_page_record(context.run.run_id, page_id)
        directory = context.book_dir / record["source_directory"]
        try:
            with IMAGE_LOCK:
                if record["source_kind"] == "pdf":
                    with fitz.open(directory / "source.pdf") as document:
                        if document.needs_pass or not 1 <= record["source_page"] <= len(document):
                            return "冻结源 PDF 页面不存在或已无法读取"
                        # Loading this actual page establishes source access;
                        # no rendering, OCR or output write is performed here.
                        document.load_page(record["source_page"] - 1).rect
                else:
                    source = directory / ("source" + Path(record["source_filename"]).suffix.lower())
                    with Image.open(source) as image:
                        image.load()
        except (OSError, ValueError, RuntimeError, fitz.FileDataError) as exc:
            return "冻结原始源页面读取失败：" + _safe_error(exc)
        return None

    def _saved_result_v2(self, context: _RunContext, page_id: str, stage: str, operation: dict,
                         *, content: PageContent | None = None, target: dict | None = None, phase: str = "initial"):
        attempt_id = operation.get("attempt_id")
        response = self.storage.get_recognition_response(attempt_id) if attempt_id else None
        attempt = next((item for item in self.storage.get_attempts(context.run.run_id, page_id)
                        if item.attempt_id == attempt_id), None)
        status = _saved_http_status(attempt.error) if attempt else None
        service_error = next((item for item in response.parse_errors if item.category in {
            "service_configuration", "temporary_service", "unknown_consumption"}), None) if response else None
        if status is not None or service_error:
            if status is not None:
                category = "service_configuration" if status in CONFIGURATION_STATUS else "temporary_service"
                reason = attempt.error
            else:
                category, reason = service_error.category, service_error.message
            error = self._error_v2(context, page_id, stage, category, reason, phase=phase, attempt_id=attempt_id)
            if response is not None:
                self.storage.record_response_parse_errors(attempt_id, [error])
            if category == "service_configuration":
                context.configuration_error = reason
                context.first_recognized.set()
                self._update(context, page_id, stage_data={"configuration_error": reason})
            raise _Unavailable(reason, recorded=True, category=category)
        if response is None or response.body_storage != "saved":
            return None
        body = self.storage.read_recognition_body(attempt_id)
        completion = "truncated" if response.body_truncated else response.completion_status
        if completion == "unknown":
            try:
                envelope = json.loads(body)
            except (ValueError, RecursionError):
                envelope = None
            if isinstance(envelope, dict) and "error" in envelope:
                reason = "已保存模型服务错误响应但未保留 HTTP 状态；消费结果未知，不自动重发或继续本页新请求"
                error = self._error_v2(context, page_id, stage, "unknown_consumption", reason,
                                       phase=phase, attempt_id=attempt_id)
                self.storage.record_response_parse_errors(attempt_id, [error])
                raise _Unavailable(reason, recorded=True, category="unknown_consumption")
        diagnostics = []
        if operation.get("kind") == "region_plan":
            try:
                parsed = ContentClientV2.parse_saved_region_plan(body, source_version=self._task(context, page_id).source_version, completion_status=completion)
            except (ValueError, RecursionError):
                parsed = None
                diagnostics = [RequestDiagnostic(stage, "invalid_structure", "$", "区域计划响应无有效绑定边界")]
        elif stage == "review":
            try:
                parsed = ContentClientV2.parse_saved_review(body, content, completion_status=completion)
            except (ValueError, RecursionError):
                parsed = None
                diagnostics = [RequestDiagnostic(stage, "truncated_response" if completion == "truncated" else "invalid_structure",
                                                 "$", "已保存复核响应未完成或不符合当前内容职责")]
        else:
            mapping = CropMapping.model_validate(operation["crop_mapping"]) if operation.get("crop_mapping") else None
            parsed = ContentClientV2.parse_saved_content(
                body, book_id=context.run.book_id, page_id=page_id, source_version=self._task(context, page_id).source_version,
                response_id=attempt_id, completion_status=completion,
                content_revision_id=operation["parse_content_revision_id"], crop_mapping=mapping,
                recognition_scope=operation.get("recognition_scope", "legacy_all_visible"),
                target_regions=[PageRegion.model_validate(item) for item in operation.get("target_regions", [])])
            diagnostics = [RequestDiagnostic(stage, failure.category, failure.field_path, failure.reason) for failure in parsed.failures]
            if parsed.content is not None:
                if parsed.content.recognition_scope == "printed_original_only":
                    order = {region.region_id: region.reading_order for region in parsed.content.regions}
                    parsed.content.blocks.sort(key=lambda block: order[block.source_region_id])
                # These IDs depend only on the persisted response and item
                # positions, so interruption cannot create another authority.
                remap = {}
                for index, block in enumerate(parsed.content.blocks):
                    identity = f"{attempt_id}-b{block.response_index if block.response_index is not None else index}"
                    remap[block.block_id], block.block_id = identity, identity
                    if target and target.get("source_region_id"):
                        block.source_region_id = target["source_region_id"]
                    for number, line in enumerate(getattr(block, "lines", [])):
                        line.line_id = f"{identity}-l{number}"
                    for number, cell in enumerate(getattr(block, "cells", [])):
                        cell.cell_id = f"{identity}-c{number}"
                        for line_number, line in enumerate(cell.lines):
                            line.line_id = f"{cell.cell_id}-l{line_number}"
                for index, problem in enumerate(parsed.content.issues):
                    problem.issue_id = f"{attempt_id}-i{index}"
                    problem.block_id = remap.get(problem.block_id, problem.block_id)
                diagnostics.extend(RequestDiagnostic(stage, problem.category, problem.field_path or "$", problem.reason)
                                   for problem in parsed.content.issues if problem.response_index is None)
        return ModelRequestResult(attempt_id=attempt_id, response_id=attempt_id, completion_status=completion,
                                  usage=response.usage, provider_request_id=response.provider_request_id,
                                  parsed=parsed, diagnostics=diagnostics)

    def _diagnostics_v2(self, context: _RunContext, page_id: str, stage: str, result, phase: str) -> None:
        errors = [self._error_v2(context, page_id, stage, item.category, item.reason, phase=phase,
                                 attempt_id=result.attempt_id, field_path=item.field_path) for item in result.diagnostics]
        self.storage.record_response_parse_errors(result.attempt_id, errors)

    async def _request_v2(self, context: _RunContext, page_id: str, client: Any, stage: str, name: str,
                          inputs, *, content: PageContent | None = None, target: dict | None = None,
                          reserved_attempt_id: str | None = None, target_regions: list[PageRegion] | None = None):
        operation = self._operation_v2(context, page_id, name)
        if not operation:
            regions = target_regions if target_regions is not None else [PageRegion.model_validate(item)
                for item in self._task(context, page_id).stage_data.get("region_plan", [])]
            if target:
                regions = [item for item in regions if item.region_id == target.get("source_region_id")]
            operation = {"parse_content_revision_id": str(uuid4()), "reservation_key": str(uuid4()),
                         "result_content_revision_id": str(uuid4()), "kind": "region_plan" if name == "region-plan" else "content",
                         "recognition_scope": self._task(context, page_id).stage_data.get("recognition_scope", "legacy_all_visible"),
                         "source_version": self._task(context, page_id).source_version,
                         "target_regions": [item.model_dump(mode="json") for item in regions]}
            if reserved_attempt_id:
                operation["attempt_id"] = reserved_attempt_id
            self._store_operation_v2(context, page_id, name, operation)
        if "recognition_scope" not in operation and content is not None:
            operation.update(recognition_scope=content.recognition_scope, source_version=content.source_version,
                target_regions=[item.model_dump(mode="json") for item in content.regions])
            self._store_operation_v2(context, page_id, name, operation)
        if "recognition_scope" not in operation and self._task(context, page_id).stage_data.get("recognition_scope", "legacy_all_visible") == "legacy_all_visible":
            operation.update(recognition_scope="legacy_all_visible", source_version=self._task(context, page_id).source_version,
                target_regions=[])
            self._store_operation_v2(context, page_id, name, operation)
        phase = "initial" if name in {"region-plan", "recognize", "review"} or name.startswith("recognize-batch-") else "recovery"
        while True:
            attempts = self.storage.get_attempts(context.run.run_id, page_id)
            previous = next((item for item in attempts
                             if item.attempt_id == operation.get("attempt_id")), None)
            if previous and previous.state == "failed" and previous.retryable:
                unused = next((item for item in attempts if item.retry_of_attempt_id == previous.attempt_id
                               and item.state == "reserved" and item.sent_at is None), None)
                if unused is not None:
                    # Reservation and operation persistence are separate small
                    # transactions. Recover this unused child before checking
                    # the already-counted shared retry limit.
                    operation["attempt_id"] = unused.attempt_id
                    self._store_operation_v2(context, page_id, name, operation)
                    continue
            retry = bool(previous and previous.state == "failed" and previous.retryable)
            if not retry:
                saved = self._saved_result_v2(context, page_id, stage, operation, content=content, target=target, phase=phase)
                if saved is not None:
                    self._diagnostics_v2(context, page_id, stage, saved, phase)
                    return saved
            if previous and not retry and (previous.state != "reserved" or previous.sent_at is not None):
                reason = "本职责已发送但没有可用已保存结果；不重复未知消费请求"
                self._error_v2(context, page_id, stage, "unknown_consumption", reason, phase=phase, attempt_id=previous.attempt_id)
                raise _Unavailable(reason, recorded=True, category="unknown_consumption")
            if retry and self._task(context, page_id).retry_count >= context.run.policy.temporary_retry_limit:
                raise _Unavailable("本页共享暂时错误重试额度已用尽")
            if self._unknown_consumption_v2(context, page_id):
                reason = "本页存在未知消费请求；禁止继续新的模型请求"
                self._error_v2(context, page_id, stage, "unknown_consumption", reason, phase=phase)
                raise _Unavailable(reason, recorded=True, category="unknown_consumption")
            self._checkpoint(context)
            if client is None:
                reason = context.configuration_error or "内容模型服务不可用"
                self._error_v2(context, page_id, stage, "service_configuration", reason, phase=phase)
                raise _Unavailable(reason, recorded=True, category="service_configuration")
            if target:
                crop = next((item.mapping for item in inputs if item.mapping is not None), None)
                operation["crop_mapping"] = crop.model_dump(mode="json") if crop else None
                self._store_operation_v2(context, page_id, name, operation)

            def start(*, retry: bool = False):
                self._checkpoint(context)
                reusable = previous if previous and previous.state == "reserved" and previous.sent_at is None else None
                retry_of = previous.attempt_id if retry and previous is not None else None
                attempt = reusable or self.storage.reserve_attempt(
                    context.run.run_id, page_id, stage, retry=retry,
                    purpose="retry" if retry else "region_plan" if name == "region-plan" else "basic_review" if stage == "review" else "local_recognition" if name.startswith("recognize-batch-") else "basic_recognition",
                    block_ids=[target["id"]] if target else [item["region_id"] for item in operation.get("target_regions", [])],
                    retry_of_attempt_id=retry_of,
                    reservation_key=operation["reservation_key"] + (f"-retry-of-{retry_of}" if retry_of else ""))
                operation["attempt_id"] = attempt.attempt_id
                self._store_operation_v2(context, page_id, name, operation)
                self._update(context, page_id, stage=stage, state="running")
                return self.storage.mark_attempt_sent(attempt.attempt_id)

            def end(attempt_id: str, **values):
                self.storage.finish_run_attempt(attempt_id, **values)

            callbacks = {"on_attempt_start": start, "on_attempt_end": end,
                         "persist_response": self.storage.save_recognition_response, "retry": retry}
            try:
                model = context.run.settings_snapshot["extraction_model"]
                scope = operation["recognition_scope"]
                regions = [PageRegion.model_validate(item) for item in operation.get("target_regions", [])]
                if name == "region-plan":
                    result = await client.plan_regions(model, inputs, book_id=context.run.book_id,
                        page_id=page_id, source_version=self._task(context, page_id).source_version,
                        recognition_scope=scope, **callbacks)
                elif stage == "review":
                    result = await client.review_content(model, inputs, content, **callbacks)
                elif target:
                    result = await client.reread_content(
                        model, inputs, book_id=context.run.book_id, page_id=page_id,
                        source_version=self._task(context, page_id).source_version, target_bbox=target["bbox"],
                        target_kind=target["kind"], target_crop=crop, source_region_id=target.get("source_region_id"),
                        recognition_scope=scope, target_regions=[item for item in regions if item.region_id == target.get("source_region_id")], **callbacks)
                else:
                    analysis = context.prepared_pages[page_id][3]
                    evidence = [{"bbox": item.bbox, "text": item.text, "visible_geometry_correspondence": item.visible_ink}
                                for item in analysis.pdf_text_lines]
                    result = await client.recognize_content(
                        model, inputs, book_id=context.run.book_id, page_id=page_id,
                        source_version=self._task(context, page_id).source_version,
                        recognition_scope=scope, target_regions=regions, coarse_regions=analysis.regions, native_text_evidence=json.dumps(evidence, ensure_ascii=False) if evidence else None,
                        **callbacks)
            except RequestBudgetExceeded as exc:
                self._checkpoint(context)
                self._error_v2(context, page_id, stage, "budget", _safe_error(exc), phase=phase)
                raise _Unavailable(_safe_error(exc), recorded=True, category="budget") from exc
            except ModelServiceError as exc:
                category = exc.reason_category or ("service_configuration" if exc.configuration_error else
                            "unknown_consumption" if exc.result_uncertain else "temporary_service" if exc.retryable else "invalid_structure")
                error = self._error_v2(context, page_id, stage, category, _safe_error(exc), phase=phase,
                                       attempt_id=operation.get("attempt_id"), field_path=exc.field_path)
                if exc.response_id and self.storage.get_recognition_response(exc.response_id) is not None:
                    self.storage.record_response_parse_errors(exc.response_id, [error])
                if exc.configuration_error:
                    context.configuration_error = _safe_error(exc)
                    context.first_recognized.set()
                    self._update(context, page_id, stage_data={"configuration_error": context.configuration_error})
                if not exc.retryable or exc.configuration_error or exc.result_uncertain:
                    raise _Unavailable(_safe_error(exc), recorded=True, category=category) from exc
                continue
            # Both fresh and resumed results use the exact persisted bounded
            # body, its completion marker and stable program-owned identities.
            result = self._saved_result_v2(context, page_id, stage, operation, content=content, target=target, phase=phase) or result
            self._diagnostics_v2(context, page_id, stage, result, phase)
            return result

    def _save_content_v2(self, context: _RunContext, page_id: str, content: PageContent) -> Revision:
        revision = self.storage.save_content_candidate(context.run.run_id, page_id, content,
                    expected_candidate_revision_id=self._task(context, page_id).candidate_revision_id)
        if content.recognition_scope == "printed_original_only" and content.regions:
            self._update(context, page_id, stage_data={"region_plan": [region.model_dump(mode="json") for region in content.regions]})
        return revision

    def _empty_content_v2(self, context: _RunContext, page_id: str, reason: str,
                          *, category: str = "missing_content") -> PageContent:
        task = self._task(context, page_id)
        return PageContent(content_revision_id=str(uuid4()), book_id=context.run.book_id, page_id=page_id,
                           source_version=task.source_version, recognition_scope=task.stage_data.get("recognition_scope", "legacy_all_visible"), issues=[ContentIssue(category=category, reason=reason)])

    async def _apply_review_v2(self, context: _RunContext, page_id: str, client, inputs, name: str,
                               *, targets=(), reserved_attempt_id=None) -> tuple[Revision, ContentReview | None]:
        operation = self._operation_v2(context, page_id, name)
        current = self._revision(self._task(context, page_id).candidate_revision_id)
        if operation.get("result_content_revision_id") == current.page_content.content_revision_id:
            saved = self._saved_result_v2(context, page_id, "review", operation,
                         content=self._revision(operation["input_revision_id"]).page_content,
                         phase="initial" if name == "review" else "recovery")
            return current, saved.parsed if saved else None
        if not operation:
            operation = {"parse_content_revision_id": str(uuid4()), "reservation_key": str(uuid4()),
                         "result_content_revision_id": str(uuid4()), "input_revision_id": current.revision_id,
                         "kind": "review", "recognition_scope": current.page_content.recognition_scope,
                         "source_version": current.page_content.source_version,
                         "target_regions": [item.model_dump(mode="json") for item in current.page_content.regions]}
            if reserved_attempt_id:
                operation["attempt_id"] = reserved_attempt_id
            self._store_operation_v2(context, page_id, name, operation)
        base = self._revision(operation["input_revision_id"])
        if current.page_content != base.page_content:
            raise _Unavailable("复核绑定内容已变化；不能将旧结论应用到新内容")
        review, reason, failure_category = None, None, None
        try:
            result = await self._request_v2(context, page_id, client, "review", name, inputs,
                                          content=base.page_content, reserved_attempt_id=reserved_attempt_id)
            review = result.parsed
        except (_Unavailable, _ConfigurationStopped) as exc:
            reason = _safe_error(exc)
            if isinstance(exc, _ConfigurationStopped):
                failure_category = "service_configuration"
                self._error_v2(context, page_id, "review", "service_configuration", reason,
                               phase="initial" if name == "review" else "recovery")
            else:
                failure_category = exc.category
        operation = self._operation_v2(context, page_id, name)
        updated = reviewed_content(base.page_content, review, reason=reason, checked_targets=targets)
        updated.content_revision_id = operation["result_content_revision_id"]
        for block in updated.blocks:
            block.content_revision_id = updated.content_revision_id
        old_ids = {item.issue_id for item in base.page_content.issues}
        for index, problem in enumerate(updated.issues):
            if problem.issue_id not in old_ids:
                problem.issue_id = f"{updated.content_revision_id}-i{index}"
                if review is None and failure_category:
                    problem.category = failure_category
        response = self.storage.get_recognition_response(operation.get("attempt_id")) if operation.get("attempt_id") else None
        if response and response.body_storage == "saved":
            updated.response_ids = list(dict.fromkeys([*updated.response_ids, response.attempt_id]))
        revision = self._save_content_v2(context, page_id, updated)
        adoption = ({region.region_id: updated.content_revision_id for region in updated.regions
                     if any(block.source_region_id == region.region_id for block in updated.blocks)}
                    if name == "review" else dict(self._task(context, page_id).stage_data.get("region_adoptions", {})))
        self._update(context, page_id, stage_data={name + "_applied_revision_id": revision.revision_id,
                     "region_adoptions": adoption,
                     "region_plan": [region.model_dump(mode="json") for region in updated.regions]})
        return revision, review

    async def _basic_page_v2(self, context: _RunContext, page_id: str, client) -> None:
        _, image_path, metadata, analysis = await self._prepare_v2(context, page_id)
        self._checkpoint(context, source_only=True)
        inputs = await asyncio.to_thread(prepare_recognition_inputs, image_path, metadata, analysis,
                                         book_dir=context.book_dir, max_crops=3)
        task = self._task(context, page_id)
        if task.stage_data.get("recognition_scope") == "printed_original_only" and not task.stage_data.get("region_plan_complete"):
            try:
                planned = await self._request_v2(context, page_id, client, "recognize", "region-plan", inputs)
                plan = planned.parsed
                if plan is None:
                    raise _Unavailable("区域计划未取得可用边界；不能开始无绑定的印刷转录")
                self._update(context, page_id, stage_data={"region_plan_complete": True, "region_plan": [item.model_dump(mode="json") for item in plan.regions]})
            except (_Unavailable, _ConfigurationStopped) as exc:
                self._save_content_v2(context, page_id, self._empty_content_v2(context, page_id, _safe_error(exc)))
                self._update(context, page_id, stage_data={"region_plan_unavailable": True,
                    "basic_recognition_unavailable": True, "basic_review_unavailable": True})
                return
            task = self._task(context, page_id)
        if (task.stage_data.get("recognition_scope") == "printed_original_only"
            and not task.stage_data.get("initial_batches_finished")
            and ("recognition_batches" in task.stage_data or not task.stage_data.get("basic_recognition_complete"))):
            if "recognition_batches" not in task.stage_data:
                regions = sorted((PageRegion.model_validate(item) for item in task.stage_data.get("region_plan", [])), key=lambda item: item.reading_order)
                batches, simple = [], []
                for region in regions:
                    if region.layer in {"annotation", "noise", "decoration", "unknown"}:
                        continue
                    if region.kind == "table" or region.layer == "mixed":
                        if simple:
                            batches.append(simple)
                            simple = []
                        batches.append([region.model_dump(mode="json")])
                    else:
                        simple.append(region.model_dump(mode="json"))
                        if len(simple) == 3:
                            batches.append(simple)
                            simple = []
                if simple:
                    batches.append(simple)
                initial = self._empty_content_v2(context, page_id, "印刷区域转录尚未完成")
                initial.issues = [ContentIssue(issue_id=f"{region.region_id}-initial-pending", region_id=region.region_id,
                    stable_target_id=region.region_id, category="missing_content", reason="印刷区域尚未完成转录", origin="local_constraint")
                    for region in regions if region.layer in {"printed", "mixed", "unknown"}]
                initial.regions = regions
                initial.blank = not any(region.layer in {"printed", "mixed", "unknown"} for region in regions)
                initial.page_kind = "blank" if initial.blank else "content"
                initial.unresolved_spans.extend(UnresolvedSpan(region_id=region.region_id, source_bbox=region.bbox,
                    reason="区域来源尚未确定，等待整页覆盖复核") for region in regions if region.layer == "unknown")
                self._save_content_v2(context, page_id, initial)
                self._update(context, page_id, stage_data={"recognition_batches": batches, "recognition_batches_done": []})
            batches = self._task(context, page_id).stage_data["recognition_batches"]
            for index, batch in enumerate(batches):
                done = list(self._task(context, page_id).stage_data.get("recognition_batches_done", []))
                if index in done:
                    continue
                current = self._revision(self._task(context, page_id).candidate_revision_id).page_content
                operation_name = f"recognize-batch-{index}"
                try:
                    result = await self._request_v2(context, page_id, client, "recognize", operation_name, inputs,
                        target_regions=[PageRegion.model_validate(item) for item in batch])
                    parsed = result.parsed.content if isinstance(result.parsed, ContentParseResult) else None
                    if result.response_id not in current.response_ids:
                        merged = renew_content(current)
                        if parsed is not None:
                            transcribed_ids = {block.source_region_id for block in parsed.blocks}
                            merged.issues = [item for item in merged.issues if not (item.region_id in transcribed_ids and item.issue_id == f"{item.region_id}-initial-pending")]
                            if parsed.page_kind != "blank":
                                if not current.blocks or current.page_kind == "content":
                                    merged.page_kind = parsed.page_kind
                                elif current.page_kind != parsed.page_kind:
                                    merged.issues.append(ContentIssue(category="invalid_structure", reason="不同区域批次的页面类型判断不一致"))
                            merged.blank = False
                            merged.blocks.extend(parsed.blocks)
                            merged.issues.extend(parsed.issues)
                            merged.unresolved_spans.extend(parsed.unresolved_spans)
                            merged.response_ids.extend(parsed.response_ids)
                        else:
                            merged.issues.extend(ContentIssue(category="missing_content", region_id=item["region_id"], reason="本批印刷区域未取得有效转录") for item in batch)
                        if result.response_id and result.response_id not in merged.response_ids:
                            merged.response_ids.append(result.response_id)
                        for block in merged.blocks:
                            block.content_revision_id = merged.content_revision_id
                        self._save_content_v2(context, page_id, merged)
                except (_Unavailable, _ConfigurationStopped) as exc:
                    merged = renew_content(current)
                    merged.issues.extend(ContentIssue(category="missing_content", region_id=item["region_id"], reason=_safe_error(exc)) for item in batch)
                    self._save_content_v2(context, page_id, merged)
                self._update(context, page_id, stage_data={"recognition_batches_done": [*done, index]})
            current = self._revision(self._task(context, page_id).candidate_revision_id).page_content
            self._update(context, page_id, stage_data={"initial_batches_finished": True,
                "basic_recognition_complete": True, "basic_recognition_unavailable": False})
            task = self._task(context, page_id)
        if task.stage_data.get("recognition_scope") != "printed_original_only" and not task.stage_data.get("basic_recognition_complete") and not task.stage_data.get("basic_recognition_unavailable"):
            content, reason, category = None, "页面未取得可用内容响应", "missing_content"
            try:
                result = await self._request_v2(context, page_id, client, "recognize", "recognize", inputs)
                content = result.parsed.content if isinstance(result.parsed, ContentParseResult) else None
                if content is None and result.diagnostics:
                    reason = result.diagnostics[0].reason
                if content is None:
                    content = self._empty_content_v2(context, page_id, reason)
                    content.content_revision_id = self._operation_v2(context, page_id, "recognize")["parse_content_revision_id"]
                    content.issues[0].issue_id = f"{result.attempt_id}-unavailable"
                    content.issues[0].category = result.diagnostics[0].category if result.diagnostics else "invalid_structure"
                    content.response_ids = [result.response_id] if result.response_id else []
            except (_Unavailable, _ConfigurationStopped) as exc:
                reason = _safe_error(exc)
                if isinstance(exc, _ConfigurationStopped):
                    category = "service_configuration"
                    self._error_v2(context, page_id, "recognize", "service_configuration", reason)
                else:
                    category = exc.category or category
            if content is not None:
                self._save_content_v2(context, page_id, content)
            else:
                self._save_content_v2(context, page_id, self._empty_content_v2(context, page_id, reason, category=category))
            valid = bool(content and (content.blocks or content.blank))
            self._update(context, page_id, completed_stage="recognize" if valid else None,
                         stage_data={"basic_recognition_unavailable": not valid})
        if self._task(context, page_id).stage_data.get("basic_recognition_complete") or context.configuration_error:
            context.first_recognized.set()
        self._checkpoint(context, source_only=True)
        task = self._task(context, page_id)
        if task.stage_data.get("basic_review_complete") or task.stage_data.get("basic_review_unavailable"):
            return
        revision = self._revision(task.candidate_revision_id)
        if revision.page_content.recognition_scope != "printed_original_only" and not revision.page_content.blocks and not revision.page_content.blank:
            self._update(context, page_id, stage_data={"basic_review_unavailable": True})
            return
        if not self._operation_v2(context, page_id, "review"):
            observed = source_coverage_content(revision.page_content, analysis)
            if observed != revision.page_content:
                revision = self._save_content_v2(context, page_id, observed)
        revision, review = await self._apply_review_v2(context, page_id, client, inputs, "review")
        valid_review = bool(review and review.full_page_reviewed)
        self._update(context, page_id, completed_stage="review" if valid_review else None,
                     stage_data={"basic_review_unavailable": not valid_review,
                                  "blank_reviewed": bool(valid_review and review.content == "usable" and review.coverage == "passed")})

    async def _reuse_inherited_render_v2(self, context: _RunContext, page_id: str) -> None:
        task = self._task(context, page_id)
        if not task.stage_data.get("inherited_from_run_id") or not task.candidate_revision_id:
            return
        revision = self._revision(task.candidate_revision_id)
        old_task = self.storage.get_task(task.stage_data["inherited_from_run_id"], page_id)
        if old_task and old_task.outcome and old_task.outcome.content == "usable" and revision.page_content.blank:
            self._update(context, page_id, stage_data={"blank_reviewed": bool(old_task.stage_data.get("basic_review_complete"))})
        parent = self.storage.get_revision(revision.parent_revision_id) if revision.parent_revision_id else None
        if parent is None or parent.page_content != revision.page_content or parent.page_layout != revision.page_layout:
            return
        old_run = self.storage.get_run(context.run.book_id, task.stage_data["inherited_from_run_id"])
        if old_run is None or old_run.settings_snapshot.get("output_settings_version") != context.book.output_settings_version:
            return
        cached = self._load_render_v2(context, revision)
        if cached is None:
            cached = self._load_render_v2(context, parent)
            if cached is not None:
                try:
                    cached = await cache_candidate_render(context.book, revision, context.book_dir, result=cached)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self._error_v2(context, page_id, "render", "render", _safe_error(exc), phase="output")
                    cached = None
        # An approximate but settled usable page also needs no new OCR/render
        # merely because another page or one export format failed.
        if cached and any(item.severity == "error" for item in cached.diagnostics):
            await self._finish_page_v2(context, page_id)
            return
        if (task.outcome is None and old_task and old_task.outcome and old_task.outcome.content == "usable"
                and revision.page_layout is not None and old_task.outcome.layout in {"faithful", "approximate"}):
            if cached is None:
                self._error_v2(context, page_id, "render", "render", "已完成继承页缺少旧渲染资产；不重新编译，PDF格式明确记录缺页",
                               phase="output")
            self.storage.save_page_outcome(context.run.run_id, page_id, old_task.outcome.model_copy(
                update={"content_revision_id": revision.page_content.content_revision_id,
                        "layout_revision_id": revision.page_layout.layout_revision_id}), adopt=False)
            self._update(context, page_id, stage="export", stage_data={"reused_complete_result": True})

    def _load_render_v2(self, context: _RunContext, revision: Revision) -> CandidateRenderResult | None:
        try:
            return load_candidate_render(context.book, revision, context.book_dir)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_recovery_v2(self, context: _RunContext, page_id: str, recovery: dict) -> None:
        self._update(context, page_id, stage="recover", state="running", stage_data={"v2_recovery": recovery})

    async def _recover_page_v2(self, context: _RunContext, page_id: str, client) -> None:
        task = self._task(context, page_id)
        if task.stage_data.get("v2_recovery_done") or task.candidate_revision_id is None:
            return
        _, image_path, metadata, analysis = await self._prepare_v2(context, page_id)
        inputs = await asyncio.to_thread(prepare_recognition_inputs, image_path, metadata, analysis,
                                         book_dir=context.book_dir, max_crops=3)
        while True:
            self._checkpoint(context, source_only=True)
            task = self._task(context, page_id)
            recovery = dict(task.stage_data.get("v2_recovery", {}))
            current = self._revision(task.candidate_revision_id)
            pending = bool(recovery and not recovery.get("done"))
            if not pending:
                if (client is None or context.configuration_error
                    or content_conclusion(current.page_content, blank_reviewed=task.stage_data.get("blank_reviewed", False)) == "usable"
                    or task.recovery_round_count >= context.run.policy.local_recovery_limit
                    or self._unknown_consumption_v2(context, page_id)):
                    break
                targets = recovery_targets(current.page_content, analysis)
                unique, identities = [], set()
                attempted_targets = task.stage_data.get("v2_recovery_attempted_targets", [])
                for target in targets:
                    identity = target.get("source_region_id") or target.get("block_id") or target["id"]
                    strategy = {"block_id": target.get("block_id"), "bbox": target.get("bbox"),
                                "category": target["category"], "kind": target["kind"], "region_id": target.get("source_region_id"),
                                "source_version": task.source_version, "input": "source_order" if target["category"] == "reading_order"
                                else "original_resolution_complete_region_with_overview", "max_crops": 1}
                    # Plain persisted fields, not a hash: another target's
                    # improvement does not authorize repeating this scheme.
                    if any({**strategy, "bbox": list(strategy["bbox"]) if strategy["bbox"] else None} == item
                           for item in attempted_targets):
                        continue
                    target["strategy"] = {**strategy, "bbox": list(strategy["bbox"]) if strategy["bbox"] else None}
                    if identity not in identities and (target["category"] == "reading_order" or target.get("bbox") is not None):
                        if target["category"] == "reading_order" and any(item["category"] == "reading_order" for item in unique):
                            continue
                        unique.append(target)
                        identities.add(identity)
                if not unique:
                    break
                fresh_run = self.storage.get_run(context.run.book_id, context.run.run_id)
                remaining = min(fresh_run.request_limit - fresh_run.request_count,
                                context.run.policy.page_request_limit - task.request_count)
                count = min(3, len(unique), remaining - 1)
                if count < 1:
                    self._error_v2(context, page_id, "recover", "budget", "局部恢复与修后完整复核的联合额度不足，保留最佳内容",
                                   phase="recovery")
                    break
                recovery = {"reservation_key": str(uuid4()), "base_revision_id": current.revision_id,
                            "targets": unique[:count], "completed": [], "checked_targets": [], "local_revision_ids": {},
                            "round": task.recovery_round_count + 1}
                self._save_recovery_v2(context, page_id, recovery)
            targets = recovery["targets"]
            try:
                attempts = self.storage.reserve_recovery_round(
                    context.run.run_id, page_id, block_ids=[target["id"] for target in targets], request_count=len(targets),
                    reservation_key=recovery["reservation_key"])
            except RequestBudgetExceeded as exc:
                self._checkpoint(context, source_only=True)
                self._error_v2(context, page_id, "recover", "budget", _safe_error(exc), phase="recovery")
                recovery["done"] = True
                self._save_recovery_v2(context, page_id, recovery)
                break
            base = self._revision(recovery["base_revision_id"])
            for index, target in enumerate(targets):
                self._checkpoint(context, source_only=True)
                if index in recovery["completed"]:
                    continue
                current = self._revision(self._task(context, page_id).candidate_revision_id)
                planned = recovery["local_revision_ids"].get(str(index))
                if planned and current.page_content.content_revision_id == planned:
                    recovery["completed"].append(index)
                    recovery["checked_targets"].append(target)
                    self._save_recovery_v2(context, page_id, recovery)
                    continue
                operation_name = f"recovery-{recovery['round']}-{index}"
                attempted_targets = list(self._task(context, page_id).stage_data.get("v2_recovery_attempted_targets", []))
                if target.get("strategy") and target["strategy"] not in attempted_targets:
                    self._update(context, page_id, stage_data={"v2_recovery_attempted_targets": [*attempted_targets, target["strategy"]]})
                try:
                    if target["category"] == "reading_order":
                        if attempts[index].state == "reserved" and attempts[index].sent_at is None:
                            self.storage.cancel_unsent_attempt(attempts[index].attempt_id)
                        order = source_reading_order(current.page_content, analysis)
                        if order == [block.block_id for block in current.page_content.blocks]:
                            raise _Unavailable("来源栏关系未得到不同且可靠的顺序；保留原顺序未知")
                        candidate = renew_content(current.page_content)
                        blocks = {block.block_id: block for block in candidate.blocks}
                        candidate.blocks = [blocks[identity] for identity in order]
                    else:
                        local_inputs = await asyncio.to_thread(
                            prepare_recognition_inputs, image_path, metadata, analysis, book_dir=context.book_dir,
                            max_crops=1, target_bbox=target["bbox"])
                        result = await self._request_v2(context, page_id, client, "recover", operation_name, local_inputs,
                                                        target=target, reserved_attempt_id=attempts[index].attempt_id)
                        local = result.parsed.content if isinstance(result.parsed, ContentParseResult) else None
                        if local is None:
                            raise _Unavailable("局部响应未取得完整有效内容；其他已保存块保留")
                        old_value = next((block for block in base.page_content.blocks if block.block_id == target.get("block_id")), None)
                        candidate = local_content_candidate(current.page_content, local, target, old_value=old_value)
                        order = ([block.block_id for block in candidate.blocks] if candidate.recognition_scope == "printed_original_only" else source_reading_order(candidate, analysis))
                        blocks = {block.block_id: block for block in candidate.blocks}
                        candidate.blocks = [blocks[identity] for identity in order]
                    if planned is None:
                        planned = str(uuid4())
                        recovery["local_revision_ids"][str(index)] = planned
                        self._save_recovery_v2(context, page_id, recovery)
                    candidate.content_revision_id = planned
                    for block in candidate.blocks:
                        block.content_revision_id = planned
                    old_ids = {problem.issue_id for problem in current.page_content.issues}
                    for issue_index, problem in enumerate(candidate.issues):
                        if problem.issue_id not in old_ids and problem.response_id is None:
                            problem.issue_id = f"{planned}-i{issue_index}"
                    revision = self._save_content_v2(context, page_id, candidate)
                    recovery["candidate_revision_id"] = revision.revision_id
                    recovery["checked_targets"].append(target)
                except (_Unavailable, _ConfigurationStopped, OSError, ValueError) as exc:
                    if isinstance(exc, _ConfigurationStopped):
                        self._error_v2(context, page_id, "recover", "service_configuration", _safe_error(exc), phase="recovery")
                    elif not isinstance(exc, _Unavailable) or not exc.recorded:
                        self._error_v2(context, page_id, "recover", target["category"], _safe_error(exc), phase="recovery",
                                       block_id=target.get("block_id"), bbox=target.get("bbox"))
                    attempt = next(item for item in self.storage.get_attempts(context.run.run_id, page_id)
                                   if item.attempt_id == attempts[index].attempt_id)
                    if attempt.state == "reserved" and attempt.sent_at is None:
                        self.storage.cancel_unsent_attempt(attempt.attempt_id)
                recovery["completed"].append(index)
                self._save_recovery_v2(context, page_id, recovery)
                if context.configuration_error or self._unknown_consumption_v2(context, page_id):
                    break
            current = self._revision(self._task(context, page_id).candidate_revision_id)
            rollback_content = base.page_content
            if current.page_content == base.page_content:
                review_attempt = next(item for item in self.storage.get_attempts(context.run.run_id, page_id)
                                      if item.attempt_id == attempts[-1].attempt_id)
                if review_attempt.state == "reserved" and review_attempt.sent_at is None:
                    self.storage.cancel_unsent_attempt(review_attempt.attempt_id)
                improved = False
            else:
                reviewed, review = await self._apply_review_v2(
                    context, page_id, client, inputs, f"recovery-{recovery['round']}-review",
                    targets=recovery["checked_targets"], reserved_attempt_id=attempts[-1].attempt_id)
                review_attempt = next(item for item in self.storage.get_attempts(context.run.run_id, page_id)
                                      if item.attempt_id == attempts[-1].attempt_id)
                if review is None and review_attempt.state == "reserved" and review_attempt.sent_at is None:
                    self.storage.cancel_unsent_attempt(review_attempt.attempt_id)
                improved = bool(review and review.full_page_reviewed and content_improves(
                    base.page_content, reviewed.page_content,
                    blank_reviewed=bool(review.content == "usable" and review.coverage == "passed")))
                if base.page_content.recognition_scope == "printed_original_only":
                    rollback_content = renew_content(base.page_content)
                    discovered_ids = {region.region_id for region in review.discovered_regions} if review else set()
                    rollback_content.regions.extend(review.discovered_regions if review else [])
                    if discovered_ids:
                        rollback_content.blank = False
                        if rollback_content.page_kind == "blank":
                            rollback_content.page_kind = "content"
                    changed_ids = {target.get("source_region_id") for target in recovery["checked_targets"]}
                    unchanged_ids = {region.region_id for region in base.page_content.regions} - changed_ids
                    evidence_ids = discovered_ids | unchanged_ids
                    rollback_content.issues = [item for item in rollback_content.issues if item.region_id not in unchanged_ids]
                    rollback_content.issues.extend(item for item in reviewed.page_content.issues if item.region_id in evidence_ids)
                    reviewed_blocks = {block.block_id: block for block in reviewed.page_content.blocks if block.source_region_id in unchanged_ids}
                    for block in rollback_content.blocks:
                        if block.block_id in reviewed_blocks:
                            block.review_status = reviewed_blocks[block.block_id].review_status
                    if review and any(item.region_id in evidence_ids and (item.content != "usable" or item.coverage != "passed") for item in review.region_verdicts):
                        rollback_content.coverage_reviewed = False
                    rollback_content.unresolved_spans.extend(item for item in reviewed.page_content.unresolved_spans if item.region_id in discovered_ids)
                    discovery_observations = {item.observation_id: item for item in reviewed.page_content.coverage_observations
                        if item.mapped_region_ids and set(item.mapped_region_ids) <= evidence_ids}
                    rollback_content.coverage_observations = [discovery_observations.pop(item.observation_id, item)
                        for item in rollback_content.coverage_observations]
                    rollback_content.coverage_observations.extend(discovery_observations.values())
                    accepted = renew_content(rollback_content)
                    adopted = dict(self._task(context, page_id).stage_data.get("region_adoptions", {}))
                    changed = {target.get("source_region_id") for target in recovery["checked_targets"]}
                    passed_regions = {verdict.region_id for verdict in review.region_verdicts
                        if verdict.content == "usable" and verdict.coverage == "passed"} if review else set()
                    accepted_regions = {region_id for region_id in changed & passed_regions
                        if any(block.source_region_id == region_id for block in reviewed.page_content.blocks)
                        and all(block.review_status == "usable" for block in reviewed.page_content.blocks if block.source_region_id == region_id)
                        and not any(item.region_id == region_id and not item.resolved and item.category not in {"layout", "render"}
                                    for item in reviewed.page_content.issues)
                        and not any(item.region_id == region_id for item in reviewed.page_content.unresolved_spans)}
                    for region_id in accepted_regions:
                        target = next(item for item in recovery["checked_targets"] if item.get("source_region_id") == region_id)
                        accepted = local_content_candidate(accepted, reviewed.page_content, target)
                        accepted.issues = [item for item in accepted.issues if item.region_id != region_id]
                        accepted.issues.extend(item for item in reviewed.page_content.issues if item.region_id == region_id)
                        adopted[region_id] = reviewed.page_content.content_revision_id
                    improved = bool(accepted_regions)
                    if improved:
                        accepted.coverage_reviewed = bool(reviewed.page_content.coverage_reviewed and changed <= accepted_regions)
                        reviewed_observations = {item.observation_id: item for item in reviewed.page_content.coverage_observations
                            if item.mapped_region_ids and set(item.mapped_region_ids) <= accepted_regions}
                        accepted.coverage_observations = [reviewed_observations.get(item.observation_id, item)
                            for item in accepted.coverage_observations]
                        reviewed = self._save_content_v2(context, page_id, accepted)
                        self._update(context, page_id, stage_data={"region_adoptions": adopted})
                if improved:
                    self._update(context, page_id, completed_stage="review", stage_data={
                        "blank_reviewed": bool(review.content == "usable" and review.coverage == "passed"),
                        "basic_review_unavailable": False, "basic_recognition_unavailable": False})
                else:
                    restored = self._save_content_v2(context, page_id, rollback_content)
                    if base.page_layout is not None and base.page_content.recognition_scope != "printed_original_only":
                        restored = self._save_layout_v2(context, page_id, base.page_layout, restored)
                        cached = self._load_render_v2(context, base)
                        if cached:
                            try:
                                await cache_candidate_render(context.book, restored, context.book_dir, result=cached)
                            except (OSError, ValueError, KeyError, TypeError) as exc:
                                self._error_v2(context, page_id, "render", "render", _safe_error(exc), phase="output")
                    if context.configuration_error:
                        self._error_v2(context, page_id, "recover", "service_configuration",
                                       "模型服务已停止，局部候选无法独立复核；保留旧内容候选", phase="recovery")
                    elif not self._unknown_consumption_v2(context, page_id):
                        self._error_v2(context, page_id, "recover", "missing_content",
                                       "局部候选未经完整独立复核确认改善或带来新问题；恢复旧内容候选", phase="recovery")
            recovery["done"], recovery["improved"] = True, improved
            self._save_recovery_v2(context, page_id, recovery)
            if not improved:
                break
        self._update(context, page_id, completed_stage="recover", stage_data={"v2_recovery_done": True})

    def _save_layout_v2(self, context: _RunContext, page_id: str, layout: PageLayout, base: Revision,
                         *, render_layout: SourceFidelityLayout | None = None) -> Revision:
        if render_layout is None:
            render_layout = to_source_fidelity_layout(base.page_content, layout,
                                                      content_revision=base.content_revision, layout_revision=base.layout_revision + 1)
        generated = None
        if render_layout.source_disposition != "source_page_preserved" and layout.conclusion != "unavailable":
            try:
                generated = generate_source_fidelity_latex(render_layout)
            except (FidelityLayoutError, ValueError):
                # The typed content remains saved; the output stage owns this
                # error and can preserve the source without a LaTeX document.
                pass
        return self.storage.save_layout_candidate(context.run.run_id, page_id, layout,
                    expected_candidate_revision_id=base.revision_id, render_layout=render_layout, generated_text=generated)

    async def _compile_v2(self, context: _RunContext, page_id: str, revision: Revision) -> CandidateRenderResult:
        cached = self._load_render_v2(context, revision)
        if cached is not None:
            return cached
        self._checkpoint(context, source_only=True)
        task = self._task(context, page_id)
        attempted = list(task.stage_data.get("compiled_revision_ids", []))
        if revision.revision_id in attempted:
            return CandidateRenderResult(layout=revision.layout_source, error="已预约编译没有保存产物；不重复同候选编译")
        try:
            index = self.storage.reserve_compile(context.run.run_id, page_id)
        except RequestBudgetExceeded as exc:
            self._checkpoint(context, source_only=True)
            return CandidateRenderResult(layout=revision.layout_source, error=_safe_error(exc))
        self._update(context, page_id, stage="render", state="running",
                     stage_data={"compiled_revision_ids": [*attempted, revision.revision_id]})
        return await render_candidate(context.book, revision, context.book_dir, run_id=context.run.run_id,
                                      page_id=page_id, compile_index=index)

    async def _render_v2(self, context: _RunContext, page_id: str, base: Revision):
        task = self._task(context, page_id)
        best_id = task.stage_data.get("v2_best_render_revision_id")
        best = self._revision(best_id) if best_id else base
        if best.page_content != base.page_content:
            best = base
        rendered = await self._compile_v2(context, page_id, best)
        while True:
            self._checkpoint(context, source_only=True)
            task = self._task(context, page_id)
            if task.stage_data.get("v2_geometry_done") or rendered.error or best.layout_source is None:
                break
            candidate_id = task.stage_data.get("v2_geometry_candidate_id")
            if candidate_id and candidate_id != task.stage_data.get("v2_geometry_evaluated_id"):
                candidate = self._revision(candidate_id)
            else:
                if task.compile_count >= context.run.policy.compile_limit:
                    break
                adjustment = local_geometry_adjustment(best.layout_source, rendered.diagnostics, rendered.measurements)
                if adjustment is None:
                    break
                layout = best.page_layout.model_copy(deep=True)
                layout.layout_revision_id = str(uuid4())
                layout.lines = [PageLinePlacement(**line.model_dump(exclude={"kind", "latex"})) for line in adjustment.lines]
                for key in ("regions", "equation_groups", "body_frame", "body_font_size_bp", "body_font_family",
                            "body_font_basis", "source_assets", "review_reasons", "canvas_width_bp", "canvas_height_bp", "canvas_basis"):
                    setattr(layout, key, getattr(adjustment, key))
                layout.conclusion = "approximate"
                current = self._revision(task.candidate_revision_id)
                candidate = self._save_layout_v2(context, page_id, layout, current)
                self._update(context, page_id, stage_data={"v2_geometry_candidate_id": candidate.revision_id,
                             "v2_best_render_revision_id": best.revision_id})
            adjusted = await self._compile_v2(context, page_id, candidate)
            better = self._better_render(rendered, adjusted)
            if better:
                best, rendered = candidate, adjusted
            self._update(context, page_id, stage_data={"v2_geometry_evaluated_id": candidate.revision_id,
                         "v2_geometry_done": not better, "v2_best_render_revision_id": best.revision_id})
            if not better:
                break
        current = self._revision(self._task(context, page_id).candidate_revision_id)
        if current.page_layout != best.page_layout:
            best = self._save_layout_v2(context, page_id, best.page_layout, current)
            rendered = await cache_candidate_render(context.book, best, context.book_dir, result=rendered)
        self._update(context, page_id, completed_stage="render", stage_data={"v2_render_done": True})
        return best, rendered

    async def _preserve_page_v2(self, context: _RunContext, page_id: str, revision: Revision,
                                image_path: Path, metadata: PageSourceMetadata, reason: str):
        self._checkpoint(context, source_only=True)
        if revision.page_content.recognition_scope == "printed_original_only":
            return revision, CandidateRenderResult(error="印刷原文结果不允许以未经确认干净的源页兜底")
        rendered = await preserve_source_page(context.book, revision, context.book_dir, image_path=image_path,
                    metadata=metadata, reason=reason, run_id=context.run.run_id, page_id=page_id)
        if rendered.pdf_path is None or rendered.layout is None:
            return revision, rendered
        layout = revision.page_layout.model_copy(deep=True) if revision.page_layout else PageLayout(
            layout_revision_id=str(uuid4()), content_revision_id=revision.page_content.content_revision_id,
            page_id=page_id, source_version=revision.source_version, source=metadata)
        layout.layout_revision_id = str(uuid4())
        layout.source_assets = rendered.layout.source_assets
        layout.conclusion = "unavailable"
        layout.review_reasons = list(dict.fromkeys([*layout.review_reasons, reason]))[:500]
        revision = self._save_layout_v2(context, page_id, layout, revision, render_layout=rendered.layout)
        rendered = await cache_candidate_render(context.book, revision, context.book_dir, result=rendered)
        self._update(context, page_id, stage_data={"v2_preserved_revision_id": revision.revision_id})
        return revision, rendered

    async def _content_source_assets_v2(self, context, page_id, content, layout, image_path, metadata, analysis):
        """Located content doubt includes omissions with no candidate block ID."""
        if content.recognition_scope == "printed_original_only":
            return layout, None
        semantic = [problem for problem in content.issues if not problem.resolved and problem.category not in {"layout", "render"}]
        regions = {region.region_id: region.bbox for region in layout.regions}
        boxes = []
        for problem in semantic:
            box = regions.get(problem.block_id) or problem.source_bbox
            if box is None or box == (0., 0., 1., 1.):
                return layout, problem.reason
            boxes.append((box, problem.block_id or problem.issue_id, problem.reason))
        for block in content.blocks:
            if block.review_status != "usable" and not any(item[1] == block.block_id for item in boxes):
                box = regions.get(block.block_id)
                if box is None:
                    return layout, "内容块未取得可靠独立复核且源位置未知，保留原页"
                boxes.append((box, block.block_id, "内容尚未可靠独立复核，保留对应完整源区域"))
        if not boxes:
            return layout, None
        result = layout.model_copy(deep=True)
        for box, identity, reason in boxes:
            self._checkpoint(context, source_only=True)
            # Keep complete crossing source rows, formula numbering and all
            # intersecting candidate regions; never paste over half a line.
            bounds = [region.bbox for region in result.regions if region.bbox is not None]
            bounds.extend(line.bbox for line in result.lines if line.bbox is not None)
            bounds.extend(band.bbox for band in analysis.region_bands((0., 0., 1., 1.)))
            for _ in range(len(bounds) + 1):
                crossing = [other for other in bounds if max(box[0], other[0]) < min(box[2], other[2])
                            and max(box[1], other[1]) < min(box[3], other[3])]
                expanded = (min([box[0], *(other[0] for other in crossing)]), min([box[1], *(other[1] for other in crossing)]),
                            max([box[2], *(other[2] for other in crossing)]), max([box[3], *(other[3] for other in crossing)]))
                if expanded == box:
                    break
                box = expanded
            if not any(asset.purpose == "uncertain_content" and asset.bbox[0] <= box[0] and asset.bbox[1] <= box[1]
                       and asset.bbox[2] >= box[2] and asset.bbox[3] >= box[3] for asset in result.source_assets):
                result.source_assets.append(await asyncio.to_thread(persist_source_region, context.book_dir, image_path,
                                           metadata, box, identity, reason, purpose="uncertain_content"))
        result.layout_revision_id = str(uuid4())
        if result.conclusion != "unavailable":
            result.conclusion = "approximate"
        return result, None

    async def _output_source_regions_v2(self, context, page_id, revision, rendered, image_path, metadata, analysis):
        """Settle located critical output faults without another TeX invocation."""
        if revision.page_content.recognition_scope == "printed_original_only":
            return revision, rendered
        critical = [item for item in rendered.diagnostics if item.severity == "error"]
        if not critical:
            return revision, rendered
        layout = revision.page_layout
        if layout is None or rendered.pdf_path is None or rendered.document is None:
            return await self._preserve_page_v2(context, page_id, revision, image_path, metadata,
                                                "关键输出问题无法安全对应源区域，保留原页")
        lines = {line.line_id: line for line in layout.lines}
        regions = {region.region_id: region.bbox for region in layout.regions}
        targets = []
        for problem in critical:
            line = lines.get(problem.line_id)
            block_id = problem.block_id or (line.block_id if line else None)
            box = regions.get(block_id) or (line.bbox if line else None) or problem.source_bbox
            if box is None or box == (0., 0., 1., 1.):
                return await self._preserve_page_v2(context, page_id, revision, image_path, metadata,
                                                    "关键输出问题缺少可靠局部边界：" + problem.message[:800])
            # A block region contains the complete original equation/table,
            # including peripheral numbering, rather than just its bad glyph.
            if problem.source_bbox is not None:
                other = problem.source_bbox
                box = (min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]), max(box[3], other[3]))
            targets.append((box, block_id or problem.line_id or f"output-{len(targets)}", problem.message))
        retained = layout.model_copy(deep=True)
        retained.layout_revision_id = str(uuid4())
        bounds = [region.bbox for region in retained.regions if region.bbox is not None]
        bounds.extend(line.bbox for line in retained.lines if line.bbox is not None)
        bounds.extend(group.bbox for group in retained.equation_groups if group.bbox is not None)
        bounds.extend(group.number.bbox for group in retained.equation_groups if group.number and group.number.bbox is not None)
        bounds.extend(band.bbox for band in analysis.region_bands((0., 0., 1., 1.)))
        try:
            for box, identity, reason in targets:
                self._checkpoint(context, source_only=True)
                for _ in range(len(bounds) + 1):
                    crossing = [other for other in bounds if max(box[0], other[0]) < min(box[2], other[2])
                                and max(box[1], other[1]) < min(box[3], other[3])]
                    expanded = (min([box[0], *(other[0] for other in crossing)]), min([box[1], *(other[1] for other in crossing)]),
                                max([box[2], *(other[2] for other in crossing)]), max([box[3], *(other[3] for other in crossing)]))
                    if expanded == box:
                        break
                    box = expanded
                retained.source_assets.append(await asyncio.to_thread(
                    persist_source_region, context.book_dir, image_path, metadata, box, identity,
                    "有限布局调整后仍有关键输出问题，保留完整对应源区域：" + reason[:1_000], purpose="uncertain_content"))
            retained.conclusion = "approximate"
            retained.review_reasons = [*retained.review_reasons, "有限布局调整后关键输出问题以必要源区域保留"][:500]
            candidate = self._save_layout_v2(context, page_id, retained, revision)
            self._checkpoint(context, source_only=True)
            replaced = await preserve_source_regions(context.book, candidate, context.book_dir, best_candidate=rendered)
            if replaced.pdf_path is not None and not replaced.error and not any(item.severity == "error" for item in replaced.diagnostics):
                return candidate, replaced
            reason = replaced.error or "源区域替代后仍存在关键输出问题，不能把候选 PDF 标记为可靠"
            revision = candidate
        except (OSError, ValueError, FidelityLayoutError) as exc:
            reason = "源区域不能安全替代：" + _safe_error(exc)
            revision = self._revision(self._task(context, page_id).candidate_revision_id)
        return await self._preserve_page_v2(context, page_id, revision, image_path, metadata, reason)

    async def _finish_page_v2(self, context: _RunContext, page_id: str) -> None:
        self._checkpoint(context, source_only=True)
        task = self._task(context, page_id)
        revision = self.storage.get_revision(task.candidate_revision_id) if task.candidate_revision_id else None
        if revision is None:
            revision = self._save_content_v2(context, page_id, self._empty_content_v2(context, page_id, "源页或内容阶段不可用"))
        for attempt in self.storage.get_attempts(context.run.run_id, page_id):
            if attempt.state == "reserved" and attempt.sent_at is None:
                self.storage.cancel_unsent_attempt(attempt.attempt_id)
        if task.stage_data.get("v2_source_unavailable"):
            self.storage.save_page_outcome(context.run.run_id, page_id, PageOutcome(
                recognition_scope=task.stage_data.get("recognition_scope", "legacy_all_visible"), page_id=page_id, source_version=task.source_version,
                content=content_conclusion(revision.page_content, blank_reviewed=task.stage_data.get("blank_reviewed", False)),
                layout="unavailable", source_readable=False, errors=self._task(context, page_id).errors), adopt=False)
            return
        try:
            if task.stage_data.get("v2_analysis_unavailable"):
                record = self.storage.get_run_page_record(context.run.run_id, page_id)
                prepared = task.stage_data.get("prepared")
                if prepared:
                    metadata = PageSourceMetadata.model_validate(prepared["metadata"])
                    image_path = context.book_dir / prepared["image_name"]
                else:
                    width, height, image_name, metadata = await asyncio.to_thread(prepare_page, context.book_dir, record)
                    image_path = context.book_dir / image_name
                    self._update(context, page_id, stage_data={"prepared": {"width": width, "height": height,
                                 "image_name": image_name, "metadata": metadata.model_dump(mode="json")}})
                revision, rendered = await self._preserve_page_v2(context, page_id, revision, image_path, metadata,
                                           "来源分析或内容技术阶段不可用；已有内容保留，直接保留可读源页")
                source_failure = None
                if rendered.error:
                    self._error_v2(context, page_id, "render", "render", rendered.error, phase="output")
                    source_failure = await asyncio.to_thread(self._source_read_failure_v2, context, page_id)
                    if source_failure:
                        self._error_v2(context, page_id, "render", "unreadable_source", source_failure, phase="output")
                        self._update(context, page_id, stage_data={"v2_source_unavailable": True})
                self.storage.save_page_outcome(context.run.run_id, page_id, PageOutcome(
                    recognition_scope=task.stage_data.get("recognition_scope", "legacy_all_visible"), page_id=page_id, source_version=task.source_version,
                    content=content_conclusion(revision.page_content, blank_reviewed=task.stage_data.get("blank_reviewed", False)),
                    layout="unavailable", source_disposition="page_preserved" if rendered.pdf_path else "transcribed",
                    source_readable=source_failure is None, errors=self._task(context, page_id).errors), adopt=bool(rendered.pdf_path))
                return
            _, image_path, metadata, analysis = await self._prepare_v2(context, page_id)
            content = revision.page_content
            whole_reason = None
            if revision.page_layout is None:
                self._update(context, page_id, stage="layout", state="running")
                try:
                    layout = await asyncio.to_thread(derive_layout, content, metadata, analysis, context.book,
                                                    image_path=image_path, book_dir=context.book_dir)
                    layout, whole_reason = await self._content_source_assets_v2(
                        context, page_id, content, layout, image_path, metadata, analysis)
                    revision = self._save_layout_v2(context, page_id, layout, revision)
                    self._update(context, page_id, completed_stage="layout")
                except (OSError, ValueError, FidelityLayoutError) as exc:
                    whole_reason = "布局派生不可用：" + _safe_error(exc)
                    self._error_v2(context, page_id, "layout", "layout", whole_reason, phase="output")
            layout = revision.page_layout
            cached = self._load_render_v2(context, revision)
            if cached and cached.pdf_path is not None and not cached.error:
                if (cached.source_disposition != "source_page_preserved" and any(item.severity == "error" for item in cached.diagnostics)
                    and self._task(context, page_id).compile_count < context.run.policy.compile_limit
                    and not self._task(context, page_id).stage_data.get("v2_geometry_done")):
                    revision, rendered = await self._render_v2(context, page_id, revision)
                else:
                    rendered = cached
            elif whole_reason or layout is None or layout.conclusion == "unavailable":
                reason = whole_reason or ("；".join(layout.review_reasons)[:1_000] if layout else "没有可靠派生布局")
                revision, rendered = await self._preserve_page_v2(context, page_id, revision, image_path, metadata, reason)
            else:
                revision, rendered = await self._render_v2(context, page_id, revision)
                if rendered.pdf_path is None or rendered.error:
                    self._error_v2(context, page_id, "render", "render", rendered.error or "重排候选没有可用 PDF", phase="output")
                    revision, rendered = await self._preserve_page_v2(context, page_id, revision, image_path, metadata,
                        rendered.error or "重排输出不可用，保留可读原页")
            critical_before = [diagnostic for diagnostic in rendered.diagnostics if diagnostic.severity == "error"]
            for diagnostic in critical_before:
                self._error_v2(context, page_id, "render", "render", diagnostic.message, phase="output",
                               block_id=diagnostic.block_id, bbox=diagnostic.source_bbox)
            if critical_before:
                revision, rendered = await self._output_source_regions_v2(
                    context, page_id, revision, rendered, image_path, metadata, analysis)
            for diagnostic in rendered.diagnostics:
                if diagnostic.severity == "error":
                    self._error_v2(context, page_id, "render", "render", diagnostic.message, phase="output",
                                   block_id=diagnostic.block_id, bbox=diagnostic.source_bbox)
            source_failure = None
            if rendered.error:
                self._error_v2(context, page_id, "render", "render", rendered.error, phase="output")
                source_failure = await asyncio.to_thread(self._source_read_failure_v2, context, page_id)
                if source_failure:
                    self._error_v2(context, page_id, "render", "unreadable_source", source_failure, phase="output")
                    self._update(context, page_id, stage_data={"v2_source_unavailable": True})
            uncertain_output = [item for item in rendered.diagnostics if item.severity == "warning"
                                or item.coverage != "complete" or item.code == "LAYOUT_UNVERIFIED"]
            if (revision.page_layout and revision.page_layout.conclusion == "faithful" and uncertain_output
                and rendered.pdf_path is not None and not rendered.error):
                uncertain = revision.page_layout.model_copy(deep=True)
                uncertain.layout_revision_id, uncertain.conclusion = str(uuid4()), "approximate"
                uncertain.review_reasons = list(dict.fromkeys([*uncertain.review_reasons,
                    *(item.message for item in uncertain_output)]))[:500]
                revision = self._save_layout_v2(context, page_id, uncertain, revision)
                rendered = await cache_candidate_render(context.book, revision, context.book_dir, result=rendered)
            disposition = "page_preserved" if rendered.source_disposition == "source_page_preserved" else (
                "regions_preserved" if rendered.source_disposition == "regions_preserved" else "transcribed")
            layout_status = revision.page_layout.conclusion if revision.page_layout else "unavailable"
            if rendered.error or rendered.pdf_path is None or disposition == "page_preserved" or any(
                item.severity == "error" for item in rendered.diagnostics
            ):
                layout_status = "unavailable"
            self.storage.save_page_outcome(context.run.run_id, page_id, PageOutcome(
                recognition_scope=task.stage_data.get("recognition_scope", "legacy_all_visible"), page_id=page_id, source_version=task.source_version,
                content=content_conclusion(revision.page_content, blank_reviewed=self._task(context, page_id).stage_data.get("blank_reviewed", False)),
                layout=layout_status, source_disposition=disposition, source_readable=source_failure is None,
                content_revision_id=revision.page_content.content_revision_id,
                layout_revision_id=revision.page_layout.layout_revision_id if revision.page_layout else None,
                errors=self._task(context, page_id).errors), adopt=True)
        except _Paused:
            raise
        except (_SourceUnavailable, OSError, ValueError, KeyError, TypeError, fitz.FileDataError) as exc:
            source_failure = await asyncio.to_thread(self._source_read_failure_v2, context, page_id)
            self._error_v2(context, page_id, "render", "unreadable_source" if source_failure else "render",
                           source_failure or _safe_error(exc), phase="output")
            if source_failure:
                self._update(context, page_id, stage_data={"v2_source_unavailable": True})
            current = self._revision(self._task(context, page_id).candidate_revision_id)
            self.storage.save_page_outcome(context.run.run_id, page_id, PageOutcome(
                recognition_scope=task.stage_data.get("recognition_scope", "legacy_all_visible"), page_id=page_id, source_version=task.source_version,
                content=content_conclusion(current.page_content, blank_reviewed=task.stage_data.get("blank_reviewed", False)),
                layout="unavailable", source_readable=source_failure is None and not task.stage_data.get("v2_source_unavailable", False),
                errors=self._task(context, page_id).errors), adopt=False)

    async def _process_run_v1(
        self, book_id: str, run_id: str, api_key: str, pause_event: asyncio.Event | None = None,
    ) -> None:
        run = self.storage.get_run(book_id, run_id)
        if run is None:
            raise KeyError("run")
        if run.status in {"succeeded", "failed"}:
            return
        if run.status == "pausing" or pause_event is not None and pause_event.is_set():
            self.storage.set_run_status(book_id, run_id, "paused", run.error)
            return
        context = _RunContext(run, self.storage.get_run_book(book_id, run_id),
                              self.storage.books_root / book_id, pause_event)
        context.configuration_error = next((task.stage_data["configuration_error"] for task in run.tasks
                                            if task.stage_data.get("configuration_error")), None)
        self._restore_configuration_error(context)
        if any(task.result_status is None and not task.stage_data.get("repair_done")
               and not task.stage_data.get("recognize_unavailable") for task in run.tasks) and (
            not api_key or not run.settings_snapshot.get("extraction_model")
        ):
            context.configuration_error = "模型密钥或页面识别模型未配置；源内容自动保留"
        self.storage.set_run_status(book_id, run_id, "running", context.configuration_error)
        for task in run.tasks:
            if task.result_status == "auto_passed" and len(context.style_samples) < 20:
                revision = self._revision(task.stage_data.get("final_revision_id"))
                if revision.layout_source:
                    context.style_samples.append(revision.layout_source)
        context.style_summary = summarize_book_styles(context.style_samples)
        for attempt in self.storage.get_attempts(run_id):
            if attempt.state == "reserved":
                self.storage.finish_run_attempt(attempt.attempt_id, state="unknown",
                                                error="上次请求未结算；不自动重发")
        pending = [task.page_id for task in run.tasks if task.result_status is None]
        if any(task.stage_data.get("recognition_revision_id") for task in run.tasks):
            context.first_recognized.set()

        async def worker(first: bool = False) -> None:
            if not first:
                await context.first_recognized.wait()
            client = None
            page_id = None
            try:
                while pending:
                    self._checkpoint(context)
                    page_id = pending.pop(0)
                    if client is None:
                        client = create_model_client(run.settings_snapshot, api_key)
                    await self._process_page(context, page_id, client)
            except (_Paused, _ConfigurationStopped):
                return
            except ModelServiceError as exc:
                if not exc.configuration_error:
                    raise
                context.configuration_error = _safe_error(exc)
                if page_id:
                    self._update(context, page_id, stage_data={"configuration_error": context.configuration_error})
            finally:
                if first:
                    context.first_recognized.set()

        try:
            if pending and not context.configuration_error:
                concurrency = max(1, int(run.settings_snapshot.get("processing_concurrency", 10)))
                async with asyncio.TaskGroup() as group:
                    for index in range(min(concurrency, len(pending))):
                        group.create_task(worker(first=index == 0))
            # Configuration errors stop new model calls; the remaining source
            # pages can still finish locally, with the run error retained.
            if context.configuration_error and not self._pause_requested(context):
                for task in self.storage.get_run_tasks(run_id):
                    if task.result_status is None:
                        await self._source_only_page(context, task.page_id, context.configuration_error)
            if self._pause_requested(context):
                self.storage.set_run_status(book_id, run_id, "paused", context.configuration_error)
                return
            if any(task.result_status is None for task in self.storage.get_run_tasks(run_id)):
                self.storage.set_run_status(book_id, run_id, "interrupted", "部分阶段未完成；恢复会复用已保存结果")
                return
            await self._export(context)
        except _Paused:
            self.storage.set_run_status(book_id, run_id, "paused", context.configuration_error)
        except asyncio.CancelledError:
            self.storage.set_run_status(book_id, run_id, "interrupted", "任务执行已中断；已发送请求不自动重发")
            raise
        except Exception:
            self.storage.set_run_status(book_id, run_id, "interrupted", "任务发生技术错误；保留已保存阶段及调用记录")
            raise

    def _task(self, context: _RunContext, page_id: str) -> PageTask:
        task = self.storage.get_task(context.run.run_id, page_id)
        if task is None:
            raise KeyError("task")
        return task

    def _pause_requested(self, context: _RunContext) -> bool:
        run = self.storage.get_run(context.run.book_id, context.run.run_id)
        return (context.pause_event is not None and context.pause_event.is_set()
                or run is not None and run.status in {"pausing", "paused"})

    def _checkpoint(self, context: _RunContext, *, source_only: bool = False) -> None:
        if self._pause_requested(context):
            raise _Paused()
        if context.configuration_error and not source_only:
            raise _ConfigurationStopped("模型服务已停止，当前请求未发送；保留已取得内容")

    def _update(self, context: _RunContext, page_id: str, **values: Any) -> PageTask:
        return self.storage.update_task(context.run.run_id, page_id, **values)

    def _revision(self, revision_id: str | None) -> Revision:
        revision = self.storage.get_revision(revision_id) if revision_id else None
        if revision is None:
            raise ValueError("阶段缺少已保存的候选修订")
        return revision

    @staticmethod
    def _layout_book(context: _RunContext) -> Book:
        # A small reliable run sample supplies defaults only. Page measurements
        # take precedence in solve_layout; no prior revision is ever rewritten.
        summary = context.style_summary
        if not summary:
            return context.book
        size_pt = summary["font_size_bp"] * 72.27 / 72
        settings = context.book.layout.model_copy(update={
            "font_family": summary["font_family"],
            "font_size_pt": size_pt if 6 <= size_pt <= 48 else context.book.layout.font_size_pt,
        })
        return context.book.model_copy(update={"layout": settings})

    async def _prepare(self, context: _RunContext, page_id: str, *, source_only: bool = False
                       ) -> tuple[dict[str, Any], Path, PageSourceMetadata, SourceAnalysis]:
        self._checkpoint(context, source_only=source_only)
        if page_id in context.prepared_pages:
            return context.prepared_pages[page_id]
        record = self.storage.get_run_page_record(context.run.run_id, page_id)
        task = self._task(context, page_id)
        prepared = task.stage_data.get("prepared")
        if prepared is None:
            self._update(context, page_id, stage="prepare", state="running")
            width, height, image_name, metadata = await asyncio.to_thread(prepare_page, context.book_dir, record)
            prepared = {"width": width, "height": height, "image_name": image_name,
                        "metadata": metadata.model_dump(mode="json")}
            self._update(context, page_id, completed_stage="prepare", stage_data={"prepared": prepared})
        else:
            metadata = PageSourceMetadata.model_validate(prepared["metadata"])
        record.update(width=prepared["width"], height=prepared["height"], image_name=prepared["image_name"])
        image_path = context.book_dir / prepared["image_name"]
        self._checkpoint(context, source_only=source_only)
        analysis = await asyncio.to_thread(analyze_source, image_path, metadata, book_dir=context.book_dir, max_crops=0)
        context.prepared_pages[page_id] = record, image_path, metadata, analysis
        return context.prepared_pages[page_id]

    async def _input(
        self, prompt: str, record: dict[str, Any], image_path: Path, *, revision: Revision | None = None,
        render: CandidateRenderResult | None = None, assessment: Assessment | None = None,
    ) -> list[dict[str, Any]]:
        image_data = await asyncio.to_thread(lambda: base64.b64encode(image_path.read_bytes()).decode("ascii"))
        text = page_context(record["source_filename"], record["source_page"], record["source_page_count"])
        if revision is not None:
            candidate = {"base_revision_id": revision.revision_id, "page_kind": revision.page_kind,
                         "layout": revision.layout_source.model_dump(mode="json") if revision.layout_source else None,
                         "cover_fields": [item.model_dump() for item in revision.cover_fields],
                         "header_segments": [item.model_dump() for item in revision.header_segments],
                         "footer_segments": [item.model_dump() for item in revision.footer_segments],
                         "diagnostics": [item.model_dump(mode="json") for item in render.diagnostics] if render else [],
                         "output_error": render.error if render else None,
                         "issues": [item.model_dump(mode="json") for item in assessment.issues] if assessment else []}
            text += "\n候选与复核数据：\n" + json.dumps(candidate, ensure_ascii=False)
        content = [{"type": "input_text", "text": text},
                   {"type": "input_text", "text": "下面是完整源页。"},
                   {"type": "input_image", "image_url": "data:image/png;base64," + image_data, "detail": "high"}]
        if render and render.png_path and render.png_path.is_file():
            png = await asyncio.to_thread(lambda: base64.b64encode(render.png_path.read_bytes()).decode("ascii"))
            content.extend([{"type": "input_text", "text": "下面是当前候选实际输出。"},
                            {"type": "input_image", "image_url": "data:image/png;base64," + png, "detail": "high"}])
        return [{"role": "system", "content": [{"type": "input_text", "text": prompt}]},
                {"role": "user", "content": content}]

    async def _call_model(self, context: _RunContext, page_id: str, client: Any,
                          stage: str, operation: str, input_value: list[dict[str, Any]]) -> Any:
        self._checkpoint(context)
        task = self._task(context, page_id)
        counter = {"recognize": task.recognize_count, "repair": task.repair_count, "verify": task.review_count}[stage]
        used = counter > (1 if operation == "review_repaired" else 0)
        retry = False
        if used:
            attempts = [item for item in self.storage.get_attempts(context.run.run_id, page_id) if item.stage == stage]
            previous = next((item for item in attempts if item.attempt_id == task.stage_data.get(operation + "_attempt_id")),
                            attempts[-1] if attempts else None)
            if (previous is None or previous.state != "failed"
                    or not task.stage_data.get(operation + "_retryable") or task.retry_count >= 2):
                raise _Unavailable("本阶段请求已消费但没有可用已保存结果；结果未知或成功未落盘时不重发")
            retry = True
        method = {"recognize": client.recognize_page, "verify": client.review_page,
                  "repair": client.propose_repair}[stage]

        def start(*, retry: bool = False):
            self._checkpoint(context)
            try:
                attempt = self.storage.reserve_attempt(context.run.run_id, page_id, stage, retry=retry)
            except RequestBudgetExceeded:
                # Pausing uses the same storage exception as an exhausted budget.
                self._checkpoint(context)
                raise
            self._update(context, page_id, stage=stage, state="running", stage_data={
                operation + "_attempt_id": attempt.attempt_id, operation + "_retryable": False,
            })
            return attempt

        def end(attempt_id: str, **values: Any) -> None:
            self.storage.finish_run_attempt(attempt_id, **values)

        for _ in range(3):
            try:
                return await method(context.run.settings_snapshot["extraction_model"], input_value,
                                    on_attempt_start=start, on_attempt_end=end, retry=retry)
            except RequestBudgetExceeded as exc:
                self._checkpoint(context)
                raise _Unavailable(_safe_error(exc)) from exc
            except ModelServiceError as exc:
                repeatable = exc.retryable and not exc.configuration_error and not exc.result_uncertain
                data = {operation + "_retryable": repeatable, operation + "_error": _safe_error(exc)}
                if exc.configuration_error:
                    context.configuration_error = _safe_error(exc)
                    context.first_recognized.set()
                    data["configuration_error"] = context.configuration_error
                self._update(context, page_id, stage_data=data, error=_safe_error(exc))
                self._checkpoint(context)
                if not repeatable or self._task(context, page_id).retry_count >= 2:
                    raise _Unavailable(_safe_error(exc)) from exc
                retry = True
        raise _Unavailable("本页暂时错误重试已用尽")

    def _save_layout(self, context: _RunContext, page_id: str, layout: SourceFidelityLayout,
                     previous: Revision | None = None, result: StructuredPageResult | None = None) -> Revision:
        try:
            text = generate_source_fidelity_latex(layout)
        except FidelityLayoutError:
            text = ""
        return self.storage.save_candidate(
            context.run.run_id, page_id, layout_source=layout, text=text,
            page_kind=result.page_kind if result else previous.page_kind if previous else "content",
            page_side=result.page_side if result else previous.page_side if previous else "unknown",
            cover_fields=result.cover_fields if result else previous.cover_fields if previous else [],
            header_segments=result.header_segments if result else previous.header_segments if previous else [],
            footer_segments=result.footer_segments if result else previous.footer_segments if previous else [],
            generator_version=GENERATOR_VERSION, parent_revision_id=previous.revision_id if previous else None,
        )

    @staticmethod
    def _observations(revision: Revision) -> StructuredPageResult:
        layout = revision.layout_source
        return StructuredPageResult(
            response_version=2, page_kind=revision.page_kind, page_side=revision.page_side,
            body_latex="", cover_fields=revision.cover_fields,
            header_segments=revision.header_segments, footer_segments=revision.footer_segments,
            layout=LayoutObservation.model_validate({name: getattr(layout, name) for name in LayoutObservation.model_fields})
            if layout and revision.page_kind == "content" else None,
        )

    async def _process_page(self, context: _RunContext, page_id: str, client: Any) -> None:
        try:
            record, image_path, metadata, analysis = await self._prepare(context, page_id)
            task = self._task(context, page_id)
            if task.stage_data.get("recognize_unavailable"):
                await self._source_only_page(context, page_id, task.stage_data["recognize_unavailable"],
                                             prepared=(record, image_path, metadata, analysis))
                return
            recognition_id = task.stage_data.get("recognition_revision_id")
            if recognition_id is None and task.candidate_revision_id and task.stage == "recognize":
                recognition_id = task.candidate_revision_id
            if recognition_id is None:
                self._checkpoint(context)
                self._update(context, page_id, stage="recognize", state="running")
                try:
                    result = await self._call_model(context, page_id, client, "recognize", "recognize",
                                                    await self._input(WORKFLOW_RECOGNITION_PROMPT, record, image_path))
                    validate_recognition(result)
                    layout = SourceFidelityLayout(
                        **(result.layout.model_dump() if result.layout else {}), source=metadata,
                        content_revision=task.base_content_revision + 1, layout_revision=task.base_layout_revision + 1,
                    )
                    raw = self.storage.save_candidate(
                        context.run.run_id, page_id, layout_source=layout, text="", page_kind=result.page_kind,
                        page_side=result.page_side, cover_fields=result.cover_fields,
                        header_segments=result.header_segments, footer_segments=result.footer_segments,
                        generator_version=GENERATOR_VERSION,
                    )
                    recognition_id = raw.revision_id
                except (_Unavailable, ValueError) as exc:
                    self._update(context, page_id, completed_stage="recognize",
                                 stage_data={"recognize_unavailable": _safe_error(exc)}, error=_safe_error(exc))
                    self._checkpoint(context)
                    await self._source_only_page(context, page_id, _safe_error(exc),
                                                 prepared=(record, image_path, metadata, analysis))
                    return
                self._update(context, page_id, completed_stage="recognize",
                             stage_data={"recognition_revision_id": recognition_id})
            else:
                self._update(context, page_id, completed_stage="recognize",
                             stage_data={"recognition_revision_id": recognition_id})
            context.first_recognized.set()
            self._checkpoint(context)
            task = self._task(context, page_id)
            layout_id = task.stage_data.get("layout_revision_id")
            if layout_id is None:
                self._update(context, page_id, stage="layout", state="running")
                raw = self._revision(recognition_id)
                if raw.page_kind == "content":
                    layout = await asyncio.to_thread(
                        solve_layout, self._observations(raw), metadata, self._layout_book(context),
                        task.base_content_revision + 1, task.base_layout_revision + 1,
                        image_path=image_path, book_dir=context.book_dir, analysis=analysis,
                    )
                else:
                    layout = raw.layout_source
                recovered = self._save_layout(context, page_id, layout, raw)
                layout_id = recovered.revision_id
                self._update(context, page_id, completed_stage="layout", stage_data={"layout_revision_id": layout_id})
            self._checkpoint(context)
            best, rendered = await self._render_page(context, page_id, layout_id)
            self._checkpoint(context)
            initial = await self._review(context, page_id, client, record, image_path, analysis,
                                         best, rendered, "review_initial")
            self._checkpoint(context)
            best, rendered, conclusion = await self._repair_page(
                context, page_id, client, record, image_path, metadata, analysis, best, rendered, initial,
            )
            self._checkpoint(context)
            await self._finalize(context, page_id, image_path, metadata, best, rendered, conclusion)
        except (_Paused, _ConfigurationStopped):
            if self._task(context, page_id).result_status is None:
                self._update(context, page_id, state="interrupted", error="停止新阶段；已发送请求及已有候选均已保留")
            raise
        except asyncio.CancelledError:
            if self._task(context, page_id).result_status is None:
                self._update(context, page_id, state="interrupted", error="阶段已中断；已发送请求不自动重发")
            raise
        except (OSError, ValueError, fitz.FileDataError) as exc:
            self._checkpoint(context)
            await self._source_only_page(context, page_id, _safe_error(exc))

    async def _compile_revision(self, context: _RunContext, page_id: str, revision: Revision) -> CandidateRenderResult:
        try:
            cached = load_candidate_render(context.book, revision, context.book_dir)
        except (OSError, ValueError, KeyError, TypeError):
            return CandidateRenderResult(layout=revision.layout_source, error="已保存候选的缓存不可读取；自动保留源内容")
        if cached is not None:
            return cached
        task = self._task(context, page_id)
        attempted = task.stage_data.get("compiled_revision_ids", [])
        if revision.revision_id in attempted:
            return CandidateRenderResult(layout=revision.layout_source,
                                         error="已预留的候选编译未留下可用产物；恢复不重复编译该候选")
        self._checkpoint(context)
        try:
            index = self.storage.reserve_compile(context.run.run_id, page_id)
        except RequestBudgetExceeded as exc:
            self._checkpoint(context)
            return CandidateRenderResult(layout=revision.layout_source, error=_safe_error(exc))
        self._update(context, page_id, stage_data={"compiled_revision_ids": [*attempted, revision.revision_id]})
        return await render_candidate(context.book, revision, context.book_dir,
                                      run_id=context.run.run_id, page_id=page_id, compile_index=index)

    @staticmethod
    def _better_render(previous: CandidateRenderResult, candidate: CandidateRenderResult) -> bool:
        key = lambda item: (item.code, item.line_id, item.block_id)
        old = {key(item): item.severity for item in previous.diagnostics if item.severity != "info"}
        new = {key(item): item.severity for item in candidate.diagnostics if item.severity != "info"}
        return (render_score(candidate) < render_score(previous) and new.keys() <= old.keys()
                and not any(old[name] != "error" and severity == "error" for name, severity in new.items()))

    async def _render_page(self, context: _RunContext, page_id: str, layout_id: str
                           ) -> tuple[Revision, CandidateRenderResult]:
        task = self._task(context, page_id)
        best = self._revision(task.stage_data.get("best_revision_id", layout_id))
        if not task.stage_data.get("render_done"):
            self._checkpoint(context)
            self._update(context, page_id, stage="render", state="running", stage_data={"best_revision_id": best.revision_id})
        rendered = await self._compile_revision(context, page_id, best)
        if task.stage_data.get("render_done"):
            return best, rendered
        for _ in range(2):
            self._checkpoint(context)
            task = self._task(context, page_id)
            if task.stage_data.get("geometry_terminated"):
                break
            candidate_id = task.stage_data.get("geometry_candidate_revision_id")
            unevaluated = candidate_id and candidate_id != task.stage_data.get("geometry_evaluated_revision_id")
            if not unevaluated:
                if task.stage_data.get("geometry_adjustments", 0) >= 2 or rendered.error or best.layout_source is None:
                    break
                adjustment = local_geometry_adjustment(best.layout_source, rendered.diagnostics, rendered.measurements)
                if adjustment is None:
                    break
                self._update(context, page_id, stage_data={"geometry_adjustments": task.stage_data.get("geometry_adjustments", 0) + 1})
                candidate = self._save_layout(context, page_id, adjustment, best)
                candidate_id = candidate.revision_id
                self._update(context, page_id, stage_data={"geometry_candidate_revision_id": candidate_id})
            candidate = self._revision(candidate_id)
            adjusted = await self._compile_revision(context, page_id, candidate)
            better = self._better_render(rendered, adjusted)
            if better:
                best, rendered = candidate, adjusted
            self._update(context, page_id, stage_data={"best_revision_id": best.revision_id,
                         "geometry_evaluated_revision_id": candidate_id, "geometry_terminated": not better})
            if not better:
                break
        self._update(context, page_id, completed_stage="render", stage_data={"render_done": True})
        return best, rendered

    async def _review(self, context: _RunContext, page_id: str, client: Any, record: dict[str, Any],
                      image_path: Path, analysis: SourceAnalysis, revision: Revision,
                      rendered: CandidateRenderResult, operation: str) -> Assessment:
        existing = self.storage.get_assessment(revision.revision_id)
        if existing is not None:
            return existing
        self._checkpoint(context)
        self._update(context, page_id, stage="verify", state="running")
        try:
            review = await self._call_model(context, page_id, client, "verify", operation,
                                            await self._input(WORKFLOW_REVIEW_PROMPT, record, image_path,
                                                              revision=revision, render=rendered))
            conclusion = assess(revision, rendered, analysis, review, run_id=context.run.run_id)
        except _Unavailable as exc:
            conclusion = assess(revision, rendered, analysis, None, reason=_safe_error(exc), run_id=context.run.run_id)
        self.storage.save_assessment(conclusion)
        self._update(context, page_id, completed_stage="verify", stage_data={operation + "_done": True,
                                                                            operation + "_revision_id": revision.revision_id})
        return conclusion

    async def _repair_page(
        self, context: _RunContext, page_id: str, client: Any, record: dict[str, Any], image_path: Path,
        metadata: PageSourceMetadata, analysis: SourceAnalysis, best: Revision,
        rendered: CandidateRenderResult, initial: Assessment,
    ) -> tuple[Revision, CandidateRenderResult, Assessment]:
        task = self._task(context, page_id)
        if task.stage_data.get("repair_done"):
            final = self._revision(task.stage_data.get("best_revision_id", best.revision_id))
            return final, await self._compile_revision(context, page_id, final), self.storage.get_assessment(final.revision_id) or initial
        if passed(initial) or not any(item.disposition == "repairable" for item in initial.issues):
            self._update(context, page_id, completed_stage="repair", stage_data={"repair_done": True})
            return best, rendered, initial
        self._checkpoint(context)
        self._update(context, page_id, stage="repair", state="running", stage_data={"repair_base_revision_id": best.revision_id})
        task = self._task(context, page_id)
        candidate_id = task.stage_data.get("repair_candidate_revision_id")
        if not candidate_id and task.repair_count and task.candidate_revision_id != best.revision_id:
            current = self._revision(task.candidate_revision_id)
            if current.parent_revision_id == best.revision_id and current.revision_id != task.stage_data.get("geometry_candidate_revision_id"):
                candidate_id = current.revision_id
        if candidate_id is None:
            try:
                proposal = await self._call_model(context, page_id, client, "repair", "repair",
                    await self._input(WORKFLOW_REPAIR_PROMPT, record, image_path, revision=best,
                                      render=rendered, assessment=initial))
                repaired = apply_repair(best, initial, proposal)
                # Remeasure proposed targets while retaining every unrelated original line.
                observed = best.model_copy(update={"layout_source": repaired})
                solved = await asyncio.to_thread(solve_layout, self._observations(observed), metadata, self._layout_book(context),
                    task.base_content_revision + 1, task.base_layout_revision + 1,
                    image_path=image_path, book_dir=context.book_dir, analysis=analysis)
                line_ids, region_ids, group_ids = set(), set(), set()
                for operation in proposal.operations:
                    if operation.op == "replace_line":
                        line_ids.add(operation.line_id)
                    elif operation.op == "update_geometry":
                        (line_ids if operation.target_type == "line" else region_ids).add(operation.target_id)
                    elif operation.op == "update_equation_group":
                        group_ids.add(operation.group_id)
                        line_ids.update(operation.new_group.line_ids)
                    elif operation.op == "insert_region":
                        region_ids.add(operation.region.region_id)
                        line_ids.update(line.line_id for line in operation.lines)
                        group_ids.update(group.group_id for group in operation.equation_groups)
                repaired = repaired.model_copy(update={
                    "lines": [next(item for item in solved.lines if item.line_id == line.line_id) if line.line_id in line_ids else line
                              for line in repaired.lines],
                    "regions": [next(item for item in solved.regions if item.region_id == region.region_id) if region.region_id in region_ids else region
                                for region in repaired.regions],
                    "equation_groups": [next(item for item in solved.equation_groups if item.group_id == group.group_id)
                                        if group.group_id in group_ids or set(group.line_ids) & line_ids else group
                                        for group in repaired.equation_groups],
                    "source_assets": [*repaired.source_assets, *(asset for asset in solved.source_assets
                        if asset.region_id in region_ids and asset.region_id not in {item.region_id for item in repaired.source_assets})],
                })
                repaired = SourceFidelityLayout.model_validate(repaired.model_dump())
                candidate = self._save_layout(context, page_id, repaired, best)
                candidate_id = candidate.revision_id
                self._update(context, page_id, completed_stage="repair", stage_data={"repair_candidate_revision_id": candidate_id})
            except (_Unavailable, ValueError) as exc:
                self._update(context, page_id, completed_stage="repair", stage_data={"repair_done": True, "repair_error": _safe_error(exc)})
                return best, rendered, initial
        self._checkpoint(context)
        candidate = self._revision(candidate_id)
        adjusted = await self._compile_revision(context, page_id, candidate)
        self._checkpoint(context)
        revised = await self._review(context, page_id, client, record, image_path, analysis,
                                     candidate, adjusted, "review_repaired")
        if improves(initial, revised):
            best, rendered, initial = candidate, adjusted, revised
        self._update(context, page_id, stage_data={"repair_done": True, "best_revision_id": best.revision_id})
        return best, rendered, initial

    async def _whole_source(self, context: _RunContext, page_id: str, image_path: Path,
                            metadata: PageSourceMetadata, best: Revision, reason: str
                            ) -> tuple[Revision, CandidateRenderResult]:
        result = await preserve_source_page(context.book, best, context.book_dir, image_path=image_path,
            metadata=metadata, reason=reason, run_id=context.run.run_id, page_id=page_id)
        if result.pdf_path is None or result.layout is None:
            return best, result
        final = self._save_layout(context, page_id, result.layout, best)
        cached = await cache_candidate_render(context.book, final, context.book_dir, result=result)
        self._update(context, page_id, stage_data={"preserved_revision_id": final.revision_id})
        return final, cached

    async def _finalize(self, context: _RunContext, page_id: str, image_path: Path,
                        metadata: PageSourceMetadata, best: Revision, rendered: CandidateRenderResult,
                        conclusion: Assessment, *, source_only: bool = False) -> None:
        self._checkpoint(context, source_only=source_only)
        self._update(context, page_id, stage="finalize", state="running")
        task = self._task(context, page_id)
        preserved_id = task.stage_data.get("preserved_revision_id")
        if preserved_id:
            final = self._revision(preserved_id)
            try:
                result = load_candidate_render(context.book, final, context.book_dir)
            except (OSError, ValueError, KeyError, TypeError):
                result = None
                self._update(context, page_id, stage_data={"preservation_cache_error": "保留产物缓存不可读取，重新从源图生成"})
            if result is not None and result.pdf_path:
                best, rendered = final, result
                conclusion = self.storage.get_assessment(final.revision_id) or rebind_assessment(
                    conclusion, final, disposition=final.layout_source.source_disposition)
            else:
                preserved_id = None
        regions = preservation_regions(conclusion, best.layout_source) if best.layout_source else []
        if not preserved_id:
            if rendered.pdf_path is None or rendered.error or any(box == (0., 0., 1., 1.) for box, _, _ in regions):
                reason = rendered.error or (regions[0][2] if regions else "重排输出不能可靠生成，自动保留源页")
                best, rendered = await self._whole_source(context, page_id, image_path, metadata, best, reason)
            elif regions:
                assets = []
                for bbox, region_id, reason in regions:
                    assets.append(await asyncio.to_thread(persist_source_region, context.book_dir, image_path,
                        metadata, bbox, region_id, reason, purpose="uncertain_content"))
                layout = best.layout_source.model_copy(update={
                    "source_assets": [*best.layout_source.source_assets, *assets],
                    "source_disposition": "regions_preserved", "disposition_reason": "有限复核/修复后仍不确定的源区域已保留",
                })
                retained = self._save_layout(context, page_id, layout, best)
                result = await preserve_source_regions(context.book, retained, context.book_dir, best_candidate=rendered)
                if result.pdf_path is None or result.error:
                    best, rendered = await self._whole_source(context, page_id, image_path, metadata, best,
                        result.error or "无法安全覆盖局部区域，自动保留必要源页")
                else:
                    best, rendered = retained, result
                    self._update(context, page_id, stage_data={"preserved_revision_id": best.revision_id})
        if rendered.pdf_path is None or rendered.error:
            self._fail_page(context, page_id, rendered.error or "源页不能读取或生成，输出不完整")
            return
        if best.revision_id != conclusion.revision_id or best.layout_source.source_disposition != "transcribed":
            disposition = best.layout_source.source_disposition
            conclusion = rebind_assessment(conclusion, best, disposition=disposition)
            conclusion.layout = "unverified" if disposition == "source_page_preserved" else "uncertain"
            conclusion.issues.append(issue(best, "source_preserved", best.layout_source.disposition_reason or "源内容已保留",
                                           run_id=context.run.run_id, disposition=disposition))
            self.storage.save_assessment(conclusion)
        status = "auto_passed" if passed(conclusion) else "completed_with_issues"
        adopted = self.storage.adopt_candidate(context.run.run_id, page_id, best.revision_id, status)
        if adopted and status == "auto_passed" and len(context.style_samples) < 20:
            context.style_samples.append(best.layout_source)
            context.style_summary = summarize_book_styles(context.style_samples)
        self._update(context, page_id, completed_stage="finalize", state="succeeded")

    async def _source_only_page(self, context: _RunContext, page_id: str, reason: str, *,
                                prepared: tuple[dict[str, Any], Path, PageSourceMetadata, SourceAnalysis] | None = None) -> None:
        self._checkpoint(context, source_only=True)
        try:
            record, image_path, metadata, analysis = prepared or await self._prepare(context, page_id, source_only=True)
            task = self._task(context, page_id)
            best_id = task.stage_data.get("best_revision_id") or task.candidate_revision_id
            best = self.storage.get_revision(best_id) if best_id else None
            if best is None:
                layout = SourceFidelityLayout(source=metadata, content_revision=task.base_content_revision + 1,
                                              layout_revision=task.base_layout_revision + 1)
                best = self._save_layout(context, page_id, layout)
            conclusion = assess(best, None, analysis, None, reason=reason, run_id=context.run.run_id)
            self.storage.save_assessment(conclusion)
            await self._finalize(context, page_id, image_path, metadata, best,
                                 CandidateRenderResult(layout=best.layout_source, error=reason), conclusion, source_only=True)
        except (OSError, ValueError, fitz.FileDataError) as exc:
            self._fail_page(context, page_id, _safe_error(exc))

    def _fail_page(self, context: _RunContext, page_id: str, reason: str) -> None:
        task = self._task(context, page_id)
        final_id = task.base_revision_id
        final_data: dict[str, Any] = {}
        if task.candidate_revision_id:
            self.storage.adopt_candidate(context.run.run_id, page_id, task.candidate_revision_id, "failed")
            final_id = self._task(context, page_id).stage_data.get("final_revision_id") or final_id
        else:
            current = self.storage.get_page_record(context.run.book_id, task.page_number)
            if current is not None:
                final_id = current["current_revision_id"] or final_id
                fields = ("page_id", "number", "source_id", "source_page", "source_version", "width", "height",
                          "image_name", "source_filename", "source_kind", "source_directory", "source_page_count")
                final_data["final_source"] = {name: current[name] for name in fields}
        final_data["final_revision_id"] = final_id
        self._update(context, page_id, stage="finalize", state="failed", completed_stage="finalize",
                     result_status="failed", error=reason, stage_data=final_data)
        final = self._revision(final_id)
        self.storage.save_assessment(Assessment(
            assessment_id=str(uuid4()), run_id=context.run.run_id, page_id=page_id, revision_id=final.revision_id,
            content="unverified", layout="failed", coverage="failed", rule_version=RULE_VERSION, created_at=now(),
            issues=[issue(final, "source_unavailable", reason, run_id=context.run.run_id, severity="error",
                          disposition="failed；明确记录缺失页，输出不完整")],
        ))

    async def _export(self, context: _RunContext) -> None:
        self._checkpoint(context, source_only=True)
        run = self.storage.get_run(context.run.book_id, context.run.run_id)
        manifest = self.storage.get_export_manifest(run.book_id, run.export_manifest_id) if run.export_manifest_id else None
        if manifest is None:
            manifest = self.storage.create_export_manifest(
                run.book_id, expected_arrangement_revision=run.arrangement_revision, run_id=run.run_id,
                generator_version=GENERATOR_VERSION,
            )
        try:
            outputs = await generate_manifest_outputs(
                self.storage.get_manifest_book(run.book_id, manifest.manifest_id), manifest,
                self.storage.get_manifest_pages(run.book_id, manifest.manifest_id), context.book_dir,
            )
            self.storage.record_manifest_outputs(run.book_id, manifest.manifest_id, outputs)
        except (OSError, ValueError, fitz.FileDataError) as exc:
            self.storage.set_run_status(run.book_id, run.run_id, "failed", "结果导出失败：" + _safe_error(exc))
            return
        failure = context.configuration_error or (
            outputs.get("error") or "导出产物不完整" if outputs.get("complete") == "false" else None
        )
        self.storage.set_run_status(run.book_id, run.run_id, "failed" if failure else "succeeded", failure)


WorkflowProcessor = BookProcessor


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or "页面处理发生技术错误")[:1000]
