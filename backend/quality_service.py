"""Local quality conclusions and the five bounded model repair operations."""

from __future__ import annotations

import re
from hashlib import sha256
from datetime import datetime, timezone
from uuid import uuid4

from .compile_service import CandidateRenderResult
from .content_contract import CoverageObservation, ContentIssue, EquationBlock, PageContent, TableBlock, TextBlock
from .layout_contract import BBox, SourceFidelityLayout
from .models import Assessment, Issue, Revision, StructuredPageResult
from .source_analysis import SourceAnalysis, merge_region_content
from .workflow_model_contract import ContentReview, PageReview, RepairProposal


RULE_VERSION = "automatic-quality-v1"
CONTENT_RULE_VERSION = "content-review-v2"


def renew_content(content: PageContent) -> PageContent:
    result = content.model_copy(deep=True)
    result.content_revision_id = str(uuid4())
    for block in result.blocks:
        block.content_revision_id = result.content_revision_id
    return result


def content_block_bbox(block) -> BBox | None:
    boxes = [block.bbox] if block.bbox is not None else []
    boxes.extend(line.bbox for line in getattr(block, "lines", []) if line.bbox is not None)
    boxes.extend(cell.bbox for cell in getattr(block, "cells", []) if cell.bbox is not None)
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes)) if boxes else None


def source_coverage_content(content: PageContent, analysis: SourceAnalysis) -> PageContent:
    """Observe the entire source, including areas outside candidate/coarse boxes."""
    if content.recognition_scope == "printed_original_only":
        result = renew_content(content)
        known = {item.observation_id for item in result.coverage_observations}
        for index, observation in enumerate(analysis.uncovered_content(content)):
            identity = f"{content.source_version}-ink-" + sha256(repr(observation.bbox).encode()).hexdigest()[:20]
            if identity not in known:
                result.coverage_observations.append(CoverageObservation(
                    observation_id=identity, source_bbox=observation.bbox, origin="local_ink",
                    scope_disposition="pending", mapped_region_ids=[]))
        return result
    additions = []
    boxes = [content_block_bbox(block) for block in content.blocks]
    boxes = [box for box in boxes if box is not None]
    for observation in analysis.uncovered_content(content):
        box = observation.bbox
        located = bool(boxes) and not any(outer[0] <= box[0] and outer[1] <= box[1]
                                         and outer[2] >= box[2] and outer[3] >= box[3] for outer in boxes)
        category = "missing_content" if located or not content.blocks else "layout"
        if any(issue.category == category and issue.source_bbox == box and not issue.resolved for issue in content.issues):
            continue
        additions.append(ContentIssue(category=category, reason=observation.reason, source_bbox=box,
                                       severity="warning" if category == "missing_content" else "info"))
    if not additions:
        return content
    result = renew_content(content)
    result.issues = [*result.issues, *additions][:2_000]
    return result


