"""V2 source preparation, persisted model operations, content review and recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymupdf as fitz
from PIL import Image

from .compile_service import cache_candidate_render
from .content_contract import UnresolvedSpan, ContentIssue, ContentParseResult, PageContent, PageRegion
from .importers import IMAGE_LOCK, prepare_analyzed_page, prepare_page
from .layout_contract import CropMapping, PageSourceMetadata
from .models import Revision, WorkflowError
from .quality_service import (
    content_conclusion, content_improves, local_content_candidate, recovery_targets, renew_content,
    reviewed_content, source_coverage_content,
)
from .responses_client import (
    CONFIGURATION_STATUS, ContentClientV2, ModelRequestResult, ModelServiceError, RequestDiagnostic,
    _saved_http_status,
)
from .source_analysis import SourceAnalysis, analyze_source, prepare_recognition_inputs, source_reading_order
from .workflow_model_contract import ContentReview
from .storage import RequestBudgetExceeded
from .workflow_context import _RunContext, _ConfigurationStopped, _Unavailable, _SourceUnavailable, _safe_error


class ContentWorkflow:
    """Stage methods composed by BookProcessor; shared state lives on its context."""

    def _error_v2(self, context: _RunContext, page_id: str, stage: str, category: str, message: str,
                  *, phase: str = "initial", attempt_id: str | None = None, field_path: str | None = None,
                  block_id: str | None = None, bbox=None) -> WorkflowError:
        error = WorkflowError(stage=stage, category=category, message=message[:2_000], phase=phase,
                              attempt_id=attempt_id, field_path=field_path, block_id=block_id, source_bbox=bbox)
        if error not in self._task(context, page_id).errors:
            self._update(context, page_id, workflow_error=error)
        return error

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
        fixed = self._task(context, page_id).stage_data.get("recognition_scope") == "printed_fixed_layout"
        prepared = self._task(context, page_id).stage_data.get("prepared")
        if prepared is None:
            self._update(context, page_id, stage="prepare", state="running")
            try:
                if fixed:
                    width, height, image_name, metadata = await asyncio.to_thread(prepare_page, context.book_dir, record)
                else:
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
            if not fixed:
                analysis = await asyncio.to_thread(analyze_source, context.book_dir / prepared["image_name"], metadata,
                                                   book_dir=context.book_dir, max_crops=3)
        if fixed:
            analysis = SourceAnalysis(width=prepared["width"], height=prepared["height"], mask=b"",
                                      page_id=page_id, source_version=metadata.source_version)
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
                           source_version=task.source_version, recognition_scope=task.stage_data.get("recognition_scope", "legacy_all_visible"),
                           fixed_layout_version=1 if task.stage_data.get("recognition_scope") == "printed_fixed_layout" else None,
                           issues=[ContentIssue(category=category, reason=reason)])

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
        fixed = self._task(context, page_id).stage_data.get("recognition_scope") == "printed_fixed_layout"
        inputs = await asyncio.to_thread(prepare_recognition_inputs, image_path, metadata, analysis,
                                         book_dir=context.book_dir, max_crops=0 if fixed else 3)
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

    def _save_recovery_v2(self, context: _RunContext, page_id: str, recovery: dict) -> None:
        self._update(context, page_id, stage="recover", state="running", stage_data={"v2_recovery": recovery})

    async def _recover_page_v2(self, context: _RunContext, page_id: str, client) -> None:
        task = self._task(context, page_id)
        if task.stage_data.get("v2_recovery_done") or task.candidate_revision_id is None:
            return
        _, image_path, metadata, analysis = await self._prepare_v2(context, page_id)
        fixed = task.stage_data.get("recognition_scope") == "printed_fixed_layout"
        inputs = await asyncio.to_thread(prepare_recognition_inputs, image_path, metadata, analysis,
                                         book_dir=context.book_dir, max_crops=0 if fixed else 3)
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
                    if fixed:
                        strategy = {"source_version": task.source_version, "input": "fixed_whole_page"}
                    # Plain persisted fields, not a hash: another target's
                    # improvement does not authorize repeating this scheme.
                    strategy = {**strategy, "bbox": list(strategy["bbox"]) if strategy.get("bbox") else None}
                    if any(strategy == item
                           for item in attempted_targets):
                        continue
                    target["strategy"] = strategy
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
                            max_crops=0 if fixed else 1, target_bbox=target["bbox"])
                        result = await self._request_v2(context, page_id, client, "recover", operation_name, local_inputs,
                                                        target=target, reserved_attempt_id=attempts[index].attempt_id)
                        local = result.parsed.content if isinstance(result.parsed, ContentParseResult) else None
                        if local is None:
                            raise _Unavailable("局部响应未取得完整有效内容；其他已保存块保留")
                        old_value = next((block for block in base.page_content.blocks if block.block_id == target.get("block_id")), None)
                        candidate = local_content_candidate(current.page_content, local, target, old_value=old_value)
                        order = ([block.block_id for block in candidate.blocks] if candidate.recognition_scope in {"printed_original_only", "printed_fixed_layout"} else source_reading_order(candidate, analysis))
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
