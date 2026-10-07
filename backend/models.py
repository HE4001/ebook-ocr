from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .layout_contract import (
    AffineTransform, BBox, BoxBp, EquationGroup, EquationNumber, FontFamily,
    LayoutLine, LayoutObservation, LayoutRegion, LineStyle,
    PageLayout, PageSourceMetadata, RenderStrategy, SourceFidelityLayout,
)
from .content_contract import ContentConclusion, LayoutConclusion, PageContent, RecoveryReason, RecognitionScope

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
    arrangement_revision: int = Field(default=0, ge=0)
    output_settings_version: int = Field(default=0, ge=0)


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
    page_id: str = ""
    source_version: int = Field(default=1, ge=1)
    current_revision_id: str | None = None
    result_status: Literal["auto_passed", "completed_with_issues", "failed"] | None = None
    manual_protected: bool = False
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


class SourcePageSummary(BaseModel):
    page_id: str
    source_version: int
    number: int
    source_id: str
    source_page: int
    source_filename: str
    width: int
    height: int


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
    processing_concurrency: StrictInt = Field(default=2, ge=1)


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
                required_fields = set(model.model_fields) - {"match_status"}
                if not isinstance(item, dict) or not required_fields <= item.keys():
                    raise ValueError("模型布局缺少必填字段；未知属性必须显式为 null")
            for line in result.layout.lines:
                line.match_status = "unknown"
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


WorkflowStage = Literal["prepare", "recognize", "review", "recover", "layout", "render", "export", "verify", "repair", "finalize"]
ExecutionStatus = Literal["queued", "running", "succeeded", "failed", "interrupted", "finished"]
RunStatus = Literal["queued", "running", "pausing", "paused", "succeeded", "failed", "interrupted", "finished"]
ResultStatus = Literal["auto_passed", "completed_with_issues", "failed"]
CheckStatus = Literal["passed", "uncertain", "unverified", "failed"]


class SelectionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    book_id: str
    selection_revision: int = Field(ge=0)
    page_ids: list[str] = Field(default_factory=list)
    source_versions: dict[str, int] = Field(default_factory=dict)
    valid: bool = True


class SelectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_ids: list[str]
    expected_selection_revision: int = Field(ge=0)
    source_versions: dict[str, int]

    @model_validator(mode="after")
    def validate_identity(self) -> "SelectionUpdate":
        if len(set(self.page_ids)) != len(self.page_ids) or set(self.page_ids) != set(self.source_versions):
            raise ValueError("选择必须提供不重复的有序 page_id 和对应来源版本")
        if any(version < 1 for version in self.source_versions.values()):
            raise ValueError("来源版本必须大于零")
        return self


class WorkflowError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: WorkflowStage
    category: RecoveryReason
    message: str = Field(min_length=1, max_length=2_000)
    field_path: str | None = Field(default=None, max_length=300)
    attempt_id: str | None = None
    block_id: str | None = None
    source_bbox: BBox | None = None
    phase: Literal["initial", "recovery", "output"] = "initial"


class PageOutcome(BaseModel):
    recognition_scope: RecognitionScope = "legacy_all_visible"
    model_config = ConfigDict(extra="forbid")
    page_id: str
    source_version: int = Field(ge=1)
    content: ContentConclusion = "unverified"
    layout: LayoutConclusion = "unverified"
    source_disposition: Literal["transcribed", "regions_preserved", "page_preserved"] = "transcribed"
    content_revision_id: str | None = None
    layout_revision_id: str | None = None
    adopted_revision_id: str | None = None
    source_readable: bool = True
    protected_existing: bool = False
    errors: list[WorkflowError] = Field(default_factory=list)


class RecognitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str
    run_id: str
    page_id: str
    body_asset: str | None = None
    body_storage: Literal["saved", "failed"]
    body_bytes: int = Field(default=0, ge=0)
    body_truncated: bool = False
    completion_status: Literal["complete", "truncated", "incomplete", "unknown"]
    usage: Usage
    provider_request_id: str | None = None
    parse_errors: list[WorkflowError] = Field(default_factory=list)
    storage_error: str | None = Field(default=None, max_length=1_000)
    created_at: str


class OutputFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "generating", "available", "failed"] = "pending"
    asset: str | None = None
    error: str | None = None


class OutputSnapshotPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_id: str
    position: int
    source_version: int
    source_file_id: str
    source_page: int
    source_filename: str
    image_name: str
    source_metadata: PageSourceMetadata | None = None
    revision_id: str | None = None
    content_revision_id: str | None = None
    layout_revision_id: str | None = None
    outcome: PageOutcome


