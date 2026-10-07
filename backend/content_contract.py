"""OCR V2 content authority and complete-JSON, per-block parsing.

Wire blocks contain visible content only. Persistent IDs and provenance are
assigned locally; invalid geometry never invalidates otherwise readable text.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .layout_contract import BBox, CropMapping, FontFamily, transform_point

RecognitionScope = Literal["printed_original_only", "legacy_all_visible"]
SourceLayer = Literal["printed", "annotation", "mixed", "decoration", "noise", "unknown"]
ScopeDisposition = Literal["pending", "mapped_printed", "confirmed_printed_missing", "annotation_excluded", "decoration", "noise", "unknown"]

ContentConclusion = Literal["usable", "uncertain", "unavailable", "unverified"]
LayoutConclusion = Literal["faithful", "approximate", "unavailable", "unverified"]
ContentRole = Literal["body", "header", "footer", "title", "footnote", "caption", "page_number", "other"]
RecoveryReason = Literal[
    "small_text", "reading_order", "truncated_response", "invalid_structure",
    "missing_content", "equation", "table", "unreadable_source", "budget",
    "service_configuration", "temporary_service", "unknown_consumption", "layout", "render",
]


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PageRegion(ContentModel):
    region_id: str = Field(min_length=1, max_length=128)
    source_version: int = Field(ge=1)
    layer: SourceLayer = "unknown"
    kind: Literal["text", "equation", "table", "figure", "unknown"] = "unknown"
    bbox: BBox
    reading_order: int = Field(ge=0)
    boundary_status: Literal["observed", "uncertain"] = "uncertain"
    cleanliness_verified: bool = False


class CoverageObservation(ContentModel):
    observation_id: str = Field(min_length=1, max_length=128)
    source_bbox: BBox
    origin: Literal["local_ink", "model"] = "local_ink"
    scope_disposition: ScopeDisposition = "pending"
    mapped_region_ids: list[str] = Field(default_factory=list, max_length=100)


class UnresolvedSpan(ContentModel):
    region_id: str = Field(min_length=1, max_length=128)
    source_bbox: BBox | None = None
    reason: str = Field(min_length=1, max_length=2_000)


_FORBIDDEN_MATH = re.compile(
    r"\\(?:documentclass|usepackage|input|include|includegraphics|write|openout|read|"
    r"catcode|csname|def|gdef|edef|xdef|newcommand|renewcommand|directlua|special|"
    r"immediate|shellescape|loop|repeat|verbatim)\b|\\(?:begin|end)\s*\{document\}",
    re.IGNORECASE,
)


def validate_math_fragment(value: str) -> str:
    if _FORBIDDEN_MATH.search(value):
        raise ValueError("数学内容只能是有界数学片段，不能包含文档、资源或执行命令")
    if value.strip().startswith(("$", r"\[", r"\(")):
        raise ValueError("数学片段不包含外层数学定界符")
    return value


class TextSpan(ContentModel):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=100_000)
    bold: bool = False
    italic: bool = False
    font_family: FontFamily | None = None


class MathSpan(ContentModel):
    kind: Literal["math"]
    text: str = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def validate_fragment(self) -> "MathSpan":
        validate_math_fragment(self.text)
        return self


InlineSpan = Annotated[TextSpan | MathSpan, Field(discriminator="kind")]


class VisualLine(ContentModel):
    line_id: str = ""
    spans: list[InlineSpan] = Field(min_length=1, max_length=1_000)
    bbox: BBox | None = None
    paragraph_start: bool = False


class EquationContentLine(ContentModel):
    line_id: str = ""
    latex: str = Field(min_length=1, max_length=100_000)
    number: str | None = Field(default=None, max_length=1_000)
    alignment: Literal["left", "center", "right", "aligned", "unknown"] = "unknown"
    bbox: BBox | None = None

    @model_validator(mode="after")
    def validate_fragment(self) -> "EquationContentLine":
        validate_math_fragment(self.latex)
        if self.number is not None:
            validate_math_fragment(self.number)
        return self


class ContentIssue(ContentModel):
    issue_id: str = Field(default_factory=lambda: str(uuid4()))
    category: RecoveryReason
    reason: str = Field(min_length=1, max_length=2_000)
    block_id: str | None = None
    source_bbox: BBox | None = None
    response_id: str | None = None
    response_index: int | None = Field(default=None, ge=0)
    field_path: str | None = Field(default=None, max_length=300)
    severity: Literal["error", "warning", "info"] = "warning"
    resolved: bool = False
    repairable: bool = False
    origin: Literal["model_review", "parse", "local_constraint", "legacy"] = "legacy"
    stable_target_id: str | None = None
    region_id: str | None = None


class BlockBase(ContentModel):
    block_id: str = ""
    role: ContentRole = "body"
    bbox: BBox | None = None
    source_layer: SourceLayer = "unknown"
    source_region_id: str | None = None
    crop_id: str | None = None
    content_revision_id: str = ""
    response_id: str | None = None
    response_index: int | None = Field(default=None, ge=0)
    recognition_status: ContentConclusion = "unverified"
    review_status: ContentConclusion = "unverified"
    unresolved_reasons: list[str] = Field(default_factory=list, max_length=100)
    uncertainty: list[str] = Field(default_factory=list, max_length=100)


class TextBlock(BlockBase):
    type: Literal["text"]
    lines: list[VisualLine] = Field(min_length=1, max_length=2_000)


class EquationBlock(BlockBase):
    type: Literal["equation"]
    lines: list[EquationContentLine] = Field(min_length=1, max_length=500)


class TableCell(ContentModel):
    cell_id: str = ""
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1, le=1_000)
    column_span: int = Field(default=1, ge=1, le=1_000)
    lines: list[VisualLine] = Field(default_factory=list, max_length=500)
    bbox: BBox | None = None
    preserved: bool = False
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_preserved(self) -> "TableCell":
        if self.preserved and (self.bbox is None or not self.reason):
            raise ValueError("保留单元格必须提供源区域与原因")
        if not self.lines and not self.preserved:
            # An explicitly observed empty cell is valid; page blankness is separate.
            return self
        return self


class TableBlock(BlockBase):
    type: Literal["table"]
    rows: int = Field(ge=1, le=1_000)
    columns: int = Field(ge=1, le=1_000)
    cells: list[TableCell] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_grid(self) -> "TableBlock":
        occupied: set[tuple[int, int]] = set()
        for cell in self.cells:
            if cell.row + cell.row_span > self.rows or cell.column + cell.column_span > self.columns:
                raise ValueError("单元格超出表格范围")
            for row in range(cell.row, cell.row + cell.row_span):
                for column in range(cell.column, cell.column + cell.column_span):
                    if (row, column) in occupied:
                        raise ValueError("单元格合并范围重叠")
                    occupied.add((row, column))
        return self


class FigureBlock(BlockBase):
    type: Literal["figure"]
    description: str | None = Field(default=None, max_length=2_000)
    caption_block_ids: list[str] = Field(default_factory=list, max_length=100)


ContentBlock = Annotated[TextBlock | EquationBlock | TableBlock | FigureBlock, Field(discriminator="type")]
_BLOCK_ADAPTER = TypeAdapter(ContentBlock)
_BBOX_ADAPTER = TypeAdapter(BBox)


class WireVisualLine(ContentModel):
    spans: list[InlineSpan] = Field(min_length=1, max_length=1_000)
    bbox: BBox | None = None
    paragraph_start: bool = False


class WireEquationLine(ContentModel):
    latex: str = Field(min_length=1, max_length=100_000)
    number: str | None = Field(default=None, max_length=1_000)
    alignment: Literal["left", "center", "right", "aligned", "unknown"] = "unknown"
    bbox: BBox | None = None


class WireBlockBase(ContentModel):
    region_id: str | None = None
    role: ContentRole = "body"
    bbox: BBox | None = None
    uncertainty: list[str] = Field(default_factory=list, max_length=100)


class WireTextBlock(WireBlockBase):
    type: Literal["text"]
    lines: list[WireVisualLine] = Field(min_length=1, max_length=2_000)


class WireEquationBlock(WireBlockBase):
    type: Literal["equation"]
    lines: list[WireEquationLine] = Field(min_length=1, max_length=500)


class WireTableCell(ContentModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1, le=1_000)
    column_span: int = Field(default=1, ge=1, le=1_000)
    lines: list[WireVisualLine] = Field(default_factory=list, max_length=500)
    bbox: BBox | None = None
    preserved: bool = False
    reason: str | None = Field(default=None, max_length=2_000)


class WireTableBlock(WireBlockBase):
    type: Literal["table"]
    rows: int = Field(ge=1, le=1_000)
    columns: int = Field(ge=1, le=1_000)
    cells: list[WireTableCell] = Field(min_length=1, max_length=10_000)


class WireFigureBlock(WireBlockBase):
    type: Literal["figure"]
    description: str | None = Field(default=None, max_length=2_000)


WireContentBlock = Annotated[WireTextBlock | WireEquationBlock | WireTableBlock | WireFigureBlock, Field(discriminator="type")]


class RecognitionEnvelope(ContentModel):
    schema_version: Literal[2] = 2
    page_kind: Literal["content", "front_cover", "back_cover", "contents", "blank", "other"] = "content"
    blank: bool = False
    blocks: list[WireContentBlock] = Field(max_length=2_000)
    unresolved_spans: list[UnresolvedSpan] = Field(default_factory=list, max_length=2_000)


def recognition_json_schema() -> dict:
    """The single wire-schema source for both provider adapters."""
    return RecognitionEnvelope.model_json_schema()


class PageContent(ContentModel):
    schema_version: Literal[2] = 2
    recognition_scope: RecognitionScope = "legacy_all_visible"
    coverage_reviewed: bool = False
    regions: list[PageRegion] = Field(default_factory=list, max_length=2_000)
    coverage_observations: list[CoverageObservation] = Field(default_factory=list, max_length=2_000)
    unresolved_spans: list[UnresolvedSpan] = Field(default_factory=list, max_length=2_000)
    content_revision_id: str
    book_id: str
    page_id: str
    source_version: int = Field(ge=1)
    page_kind: Literal["content", "front_cover", "back_cover", "contents", "blank", "other"] = "content"
    blank: bool = False
    blocks: list[ContentBlock] = Field(default_factory=list, max_length=2_000)
    issues: list[ContentIssue] = Field(default_factory=list, max_length=2_000)
    response_ids: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_identity(self) -> "PageContent":
        region_map = {region.region_id: region for region in self.regions}
        if len(region_map) != len(self.regions) or any(region.source_version != self.source_version for region in self.regions):
            raise ValueError("区域计划必须具有唯一身份且关联当前来源版本")
        observation_ids = [item.observation_id for item in self.coverage_observations]
        if len(observation_ids) != len(set(observation_ids)) or any(not set(item.mapped_region_ids) <= set(region_map) for item in self.coverage_observations):
            raise ValueError("覆盖观察必须有唯一身份且仅映射当前计划区域")
        if self.recognition_scope == "printed_original_only":
            if self.blank and (self.unresolved_spans or any(region.layer in {"printed", "mixed", "unknown"} for region in self.regions)):
                raise ValueError("存在印刷或来源未决区域时不能声明范围内空白")
            if any(block.source_region_id not in region_map or region_map[block.source_region_id].layer not in {"printed", "mixed"} or block.source_layer != region_map[block.source_region_id].layer for block in self.blocks):
                raise ValueError("印刷正文必须绑定计划中的印刷或混合区域")
            if any(span.region_id not in region_map for span in self.unresolved_spans):
                raise ValueError("未决片段必须绑定计划区域")
            if any(region_map[span.region_id].layer not in {"printed", "mixed", "unknown"} for span in self.unresolved_spans):
                raise ValueError("范围外批注、装饰和噪声不能作为未决印刷片段")
        ids = [block.block_id for block in self.blocks]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("内容块必须有唯一的程序 ID")
        if any(block.content_revision_id != self.content_revision_id for block in self.blocks):
            raise ValueError("内容块必须关联同一内容修订")
        if self.blank and self.blocks:
            raise ValueError("空白页声明不能与可见内容同时存在")
        if not self.blocks and not self.blank and not self.issues:
            raise ValueError("无内容不能冒充成功空白页")
        line_ids = [line.line_id for block in self.blocks for line in getattr(block, "lines", [])]
        line_ids.extend(line.line_id for block in self.blocks for cell in getattr(block, "cells", []) for line in cell.lines)
        if any(not value for value in line_ids) or len(line_ids) != len(set(line_ids)):
            raise ValueError("内容原行必须有唯一的程序 ID")
        if len(line_ids) > 10_000:
            raise ValueError("页面原行数量超限")
        if len(content_plain_text(self)) > 1_000_000:
            raise ValueError("页面可编辑内容过长")
        return self


class BlockParseFailure(ContentModel):
    response_index: int | None = None
    field_path: str
    reason: str
    source_bbox: BBox | None = None
    category: RecoveryReason = "invalid_structure"


class ContentParseResult(ContentModel):
    content: PageContent | None = None
    failures: list[BlockParseFailure] = Field(default_factory=list)
    json_complete: bool = False


_LOCAL_BLOCK_FIELDS = {
    "block_id", "content_revision_id", "response_id", "response_index", "recognition_status",
    "review_status", "unresolved_reasons", "source_layer", "source_region_id", "crop_id", "caption_block_ids",
}


def _map_bbox(bbox: BBox, mapping: CropMapping | None) -> BBox:
    if mapping is None:
        return bbox
    points = [transform_point(mapping.crop_to_canonical_affine, x, y)
              for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3]))]
    return _BBOX_ADAPTER.validate_python((min(p[0] for p in points), min(p[1] for p in points),
                                          max(p[0] for p in points), max(p[1] for p in points)))


def parse_content_response(
    body: str, *, book_id: str, page_id: str, source_version: int, response_id: str,
    content_revision_id: str | None = None, crop_mapping: CropMapping | None = None,
    recognition_scope: RecognitionScope = "legacy_all_visible", target_regions: list[PageRegion] | tuple[PageRegion, ...] = (),
) -> ContentParseResult:
    """Only independently valid entries of a complete JSON object are salvaged."""
    if crop_mapping is not None and (crop_mapping.page_id, crop_mapping.source_version) != (page_id, source_version):
        raise ValueError("裁切映射不属于当前来源页")
    region_map = {region.region_id: region for region in target_regions}
    if len(region_map) != len(target_regions) or any(region.source_version != source_version for region in target_regions):
        raise ValueError("区域计划身份或来源版本无效")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, RecursionError) as error:
        return ContentParseResult(failures=[BlockParseFailure(
            field_path="$", reason=f"响应不是完整 JSON：{str(error)[:250]}", category="invalid_structure",
        )])
    if not isinstance(value, dict) or value.get("schema_version") != 2 or not isinstance(value.get("blocks"), list):
        return ContentParseResult(json_complete=True, failures=[BlockParseFailure(
            field_path="$", reason="响应必须含 schema_version=2 和 blocks 数组",
        )])
    if set(value) - {"schema_version", "page_kind", "blank", "blocks", "unresolved_spans"} or (
        "blank" in value and not isinstance(value["blank"], bool)
    ) or value.get("page_kind", "content") not in {"content", "front_cover", "back_cover", "contents", "blank", "other"}:
        return ContentParseResult(json_complete=True, failures=[BlockParseFailure(
            field_path="$", reason="页面元信息类型、页面种类或额外字段无效",
        )])
    if len(value["blocks"]) > 2_000:
        return ContentParseResult(json_complete=True, failures=[BlockParseFailure(field_path="blocks", reason="内容块数量超限")])
    revision_id = content_revision_id or str(uuid4())
    blocks, failures, issues = [], [], []
    content_length = 0
    line_count = 0
    for index, raw in enumerate(value["blocks"]):
        path = f"blocks[{index}]"
        if not isinstance(raw, dict):
            failures.append(BlockParseFailure(response_index=index, field_path=path, reason="内容块必须是对象"))
            continue
        region_id = raw.get("region_id")
        if region_id is not None and not isinstance(region_id, str):
            failures.append(BlockParseFailure(response_index=index, field_path=f"{path}.region_id", reason="目标区域 ID 必须是字符串"))
            continue
        if recognition_scope == "printed_original_only" and (region_id not in region_map or region_map[region_id].layer not in {"printed", "mixed"}):
            failures.append(BlockParseFailure(response_index=index, field_path=f"{path}.region_id", reason="转录块必须绑定本次印刷目标区域"))
            continue
        item = {key: val for key, val in raw.items() if key not in _LOCAL_BLOCK_FIELDS and key != "region_id"}
        local_issues: list[ContentIssue] = []

        def normalize_box(target: dict, field_path: str) -> None:
            if target.get("bbox") is None:
                return
            try:
                target["bbox"] = _map_bbox(_BBOX_ADAPTER.validate_python(target["bbox"]), crop_mapping)
            except (ValidationError, ValueError):
                target["bbox"] = None
                local_issues.append(ContentIssue(category="layout", reason="可选源框无效，已保存内容并标记定位未知",
                                                 response_id=response_id, response_index=index, field_path=field_path))

        normalize_box(item, f"{path}.bbox")
        if isinstance(item.get("lines"), list):
            item["lines"] = [dict(line) if isinstance(line, dict) else line for line in item["lines"]]
            for line_index, line in enumerate(item["lines"]):
                if isinstance(line, dict):
                    line.pop("line_id", None)
                    normalize_box(line, f"{path}.lines[{line_index}].bbox")
        if isinstance(item.get("cells"), list):
            item["cells"] = [dict(cell) if isinstance(cell, dict) else cell for cell in item["cells"]]
            for cell_index, cell in enumerate(item["cells"]):
                if not isinstance(cell, dict):
                    continue
                cell.pop("cell_id", None)
                normalize_box(cell, f"{path}.cells[{cell_index}].bbox")
                if isinstance(cell.get("lines"), list):
                    cell["lines"] = [dict(line) if isinstance(line, dict) else line for line in cell["lines"]]
                    for line_index, line in enumerate(cell["lines"]):
                        if isinstance(line, dict):
                            line.pop("line_id", None)
                            normalize_box(line, f"{path}.cells[{cell_index}].lines[{line_index}].bbox")
        try:
            block = _BLOCK_ADAPTER.validate_python(item)
        except ValidationError as error:
            detail = error.errors(include_input=False)[0]
            field_path = path + "." + ".".join(str(part) for part in detail["loc"])
            failures.append(BlockParseFailure(response_index=index, field_path=field_path[:300],
                                               reason=detail["msg"][:500], source_bbox=item.get("bbox")))
            continue
        if recognition_scope == "printed_original_only" and isinstance(block, TableBlock) and any(cell.preserved for cell in block.cells):
            failures.append(BlockParseFailure(response_index=index, field_path=f"{path}.cells", reason="印刷成品不能用未经清洁核验的源表格单元格兜底", source_bbox=block.bbox))
            continue
        block.block_id = str(uuid4())
        block_length = len(_block_plain_text(block))
        block_line_count = len(getattr(block, "lines", [])) + sum(len(cell.lines) for cell in getattr(block, "cells", []))
        if content_length + block_length + 2 * len(blocks) > 1_000_000 or line_count + block_line_count > 10_000:
            failures.append(BlockParseFailure(response_index=index, field_path=path, reason="此块超过页面内容保存上限；其他有效块仍保留", source_bbox=block.bbox))
            continue
        content_length += block_length
        line_count += block_line_count
        block.content_revision_id = revision_id
        block.response_id, block.response_index = response_id, index
        block.crop_id = crop_mapping.crop_id if crop_mapping else None
        block.source_region_id = crop_mapping.source_region_ids[0] if crop_mapping and len(crop_mapping.source_region_ids) == 1 else None
        if region_id in region_map:
            block.source_region_id = region_id
            block.source_layer = region_map[region_id].layer
        block.recognition_status = "uncertain" if block.uncertainty else "unverified"
        block.unresolved_reasons = list(block.uncertainty)
        for line in getattr(block, "lines", []):
            line.line_id = str(uuid4())
        for cell in getattr(block, "cells", []):
            cell.cell_id = str(uuid4())
            for line in cell.lines:
                line.line_id = str(uuid4())
        for issue in local_issues:
            issue.block_id = block.block_id
        issues.extend(local_issues)
        blocks.append(block)
    for failure in failures:
        issues.append(ContentIssue(category=failure.category, reason=failure.reason, response_id=response_id,
                                   response_index=failure.response_index, field_path=failure.field_path,
                                   source_bbox=failure.source_bbox, severity="error", origin="parse"))
    unresolved_spans = []
    raw_spans = value.get("unresolved_spans", [])
    if not isinstance(raw_spans, list) or len(raw_spans) > 2_000:
        issues.append(ContentIssue(category="invalid_structure", reason="未决片段必须为有界数组", origin="parse", field_path="unresolved_spans"))
        raw_spans = []
    for index, raw_span in enumerate(raw_spans):
        try:
            span = UnresolvedSpan.model_validate(raw_span)
            if span.region_id not in region_map:
                raise ValueError("未决片段不属于本次目标区域")
            if recognition_scope == "printed_original_only" and region_map[span.region_id].layer not in {"printed", "mixed", "unknown"}:
                raise ValueError("范围外区域不能作为未决印刷片段")
            if span.source_bbox is not None:
                span.source_bbox = _map_bbox(span.source_bbox, crop_mapping)
            unresolved_spans.append(span)
        except (ValidationError, ValueError) as error:
            span_region_id = raw_span.get("region_id") if isinstance(raw_span, dict) else None
            if (recognition_scope != "printed_original_only" or not isinstance(span_region_id, str)
                or span_region_id not in region_map or region_map[span_region_id].layer not in {"printed", "mixed", "unknown"}):
                span_region_id = None
            issues.append(ContentIssue(category="invalid_structure", reason=str(error)[:500], origin="parse",
                                       field_path=f"unresolved_spans[{index}]", region_id=span_region_id))
    blank = value.get("blank") is True
    kind = value.get("page_kind", "content")
    if kind not in {"content", "front_cover", "back_cover", "contents", "blank", "other"}:
        kind = "other"
    if blank and (blocks or failures or unresolved_spans or any(region.layer in {"printed", "mixed", "unknown"} for region in target_regions)):
        blank = False
        issues.append(ContentIssue(category="invalid_structure", reason="空白页声明与返回内容冲突", response_id=response_id))
    if not blocks and not blank and not issues:
        issues.append(ContentIssue(category="missing_content", reason="响应未提供可见内容，也未明确观察为空白页", response_id=response_id))
    if recognition_scope == "printed_original_only":
        authorized_ids = [region.region_id for region in target_regions if region.layer in {"printed", "mixed"}]
        bound_issues = []
        for issue in issues:
            region_id = issue.region_id
            if region_id is None and issue.response_index is not None:
                raw = value["blocks"][issue.response_index]
                raw_region_id = raw.get("region_id") if isinstance(raw, dict) else None
                if isinstance(raw_region_id, str) and raw_region_id in authorized_ids:
                    region_id = raw_region_id
            targets = [region_id] if region_id is not None else authorized_ids
            if not targets:
                bound_issues.append(issue)
                continue
            for target_id in targets:
                bound_issues.append(issue.model_copy(update={"issue_id": str(uuid4()),
                    "region_id": target_id, "stable_target_id": target_id}))
        issues = bound_issues
    return ContentParseResult(json_complete=True, failures=failures, content=PageContent(
        content_revision_id=revision_id, book_id=book_id, page_id=page_id, source_version=source_version,
        recognition_scope=recognition_scope, regions=list(target_regions), unresolved_spans=unresolved_spans,
        page_kind=kind, blank=blank, blocks=blocks, issues=issues, response_ids=[response_id],
    ))


def _block_plain_text(block: ContentBlock) -> str:
    if isinstance(block, TextBlock):
        return "\n".join("".join(span.text for span in line.spans) for line in block.lines)
    if isinstance(block, EquationBlock):
        return "\n".join(line.latex + (" " + line.number if line.number else "") for line in block.lines)
    if isinstance(block, TableBlock):
        return "\n".join(" ".join("".join(span.text for span in line.spans) for line in cell.lines) for cell in block.cells)
    return ""


def content_plain_text(content: PageContent) -> str:
    """Compatibility projection; PageContent remains the content authority."""
    return "\n\n".join(_block_plain_text(block) for block in content.blocks)