def reviewed_content(content: PageContent, review: ContentReview | None, *, reason: str | None = None,
                     checked_targets: list[dict] | tuple[dict, ...] = ()) -> PageContent:
    """A fresh revision records independent review; ink/agreement never pass it."""
    if review is not None and review.base_content_revision_id != content.content_revision_id:
        raise ValueError("内容复核不属于当前内容修订")
    result = renew_content(content)
    if content.recognition_scope == "printed_original_only":
        result.coverage_reviewed = bool(review and review.full_page_reviewed)
        if review:
            result.regions.extend(review.discovered_regions)
            if review.discovered_regions:
                result.blank = False
                if result.page_kind == "blank":
                    result.page_kind = "content"
        verdicts = {item.region_id: item for item in review.region_verdicts
                    if item.candidate_id == content.content_revision_id and item.region_id in review.checked_region_ids} if review else {}
        for region in result.regions:
            verdict = verdicts.get(region.region_id)
            region.cleanliness_verified = bool(verdict and verdict.cleanliness_verified
                and verdict.content == "usable" and verdict.coverage == "passed"
                and region.layer == "printed" and region.kind == "figure")
        for observation in result.coverage_observations:
            disposition = next((item for item in review.observation_dispositions
                                if item.observation_id == observation.observation_id), None) if review else None
            if disposition:
                observation.scope_disposition = disposition.scope_disposition
                observation.mapped_region_ids = disposition.mapped_region_ids
        for previous in result.issues:
            verdict = verdicts.get(previous.region_id)
            if verdict and verdict.content == "usable" and verdict.coverage == "passed":
                previous.resolved = True
        if review:
            for problem in review.issues:
                region_id = problem.region_id
                region = next((item for item in result.regions if item.region_id == region_id), None)
                if region is None or region.layer in {"annotation", "noise", "decoration"}:
                    continue
                result.issues.append(ContentIssue(category=problem.category, reason=problem.reason,
                    block_id=problem.block_id, source_bbox=problem.source_bbox, field_path=problem.field_path,
                    severity=problem.severity, repairable=problem.repairable, origin="model_review",
                    region_id=region_id, stable_target_id=problem.stable_target_id or region_id))
        for block in result.blocks:
            verdict = verdicts.get(block.source_region_id)
            problems = [item for item in result.issues if not item.resolved and item.region_id == block.source_region_id
                        and item.category not in {"layout", "render"}]
            if verdict and verdict.content == "usable" and verdict.coverage == "passed" and not problems:
                block.recognition_status = block.review_status = "usable"
                block.unresolved_reasons = []
            elif verdict:
                block.review_status = "uncertain"
                block.unresolved_reasons = [item.reason for item in problems]
            else:
                block.review_status = "unverified"
        return PageContent.model_validate(result.model_dump())
    reliable = bool(review and review.full_page_reviewed)
    passed_all = bool(reliable and review.content == "usable" and review.coverage == "passed" and not review.issues)
    def overlaps(left, right):
        return (left is not None and right is not None and max(left[0], right[0]) < min(left[2], right[2])
                and max(left[1], right[1]) < min(left[3], right[3]))

    if reliable:
        for previous in result.issues:
            # Local ink is a review hint, not a ground truth veto. The full-page
            # visual review can settle it without claiming an accuracy proof.
            checked = any(
                (target.get("block_id") is not None and target["block_id"] == previous.block_id)
                or (previous.source_bbox is not None and target.get("bbox") is not None
                    and target["bbox"][0] <= previous.source_bbox[0] and target["bbox"][1] <= previous.source_bbox[1]
                    and target["bbox"][2] >= previous.source_bbox[2] and target["bbox"][3] >= previous.source_bbox[3])
                or (previous.category == "reading_order" and target.get("category") == "reading_order")
                for target in checked_targets
            )
            remains = any(
                (previous.block_id is not None and problem.block_id == previous.block_id)
                or overlaps(previous.source_bbox, problem.source_bbox)
                or (problem.category == "reading_order" and previous.category == "reading_order")
                for problem in review.issues
            )
            # Only a complete independent source review of this candidate can
            # settle a changed target. Other unresolved areas remain recorded.
            if passed_all or previous.category == "layout" or (checked and not remains and review.content != "unavailable"):
                previous.resolved = True
        for problem in review.issues:
            result.issues.append(ContentIssue(category=problem.category, reason=problem.reason,
                                               block_id=problem.block_id, source_bbox=problem.source_bbox,
                                               field_path=problem.field_path, severity=problem.severity,
                                               repairable=problem.repairable, origin="model_review",
                                               region_id=problem.region_id, stable_target_id=problem.stable_target_id))
        if not review.issues and not passed_all:
            result.issues.append(ContentIssue(category="unreadable_source" if review.content == "unavailable" else "missing_content",
                                               reason="独立全页复核未确认内容及完整覆盖", source_bbox=(0., 0., 1., 1.)))
    else:
        result.issues.append(ContentIssue(category="invalid_structure", reason=reason or "独立内容复核未取得可用结果",
                                           source_bbox=(0., 0., 1., 1.)))
    for block in result.blocks:
        problems = [problem for problem in result.issues if not problem.resolved
                    and (problem.block_id == block.block_id or (problem.block_id is None
                         and overlaps(content_block_bbox(block), problem.source_bbox)))
                    and problem.category not in {"layout", "render"}]
        if passed_all:
            block.recognition_status = block.review_status = "usable"
            block.unresolved_reasons = []
        elif reliable and not problems and review.content != "unavailable":
            block.recognition_status = block.review_status = "usable"
            block.unresolved_reasons = []
        elif problems:
            block.review_status = "uncertain"
            block.unresolved_reasons = [problem.reason for problem in problems][:100]
        elif block.review_status != "usable":
            block.review_status = "unverified"
    result.issues = result.issues[:2_000]
    return PageContent.model_validate(result.model_dump())


def content_conclusion(content: PageContent, *, blank_reviewed: bool = False) -> str:
    active = [issue for issue in content.issues if not issue.resolved and issue.category not in {"layout", "render"}]
    if content.recognition_scope == "printed_original_only":
        required = {region.region_id for region in content.regions if region.layer in {"printed", "mixed", "unknown"}}
        covered = {block.source_region_id for block in content.blocks if block.review_status == "usable"}
        if not content.coverage_reviewed or content.unresolved_spans or required - covered or any(item.scope_disposition in {"pending", "unknown", "confirmed_printed_missing"} for item in content.coverage_observations):
            return "uncertain" if content.blocks else "unavailable"
    if not content.blocks:
        return "usable" if content.blank and blank_reviewed and not active else "unverified" if content.blank else "unavailable"
    if active or any(block.review_status in {"uncertain", "unavailable"} for block in content.blocks):
        return "uncertain"
    return "usable" if all(block.review_status == "usable" for block in content.blocks) else "unverified"


