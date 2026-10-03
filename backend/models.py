from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from .layout_contract import (
    AffineTransform, BBox, BoxBp, EquationGroup, EquationNumber, FontFamily,
    LayoutLine, LayoutObservation, LayoutRegion, LineStyle,
    PageSourceMetadata, RenderStrategy, SourceFidelityLayout,
)

PageStatus = Literal["uploaded", "processing", "ready", "failed", "interrupted"]
BookStatus = PageStatus | Literal["pausing", "paused"]
PageKind = Literal["content", "front_cover", "back_cover"]
PageSide = Literal["left", "right", "unknown"]
PaperSize = Literal["a4", "a5", "a6", "b5", "b6", "trade_6x9"]
CoverFieldKind = Literal[
    "title", "subtitle", "author", "translator", "editor", "publisher",
    "series", "edition", "publication_year", "isbn",
]


class Usage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool


class LayoutSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_fidelity_paper: Literal["project", "source"] = "project"
    font_family: Literal["songti", "heiti", "kaiti"] = "songti"
    font_size_pt: float | None = Field(default=None, ge=6, le=48)
    line_height: float = Field(default=1.6, ge=1, le=3)
    paragraph_indent: float = Field(default=2, ge=0, le=8)
    paragraph_spacing_pt: float = Field(default=0, ge=0, le=48)
    margin_mm: float | None = Field(default=None, ge=2, le=50)


class Book(BaseModel):
    id: str
    title: str
    filename: str
    paper_size: PaperSize = "a4"
    layout: LayoutSettings = Field(default_factory=LayoutSettings)
    content_format: Literal["latex"] = "latex"
    render_strategy: RenderStrategy = "source_fidelity"
    status: BookStatus
    page_count: int
    file_count: int = 1
    upload_confirmed: bool = True
    selection_confirmed: bool
    selected_page_count: int
    completed_pages: int
    error: str | None
    created_at: str
    usage: Usage


class MarginSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text", "page_number"]
    text: str = Field(max_length=50_000)
    alignment: Literal["left", "center", "right"] = "center"
    row: int = Field(default=1, ge=1, le=10)
    font_size: Literal["small", "normal"] = "small"
    bold: bool = False
    italic: bool = False


class CoverField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: CoverFieldKind
    text: str = Field(max_length=50_000)


class Page(BaseModel):
    number: int
    source_id: str = "legacy"
    source_filename: str = ""
    source_page: int = 1
    status: PageStatus
    error: str | None
    text: str
    render_strategy: RenderStrategy = "legacy_template"
    content_revision: int = Field(default=0, ge=0)
    layout_revision: int = Field(default=0, ge=0)
    generated_content_revision: int | None = Field(default=None, ge=0)
    layout_source: SourceFidelityLayout | None = None
    source_metadata: PageSourceMetadata | None = None
    page_kind: PageKind = "content"
    page_side: PageSide = "unknown"
    cover_fields: list[CoverField] = Field(default_factory=list)
    header_segments: list[MarginSegment]
    footer_segments: list[MarginSegment]
    usage: Usage
    attempts: int


class SourceFile(BaseModel):
    id: str
    filename: str
    kind: Literal["pdf", "image"]
    page_count: int
    position: int
    parent_id: str | None = None


class BookDetail(BaseModel):
    book: Book
    files: list[SourceFile] = Field(default_factory=list)
    pages: list[Page]


class Arrangement(BookDetail):
    order: list[int]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目名称不能为空")
        return value.strip()


class ArrangementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_order: list[str] = Field(min_length=1)
    page_order: list[StrictInt] = Field(min_length=1)
    file_parents: dict[str, str | None] | None = None


class LayoutUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_size: PaperSize | None = None
    layout: LayoutSettings | None = None
    render_strategy: RenderStrategy | None = None

    @model_validator(mode="after")
    def validate_layout(self) -> "LayoutUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供纸型或排版设置")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能为 null")
        return self


class PageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=1_000_000)
    page_kind: PageKind | None = None
    cover_fields: list[CoverField] | None = Field(default=None, max_length=100)
    render_strategy: RenderStrategy | None = None
    expected_content_revision: int | None = Field(default=None, ge=0)
    expected_layout_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_page_content(self) -> "PageUpdate":
        for field in ("page_kind", "cover_fields", "render_strategy", "expected_content_revision", "expected_layout_revision"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        if self.page_kind == "content" and self.cover_fields:
            raise ValueError("正文页不能包含封面书目信息")
        if self.page_kind in {"front_cover", "back_cover"} and self.text:
            raise ValueError("封面和封底不能包含正文")
        if len(self.text) + sum(len(field.text) for field in self.cover_fields or []) > 1_000_000:
            raise ValueError("页面文本过长")
        return self


class LayoutCalibrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    observation: LayoutObservation
    expected_content_revision: int = Field(ge=0)
    expected_layout_revision: int = Field(ge=0)
    canvas_width_bp: float | None = Field(default=None, gt=0)
    canvas_height_bp: float | None = Field(default=None, gt=0)
    body_font_size_bp: float | None = Field(default=None, gt=0, le=200)
    body_font_family: FontFamily | None = None
    render_strategy: RenderStrategy = "source_fidelity"

    @model_validator(mode="after")
    def validate_canvas(self) -> "LayoutCalibrationUpdate":
        if (self.canvas_width_bp is None) != (self.canvas_height_bp is None):
            raise ValueError("规范画布宽高必须同时提供")
        return self


class SettingsOut(BaseModel):
    api_protocol: Literal["openai_responses", "gemini"] = "openai_responses"
    base_url: str
    responses_path: str
    models_path: str
    extraction_model: str
    reasoning_effort: str
    classification_model: str
    has_api_key: bool
    structured_output: bool
    timeout_seconds: int
    processing_concurrency: StrictInt = Field(default=10, ge=1)
    context_reuse_enabled: StrictBool = False
    context_reuse_max_pages: StrictInt = Field(default=10, ge=1, le=10)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_protocol: Literal["openai_responses", "gemini"] = "openai_responses"
    base_url: str | None = None
    responses_path: str | None = None
    models_path: str | None = None
    extraction_model: str | None = Field(default=None, max_length=200)
    reasoning_effort: str | None = Field(default=None, max_length=200)
    classification_model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=10_000)
    clear_api_key: bool = False
    structured_output: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    processing_concurrency: StrictInt | None = Field(default=None, ge=1)
    context_reuse_enabled: StrictBool | None = None
    context_reuse_max_pages: StrictInt | None = Field(default=None, ge=1, le=10)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("API 根地址必须是无凭据、查询或片段的 http(s) 地址")
        return value

    @field_validator("responses_path", "models_path")
    @classmethod
    def validate_responses_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("接入路径必须是相对路径")
        if not value.startswith("/"):
            value = "/" + value
        if any(part == ".." for part in value.split("/")):
            raise ValueError("接入路径不能包含 ..")
        return value

    @field_validator("extraction_model", "classification_model", "reasoning_effort")
    @classmethod
    def trim_model(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str


class ModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_protocol: Literal["openai_responses", "gemini"] = "openai_responses"
    base_url: str
    models_path: str
    api_key: str | None = Field(default=None, max_length=10_000)
    clear_api_key: bool = False
    timeout_seconds: int = Field(ge=5, le=600)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return SettingsUpdate.validate_base_url(value)

    @field_validator("models_path")
    @classmethod
    def validate_models_path(cls, value: str) -> str:
        return SettingsUpdate.validate_responses_path(value)


class ModelsResult(BaseModel):
    models: list[str]


class ProcessResult(BaseModel):
    started: bool


class ProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[StrictInt] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_pages(self) -> "ProcessRequest":
        if "pages" in self.model_fields_set and self.pages is None:
            raise ValueError("pages 必须是非空页码列表")
        return self


class PagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[StrictInt] = Field(min_length=1)


class PauseResult(BaseModel):
    requested: bool


QualityStatus = Literal["passed", "needs_review", "unverified", "compile_failed"]


class RenderDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    suggestion: str
    basis: str
    book_id: str | None = None
    page_number: int
    source_id: str
    source_page: int
    arrangement_position: int
    output_page_start: int | None = None
    output_page_end: int | None = None
    line_id: str | None = None
    block_id: str | None = None
    source_bbox: BBox | None = None
    output_bbox_bp: BoxBp | None = None
    overflow_bp: float | None = None
    content_revision: int
    layout_revision: int
    coverage: Literal["complete", "partial", "none"]


class PageMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    page_number: int
    source_id: str
    source_page: int
    arrangement_position: int
    output_page_start: int
    output_page_end: int
    content_revision: int
    layout_revision: int
    render_strategy: RenderStrategy
    output_width_bp: float | None = None
    output_height_bp: float | None = None
    source_to_output_affine: AffineTransform | None = None
    canvas_scale: float | None = None


class PdfCompileResult(BaseModel):
    pdf_url: str | None
    warnings: list[str]
    diagnostics: list[RenderDiagnostic] = Field(default_factory=list)
    page_map: list[PageMapEntry] = Field(default_factory=list)
    quality_status: QualityStatus = "unverified"
    generator_version: str | None = None
    diagnostics_version: str | None = None


class StructuredPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_kind: PageKind = "content"
    page_side: PageSide = "unknown"
    cover_fields: list[CoverField] = Field(default_factory=list, max_length=100)
    header_segments: list[MarginSegment] = Field(max_length=100)
    body_latex: str = Field(max_length=1_000_000)
    footer_segments: list[MarginSegment] = Field(max_length=100)
    response_version: Literal[1, 2] = 1
    layout: LayoutObservation | None = None

    @classmethod
    def from_model_response(cls, value: object, response_version: Literal[1, 2] = 1) -> "StructuredPageResult":
        """模型响应必须显式提供页面字段和完整的页眉页脚属性。"""
        required = set(cls.model_fields) - {"response_version", "layout"}
        if response_version == 2:
            required |= {"response_version", "layout"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError("模型页面结构缺少必填字段")
        if value.get("response_version", 1) != response_version:
            raise ValueError("模型响应版本与请求不一致")
        if response_version == 1 and ({"response_version", "layout"} & value.keys()):
            raise ValueError("旧响应版本不能包含新版字段")
        for name in ("header_segments", "footer_segments"):
            segments = value[name]
            if isinstance(segments, list):
                for segment in segments:
                    if not isinstance(segment, dict) or not MarginSegment.model_fields.keys() <= segment.keys():
                        raise ValueError("模型页眉页脚缺少必填字段")
        result = cls.model_validate(value)
        if response_version == 2 and result.layout is not None:
            if len(result.layout.review_reasons) > 100:
                raise ValueError("模型复核原因最多为 100 条")
            observation_length = sum(len(line.latex) for line in result.layout.lines) + sum(
                len(group.number.latex) for group in result.layout.equation_groups if group.number is not None
            ) + sum(len(reason) for reason in result.layout.review_reasons)
            page_length = len(result.body_latex) + sum(
                len(segment.text) for segment in result.header_segments + result.footer_segments
            ) + sum(len(field.text) for field in result.cover_fields)
            if page_length + observation_length > 1_000_000:
                raise ValueError("模型页面内容过长")
            raw_layout = value["layout"]
            required_objects = [(raw_layout, LayoutObservation)]
            required_objects.extend((item, LayoutRegion) for item in raw_layout.get("regions", []))
            for line in raw_layout.get("lines", []):
                required_objects.extend(((line, LayoutLine), (line.get("style", {}), LineStyle)))
            for group in raw_layout.get("equation_groups", []):
                required_objects.append((group, EquationGroup))
                if group.get("number") is not None:
                    required_objects.append((group["number"], EquationNumber))
            for item, model in required_objects:
                if not isinstance(item, dict) or not model.model_fields.keys() <= item.keys():
                    raise ValueError("模型布局缺少必填字段；未知属性必须显式为 null")
            observations = result.layout.regions + result.layout.lines + result.layout.equation_groups
            if any(item.basis not in {None, "model_estimate"} for item in observations) or any(
                line.style.basis not in {None, "model_estimate"} for line in result.layout.lines
            ):
                raise ValueError("模型观察只能声明 model_estimate 或未知依据")
        if result.page_kind == "content":
            footer = [segment for segment in result.footer_segments if segment.text.strip()]
            page_numbers = [segment for segment in footer if segment.kind == "page_number"]
            alignments = {segment.alignment for segment in (page_numbers or footer)}
            if alignments == {"left"}:
                result.page_side = "left"
            elif alignments == {"right"}:
                result.page_side = "right"
            else:
                result.page_side = "unknown"
        return result

    @model_validator(mode="after")
    def validate_length(self) -> "StructuredPageResult":
        if self.response_version == 1 and self.layout is not None:
            raise ValueError("旧响应版本不能包含新版布局")
        if self.response_version == 2 and self.page_kind == "content" and self.layout is None:
            raise ValueError("新版正文响应必须包含布局")
        if self.page_kind == "content":
            if self.cover_fields:
                raise ValueError("正文页不能包含封面书目信息")
        elif self.body_latex or self.header_segments or self.footer_segments or self.layout is not None:
            raise ValueError("封面和封底只能包含书目信息")
        total = len(self.body_latex) + sum(
            len(segment.text) for segment in self.header_segments + self.footer_segments
        ) + sum(len(field.text) for field in self.cover_fields)
        if total > 1_000_000:
            raise ValueError("页面文本过长")
        if self.page_kind != "content" or not any(segment.text.strip() for segment in self.footer_segments):
            self.page_side = "unknown"
        return self
