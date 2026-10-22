"""V2 inherited renders, local layout, source preservation and page outcomes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pymupdf as fitz

from .compile_service import (
    CandidateRenderResult, cache_candidate_render, load_candidate_render, preserve_source_page,
    preserve_source_regions, render_candidate,
)
from .content_contract import FigureBlock
from .importers import prepare_page
from .latex_diagnostics import local_geometry_adjustment
from .latex_export import FidelityLayoutError, generate_source_fidelity_latex
from .layout_contract import PageLayout, PageLinePlacement, PageSourceMetadata, SourceFidelityLayout
from .layout_solver import derive_layout, to_source_fidelity_layout
from .models import PageOutcome, Revision
from .quality_service import content_conclusion
from .source_analysis import persist_source_region
from .storage import RequestBudgetExceeded
from .workflow_context import _RunContext, _Paused, _SourceUnavailable, _safe_error


class OutputWorkflow:
    """Stage methods composed by BookProcessor; shared state lives on its context."""

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

    def _save_layout_v2(self, context: _RunContext, page_id: str, layout: PageLayout, base: Revision,
                         *, render_layout: SourceFidelityLayout | None = None) -> Revision:
        if base.page_content.recognition_scope == "printed_fixed_layout":
            from .latex_export import fixed_content_latex
            if layout.content_revision_id != base.page_content.content_revision_id:
                layout = layout.model_copy(update={"layout_revision_id": str(uuid4()),
                                                    "content_revision_id": base.page_content.content_revision_id})
            generated = fixed_content_latex(base.page_content, paper_size=context.book.paper_size,
                                            figure_assets=layout.source_assets)
            return self.storage.save_layout_candidate(context.run.run_id, page_id, layout,
                        expected_candidate_revision_id=base.revision_id, generated_text=generated)
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

    async def _finish_fixed_page_v2(self, context: _RunContext, page_id: str, revision: Revision) -> None:
        """Use fixed typography and verified figures without source positioning."""
        task = self._task(context, page_id)
        rendered = None
        try:
            _, image_path, metadata, _ = await self._prepare_v2(context, page_id)
            existing_layout = revision.page_layout
            assets = list(existing_layout.source_assets) if existing_layout else []
            available = {asset.region_id for asset in assets if asset.purpose == "figure"
                         and asset.source_layer == "printed" and asset.cleanliness_verified
                         and (context.book_dir / asset.image_name).is_file()}
            missing_figures = []
            for block in revision.page_content.blocks:
                if not isinstance(block, FigureBlock) or block.block_id in available:
                    continue
                verified = next((item for item in revision.page_content.regions if item.region_id == block.block_id), None)
                if block.bbox is not None and block.review_status == "usable" and verified and verified.cleanliness_verified:
                    missing_figures.append(block)
            if existing_layout is None or missing_figures:
                self._update(context, page_id, stage="layout", state="running")
                missing_ids = {block.block_id for block in missing_figures}
                assets = [asset for asset in assets if asset.region_id not in missing_ids]
                for block in missing_figures:
                    try:
                        asset = await asyncio.to_thread(persist_source_region, context.book_dir, image_path, metadata,
                            block.bbox, block.block_id, "固定排版中的印刷图形", purpose="figure")
                    except (OSError, ValueError, fitz.FileDataError) as exc:
                        self._error_v2(context, page_id, "layout", "layout", "图形裁切不可用：" + _safe_error(exc),
                                       phase="output", block_id=block.block_id, bbox=block.bbox)
                        continue
                    assets.append(asset.model_copy(update={"source_layer": "printed", "cleanliness_verified": True}))
                if existing_layout is None or assets != existing_layout.source_assets:
                    layout = PageLayout(recognition_scope="printed_fixed_layout", layout_revision_id=str(uuid4()),
                        content_revision_id=revision.page_content.content_revision_id, page_id=page_id,
                        source_version=task.source_version, source=metadata, source_assets=assets,
                        conclusion="approximate")
                    revision = self._save_layout_v2(context, page_id, layout, revision)
                self._update(context, page_id, completed_stage="layout")
            rendered = await self._compile_v2(context, page_id, revision)
            if rendered.error or rendered.pdf_path is None:
                self._error_v2(context, page_id, "render", "render", rendered.error or "固定排版 PDF 未生成", phase="output")
            for diagnostic in rendered.diagnostics:
                if diagnostic.severity == "error":
                    self._error_v2(context, page_id, "render", "render", diagnostic.message,
                                   phase="output", block_id=diagnostic.block_id)
            self._update(context, page_id, completed_stage="render", stage_data={"v2_render_done": True})
        except _Paused:
            raise
        except (_SourceUnavailable, OSError, ValueError, KeyError, TypeError, fitz.FileDataError) as exc:
            self._error_v2(context, page_id, "render", "render", _safe_error(exc), phase="output")
            revision = self._revision(self._task(context, page_id).candidate_revision_id)
        usable_output = bool(rendered and rendered.pdf_path and not rendered.error
                             and not any(item.severity == "error" for item in rendered.diagnostics))
        self.storage.save_page_outcome(context.run.run_id, page_id, PageOutcome(
            recognition_scope="printed_fixed_layout", page_id=page_id, source_version=task.source_version,
            content=content_conclusion(revision.page_content, blank_reviewed=task.stage_data.get("blank_reviewed", False)),
            layout="approximate" if usable_output else "unavailable", source_disposition="transcribed",
            content_revision_id=revision.page_content.content_revision_id,
            layout_revision_id=revision.page_layout.layout_revision_id if revision.page_layout else None,
            errors=self._task(context, page_id).errors), adopt=bool(revision.page_content.blocks or revision.page_content.blank))

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
        if revision.page_content.recognition_scope == "printed_fixed_layout":
            await self._finish_fixed_page_v2(context, page_id, revision)
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