def recovery_targets(content: PageContent, analysis: SourceAnalysis) -> list[dict]:
    """Finite located content responsibilities, separate from font/render errors."""
    blocks = {block.block_id: block for block in content.blocks}
    targets, used = [], set()
    for problem in sorted(content.issues, key=lambda item: (item.category != "missing_content", item.category not in {"equation", "table"})):
        if content.recognition_scope == "printed_original_only" and (not problem.repairable or problem.origin != "model_review"):
            continue
        if problem.resolved or problem.category not in {"small_text", "reading_order", "truncated_response",
                                                       "invalid_structure", "missing_content", "equation", "table"}:
            continue
        block = blocks.get(problem.block_id)
        box = (content_block_bbox(block) if block is not None else None) or problem.source_bbox
        if content.recognition_scope == "printed_original_only":
            region = next((item for item in content.regions if item.region_id == problem.region_id), None)
            if region is None or region.layer not in {"printed", "mixed"}:
                continue
            box = region.bbox
        if problem.category == "reading_order" and content.recognition_scope != "printed_original_only":
            targets.append({"id": problem.issue_id, "block_id": problem.block_id, "bbox": box,
                            "category": "reading_order", "kind": "text"})
            continue
        if box is None:
            # Complete malformed/truncated pages can recover complete observed
            # regions; unknown geometry stays unavailable, never a fake crop.
            observations = [region for region in analysis.regions if region.kind in {
                "text", "body", "column", "title", "spanning_title", "header", "footer", "footnote", "equation", "table", "figure"}]
            for region in observations:
                if region.region_id not in used:
                    targets.append({"id": region.region_id, "block_id": None, "bbox": region.bbox,
                                    "category": problem.category, "kind": region.kind if region.kind in {"equation", "table", "figure"} else "text",
                                    "source_region_id": region.region_id})
                    used.add(region.region_id)
            continue
        identity = problem.region_id or problem.stable_target_id or (block.block_id if block is not None else problem.issue_id)
        if identity in used:
            continue
        used.add(identity)
        targets.append({"id": identity, "block_id": block.block_id if block is not None else None,
                        "bbox": box, "category": "invalid_structure" if problem.category == "reading_order" else problem.category,
                        "kind": block.type if block is not None else "equation" if problem.category == "equation" else
                        "table" if problem.category == "table" else "text",
                        "source_region_id": problem.region_id or (block.source_region_id if block is not None else None)})
    return targets


def local_content_candidate(base: PageContent, local: PageContent, target: dict, *, old_value=None) -> PageContent:
    """Only an explicitly bound source target can be replaced before review."""
    selected = local.model_copy(deep=True)
    box = target["bbox"]
    if base.recognition_scope == "printed_original_only":
        region_id = target.get("source_region_id")
        region = next((item for item in base.regions if item.region_id == region_id), None)
        if region is None or region.layer not in {"printed", "mixed"}:
            raise ValueError("恢复目标没有绑定可转录的印刷区域")
        selected.blocks = [block for block in selected.blocks if block.source_region_id == region_id]
        if not selected.blocks:
            raise ValueError("局部候选没有返回绑定区域内容")
        working = renew_content(base)
        indices = [index for index, block in enumerate(working.blocks) if block.source_region_id == region_id]
        position = min(indices) if indices else next((index for index, block in enumerate(working.blocks)
            if next((item.reading_order for item in working.regions if item.region_id == block.source_region_id), 0) > region.reading_order), len(working.blocks))
        working.blocks = [block for block in working.blocks if block.source_region_id != region_id]
        working.blocks[position:position] = selected.blocks
        local_region = next((item for item in selected.regions if item.region_id == region_id), None)
        if local_region is not None:
            next(item for item in working.regions if item.region_id == region_id).cleanliness_verified = local_region.cleanliness_verified
        working.issues.extend(item for item in selected.issues if item.region_id == region_id)
        working.unresolved_spans = [item for item in working.unresolved_spans if item.region_id != region_id] + [item for item in selected.unresolved_spans if item.region_id == region_id]
        for block in working.blocks:
            block.content_revision_id = working.content_revision_id
        working.blank = False
        return PageContent.model_validate(working.model_dump())
    selected.blocks = [block for block in selected.blocks if (position := content_block_bbox(block)) is not None
                       and max(box[0], position[0]) < min(box[2], position[2])
                       and max(box[1], position[1]) < min(box[3], position[3])]
    if not selected.blocks:
        raise ValueError("局部重读未取得能对应目标源区域的有效内容")
    working = base.model_copy(deep=True)
    if target.get("block_id"):
        previous = next((block for block in working.blocks if block.block_id == target["block_id"]), None)
        if previous is None or old_value is None or previous.model_dump(exclude={"content_revision_id"}) != old_value.model_dump(exclude={"content_revision_id"}):
            raise ValueError("局部恢复旧值或内容修订已变化，不能替换当前稿")
        positions = [content_block_bbox(block) for block in selected.blocks]
        union = (min(value[0] for value in positions), min(value[1] for value in positions),
                 max(value[2] for value in positions), max(value[3] for value in positions))
        if not (union[0] <= box[0] + .01 and union[1] <= box[1] + .01
                and union[2] >= box[2] - .01 and union[3] >= box[3] - .01):
            raise ValueError("局部候选没有覆盖完整绑定块，保留旧值")
        replaced_id = selected.blocks[0].block_id
        selected.blocks[0].block_id = previous.block_id
        for problem in selected.issues:
            if problem.block_id == replaced_id:
                problem.block_id = previous.block_id
        working.blocks = [block for block in working.blocks if block.block_id != previous.block_id]
    working.blank = False
    merged = merge_region_content(working, selected, target_bbox=box)
    if base.blank:
        merged.page_kind = local.page_kind if local.page_kind != "blank" else "content"
    return merged


