from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

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

    @model_validator(mode="after")
    def validate_page_content(self) -> "PageUpdate":
        for field in ("page_kind", "cover_fields"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        if self.page_kind == "content" and self.cover_fields:
            raise ValueError("正文页不能包含封面书目信息")
        if self.page_kind in {"front_cover", "back_cover"} and self.text:
            raise ValueError("封面和封底不能包含正文")
        if len(self.text) + sum(len(field.text) for field in self.cover_fields or []) > 1_000_000:
            raise ValueError("页面文本过长")
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


class SpecialPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_kind: Literal["front_cover", "back_cover"]
    cover_fields: list[CoverField] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_length(self) -> "SpecialPageResult":
        if sum(len(field.text) for field in self.cover_fields) > 1_000_000:
            raise ValueError("页面文本过长")
        return self


class StructuredPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_kind: PageKind = "content"
    page_side: PageSide = "unknown"
    cover_fields: list[CoverField] = Field(default_factory=list, max_length=100)
    header_segments: list[MarginSegment] = Field(max_length=100)
    body_latex: str = Field(max_length=1_000_000)
    footer_segments: list[MarginSegment] = Field(max_length=100)

    @classmethod
    def from_model_response(cls, value: object) -> "StructuredPageResult":
        """模型响应必须显式提供页面字段和完整的页眉页脚属性。"""
        required = cls.model_fields.keys() - {"cover_fields"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError("模型页面结构缺少必填字段")
        for name in ("header_segments", "footer_segments"):
            segments = value[name]
            if isinstance(segments, list):
                for segment in segments:
                    if not isinstance(segment, dict) or not MarginSegment.model_fields.keys() <= segment.keys():
                        raise ValueError("模型页眉页脚缺少必填字段")
        result = cls.model_validate(value)
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
        if self.page_kind == "content":
            if self.cover_fields:
                raise ValueError("正文页不能包含封面书目信息")
        elif self.body_latex or self.header_segments or self.footer_segments:
            raise ValueError("封面和封底只能包含书目信息")
        total = len(self.body_latex) + sum(
            len(segment.text) for segment in self.header_segments + self.footer_segments
        ) + sum(len(field.text) for field in self.cover_fields)
        if total > 1_000_000:
            raise ValueError("页面文本过长")
        if self.page_kind != "content" or not any(segment.text.strip() for segment in self.footer_segments):
            self.page_side = "unknown"
        return self