class OutputSnapshot(BaseModel):
    recognition_scope: RecognitionScope = "legacy_all_visible"
    model_config = ConfigDict(extra="forbid", frozen=True)
    workflow_version: Literal[2] = 2
    output_snapshot_id: str
    book_id: str
    run_id: str
    selection_revision: int
    page_ids: list[str]
    output_settings_version: int
    settings_snapshot: dict
    generator_version: str
    pages: list[OutputSnapshotPage]
    formats: dict[Literal["json", "pdf", "latex"], OutputFormat] = Field(default_factory=lambda: {
        name: OutputFormat() for name in ("json", "pdf", "latex")
    })
    created_at: str


class RunPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replace_page_ids: list[str] = Field(default_factory=list)
    requests_per_page: int = Field(default=6, ge=0, le=100)
    page_request_limit: int = Field(default=12, ge=1, le=100)
    temporary_retry_limit: int = Field(default=2, ge=0, le=10)
    local_recovery_limit: int = Field(default=2, ge=0, le=10)
    compile_limit: int = Field(default=3, ge=1, le=10)
    response_body_limit_bytes: int = Field(default=4_000_000, ge=1_024, le=16_000_000)


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_ids: list[str] | None = Field(default=None, min_length=1)
    pages: list[StrictInt] | None = Field(default=None, min_length=1)
    expected_arrangement_revision: int | None = Field(default=None, ge=0)
    selection_revision: int | None = Field(default=None, ge=0)
    continuation_run_id: str | None = None
    policy: RunPolicy = Field(default_factory=RunPolicy)
    request_limit: int | None = Field(default=None, ge=0)
    client_request_id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_scope(self) -> "RunCreate":
        if self.page_ids is not None and self.pages is not None:
            raise ValueError("page_ids 与 pages 只能提供一种")
        if self.selection_revision is not None and (self.page_ids is not None or self.pages is not None):
            raise ValueError("新版任务范围只来自已保存选择，不重复传页码或 page_id")
        if self.selection_revision is None and self.expected_arrangement_revision is None:
            raise ValueError("启动必须提供选择修订；旧版入口必须提供编排修订")
        if self.continuation_run_id is not None and self.selection_revision is None:
            raise ValueError("补做运行必须使用新版选择修订")
        return self


class PageTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    page_id: str
    page_number: int
    source_version: int
    position: int
    base_revision_id: str | None
    base_content_revision: int
    base_layout_revision: int
    stage: WorkflowStage = "prepare"
    state: ExecutionStatus = "queued"
    completed_stages: list[WorkflowStage] = Field(default_factory=list)
    candidate_revision_id: str | None = None
    stage_data: dict = Field(default_factory=dict)
    result_status: ResultStatus | None = None
    error: str | None = None
    request_count: int = 0
    retry_count: int = 0
    recognize_count: int = 0
    review_count: int = 0
    repair_count: int = 0
    compile_count: int = 0
    recovery_round_count: int = 0
    outcome: PageOutcome | None = None
    errors: list[WorkflowError] = Field(default_factory=list)


class RunCounts(BaseModel):
    partial_content: int = 0
    total: int = 0
    completed: int = 0
    auto_passed: int = 0
    completed_with_issues: int = 0
    failed: int = 0
    editable: int = 0
    regions_preserved: int = 0
    page_preserved: int = 0
    no_result: int = 0
    protected_existing: int = 0


class Run(BaseModel):
    recognition_scope: RecognitionScope = "legacy_all_visible"
    workflow_version: Literal[1, 2] = 1
    run_id: str
    book_id: str
    arrangement_revision: int
    page_ids: list[str]
    settings_snapshot: dict
    policy: RunPolicy
    status: RunStatus
    request_limit: int
    request_count: int = 0
    generator_version: str
    created_at: str
    updated_at: str
    error: str | None = None
    usage: Usage
    counts: RunCounts = Field(default_factory=RunCounts)
    tasks: list[PageTask] = Field(default_factory=list)
    export_manifest_id: str | None = None
    selection_revision: int | None = None
    continuation_run_id: str | None = None
    output_snapshot_id: str | None = None
    continuation_outputs_needed: bool = False