def content_improves(previous: PageContent, candidate: PageContent, *, blank_reviewed: bool = False) -> bool:
    """Accept independent review improvement, never mere repeated text agreement."""
    def key(item, content):
        return ((item.category, item.stable_target_id or item.region_id or item.block_id or item.issue_id)
                if content.recognition_scope == "printed_original_only" else (item.category, item.block_id, item.source_bbox))
    def keys(content):
        return {key(item, content) for item in content.issues
                if not item.resolved and item.category not in {"layout", "render"}}
    old, new = keys(previous), keys(candidate)
    if not new <= old:
        return False
    severity = {"info": 0, "warning": 1, "error": 2}
    old_severity = {}
    for item in previous.issues:
        if not item.resolved:
            identity = key(item, previous)
            old_severity[identity] = max(old_severity.get(identity, -1), severity[item.severity])
    if any(severity[item.severity] > old_severity.get(key(item, candidate), -1)
           for item in candidate.issues if not item.resolved and item.category not in {"layout", "render"}):
        return False
    if previous.recognition_scope == "printed_original_only":
        fields = {"content_revision_id", "recognition_status", "review_status", "unresolved_reasons"}
        old_regions = {block.source_region_id for block in previous.blocks}
        gained = False
        for region_id in old_regions | {block.source_region_id for block in candidate.blocks}:
            before_blocks = [block for block in previous.blocks if block.source_region_id == region_id]
            after_blocks = [block for block in candidate.blocks if block.source_region_id == region_id]
            protected = before_blocks and all(block.review_status == "usable" for block in before_blocks) and not any(
                item.region_id == region_id and not item.resolved for item in previous.issues)
            if protected and [block.model_dump(exclude=fields) for block in before_blocks] != [block.model_dump(exclude=fields) for block in after_blocks]:
                return False
            if after_blocks and all(block.review_status == "usable" for block in after_blocks) and not protected:
                gained = True
        return gained or len(new) < len(old)
    before, after = {block.block_id: block for block in previous.blocks}, {block.block_id: block for block in candidate.blocks}
    if not before.keys() <= after.keys():
        return False
    status_fields = {"content_revision_id", "recognition_status", "review_status", "unresolved_reasons"}
    # An unrelated usable block cannot change as a side effect. A retained
    # target can improve, and an independently reviewed new located block can
    # improve partial recovery while the remaining page issue stays uncertain.
    for identity, block in before.items():
        if block.review_status == "usable" and not any(item.block_id == identity and not item.resolved for item in previous.issues):
            if block.model_dump(exclude=status_fields) != after[identity].model_dump(exclude=status_fields):
                return False
    gained = any(block.review_status == "usable" and content_block_bbox(block) is not None
                 for identity, block in after.items() if identity not in before)
    target_improved = any(after[identity].review_status == "usable" and block.review_status != "usable"
                          for identity, block in before.items())
    rank = {"usable": 0, "uncertain": 1, "unverified": 2, "unavailable": 3}
    return (gained or target_improved or len(new) < len(old)
            or rank[content_conclusion(candidate, blank_reviewed=blank_reviewed)]
            < rank[content_conclusion(previous, blank_reviewed=blank_reviewed)])


