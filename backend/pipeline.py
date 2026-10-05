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

from .compile_service import (
    CandidateRenderResult, cache_candidate_render, generate_manifest_outputs,
    load_candidate_render, preserve_source_page, preserve_source_regions, render_candidate,
)
from .importers import prepare_page
from .latex_diagnostics import local_geometry_adjustment
from .latex_export import GENERATOR_VERSION, FidelityLayoutError, generate_source_fidelity_latex
from .layout_contract import LayoutObservation, PageSourceMetadata, SourceFidelityLayout
from .layout_solver import solve_layout, summarize_book_styles
from .model_client import create_model_client
from .models import Assessment, Book, PageTask, Revision, Run, RunCreate, StructuredPageResult
from .prompts import (
    WORKFLOW_RECOGNITION_PROMPT, WORKFLOW_REPAIR_PROMPT, WORKFLOW_REVIEW_PROMPT, page_context,
)
from .quality_service import (
    RULE_VERSION, apply_repair, assess, improves, issue, now, passed,
    preservation_regions, rebind_assessment, render_score, validate_recognition,
)
from .responses_client import ModelServiceError
from .source_analysis import SourceAnalysis, analyze_source, persist_source_region
from .storage import RequestBudgetExceeded, Storage


# Existing preview routes use the importer-owned shared image lock.
_prepare_page = prepare_page


class _Paused(Exception):
    pass


class _ConfigurationStopped(Exception):
    pass


class _Unavailable(Exception):
    """A settled or unrepeatable operation has no usable persisted result."""


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
            raise _ConfigurationStopped()

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
        analysis = await asyncio.to_thread(analyze_source, image_path, metadata)
        return record, image_path, metadata, analysis

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
                        image_path=image_path, book_dir=context.book_dir,
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
                    image_path=image_path, book_dir=context.book_dir)
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