class RunSummary(BaseModel):
    recognition_scope: RecognitionScope = "legacy_all_visible"
    workflow_version: Literal[1, 2]
    run_id: str
    book_id: str
    selection_revision: int | None
    status: RunStatus
    counts: RunCounts
    active_stages: dict[str, int] = Field(default_factory=dict)
    updated_at: str
    error: str | None = None
    recent_errors: list[WorkflowError] = Field(default_factory=list)
    output_snapshot_id: str | None = None
    formats: dict[str, OutputFormat] = Field(default_factory=dict)
    request_limit: int
    request_count: int
    usage: Usage
    model_wait_started_at: str | None = None


class PageOutcomeSummary(BaseModel):
    category: Literal["editable", "partial_content", "regions_preserved", "page_preserved", "no_result", "protected_existing"] = "no_result"
    page_id: str
    page_number: int
    position: int
    stage: WorkflowStage
    state: ExecutionStatus
    outcome: PageOutcome | None = None


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision_id: str
    parent_revision_id: str | None
    book_id: str
    page_id: str
    page_number: int
    source_version: int
    content_revision: int
    layout_revision: int
    origin: Literal["legacy", "manual", "automatic"]
    run_id: str | None
    text: str
    render_strategy: RenderStrategy
    layout_source: SourceFidelityLayout | None = None
    source_metadata: PageSourceMetadata | None = None
    page_kind: PageKind = "content"
    page_side: PageSide = "unknown"
    cover_fields: list[CoverField] = Field(default_factory=list)
    header_segments: list[MarginSegment] = Field(default_factory=list)
    footer_segments: list[MarginSegment] = Field(default_factory=list)
    generated_content_revision: int | None = None
    generator_version: str | None = None
    created_at: str
    workflow_version: Literal[1, 2] = 1
    page_content: PageContent | None = None
    page_layout: PageLayout | None = None


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str
    run_id: str
    page_id: str
    revision_id: str
    category: str
    severity: Literal["error", "warning", "info"]
    region_id: str | None = None
    line_id: str | None = None
    source_bbox: BBox | None = None
    reason: str
    disposition: str


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment_id: str
    revision_id: str
    run_id: str
    page_id: str
    content: CheckStatus = "unverified"
    layout: CheckStatus = "unverified"
    coverage: CheckStatus = "unverified"
    rule_version: str
    issues: list[Issue] = Field(default_factory=list)
    created_at: str


class Attempt(BaseModel):
    attempt_id: str
    run_id: str
    page_id: str
    stage: WorkflowStage
    ordinal: int
    retry: bool = False
    state: Literal["reserved", "succeeded", "failed", "unknown", "cancelled"] = "reserved"
    usage: Usage = Field(default_factory=lambda: Usage(
        input_tokens=None, output_tokens=None, total_tokens=None, complete=False,
    ))
    provider_request_id: str | None = None
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    purpose: Literal["region_plan", "basic_recognition", "basic_review", "local_recognition", "recovery", "recovery_review", "retry"] | None = None
    reservation_key: str | None = None
    recovery_round: int | None = None
    block_ids: list[str] = Field(default_factory=list)
    sent_at: str | None = None
    retryable: bool = False
    settled_at: str | None = None
    retry_of_attempt_id: str | None = None


class ExportManifestPage(BaseModel):
    page_id: str
    page_number: int
    position: int
    revision_id: str
    source_file_id: str
    source_page: int
    source_version: int
    source_filename: str
    image_name: str
    result_status: ResultStatus | None
    assessment: Assessment | None = None


class ExportManifest(BaseModel):
    recognition_scope: RecognitionScope = "legacy_all_visible"
    model_config = ConfigDict(frozen=True)
    manifest_id: str
    book_id: str
    run_id: str | None = None
    arrangement_revision: int
    output_settings_version: int
    settings_snapshot: dict
    generator_version: str
    pages: list[ExportManifestPage]
    complete: bool
    issues: list[Issue] = Field(default_factory=list)
    created_at: str
    outputs: dict[str, str] = Field(default_factory=dict)


class ExportManifestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_arrangement_revision: int = Field(ge=0)
    page_ids: list[str] | None = Field(default=None, min_length=1)
    run_id: str | None = None


class PageResult(BaseModel):
    page_id: str
    page_number: int
    current_revision_id: str | None
    result_status: ResultStatus | None
    content: CheckStatus = "unverified"
    layout: CheckStatus = "unverified"
    coverage: CheckStatus = "unverified"
    source_disposition: Literal["transcribed", "regions_preserved", "source_page_preserved"] | None = None
    issues: list[Issue] = Field(default_factory=list)