_PROGRAM_COMMAND = re.compile(
    r"\\(?:documentclass|usepackage|RequirePackage|PassOptionsTo\w+|input|include|includegraphics|"
    r"openin|openout|read\d*|write\d*|immediate|special|directlua|luaexec|latelua|catcode|csname|scantokens|"
    r"def|edef|gdef|xdef|let|futurelet|newcommand|renewcommand|providecommand|newenvironment|"
    r"renewenvironment|newread|newwrite|loop|repeat|shipout|every\w+|endinput)\b|"
    r"\\(?:begin|end)\s*\{\s*document\s*\}", re.IGNORECASE,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_fragment(value: str) -> None:
    # Only new model fragments pass this boundary; existing custom LaTeX stays authoritative.
    if _PROGRAM_COMMAND.search(value) or "^^" in value:
        raise ValueError("模型片段不能包含完整文档、文件/程序操作或宏定义")


def validate_recognition(result: StructuredPageResult) -> None:
    if result.page_kind == "content" and result.body_latex.strip():
        raise ValueError("自动识别内容必须由原行结构表达，不能另返整页正文")
    validate_fragment(result.body_latex)
    if result.layout:
        for line in result.layout.lines:
            validate_fragment(line.latex)
        for group in result.layout.equation_groups:
            if group.number:
                validate_fragment(group.number.latex)
    for segment in result.header_segments + result.footer_segments:
        validate_fragment(segment.text)


def issue(revision: Revision, category: str, reason: str, *, run_id: str | None = None, severity: str = "warning",
          bbox: BBox | None = None, region_id: str | None = None, line_id: str | None = None,
          disposition: str = "自动标记；保留最佳候选") -> Issue:
    bound_run_id = run_id or revision.run_id
    if bound_run_id is None:
        raise ValueError("问题说明必须关联本次运行")
    return Issue(issue_id=str(uuid4()), run_id=bound_run_id, page_id=revision.page_id,
                 revision_id=revision.revision_id, category=category, severity=severity,
                 source_bbox=bbox, region_id=region_id, line_id=line_id,
                 reason=reason[:2_000], disposition=disposition)


def assess(revision: Revision, render: CandidateRenderResult | None, analysis: SourceAnalysis,
           review: PageReview | None, *, reason: str | None = None, run_id: str | None = None) -> Assessment:
    bound_run_id = run_id or revision.run_id
    if bound_run_id is None:
        raise ValueError("复核必须关联本次运行")

    def problem(category: str, reason: str, **values) -> Issue:
        return issue(revision, category, reason, run_id=bound_run_id, **values)

    problems: list[Issue] = []
    content = review.content if review and review.full_page_reviewed else "unverified"
    coverage = review.coverage if review and review.full_page_reviewed else "unverified"
    layout_status = "passed" if render and render.pdf_path and not render.error else "failed"
    layout = revision.layout_source
    if review:
        for item in review.issues:
            problems.append(problem("content:" + item.category, item.reason,
                                  severity=item.severity, bbox=item.source_bbox,
                                  region_id=item.region_id, line_id=item.line_id,
                                  disposition="repairable" if item.repairable else "自动保留源内容"))
        if not review.full_page_reviewed:
            problems.append(problem("coverage", "独立审查未检查完整源页", disposition="自动保留源内容"))
        if review.full_page_reviewed and review.content != "passed" and not review.issues:
            problems.append(problem("content", "独立全页审查未确认可靠转录", disposition="自动保留源内容"))
        if review.full_page_reviewed and review.coverage != "passed" and not review.issues:
            problems.append(problem("coverage", "独立全页审查未确认覆盖完整", disposition="自动保留源内容"))
    else:
        problems.append(problem("review_unavailable", reason or "独立全页内容审查未完成", disposition="自动保留源内容"))
    if layout and revision.page_kind == "content":
        local = analysis.uncovered_regions(layout)
        for item in local:
            problems.append(problem("coverage", item.reason, bbox=item.bbox,
                                  region_id=item.region_id, line_id=item.line_ids[0] if item.line_ids else None,
                                  disposition="repairable"))
        if local:
            coverage = "uncertain"
        if layout.lines and layout.body_font_basis in {None, "project", "model_estimate"}:
            problems.append(problem("layout:font_basis", "字形依据不足，已采用自动估计并保留其依据"))
            if layout_status == "passed":
                layout_status = "uncertain"
    elif revision.page_kind != "content":
        # Cover metadata alone is not evidence of complete geometric restoration.
        problems.append(problem("layout:cover", "封面书目信息已转录；未取得全页几何恢复证据"))
        if layout_status == "passed":
            layout_status = "uncertain"
        problems.append(problem("coverage", "封面观察未包含完整源页布局；源页将自动保留", disposition="自动保留源内容"))
        coverage = "uncertain"
    if render:
        # A PDF by itself does not establish that output checks were performed.
        if render.pdf_path and not render.error and (render.document is None or (
            layout and layout.lines and (not render.measurements or render.document.source_to_output_affine is None)
        )):
            layout_status = "unverified"
            problems.append(problem("layout:checks_incomplete", "缺少当前候选的文档映射或自然尺寸，实际布局检查未完成"))
        for item in render.diagnostics:
            if item.code == "SOURCE_CONTENT_REVIEW":
                # Independent full-page review supersedes model observations;
                # local coverage findings above still retain their source boxes.
                problems.append(problem("source_observation", item.message, severity="info"))
                continue
            problems.append(problem("output:" + item.code, item.message,
                                  severity=item.severity, bbox=item.source_bbox,
                                  region_id=item.block_id, line_id=item.line_id,
                                  disposition="repairable" if item.line_id or item.source_bbox else "自动标记；保留最佳候选"))
            if item.code == "LAYOUT_UNVERIFIED" or item.coverage != "complete":
                if layout_status != "failed":
                    layout_status = "unverified" if item.coverage == "none" or layout_status == "unverified" else "uncertain"
            elif item.severity in {"warning", "error"} and layout_status == "passed":
                layout_status = "uncertain"
            if item.code in {"MISSING_GLYPH", "MISSING_GLYPHS", "UNMAPPED_GLYPH", "UNMAPPED_GLYPHS"}:
                content = "uncertain"
            if item.coverage != "complete" and coverage == "passed" and item.code in {
                "MISSING_GLYPH", "MISSING_GLYPHS", "UNMAPPED_GLYPH", "UNMAPPED_GLYPHS",
                "TEXT_MAPPING_INCOMPLETE", "SOURCE_FIGURE_MISSING", "SOURCE_PAGE_PRESERVED",
                "SOURCE_REGION_PRESERVED", "CONTENT_OUTSIDE_PAGE", "CONTENT_OUTSIDE_FRAME", "BLOCK_OVERLAP",
            }:
                coverage = "uncertain"
            if item.code in {"LAYOUT_SOURCE_MISMATCH", "UNEXPECTED_PAGE_COUNT", "OUTPUT_GEOMETRY_MISMATCH"}:
                layout_status = "failed"
        if render.error:
            problems.append(problem("output:unavailable", render.error, severity="error"))
    if reason and review:
        problems.append(problem("processing", reason))
    if layout and layout.source_disposition != "transcribed":
        content, coverage = "uncertain", "uncertain"
        problems.append(problem("source_preserved", layout.disposition_reason or "未可靠转录部分已保留源图",
                              disposition=layout.source_disposition))
    return Assessment(assessment_id=str(uuid4()), revision_id=revision.revision_id,
                      run_id=bound_run_id, page_id=revision.page_id, content=content,
                      layout=layout_status, coverage=coverage, rule_version=RULE_VERSION,
                      issues=problems, created_at=now())


def rebind_assessment(value: Assessment, revision: Revision, *, disposition: str | None = None) -> Assessment:
    return value.model_copy(update={
        "assessment_id": str(uuid4()), "revision_id": revision.revision_id, "created_at": now(),
        "content": "uncertain" if disposition else value.content,
        "coverage": "uncertain" if disposition else value.coverage,
        "issues": [item.model_copy(update={"issue_id": str(uuid4()), "revision_id": revision.revision_id,
                                            "disposition": disposition or item.disposition}) for item in value.issues],
    })


def passed(value: Assessment) -> bool:
    return all(getattr(value, name) == "passed" for name in ("content", "layout", "coverage")) and not any(
        item.severity in {"error", "warning"} for item in value.issues
    )


def _problem_key(item: Issue) -> tuple:
    return item.category, item.region_id, item.line_id, tuple(round(x, 3) for x in item.source_bbox) if item.source_bbox else None


def improves(previous: Assessment, candidate: Assessment) -> bool:
    rank = {"passed": 0, "uncertain": 1, "unverified": 2, "failed": 3}
    before = {name: rank[getattr(previous, name)] for name in ("content", "layout", "coverage")}
    after = {name: rank[getattr(candidate, name)] for name in before}
    old = {_problem_key(item): item.severity for item in previous.issues}
    new = {_problem_key(item): item.severity for item in candidate.issues}
    if any(after[name] > before[name] for name in before) or not new.keys() <= old.keys():
        return False
    if any(old[key] != "error" and severity == "error" for key, severity in new.items()):
        return False
    return len(new) < len(old) or any(after[name] < before[name] for name in before)


def render_score(value: CandidateRenderResult) -> tuple[int, int, int]:
    return (int(value.pdf_path is None or bool(value.error)),
            sum(item.severity == "error" for item in value.diagnostics),
            sum(item.severity == "warning" for item in value.diagnostics))


def _intersects(left: BBox, right: BBox) -> bool:
    return max(left[0], right[0]) < min(left[2], right[2]) and max(left[1], right[1]) < min(left[3], right[3])


def _inside(inner: BBox, outer: BBox) -> bool:
    return all((inner[0] >= outer[0] - .015, inner[1] >= outer[1] - .015,
                inner[2] <= outer[2] + .015, inner[3] <= outer[3] + .015))


def apply_repair(revision: Revision, assessment: Assessment, proposal: RepairProposal) -> SourceFidelityLayout:
    if proposal.base_revision_id != revision.revision_id or revision.layout_source is None or not proposal.operations:
        raise ValueError("局部修复的基准修订或操作为空")
    layout = revision.layout_source.model_copy(deep=True)
    actionable = [item for item in assessment.issues if item.disposition == "repairable"]
    if not actionable:
        raise ValueError("没有已定位的可修复问题")
    for operation in proposal.operations:
        regions = {item.region_id: item for item in layout.regions}
        lines = {item.line_id: item for item in layout.lines}
        groups = {item.group_id: item for item in layout.equation_groups}
        target_line = getattr(operation, "line_id", None)
        target_region = getattr(operation, "region_id", None)
        if operation.op == "update_geometry":
            target_line = operation.target_id if operation.target_type == "line" else None
            target_region = operation.target_id if operation.target_type == "region" else None
        if operation.op == "update_equation_group":
            group = groups.get(operation.group_id)
            target_line = group.line_ids[0] if group else operation.new_group.line_ids[0]
        if target_line in lines:
            target_region = lines[target_line].block_id
        matching = [item for item in actionable if (
            item.line_id == target_line and target_line is not None
            or item.region_id == target_region and target_region is not None
            or item.source_bbox is not None and _inside(operation.evidence_bbox, item.source_bbox)
        )]
        if not matching:
            raise ValueError("修复目标未由本轮复核定位")
        if not any(_inside(operation.evidence_bbox, item.source_bbox or (
            lines[target_line].bbox if target_line in lines else regions[target_region].bbox if target_region in regions else None
        ) or (0., 0., 1., 1.)) for item in matching):
            raise ValueError("修复依据与定位源区域不一致")
        if operation.op == "replace_line":
            line = lines.get(operation.line_id)
            if (line is None or line.latex != operation.old_latex or len(operation.new_latex) > 20_000
                    or line.latex.strip() and not operation.new_latex.strip()):
                raise ValueError("替换原行的旧值或片段范围不匹配")
            if line.bbox is not None and not _intersects(line.bbox, operation.evidence_bbox):
                raise ValueError("原行修复没有对应源图依据")
            validate_fragment(operation.new_latex)
            line.latex = operation.new_latex
        elif operation.op == "update_geometry":
            target = lines.get(operation.target_id) if operation.target_type == "line" else regions.get(operation.target_id)
            if target is None or target.bbox != operation.old_bbox or getattr(target, "baseline", None) != operation.old_baseline:
                raise ValueError("几何修复的目标旧值不匹配")
            if operation.new_bbox is None or not _inside(operation.new_bbox, operation.evidence_bbox):
                raise ValueError("几何修复必须保留位于证据区域的有效位置")
            if operation.new_baseline is not None and not operation.new_bbox[1] <= operation.new_baseline <= operation.new_bbox[3]:
                raise ValueError("修复后的基线必须位于有源依据的新原行框内")
            target.bbox, target.basis = operation.new_bbox, "model_estimate"
            if operation.target_type == "line":
                target.baseline = operation.new_baseline
        elif operation.op == "update_equation_group":
            old = groups.get(operation.group_id)
            if old != operation.old_group or (old and old.line_ids != operation.new_group.line_ids):
                raise ValueError("公式组修复的旧值或引用原行不匹配")
            if any(line_id not in lines or not lines[line_id].bbox or not _inside(lines[line_id].bbox, operation.evidence_bbox)
                   for line_id in operation.new_group.line_ids):
                raise ValueError("公式组超出局部来源依据")
            if (operation.new_group.bbox is not None and not _inside(operation.new_group.bbox, operation.evidence_bbox)
                    or operation.new_group.align_x is not None and not operation.evidence_bbox[0] <= operation.new_group.align_x <= operation.evidence_bbox[2]):
                raise ValueError("公式组框或对齐锚点超出局部来源依据")
            if operation.new_group.number:
                number = operation.new_group.number
                if (number.bbox is not None and not _inside(number.bbox, operation.evidence_bbox)
                        or number.anchor_x is not None and not operation.evidence_bbox[0] <= number.anchor_x <= operation.evidence_bbox[2]):
                    raise ValueError("公式编号位置超出局部来源依据")
                validate_fragment(operation.new_group.number.latex)
            layout.equation_groups = [item for item in layout.equation_groups if item.group_id != operation.group_id] + [operation.new_group]
        elif operation.op == "insert_region":
            if [item.region_id for item in layout.regions] != operation.old_region_ids or operation.region.region_id in regions:
                raise ValueError("遗漏块插入的旧区域范围不匹配")
            if operation.region.bbox is None or not _inside(operation.region.bbox, operation.evidence_bbox):
                raise ValueError("新区域必须位于定位的遗漏证据内")
            if operation.after_region_id is not None and operation.after_region_id not in regions:
                raise ValueError("遗漏块的插入锚点不存在")
            for line in operation.lines:
                if line.line_id in lines or line.bbox is None or not _inside(line.bbox, operation.evidence_bbox) or len(line.latex) > 20_000:
                    raise ValueError("新增原行重复或超出来源依据")
                validate_fragment(line.latex)
            for group in operation.equation_groups:
                if group.group_id in groups or not set(group.line_ids) <= {line.line_id for line in operation.lines}:
                    raise ValueError("新增公式组只能属于新增区域")
                if (group.bbox is not None and not _inside(group.bbox, operation.evidence_bbox)
                        or group.align_x is not None and not operation.evidence_bbox[0] <= group.align_x <= operation.evidence_bbox[2]):
                    raise ValueError("新增公式组框或对齐锚点超出局部来源依据")
                if group.number:
                    if (group.number.bbox is not None and not _inside(group.number.bbox, operation.evidence_bbox)
                            or group.number.anchor_x is not None and not operation.evidence_bbox[0] <= group.number.anchor_x <= operation.evidence_bbox[2]):
                        raise ValueError("新增公式编号位置超出局部来源依据")
                    validate_fragment(group.number.latex)
            ordered = sorted(layout.regions, key=lambda item: item.order)
            index = next((i + 1 for i, item in enumerate(ordered) if item.region_id == operation.after_region_id), 0)
            ordered.insert(index, operation.region)
            for index, item in enumerate(ordered):
                item.order = index
            ordered_lines = sorted(layout.lines, key=lambda item: item.order)
            index = max((i + 1 for i, item in enumerate(ordered_lines) if item.block_id == operation.after_region_id), default=0)
            ordered_lines[index:index] = sorted(operation.lines, key=lambda item: item.order)
            for index, item in enumerate(ordered_lines):
                item.order = index
            layout.regions, layout.lines = ordered, ordered_lines
            layout.equation_groups.extend(operation.equation_groups)
        elif operation.op == "delete_duplicate_region":
            deleted = regions.get(operation.region_id)
            retained = regions.get(operation.duplicate_of_region_id)
            old_lines = sorted((item for item in layout.lines if item.block_id == operation.region_id), key=lambda item: item.order)
            other_lines = sorted((item for item in layout.lines if item.block_id == operation.duplicate_of_region_id), key=lambda item: item.order)
            if deleted != operation.old_region or [item.model_dump(exclude={"match_status"}) for item in old_lines] != [item.model_dump(exclude={"match_status"}) for item in operation.old_lines] or retained is None or not old_lines:
                raise ValueError("重复块删除的旧值或保留块不匹配")
            if not deleted.bbox or deleted.bbox != retained.bbox:
                raise ValueError("源图不同位置的相同文字不能按重复内容删除")
            if any(item.parent_id == operation.region_id for item in layout.regions):
                raise ValueError("重复块包含子区域，不能局部删除")
            signature = lambda values: [item.model_dump(exclude={"line_id", "block_id", "order"}) for item in values]
            if signature(old_lines) != signature(other_lines):
                raise ValueError("重复块与保留块并非相同源位置及内容")
            removed_ids = {item.line_id for item in old_lines}
            # Do not delete a distinct equation number or group with its block.
            if any(removed_ids.intersection(item.line_ids) for item in layout.equation_groups):
                raise ValueError("含公式组的重复块必须先由局部原行/公式修复解决")
            layout.lines = [item for item in layout.lines if item.line_id not in removed_ids]
            layout.regions = [item for item in layout.regions if item.region_id != operation.region_id]
    # Revalidate the whole structure once, including references, baselines and reading order.
    return SourceFidelityLayout.model_validate(layout.model_dump())


def preservation_regions(value: Assessment, layout: SourceFidelityLayout) -> list[tuple[BBox, str | None, str]]:
    regions = {item.region_id: item for item in layout.regions}
    lines = {item.line_id: item for item in layout.lines}
    result: list[tuple[BBox, str | None, str]] = []
    for item in value.issues:
        retain = item.category.startswith("content") or item.category in {"coverage", "review_unavailable"}
        retain = retain or item.category in {"output:" + code for code in {
            "MISSING_GLYPH", "MISSING_GLYPHS", "UNMAPPED_GLYPH", "UNMAPPED_GLYPHS",
            "CONTENT_OUTSIDE_PAGE", "CONTENT_OUTSIDE_FRAME", "BLOCK_OVERLAP", "SOURCE_FIGURE_MISSING",
            "LAYOUT_SOURCE_MISMATCH", "UNEXPECTED_PAGE_COUNT", "OUTPUT_GEOMETRY_MISMATCH",
        }}
        if not retain:
            continue
        line = lines.get(item.line_id)
        region = regions.get(item.region_id or (line.block_id if line else None))
        bbox = item.source_bbox or (line.bbox if line else None) or (region.bbox if region else None)
        if bbox is None:
            # Unlocated content uncertainty requires retaining the necessary whole source page.
            return [((0., 0., 1., 1.), None, item.reason)]
        if not any(bbox == existing[0] for existing in result):
            result.append((bbox, item.region_id, item.reason))
    return result
