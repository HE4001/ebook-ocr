"""Version 1 source layout: normalized observations and program-owned provenance.

All observed boxes and baselines use the canonical image's top-left origin.
Affine tuples use x'=a*x+c*y+e, y'=b*x+d*y+f. Source PDF coordinates
are unrotated crop-local bp; image coordinates are original-file pixels.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


RenderStrategy = Literal["source_fidelity", "legacy_template", "custom_latex"]
FontFamily = Literal["songti", "heiti", "kaiti"]
EvidenceBasis = Literal["file_metadata", "local_measurement", "model_estimate", "manual", "project"]
NormalizedCoordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
LayoutId = Annotated[str, Field(min_length=1, max_length=128)]


def _ordered_bbox(value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if value[0] >= value[2] or value[1] >= value[3]:
        raise ValueError("区域必须满足 x0 < x1、y0 < y1")
    return value


BBox = Annotated[
    tuple[NormalizedCoordinate, NormalizedCoordinate, NormalizedCoordinate, NormalizedCoordinate],
    AfterValidator(_ordered_bbox),
]
BoxBp = Annotated[tuple[float, float, float, float], AfterValidator(_ordered_bbox)]
AffineTransform = tuple[float, float, float, float, float, float]


class LayoutModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


_MATH_ENVIRONMENTS = {
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "gather", "gather*", "multline", "multline*", "displaymath", "math",
    "eqnarray", "eqnarray*", "aligned", "alignedat", "gathered", "split",
}
_ENVIRONMENT_TOKEN = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")


def equation_alignment_index(latex: str, *, allow_multiple: bool = False) -> int | None:
    """Return the single top-level alignment marker in an equation math body."""
    stripped = latex.strip()
    outer = _ENVIRONMENT_TOKEN.match(stripped)
    if stripped.startswith(("$", r"\[", r"\(")) or (
        outer is not None and outer.group(1) == "begin" and outer.group(2) in _MATH_ENVIRONMENTS
    ):
        raise ValueError("公式原行必须是无外层数学定界或环境的数学体")
    marker = None
    braces = 0
    environments = 0
    index = 0
    while index < len(latex):
        character = latex[index]
        if character == "%":
            newline = latex.find("\n", index)
            index = len(latex) if newline < 0 else newline + 1
            continue
        if character == "\\":
            token = _ENVIRONMENT_TOKEN.match(latex, index)
            if token is not None:
                environments += 1 if token.group(1) == "begin" else -1
                index = token.end()
            else:
                index += 2  # Escaped delimiters, ampersands and braces are literals.
            continue
        if character == "{":
            braces += 1
        elif character == "}":
            braces -= 1
        elif character == "&" and braces == 0 and environments == 0:
            if marker is not None and not allow_multiple:
                raise ValueError("一个公式原行最多包含一个顶层对齐锚点 &")
            if marker is None:
                marker = index
        index += 1
    return marker


class LineStyle(LayoutModel):
    font_family: FontFamily | None = None
    font_size_bp: float | None = Field(default=None, gt=0, le=200)
    font_size_ratio: float | None = Field(default=None, gt=0, le=10)
    bold: bool | None = None
    italic: bool | None = None
    basis: EvidenceBasis | None = None


class LayoutRegion(LayoutModel):
    region_id: LayoutId
    kind: Literal["body", "header", "footer", "column", "paragraph", "equation", "table", "figure", "footnote", "other"]
    order: int = Field(ge=0)
    bbox: BBox | None = None
    parent_id: LayoutId | None = None
    basis: EvidenceBasis | None = None


class LayoutLine(LayoutModel):
    line_id: LayoutId
    block_id: LayoutId
    order: int = Field(ge=0)
    kind: Literal["text", "equation", "header", "footer", "page_number", "table", "footnote", "caption"] = "text"
    latex: str = Field(max_length=1_000_000)
    bbox: BBox | None = None
    baseline: NormalizedCoordinate | None = None
    style: LineStyle = Field(default_factory=LineStyle)
    basis: EvidenceBasis | None = None
    match_status: Literal["matched", "unknown"] = "unknown"


class EquationNumber(LayoutModel):
    latex: str = Field(min_length=1, max_length=10_000)
    line_id: LayoutId
    bbox: BBox | None = None
    anchor_x: NormalizedCoordinate | None = None


class EquationGroup(LayoutModel):
    group_id: LayoutId
    line_ids: list[LayoutId] = Field(min_length=1, max_length=2_000)
    bbox: BBox | None = None
    align_x: NormalizedCoordinate | None = None
    number: EquationNumber | None = None
    basis: EvidenceBasis | None = None


class LayoutObservation(LayoutModel):
    """Only content and visible relationships may be returned by a model."""

    schema_version: Literal[1] = 1
    body_frame: BBox | None = None
    regions: list[LayoutRegion] = Field(default_factory=list, max_length=500)
    lines: list[LayoutLine] = Field(default_factory=list, max_length=2_000)
    equation_groups: list[EquationGroup] = Field(default_factory=list, max_length=500)
    review_reasons: list[str] = Field(default_factory=list, max_length=103)

    @model_validator(mode="after")
    def validate_structure(self) -> "LayoutObservation":
        regions = {region.region_id: region for region in self.regions}
        lines = {line.line_id: line for line in self.lines}
        if len(regions) != len(self.regions) or len(lines) != len(self.lines):
            raise ValueError("区域 ID 和原行 ID 不能重复")
        if len({line.order for line in self.lines}) != len(self.lines):
            raise ValueError("原行阅读顺序不能重复")
        for region in self.regions:
            visited = {region.region_id}
            parent = region.parent_id
            while parent is not None:
                if parent not in regions:
                    raise ValueError("区域父 ID 不存在")
                if parent in visited:
                    raise ValueError("区域关系不能循环引用")
                visited.add(parent)
                parent = regions[parent].parent_id
        alignment_lines: set[str] = set()
        for line in self.lines:
            if line.block_id not in regions:
                raise ValueError("原行的块 ID 不存在")
            if line.bbox is not None and line.baseline is not None:
                if not line.bbox[1] <= line.baseline <= line.bbox[3]:
                    raise ValueError("基线必须位于原行区域内")
            if line.kind == "equation" and equation_alignment_index(line.latex) is not None:
                alignment_lines.add(line.line_id)
        group_ids: set[str] = set()
        grouped_lines: set[str] = set()
        for group in self.equation_groups:
            if group.group_id in group_ids:
                raise ValueError("公式组 ID 不能重复")
            group_ids.add(group.group_id)
            if len(set(group.line_ids)) != len(group.line_ids):
                raise ValueError("公式组原行 ID 不能重复")
            if any(line_id not in lines or lines[line_id].kind != "equation" for line_id in group.line_ids):
                raise ValueError("公式组必须引用已存在的公式行")
            if grouped_lines.intersection(group.line_ids):
                raise ValueError("公式行只能属于一个公式组")
            grouped_lines.update(group.line_ids)
            if group.line_ids != sorted(group.line_ids, key=lambda line_id: lines[line_id].order):
                raise ValueError("公式组顺序必须与原行阅读顺序一致")
            if group.number is not None and group.number.line_id not in group.line_ids:
                raise ValueError("公式编号必须归属组内原行")
        if alignment_lines - grouped_lines:
            raise ValueError("含对齐锚点的公式原行必须归属公式组")
        body_length = sum(len(line.latex) for line in self.lines) + sum(
            len(group.number.latex) for group in self.equation_groups if group.number is not None
        )
        if body_length > 1_000_000:
            raise ValueError("布局内容过长")
        # Three program review reasons may be appended after wire validation.
        if sum(len(reason) for reason in self.review_reasons) > 1_003_000:
            raise ValueError("布局复核原因过长")
        return self


class PdfSourceGeometry(LayoutModel):
    """Boxes retain source offsets; width/height describe the unrotated crop."""
    media_box_bp: BoxBp
    crop_box_bp: BoxBp
    rotation: Literal[0, 90, 180, 270]
    width_bp: float = Field(gt=0)
    height_bp: float = Field(gt=0)


class PageSourceMetadata(LayoutModel):
    book_id: str
    page_number: int = Field(ge=1)
    page_id: str = ""
    source_id: str
    source_file_id: str = ""
    source_page: int = Field(ge=1)
    source_version: int = Field(default=1, ge=1)
    source_kind: Literal["pdf", "image"]
    source_file_fingerprint: str = ""  # Existing cache compatibility only.
    image_fingerprint: str = ""
    canonical_width_px: int = Field(gt=0)
    canonical_height_px: int = Field(gt=0)
    source_width_px: int | None = Field(default=None, gt=0)
    source_height_px: int | None = Field(default=None, gt=0)
    source_coordinate_space: Literal["original_image_px", "unrotated_crop_bp"]
    canonical_to_source_affine: AffineTransform
    pdf_geometry: PdfSourceGeometry | None = None

    @model_validator(mode="after")
    def validate_transform(self) -> "PageSourceMetadata":
        a, b, c, d, _, _ = self.canonical_to_source_affine
        if a * d - b * c == 0:
            raise ValueError("规范图到源页的变换必须可逆")
        if (self.source_kind == "pdf") != (self.pdf_geometry is not None):
            raise ValueError("PDF 来源必须保存页面几何，图片不能伪造 PDF 几何")
        return self


class SourceRegionAsset(LayoutModel):
    """A local source crop retained in the document and every resource export."""

    asset_id: LayoutId
    region_id: LayoutId | None = None
    bbox: BBox
    image_name: str = Field(min_length=1, max_length=1_000)
    purpose: Literal["figure", "uncertain_content", "source_page"]
    reason: str = Field(default="", max_length=2_000)
    source_layer: Literal["printed", "annotation", "mixed", "unknown"] = "unknown"
    cleanliness_verified: bool = False


def output_source_assets(layout):
    """Only explicitly verified clean figures are eligible in printed scope."""
    if layout.recognition_scope == "legacy_all_visible":
        return layout.source_assets
    return [asset for asset in layout.source_assets if asset.purpose == "figure"
            and asset.source_layer == "printed" and asset.cleanliness_verified]


class SourceFidelityLayout(LayoutObservation):
    recognition_scope: Literal["legacy_all_visible", "printed_original_only"] = "legacy_all_visible"
    unresolved_notices: list[str] = Field(default_factory=list, max_length=2_000)
    # The old model wire remains LayoutObservation (500 / 2,000). V2 local
    # derived rendering may reference the larger saved content collection.
    regions: list[LayoutRegion] = Field(default_factory=list, max_length=2_000)
    lines: list[LayoutLine] = Field(default_factory=list, max_length=10_000)
    review_reasons: list[str] = Field(default_factory=list, max_length=500)
    source: PageSourceMetadata
    content_revision: int = Field(ge=0)
    layout_revision: int = Field(ge=0)
    generated_content_revision: int | None = Field(default=None, ge=0)
    generator_version: str | None = None
    canvas_width_bp: float | None = Field(default=None, gt=0)
    canvas_height_bp: float | None = Field(default=None, gt=0)
    canvas_basis: Literal["file_metadata", "manual", "project"] | None = None
    body_font_size_bp: float | None = Field(default=None, gt=0, le=200)
    body_font_family: FontFamily | None = None
    body_font_basis: EvidenceBasis | None = None
    source_assets: list[SourceRegionAsset] = Field(default_factory=list, max_length=2_000)
    source_disposition: Literal["transcribed", "regions_preserved", "source_page_preserved"] = "transcribed"
    disposition_reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_canvas(self) -> "SourceFidelityLayout":
        if (self.canvas_width_bp is None) != (self.canvas_height_bp is None):
            raise ValueError("规范画布宽高必须同时提供")
        if self.canvas_width_bp is not None and self.canvas_basis is None:
            raise ValueError("规范画布必须记录尺寸依据")
        if (self.body_font_size_bp is not None or self.body_font_family is not None) and self.body_font_basis is None:
            raise ValueError("基准字体必须记录依据")
        return self


# V2 observations precede content recognition. These are evidence, not an
# assertion that the page's visible content has already been covered.
class CoarseRegion(LayoutModel):
    region_id: LayoutId
    kind: Literal["text", "body", "column", "equation", "table", "figure", "header", "footer", "title", "spanning_title", "footnote", "margin", "unknown"]
    bbox: BBox
    order: int = Field(ge=0)
    column_id: str | None = None
    basis: EvidenceBasis = "local_measurement"
    reasons: list[str] = Field(default_factory=list, max_length=100)


class CropMapping(LayoutModel):
    """Affine maps normalized crop coordinates to normalized canonical page."""
    crop_id: LayoutId
    page_id: str
    source_version: int = Field(ge=1)
    bbox: BBox
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    crop_to_canonical_affine: AffineTransform
    source_region_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_mapping(self) -> "CropMapping":
        a, b, c, d, _, _ = self.crop_to_canonical_affine
        if a * d - b * c == 0:
            raise ValueError("裁切坐标映射必须可逆")
        return self


class RecognitionInput(LayoutModel):
    image_path: str
    kind: Literal["overview", "crop"] = "overview"
    mapping: CropMapping | None = None

    @model_validator(mode="after")
    def validate_input(self) -> "RecognitionInput":
        if (self.kind == "crop") != (self.mapping is not None):
            raise ValueError("高清局部图必须带映射；概览图不使用局部映射")
        return self


class PageLinePlacement(LayoutModel):
    match_status: Literal["matched", "unknown"] = "unknown"
    """Positions reference saved content; they carry no independent text."""
    line_id: LayoutId
    block_id: LayoutId
    order: int = Field(ge=0)
    bbox: BBox | None = None
    baseline: NormalizedCoordinate | None = None
    style: LineStyle = Field(default_factory=LineStyle)
    basis: EvidenceBasis | None = None


class PageLayout(LayoutModel):
    recognition_scope: Literal["legacy_all_visible", "printed_original_only"] = "legacy_all_visible"
    schema_version: Literal[2] = 2
    layout_revision_id: str
    content_revision_id: str
    page_id: str
    source_version: int = Field(ge=1)
    source: PageSourceMetadata
    canvas_width_bp: float | None = Field(default=None, gt=0)
    canvas_height_bp: float | None = Field(default=None, gt=0)
    canvas_basis: Literal["file_metadata", "manual", "project"] | None = None
    body_frame: BBox | None = None
    regions: list[LayoutRegion] = Field(default_factory=list, max_length=2_000)
    lines: list[PageLinePlacement] = Field(default_factory=list, max_length=10_000)
    equation_groups: list[EquationGroup] = Field(default_factory=list, max_length=500)
    body_font_size_bp: float | None = Field(default=None, gt=0, le=200)
    body_font_family: FontFamily | None = None
    body_font_basis: EvidenceBasis | None = None
    source_assets: list[SourceRegionAsset] = Field(default_factory=list, max_length=2_000)
    conclusion: Literal["faithful", "approximate", "unavailable", "unverified"] = "unverified"
    review_reasons: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_source(self) -> "PageLayout":
        if (self.page_id, self.source_version) != (self.source.page_id, self.source.source_version):
            raise ValueError("布局与来源版本不一致")
        if (self.canvas_width_bp is None) != (self.canvas_height_bp is None):
            raise ValueError("画布宽高必须同时提供")
        if self.canvas_width_bp is not None and self.canvas_basis is None:
            raise ValueError("画布尺寸必须记录依据")
        if (self.body_font_size_bp is not None or self.body_font_family is not None) and self.body_font_basis is None:
            raise ValueError("字体必须记录依据")
        if len({line.line_id for line in self.lines}) != len(self.lines):
            raise ValueError("布局原行 ID 不能重复")
        return self


def transform_point(transform: AffineTransform, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = transform
    return a * x + c * y + e, b * x + d * y + f


def inverse_transform(transform: AffineTransform) -> AffineTransform:
    a, b, c, d, e, f = transform
    determinant = a * d - b * c
    if determinant == 0:
        raise ValueError("坐标变换不可逆")
    return d / determinant, -b / determinant, -c / determinant, a / determinant, (c * f - d * e) / determinant, (b * e - a * f) / determinant


def normalized_bbox_to_bp(bbox: BBox, width_bp: float, height_bp: float) -> BoxBp:
    return bbox[0] * width_bp, bbox[1] * height_bp, bbox[2] * width_bp, bbox[3] * height_bp


def tex_pt_to_bp(value: float) -> float:
    return value * 72 / 72.27
