from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


SectionType = Literal[
    "heading", "paragraph", "quote", "example", "list", "code", "equation",
    "table", "figure", "caption", "footnote", "header", "footer",
    "page_number", "unknown",
]
PageStatus = Literal["uploaded", "processing", "ready", "failed", "interrupted"]
BookStatus = PageStatus | Literal["pausing", "paused"]


class Usage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool


class Book(BaseModel):
    id: str
    title: str
    filename: str
    status: BookStatus
    page_count: int
    completed_pages: int
    error: str | None
    created_at: str
    usage: Usage


class MarginSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text", "page_number"]
    text: str = Field(max_length=50_000)


class Page(BaseModel):
    number: int
    status: PageStatus
    error: str | None
    text: str
    header_segments: list[MarginSegment]
    footer_segments: list[MarginSegment]
    usage: Usage
    attempts: int


class BookDetail(BaseModel):
    book: Book
    pages: list[Page]


class PageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=1_000_000)


class SettingsOut(BaseModel):
    base_url: str
    responses_path: str
    extraction_model: str
    reasoning_effort: str
    classification_model: str
    has_api_key: bool
    structured_output: bool
    timeout_seconds: int
    max_output_tokens: int


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    responses_path: str | None = None
    extraction_model: str | None = Field(default=None, max_length=200)
    reasoning_effort: str | None = Field(default=None, max_length=200)
    classification_model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=10_000)
    clear_api_key: bool = False
    structured_output: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)

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

    @field_validator("responses_path")
    @classmethod
    def validate_responses_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Responses 接入路径必须是相对路径")
        if not value.startswith("/"):
            value = "/" + value
        if any(part == ".." for part in value.split("/")):
            raise ValueError("Responses 接入路径不能包含 ..")
        return value

    @field_validator("extraction_model", "classification_model", "reasoning_effort")
    @classmethod
    def trim_model(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str


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


class PauseResult(BaseModel):
    requested: bool


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: SectionType
    text: str = Field(max_length=50_000)


class PageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[Section] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_length(self) -> "PageResult":
        if sum(len(section.text) for section in self.sections) > 1_000_000:
            raise ValueError("页面文本过长")
        return self


class StructuredPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header_segments: list[MarginSegment] = Field(max_length=100)
    body_markdown: str = Field(max_length=1_000_000)
    footer_segments: list[MarginSegment] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_length(self) -> "StructuredPageResult":
        total = len(self.body_markdown) + sum(
            len(segment.text) for segment in self.header_segments + self.footer_segments
        )
        if total > 1_000_000:
            raise ValueError("页面文本过长")
        return self
