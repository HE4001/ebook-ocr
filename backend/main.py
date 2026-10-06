from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .content_contract import PageContent
from .compile_service import (
    compile_documents, failed_result, generate_manifest_outputs, render_output_page, same_source_identity,
)
from .importers import ImportFailure, import_document, prepare_page as _prepare_page, prepare_source_preview
from .latex_export import (
    GENERATOR_VERSION, FidelityLayoutError, LatexCompileError,
    build_latex_documents, generate_source_fidelity_latex,
)
from .layout_contract import SourceFidelityLayout
from .latex_content import source_resource_name
from .models import (
    Arrangement,
    ArrangementUpdate,
    Book,
    BookDetail,
    ConnectionTestResult,
    ExportManifest,
    ExportManifestCreate,
    Issue,
    LayoutCalibrationUpdate,
    LayoutUpdate,
    ModelsRequest,
    ModelsResult,
    OutputSnapshot,
    Page,
    PageOutcomeSummary,
    PageUpdate,
    PageResult,
    PdfCompileResult,
    PagesRequest,
    PauseResult,
    ProcessRequest,
    ProcessResult,
    ProjectCreate,
    Run,
    RunCreate,
    RunSummary,
    SelectionDraft,
    SelectionUpdate,
    SettingsOut,
    SettingsUpdate,
    SourcePageSummary,
)
from .pipeline import WorkflowProcessor
from .gemini_client import fetch_gemini_models
from .responses_client import ModelServiceError, fetch_models
from .storage import RevisionConflict, Storage


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
COMPILED_DIGEST = re.compile(r"^[0-9a-f]{64}$")
UNFINISHED_RUN_STATUSES = {"queued", "running", "pausing", "paused"}
MANIFEST_FILE_KEYS = {"pdf", "partial_pdf", "latex", "json"}
logger = logging.getLogger(__name__)


class RuntimeSecrets:
    api_key: str = ""


class ImportErrorDetail(BaseModel):
    filename: str
    reason: str = Field(max_length=1_000)


class UploadOutcome(Arrangement):
    import_errors: list[ImportErrorDetail] = Field(default_factory=list)


class SourcePageList(BaseModel):
    items: list[SourcePageSummary]
    total: int
    offset: int
    limit: int


class OutputPageError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_id: str
    position: int = Field(ge=0)
    source_filename: str
    source_page: int = Field(ge=1)
    source_version: int = Field(ge=1)
    revision_id: str | None
    reason: str = Field(min_length=1, max_length=1_000)


class OutputErrors(BaseModel):
    pdf: list[OutputPageError] = Field(default_factory=list)
    latex: list[OutputPageError] = Field(default_factory=list)


def _valid_book_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="书籍不存在") from exc
    if str(parsed) != value.lower():
        raise HTTPException(status_code=404, detail="书籍不存在")
    return value


def create_app(data_root: Path | None = None) -> FastAPI:
    root = data_root or Path(
        os.environ.get("EBOOK_OCR_DATA_DIR", Path(__file__).resolve().parent / "data")
    )
    storage = Storage(root)
    secrets = RuntimeSecrets()
    running: dict[str, asyncio.Task[None]] = {}
    pause_requests: dict[str, asyncio.Event] = {}
    active_run_ids: dict[str, str] = {}
    uploading: set[str] = set()
    preview_lock = asyncio.Lock()
    compile_locks: dict[str, asyncio.Lock] = {}
    manifest_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        storage.initialize()
        secrets.api_key = storage.get_api_key() or ""
        storage.recover_interrupted()
        yield
        tasks = list(running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="电子书 OCR API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "请求参数无效"})

    def settings_response() -> SettingsOut:
        settings = storage.get_settings()
        return SettingsOut(**settings, has_api_key=bool(secrets.api_key))

    def ensure_book(book_id: str) -> Book:
        _valid_book_id(book_id)
        book = storage.get_book(book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="书籍不存在")
        return book

    def is_running(book_id: str) -> bool:
        task = running.get(book_id)
        return task is not None and not task.done()

    def book_detail(book_id: str) -> BookDetail:
        return BookDetail(book=ensure_book(book_id), files=storage.get_files(book_id), pages=storage.get_pages(book_id))

    def arrangement(book_id: str) -> Arrangement:
        return Arrangement(
            book=ensure_book(book_id), files=storage.get_files(book_id),
            pages=storage.get_pages(book_id, all_pages=True),
            order=[page.number for page in storage.get_pages(book_id)],
        )

    def ensure_idle(book_id: str) -> None:
        if (is_running(book_id) or book_id in uploading
                or any(run.status in UNFINISHED_RUN_STATUSES for run in storage.list_runs(book_id))):
            raise HTTPException(status_code=409, detail="项目正在上传或有未结束任务，请结束或恢复任务后操作")

    def ensure_run(book_id: str, run_id: str) -> Run:
        ensure_book(book_id)
        try:
            UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        run = storage.get_run(book_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return run

    def ensure_manifest(book_id: str, manifest_id: str) -> ExportManifest:
        ensure_book(book_id)
        try:
            UUID(manifest_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="导出清单不存在") from exc
        manifest = storage.get_export_manifest(book_id, manifest_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="导出清单不存在")
        return manifest

    def ensure_snapshot(book_id: str, snapshot_id: str) -> OutputSnapshot:
        ensure_book(book_id)
        try:
            UUID(snapshot_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="输出快照不存在") from exc
        snapshot = storage.get_output_snapshot(book_id, snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="输出快照不存在")
        return snapshot

    def snapshot_errors(snapshot: OutputSnapshot, format_name: str) -> list[OutputPageError]:
        book_dir = (storage.books_root / snapshot.book_id).resolve()
        path = (book_dir / "exports" / snapshot.output_snapshot_id / f"{format_name}-errors.json").resolve()
        if not path.is_relative_to(book_dir):
            raise HTTPException(status_code=409, detail="输出错误记录路径无效")
        if not path.is_file():
            return []
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(body, dict) or set(body) != {"output_snapshot_id", "issues"}
                    or body["output_snapshot_id"] != snapshot.output_snapshot_id
                    or not isinstance(body["issues"], list)):
                raise ValueError("snapshot errors")
            pages = {entry.page_id: entry for entry in snapshot.pages}
            issues = []
            for value in body["issues"]:
                issue = OutputPageError.model_validate(value)
                entry = pages.get(issue.page_id)
                if entry is None or (issue.position, issue.source_filename, issue.source_page,
                                     issue.source_version, issue.revision_id) != (
                        entry.position, entry.source_filename, entry.source_page,
                        entry.source_version, entry.revision_id):
                    raise ValueError("snapshot page")
                issues.append(issue)
            return issues
        except (OSError, UnicodeError, ValueError, ValidationError) as exc:
            # Never expose malformed sidecar data or model response bodies.
            raise HTTPException(status_code=409, detail="输出错误记录无法读取或与冻结快照不一致") from exc

    def ensure_run_available(book_id: str, run_id: str) -> None:
        if book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在上传，请稍后启动任务")
        if (is_running(book_id) and active_run_ids.get(book_id) != run_id
                or any(run.run_id != run_id and run.status in UNFINISHED_RUN_STATUSES
                       for run in storage.list_runs(book_id))):
            raise HTTPException(status_code=409, detail="项目已有未结束任务，请先完成或恢复该任务")

    def dispatch_run(run: Run) -> Run:
        ensure_run_available(run.book_id, run.run_id)
        if is_running(run.book_id):
            return ensure_run(run.book_id, run.run_id)
        current_settings = storage.get_settings()
        if (SettingsUpdate.validate_base_url(current_settings["base_url"])
                != SettingsUpdate.validate_base_url(run.settings_snapshot["base_url"])
                or current_settings.get("api_protocol", "openai_responses")
                != run.settings_snapshot.get("api_protocol", "openai_responses")):
            raise HTTPException(
                status_code=409,
                detail="当前 API 接入与任务冻结的地址或协议不同；请恢复原接入并保存其密钥，再恢复此任务",
            )
        pause_event = asyncio.Event()
        api_key = secrets.api_key

        async def execute() -> None:
            try:
                await WorkflowProcessor(storage).process_run(run.book_id, run.run_id, api_key, pause_event)
            except asyncio.CancelledError:
                if ensure_run(run.book_id, run.run_id).status in {"queued", "running", "pausing"}:
                    storage.set_run_status(
                        run.book_id, run.run_id, "interrupted",
                        "服务退出中断任务；已发送但未取得结果的请求不会自动重发",
                    )
                raise
            except Exception as error:
                # Arbitrary exception text may contain an upstream URL or credential.
                if ensure_run(run.book_id, run.run_id).status in {"queued", "running", "pausing"}:
                    storage.set_run_status(run.book_id, run.run_id, "failed", "任务执行发生技术错误，已有结果保留")
                logger.error("Workflow %s failed (%s)", run.run_id, type(error).__name__)
                raise

        storage.set_run_status(run.book_id, run.run_id, "running")
        task = asyncio.create_task(execute(), name=f"workflow-{run.run_id}")
        running[run.book_id] = task
        active_run_ids[run.book_id] = run.run_id
        pause_requests[run.book_id] = pause_event

        def remove_finished(finished: asyncio.Task[None]) -> None:
            if running.get(run.book_id) is finished:
                running.pop(run.book_id, None)
                active_run_ids.pop(run.book_id, None)
                pause_requests.pop(run.book_id, None)
            if not finished.cancelled():
                finished.exception()  # The wrapper records and logs any failure without secrets.

        task.add_done_callback(remove_finished)
        return ensure_run(run.book_id, run.run_id)

    async def populate_manifest(manifest: ExportManifest) -> ExportManifest:
        async with manifest_locks.setdefault(manifest.manifest_id, asyncio.Lock()):
            outputs = await generate_manifest_outputs(
                storage.get_manifest_book(manifest.book_id, manifest.manifest_id), manifest,
                storage.get_manifest_pages(manifest.book_id, manifest.manifest_id),
                storage.books_root / manifest.book_id,
            )
            return storage.record_manifest_outputs(manifest.book_id, manifest.manifest_id, outputs)

    async def compiled_pdf(
        detail: BookDetail, print_version: bool, page_order: list[int] | None = None,
    ) -> PdfCompileResult:
        if not detail.pages:
            raise HTTPException(status_code=422, detail="当前清单没有可编译页面")
        positions = page_order if page_order is not None else list(range(1, len(detail.pages) + 1))
        try:
            documents = build_latex_documents(detail, print_version)
        except LatexCompileError as error:
            return failed_result(detail, error, positions)
        documents = [replace(document, page_order=[positions[position - 1] for position in document.page_order])
                     for document in documents]
        async with compile_locks.setdefault(detail.book.id, asyncio.Lock()):
            current_sources = []
            for page in detail.pages:
                record = storage.get_page_record(detail.book.id, page.number)
                try:
                    _, _, _, metadata = await asyncio.to_thread(
                        _prepare_page, storage.books_root / detail.book.id, record,
                    )
                except (ImportFailure, OSError, ValueError) as error:
                    return failed_result(detail, LatexCompileError(
                        f"无法读取源页 {page.number} 的当前来源：{error}",
                        code="LAYOUT_SOURCE_MISMATCH", page_number=page.number,
                    ), positions)
                current_sources.append(metadata)
            return await compile_documents(detail, documents, storage.books_root / detail.book.id,
                                           current_sources, print_version, positions)

    def editable_page(book_id: str, number: int) -> Page:
        ensure_book(book_id)
        if book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在上传，请稍后保存")
        record = storage.get_page_record(book_id, number)
        if record is None or not record["selected"]:
            raise HTTPException(status_code=404, detail="页面不存在")
        return next(page for page in storage.get_pages(book_id) if page.number == number)

    async def calibrated_page(book_id: str, number: int, update: LayoutCalibrationUpdate) -> Page:
        page = editable_page(book_id, number)
        if update.render_strategy != "source_fidelity":
            raise HTTPException(status_code=422, detail="布局校准使用原书还原策略；其他策略请通过页面或项目设置选择")
        if page.page_kind != "content":
            raise HTTPException(status_code=422, detail="封面和封底不能校准正文布局")
        if (page.content_revision != update.expected_content_revision
                or page.layout_revision != update.expected_layout_revision):
            raise HTTPException(status_code=409, detail="页面已更新，请加载最新版本后再校准")
        record = storage.get_page_record(book_id, number)
        try:
            _, _, _, metadata = await asyncio.to_thread(_prepare_page, storage.books_root / book_id, record)
        except (ImportFailure, OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=f"无法读取当前来源：{error}") from error
        previous = page.layout_source
        preservation = {}
        if previous is not None:
            if same_source_identity(previous.source, metadata):
                preservation = {
                    "source_assets": previous.source_assets,
                    "source_disposition": previous.source_disposition,
                    "disposition_reason": previous.disposition_reason,
                }
            elif previous.source_assets or previous.source_disposition != "transcribed":
                raise HTTPException(status_code=409, detail="源页身份或版本已改变，不能丢弃或沿用旧源图保留资源")
        parameters = {name: getattr(previous, name) if previous is not None else None for name in (
            "canvas_width_bp", "canvas_height_bp", "canvas_basis",
            "body_font_size_bp", "body_font_family", "body_font_basis",
        )}
        provided = update.model_fields_set
        canvas_fields = {"canvas_width_bp", "canvas_height_bp"}
        if provided & canvas_fields and not canvas_fields <= provided:
            raise HTTPException(status_code=422, detail="规范画布宽高必须同时提供或同时设为 null")
        if canvas_fields <= provided:
            parameters.update(canvas_width_bp=update.canvas_width_bp, canvas_height_bp=update.canvas_height_bp,
                              canvas_basis="manual" if update.canvas_width_bp is not None else None)
        font_fields = {"body_font_size_bp", "body_font_family"}
        for name in provided & font_fields:
            parameters[name] = getattr(update, name)
        reasons = list(update.observation.review_reasons)
        if provided & font_fields:
            cleared = any(getattr(update, name) is None for name in provided & font_fields)
            jointly_confirmed = font_fields <= provided and not cleared
            if all(parameters[name] is None for name in font_fields):
                parameters["body_font_basis"] = None
            elif jointly_confirmed or (parameters["body_font_basis"] == "manual" and not cleared):
                parameters["body_font_basis"] = "manual"
            elif parameters["body_font_basis"] in {None, "manual"}:
                parameters["body_font_basis"] = "project"
                reason = "基准字族与字号尚未共同确认；未知项保持未知，预览采用临时项目配置。"
                if reason not in reasons:
                    reasons.append(reason)
        layout = SourceFidelityLayout(
            **update.observation.model_dump(exclude={"review_reasons"}), review_reasons=reasons,
            **parameters, **preservation, source=metadata,
            content_revision=page.content_revision + 1, layout_revision=page.layout_revision + 1,
            generated_content_revision=page.content_revision + 1, generator_version=GENERATOR_VERSION,
        )
        return page.model_copy(update={
            "layout_source": layout, "source_metadata": metadata, "render_strategy": "source_fidelity",
            "content_revision": layout.content_revision, "layout_revision": layout.layout_revision,
            "generated_content_revision": layout.generated_content_revision,
        })

    async def read_upload(file: UploadFile) -> tuple[str, bytes]:
        filename = Path(file.filename or "").name
        if not filename or len(filename) > 255:
            raise HTTPException(status_code=400, detail="文件名为空或过长")
        chunks: list[bytes] = []
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="每个文件不能超过 100 MB")
            chunks.append(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="文件为空")
        return filename, b"".join(chunks)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings", response_model=SettingsOut)
    async def get_settings() -> SettingsOut:
        return settings_response()

    @app.put("/api/settings", response_model=SettingsOut)
    async def put_settings(update: SettingsUpdate) -> SettingsOut:
        values = update.model_dump(exclude_unset=True)
        clear_api_key = bool(values.pop("clear_api_key", False))
        api_key = values.pop("api_key", None)
        new_api_key = api_key.strip() if api_key is not None and api_key.strip() else None
        values = {key: value for key, value in values.items() if value is not None}
        settings = storage.get_settings()
        saved_base_url = SettingsUpdate.validate_base_url(settings["base_url"])
        saved_protocol = settings.get("api_protocol", "openai_responses")
        settings.update(values)
        connection_changed = (
            SettingsUpdate.validate_base_url(settings["base_url"]) != saved_base_url
            or settings["api_protocol"] != saved_protocol
        )
        if connection_changed and secrets.api_key and new_api_key is None and not clear_api_key:
            raise HTTPException(
                status_code=400,
                detail="API 根地址或协议已改变，请输入新连接的 API 密钥或明确清除已保存的密钥",
            )
        storage.save_settings(
            settings,
            api_key=new_api_key,
            clear_api_key=clear_api_key,
        )
        if clear_api_key:
            secrets.api_key = ""
        elif new_api_key is not None:
            secrets.api_key = new_api_key
        return settings_response()

    @app.post("/api/settings/models", response_model=ModelsResult)
    async def list_models(request: ModelsRequest) -> ModelsResult:
        api_key = (request.api_key or "").strip()
        if not api_key and not request.clear_api_key:
            settings = storage.get_settings()
            saved_base_url = SettingsUpdate.validate_base_url(settings["base_url"])
            if (request.base_url != saved_base_url
                    or request.api_protocol != settings.get("api_protocol", "openai_responses")):
                raise HTTPException(status_code=400, detail="API 根地址或协议已改变，请输入新连接的 API 密钥")
            api_key = secrets.api_key
        if not api_key:
            raise HTTPException(status_code=400, detail="请先输入 API 密钥")
        try:
            fetch = fetch_gemini_models if request.api_protocol == "gemini" else fetch_models
            models = await fetch(
                base_url=request.base_url,
                models_path=request.models_path,
                api_key=api_key,
                timeout_seconds=request.timeout_seconds,
            )
        except ModelServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return ModelsResult(models=models)

    @app.post("/api/settings/test", response_model=ConnectionTestResult)
    async def test_settings() -> ConnectionTestResult:
        settings = storage.get_settings()
        model = settings["extraction_model"]
        if not secrets.api_key:
            return ConnectionTestResult(ok=False, message="请先输入 API 密钥")
        if not model:
            return ConnectionTestResult(ok=False, message="请先填写模型名称")
        return ConnectionTestResult(
            ok=True,
            message="仅配置检查，未请求模型；实际图片与 Schema 能力由正常首页面请求确认",
        )

    @app.get("/api/books", response_model=list[Book])
    async def list_books() -> list[Book]:
        return storage.list_books()

    @app.post("/api/projects", response_model=Book, status_code=201)
    async def create_project(request: ProjectCreate) -> Book:
        return storage.create_project(str(uuid4()), request.title)

    @app.get("/api/books/{book_id}/arrangement", response_model=Arrangement)
    async def get_arrangement(book_id: str) -> Arrangement:
        return arrangement(book_id)

    @app.post("/api/books/{book_id}/files", response_model=UploadOutcome)
    async def upload_files(book_id: str, files: list[UploadFile] = File(...)) -> UploadOutcome:
        ensure_book(book_id)
        ensure_idle(book_id)
        if not files:
            raise HTTPException(status_code=400, detail="请选择上传文件")
        uploading.add(book_id)
        book_dir = storage.books_root / book_id
        staging = book_dir / f".upload-{uuid4()}"
        import_errors: list[ImportErrorDetail] = []
        try:
            staging.mkdir(parents=True)
            (book_dir / "sources").mkdir(exist_ok=True)
            for file in files:
                source_id = str(uuid4())
                directory = staging / source_id
                target = book_dir / "sources" / source_id
                filename = Path(file.filename or "").name[:255] or "未命名文件"
                moved = False
                try:
                    filename, data = await read_upload(file)
                    directory.mkdir()
                    pages = await asyncio.to_thread(import_document, data, filename, directory)
                    directory.rename(target)
                    moved = True
                    storage.append_files(book_id, [{
                        "id": source_id, "filename": filename,
                        "kind": "pdf" if filename.lower().endswith(".pdf") else "image",
                        "directory": f"sources/{source_id}", "pages": pages,
                    }])
                except Exception as exc:
                    # Only this unregistered source is removed; earlier successes stay saved.
                    if moved:
                        shutil.rmtree(target, ignore_errors=True)
                    reason = (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                              if isinstance(exc, ImportFailure) else "文件导入发生技术错误，其他成功文件已保留")
                    import_errors.append(ImportErrorDetail(filename=filename, reason=reason[:1_000]))
                    if not isinstance(exc, (HTTPException, ImportFailure)):
                        logger.error("Import failed (%s)", type(exc).__name__)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            uploading.discard(book_id)
        return UploadOutcome(**arrangement(book_id).model_dump(), import_errors=import_errors)

    @app.post("/api/books/{book_id}/confirm-upload", response_model=Arrangement)
    async def confirm_upload(book_id: str) -> Arrangement:
        book = ensure_book(book_id)
        ensure_idle(book_id)
        if not book.file_count:
            raise HTTPException(status_code=400, detail="请先上传文件")
        storage.confirm_upload(book_id)
        return arrangement(book_id)

    @app.put("/api/books/{book_id}/arrangement", response_model=BookDetail)
    async def put_arrangement(book_id: str, request: ArrangementUpdate) -> BookDetail:
        book = ensure_book(book_id)
        ensure_idle(book_id)
        file_ids = {source.id for source in storage.get_files(book_id)}
        if len(request.file_order) != len(file_ids) or set(request.file_order) != file_ids:
            raise HTTPException(status_code=400, detail="文件排序必须包含全部文件且不能重复")
        if request.file_parents is not None:
            if set(request.file_parents) != file_ids:
                raise HTTPException(status_code=400, detail="文件层级必须包含全部文件")
            for source_id, parent_id in request.file_parents.items():
                if parent_id is not None and parent_id not in file_ids:
                    raise HTTPException(status_code=400, detail="父文件必须属于当前项目")
                if parent_id == source_id:
                    raise HTTPException(status_code=400, detail="文件不能嵌入自身")
            for source_id in file_ids:
                seen: set[str] = set()
                current: str | None = source_id
                while current is not None:
                    if current in seen:
                        raise HTTPException(status_code=400, detail="文件层级不能形成循环")
                    seen.add(current)
                    current = request.file_parents[current]
            if any(
                parent_id is not None and request.file_parents[parent_id] is not None
                for parent_id in request.file_parents.values()
            ):
                raise HTTPException(status_code=400, detail="子文件必须属于顶层文件，不能继续嵌套子文件")
        numbers = set(request.page_order)
        if len(numbers) != len(request.page_order) or any(number < 1 or number > book.page_count for number in numbers):
            raise HTTPException(status_code=400, detail="页面排序包含重复或无效页面")
        storage.save_arrangement(book_id, request.file_order, request.page_order, request.file_parents)
        return book_detail(book_id)

    @app.get("/api/books/{book_id}/pages/{number}/preview")
    async def preview_page(book_id: str, number: int) -> FileResponse:
        ensure_book(book_id)
        record = storage.get_page_record(book_id, number)
        if record is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        try:
            async with preview_lock:
                path = await asyncio.to_thread(prepare_source_preview, storage.books_root / book_id, record)
        except ImportFailure as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(path, media_type="image/png")

    @app.post("/api/books", response_model=Book, status_code=201)
    async def upload_book(file: UploadFile = File(...)) -> Book:
        filename = Path(file.filename or "").name
        if not filename:
            raise HTTPException(status_code=400, detail="缺少文件名")
        if len(filename) > 255:
            raise HTTPException(status_code=400, detail="文件名过长")
        chunks: list[bytes] = []
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="文件不能超过 100 MB")
            chunks.append(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="文件为空")
        book_id = str(uuid4())
        book_dir = (storage.books_root / book_id).resolve()
        if book_dir.parent != storage.books_root.resolve():
            raise HTTPException(status_code=400, detail="书籍路径无效")
        book_dir.mkdir(parents=True)
        try:
            pages = await asyncio.to_thread(import_document, b"".join(chunks), filename, book_dir)
            return storage.create_book(book_id, Path(filename).stem, filename, pages)
        except ImportFailure as exc:
            shutil.rmtree(book_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            shutil.rmtree(book_dir, ignore_errors=True)
            raise

    @app.get("/api/books/{book_id}", response_model=BookDetail)
    async def get_book(book_id: str) -> BookDetail:
        return book_detail(book_id)

    @app.get("/api/books/{book_id}/overview", response_model=BookDetail)
    async def get_overview(book_id: str) -> BookDetail:
        return BookDetail(book=ensure_book(book_id), files=storage.get_files(book_id), pages=[])

    @app.get("/api/books/{book_id}/selection", response_model=SelectionDraft)
    async def get_selection(book_id: str) -> SelectionDraft:
        ensure_book(book_id)
        return storage.get_selection(book_id)

    @app.put("/api/books/{book_id}/selection", response_model=SelectionDraft)
    async def save_selection(book_id: str, update: SelectionUpdate) -> SelectionDraft:
        ensure_book(book_id)
        if book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在上传，请稍后保存选页")
        try:
            # The run already owns its frozen selection; this writes only the next draft.
            return storage.save_selection(book_id, update)
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/books/{book_id}/source-pages", response_model=SourcePageList)
    async def list_source_pages(
        book_id: str, offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500), source_id: str | None = None,
    ) -> SourcePageList:
        ensure_book(book_id)
        if source_id is not None and source_id not in {source.id for source in storage.get_files(book_id)}:
            raise HTTPException(status_code=404, detail="源文件不存在")
        return SourcePageList(items=storage.list_source_pages(book_id, offset, limit, source_id),
                              total=storage.count_source_pages(book_id, source_id), offset=offset, limit=limit)

    @app.get("/api/books/{book_id}/source-pages/{page_id}/preview")
    async def preview_source_page(
        book_id: str, page_id: str, run_id: str | None = None,
        source_version: int | None = Query(default=None, ge=1),
    ) -> FileResponse:
        ensure_book(book_id)
        if run_id is not None:
            run = ensure_run(book_id, run_id)
            if page_id not in run.page_ids:
                raise HTTPException(status_code=404, detail="源页面不属于该任务")
            try:
                record = storage.get_run_page_record(run_id, page_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="任务冻结源页面不存在") from exc
            task = storage.get_task(run_id, page_id)
            if task is None or record["source_version"] != task.source_version:
                raise HTTPException(status_code=409, detail="任务源页面版本与冻结范围不一致")
        else:
            record = storage.get_source_page_record(book_id, page_id)
        if record is None:
            raise HTTPException(status_code=404, detail="源页面不存在")
        if (record["book_id"], record["page_id"]) != (book_id, page_id):
            raise HTTPException(status_code=409, detail="源页面不属于该项目")
        if source_version is not None and source_version != record["source_version"]:
            raise HTTPException(status_code=409, detail="源页面版本与请求不一致，请重新加载来源")
        book_dir = (storage.books_root / book_id).resolve()
        try:
            async with preview_lock:
                path = await asyncio.to_thread(prepare_source_preview, book_dir, record)
        except ImportFailure as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path = path.resolve()
        if not path.is_relative_to(book_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="源页预览不存在")
        return FileResponse(path, media_type="image/png")

    @app.put("/api/books/{book_id}/layout", response_model=Book)
    async def put_layout(book_id: str, request: LayoutUpdate) -> Book:
        ensure_book(book_id)
        storage.save_layout(book_id, request.paper_size, request.layout, request.render_strategy)
        return ensure_book(book_id)

    @app.delete("/api/books/{book_id}", status_code=204)
    async def delete_book(book_id: str) -> Response:
        ensure_book(book_id)
        ensure_idle(book_id)
        storage.delete_book(book_id)
        return Response(status_code=204)

    @app.post("/api/books/{book_id}/runs", response_model=Run, status_code=201)
    async def create_run(book_id: str, request: RunCreate) -> Run:
        ensure_book(book_id)
        if book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在上传，请稍后启动任务")
        try:
            run = storage.create_run(book_id, request, generator_version=GENERATOR_VERSION)
        except RevisionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # A repeated client_request_id returns its original run, including a paused or finished one.
        return dispatch_run(run) if run.status == "queued" else run

    @app.get("/api/books/{book_id}/runs", response_model=list[Run])
    async def list_runs(book_id: str) -> list[Run]:
        ensure_book(book_id)
        return storage.list_runs(book_id)

    @app.get("/api/books/{book_id}/runs/summaries", response_model=list[RunSummary])
    async def list_run_summaries(book_id: str) -> list[RunSummary]:
        ensure_book(book_id)
        summaries = []
        for run in storage.list_runs(book_id):
            summary = storage.get_run_summary(book_id, run.run_id)
            if summary is not None:
                summaries.append(summary)
        return summaries

    @app.get("/api/books/{book_id}/runs/{run_id}", response_model=Run)
    async def get_run(book_id: str, run_id: str) -> Run:
        return ensure_run(book_id, run_id)

    @app.get("/api/books/{book_id}/runs/{run_id}/summary", response_model=RunSummary)
    async def get_run_summary(book_id: str, run_id: str) -> RunSummary:
        ensure_run(book_id, run_id)
        summary = storage.get_run_summary(book_id, run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return summary

    @app.get("/api/books/{book_id}/runs/{run_id}/outcomes", response_model=list[PageOutcomeSummary])
    async def get_run_outcomes(
        book_id: str, run_id: str, offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[PageOutcomeSummary]:
        ensure_run(book_id, run_id)
        return storage.get_run_outcomes(run_id, offset, limit)

    @app.get("/api/books/{book_id}/runs/{run_id}/pages/{page_id}/content", response_model=PageContent | None)
    async def get_run_content(book_id: str, run_id: str, page_id: str) -> PageContent | None:
        run = ensure_run(book_id, run_id)
        if page_id not in run.page_ids:
            raise HTTPException(status_code=404, detail="页面不属于该任务")
        task = storage.get_task(run_id, page_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务页面不存在")
        if task.candidate_revision_id is None:
            return None
        revision = storage.get_revision(task.candidate_revision_id)
        if revision is None or (revision.book_id, revision.page_id, revision.source_version) != (
                book_id, page_id, task.source_version):
            raise HTTPException(status_code=409, detail="任务内容修订与冻结来源不一致")
        if revision.workflow_version == 2 and revision.run_id != run_id:
            raise HTTPException(status_code=409, detail="任务内容修订不属于该运行")
        return revision.page_content

    @app.post("/api/books/{book_id}/runs/{run_id}/pause", response_model=Run)
    async def pause_run(book_id: str, run_id: str) -> Run:
        run = ensure_run(book_id, run_id)
        if run.status in {"paused", "pausing"}:
            return run
        if run.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="任务未在运行")
        if not is_running(book_id) or active_run_ids.get(book_id) != run_id:
            raise HTTPException(status_code=409, detail="任务没有活动处理进程")
        pause_requests[book_id].set()
        return storage.set_run_status(book_id, run_id, "pausing")

    @app.post("/api/books/{book_id}/runs/{run_id}/resume", response_model=Run)
    async def resume_run(book_id: str, run_id: str) -> Run:
        run = ensure_run(book_id, run_id)
        ensure_run_available(book_id, run_id)
        if run.status == "running" and is_running(book_id) and active_run_ids.get(book_id) == run_id:
            return run
        if run.status not in {"paused", "interrupted"}:
            raise HTTPException(status_code=409, detail="只能恢复已暂停或已中断的任务")
        if is_running(book_id):
            raise HTTPException(status_code=409, detail="任务正在结算，请稍后恢复")
        return dispatch_run(run)

    @app.get("/api/books/{book_id}/results", response_model=list[PageResult])
    async def list_results(
        book_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[PageResult]:
        ensure_book(book_id)
        return storage.list_results(book_id, offset, limit)

    @app.get("/api/books/{book_id}/issues", response_model=list[Issue])
    async def list_issues(
        book_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[Issue]:
        ensure_book(book_id)
        return storage.list_issues(book_id, offset, limit)

    @app.get("/api/books/{book_id}/output-snapshots/{snapshot_id}", response_model=OutputSnapshot)
    async def get_output_snapshot(book_id: str, snapshot_id: str) -> OutputSnapshot:
        return ensure_snapshot(book_id, snapshot_id)

    @app.get("/api/books/{book_id}/output-snapshots/{snapshot_id}/errors", response_model=OutputErrors)
    async def get_output_errors(book_id: str, snapshot_id: str) -> OutputErrors:
        snapshot = ensure_snapshot(book_id, snapshot_id)
        return OutputErrors(pdf=snapshot_errors(snapshot, "pdf"), latex=snapshot_errors(snapshot, "latex"))

    @app.get("/api/books/{book_id}/output-snapshots/{snapshot_id}/{format_name}")
    async def download_snapshot(
        book_id: str, snapshot_id: str, format_name: str, inline: bool = False,
    ) -> FileResponse:
        snapshot = ensure_snapshot(book_id, snapshot_id)
        if format_name not in {"json", "pdf", "latex"}:
            raise HTTPException(status_code=404, detail="输出格式不存在")
        output = snapshot.formats[format_name]
        if output.status != "available" or not output.asset:
            raise HTTPException(status_code=409, detail="该格式尚不可用，请查看输出状态和原因")
        book_dir = (storage.books_root / book_id).resolve()
        # A continuation may inherit an older snapshot's immutable PDF asset.
        path = (book_dir / output.asset).resolve()
        if not path.is_relative_to(book_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="冻结输出文件不存在")
        extension, media_type = {
            "json": ("json", "application/json"), "pdf": ("pdf", "application/pdf"),
            "latex": ("zip", "application/zip"),
        }[format_name]
        return FileResponse(path, media_type=media_type,
                            filename=f"book-{book_id}-{snapshot_id}.{extension}",
                            content_disposition_type="inline" if format_name == "pdf" and inline else "attachment")

    @app.post("/api/books/{book_id}/export-manifests", response_model=ExportManifest, status_code=201)
    async def create_manifest(book_id: str, request: ExportManifestCreate) -> ExportManifest:
        ensure_book(book_id)
        full_manifest_id = None
        if request.run_id is not None:
            run = ensure_run(book_id, request.run_id)
            if run.workflow_version == 2:
                raise HTTPException(status_code=409, detail="新版运行请读取其自动生成的输出快照")
            if run.status not in {"succeeded", "failed"} or any(task.result_status is None for task in run.tasks):
                raise HTTPException(status_code=409, detail="任务尚未形成最终结果，请等待结束或导出当前已保存稿")
            if request.expected_arrangement_revision != run.arrangement_revision:
                raise HTTPException(status_code=409, detail="导出页序修订与任务快照不一致")
            if request.page_ids is not None and not set(request.page_ids) <= set(run.page_ids):
                raise HTTPException(status_code=422, detail="导出范围包含任务快照之外的页面")
            if run.export_manifest_id is None:
                existing = storage.create_export_manifest(
                    book_id, expected_arrangement_revision=run.arrangement_revision, run_id=run.run_id,
                    generator_version=GENERATOR_VERSION,
                )
            else:
                existing = ensure_manifest(book_id, run.export_manifest_id)
            full_manifest_id = existing.manifest_id
            if request.page_ids is None or set(request.page_ids) == {page.page_id for page in existing.pages}:
                return await populate_manifest(existing)
        try:
            manifest = storage.create_export_manifest(
                book_id, expected_arrangement_revision=request.expected_arrangement_revision,
                page_ids=request.page_ids, run_id=request.run_id, generator_version=GENERATOR_VERSION,
            )
        except RevisionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if full_manifest_id is not None:
            # A scoped download must not replace the run's full automatic output link.
            storage.set_run_manifest(book_id, request.run_id, full_manifest_id)
        return await populate_manifest(manifest)

    @app.get("/api/books/{book_id}/export-manifests/{manifest_id}", response_model=ExportManifest)
    async def get_manifest(book_id: str, manifest_id: str) -> ExportManifest:
        return ensure_manifest(book_id, manifest_id)

    @app.get("/api/books/{book_id}/export-manifests/{manifest_id}/{format}")
    async def download_manifest(book_id: str, manifest_id: str, format: str) -> FileResponse:
        manifest = ensure_manifest(book_id, manifest_id)
        if format not in MANIFEST_FILE_KEYS:
            raise HTTPException(status_code=404, detail="导出格式不存在")
        relative = manifest.outputs.get(format)
        if not relative:
            raise HTTPException(status_code=404, detail="该格式尚未生成，请查看导出说明")
        book_dir = (storage.books_root / book_id).resolve()
        path = (book_dir / relative).resolve()
        if not path.is_relative_to(book_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="导出文件不存在")
        pdf = format in {"pdf", "partial_pdf"}
        extension = "pdf" if pdf else "zip" if format == "latex" else "json"
        return FileResponse(
            path, media_type="application/pdf" if pdf else "application/zip" if format == "latex" else "application/json",
            filename=f"book-{book_id}{'-partial' if format == 'partial_pdf' else ''}.{extension}",
            content_disposition_type="inline" if pdf else "attachment",
        )

    @app.post("/api/books/{book_id}/process", response_model=ProcessResult)
    async def process_book(book_id: str, request: ProcessRequest | None = None) -> ProcessResult:
        book = ensure_book(book_id)
        if not storage.get_pages(book_id):
            return ProcessResult(started=False)
        paused = next((run for run in storage.list_runs(book_id)
                       if run.workflow_version == 1 and run.status == "paused"), None)
        if paused is not None:
            requested = set(request.pages) if request is not None and request.pages is not None else None
            if requested is not None and requested != {task.page_number for task in paused.tasks}:
                raise HTTPException(status_code=409, detail="已有暂停任务；恢复时不能改变原处理范围")
            await resume_run(book_id, paused.run_id)
        else:
            await create_run(book_id, RunCreate(
                pages=request.pages if request is not None else None,
                expected_arrangement_revision=book.arrangement_revision, client_request_id=str(uuid4()),
            ))
        return ProcessResult(started=True)

    @app.post("/api/books/{book_id}/pause", response_model=PauseResult)
    async def pause_book(book_id: str) -> PauseResult:
        ensure_book(book_id)
        run_id = active_run_ids.get(book_id)
        if run_id is None:
            raise HTTPException(status_code=409, detail="该项目未在处理")
        await pause_run(book_id, run_id)
        return PauseResult(requested=True)

    @app.post("/api/books/{book_id}/pages", response_model=BookDetail)
    async def add_pages(book_id: str, request: PagesRequest) -> BookDetail:
        book = ensure_book(book_id)
        ensure_idle(book_id)
        numbers = sorted(set(request.pages))
        if any(number < 1 or number > book.page_count for number in numbers):
            raise HTTPException(status_code=400, detail="页码超出源文件范围")
        storage.add_pages(book_id, numbers)
        storage.refresh_book(book_id, processing=False)
        return book_detail(book_id)

    @app.delete("/api/books/{book_id}/pages/{number}", response_model=BookDetail)
    async def delete_page(book_id: str, number: int) -> BookDetail:
        ensure_book(book_id)
        ensure_idle(book_id)
        record = storage.get_page_record(book_id, number)
        if record is None or not record["selected"]:
            raise HTTPException(status_code=404, detail="页面不在当前清单中")
        storage.remove_page(book_id, number)
        storage.refresh_book(book_id, processing=False)
        return book_detail(book_id)

    @app.put("/api/books/{book_id}/pages/{number}", response_model=Page)
    async def save_page(book_id: str, number: int, update: PageUpdate) -> Page:
        ensure_book(book_id)
        if book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在上传，请稍后保存")
        record = storage.get_page_record(book_id, number)
        if record is None or not record["selected"]:
            raise HTTPException(status_code=404, detail="页面不存在")
        active = is_running(book_id)
        try:
            storage.save_manual_text(
                book_id, number, update.text,
                page_kind=update.page_kind, cover_fields=update.cover_fields,
                processing=active,
                expected_content_revision=update.expected_content_revision,
                expected_layout_revision=update.expected_layout_revision,
                render_strategy=update.render_strategy,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="页面不存在") from exc
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        pages = storage.get_pages(book_id)
        return next(page for page in pages if page.number == number)

    @app.put("/api/books/{book_id}/pages/{number}/layout", response_model=Page)
    async def save_page_layout(book_id: str, number: int, update: LayoutCalibrationUpdate) -> Page:
        draft = await calibrated_page(book_id, number, update)
        try:
            source = generate_source_fidelity_latex(draft.layout_source)
            saved = storage.save_page_layout(
                book_id, number, draft.layout_source, source,
                update.expected_content_revision, update.expected_layout_revision,
            )
        except RevisionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (FidelityLayoutError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return saved

    @app.post("/api/books/{book_id}/pages/{number}/compile-layout-pdf", response_model=PdfCompileResult)
    async def compile_page_layout(
        book_id: str, number: int, update: LayoutCalibrationUpdate, print_version: bool = False,
    ) -> PdfCompileResult:
        page = await calibrated_page(book_id, number, update)
        detail = book_detail(book_id)
        position = next(index + 1 for index, item in enumerate(detail.pages) if item.number == number)
        draft = detail.model_copy(update={"pages": [page]})
        return await compiled_pdf(draft, print_version, page_order=[position])

    @app.get("/api/books/{book_id}/export")
    async def export_book(book_id: str) -> JSONResponse:
        ensure_book(book_id)
        payload = book_detail(book_id).model_dump(mode="json")
        return JSONResponse(
            content=payload,
            headers={
                "Content-Disposition": f'attachment; filename="book-{book_id}.json"'
            },
        )

    @app.get("/api/books/{book_id}/export.tex")
    async def export_latex(book_id: str, print_version: bool = False) -> Response:
        ensure_book(book_id)
        book_dir = (storage.books_root / book_id).resolve()
        resources: dict[str, Path] = {}
        try:
            documents = build_latex_documents(book_detail(book_id), print_version)
            reserved_names = {"readme.txt", *(f"{index:04d}.tex" for index in range(1, len(documents) + 1))}
            for document in documents:
                for name in document.resource_names:
                    relative = source_resource_name(name)
                    path = (book_dir / relative).resolve()
                    if not path.is_relative_to(book_dir) or not path.is_file():
                        raise ValueError(f"引用的源资源不存在或超出书目录：{relative}")
                    if relative.casefold() in reserved_names:
                        raise ValueError(f"引用的源资源与源码包文件重名：{relative}")
                    resources[relative] = path
        except (LatexCompileError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if len(documents) == 1 and not resources:
            return Response(
                content=documents[0].source,
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="book-{book_id}.tex"'},
            )
        archive = BytesIO()
        instructions = [
            "请按以下顺序分别用 XeLaTeX 编译每份 .tex，再按相同顺序合并 PDF。",
            "独立文档保留原源码，连续片段页使用应用模板；引用资源保持包内的书目录相对路径。",
            "请在解压目录编译；完整自定义文档所需的额外宏包和字体需自行准备。",
            "",
            "文件\t对应原页面编排位置",
        ]
        try:
            with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
                for index, document in enumerate(documents, 1):
                    filename = f"{index:04d}.tex"
                    package.writestr(filename, document.source)
                    positions = ", ".join(str(position) for position in document.page_order)
                    instructions.append(f"{filename}\t{positions}")
                for relative, path in resources.items():
                    package.write(path, arcname=relative)
                package.writestr("README.txt", "\n".join(instructions) + "\n")
        except OSError as error:
            raise HTTPException(status_code=422, detail="无法读取引用的源资源，源码包未生成") from error
        return Response(
            content=archive.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="book-{book_id}.zip"'},
        )

    @app.post("/api/books/{book_id}/compile", response_model=PdfCompileResult)
    async def compile_book(book_id: str, print_version: bool = False) -> PdfCompileResult:
        ensure_book(book_id)
        return await compiled_pdf(book_detail(book_id), print_version)

    @app.post("/api/books/{book_id}/pages/{number}/compile", response_model=PdfCompileResult)
    async def compile_page(
        book_id: str, number: int, update: PageUpdate, print_version: bool = False,
    ) -> PdfCompileResult:
        detail = book_detail(book_id)
        page = next((page for page in detail.pages if page.number == number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        if ((update.expected_content_revision is not None and update.expected_content_revision != page.content_revision)
                or (update.expected_layout_revision is not None and update.expected_layout_revision != page.layout_revision)):
            raise HTTPException(status_code=409, detail="页面已更新，请加载最新版本后再编译草稿")
        kind = update.page_kind if update.page_kind is not None else page.page_kind
        if kind == "content":
            if update.cover_fields:
                raise HTTPException(status_code=422, detail="正文页不能包含封面书目信息")
            changes = {"text": update.text, "page_kind": kind, "cover_fields": []}
        else:
            if update.text:
                raise HTTPException(status_code=422, detail="封面和封底不能包含正文")
            changes = {
                "text": "", "page_kind": kind, "page_side": "unknown",
                "cover_fields": update.cover_fields if update.cover_fields is not None else page.cover_fields,
                "header_segments": [], "footer_segments": [],
            }
        detached = update.text != page.text or kind != page.page_kind
        if kind == "content" and detached:
            if update.render_strategy == "source_fidelity":
                raise HTTPException(status_code=422, detail="自由源码已修改，请通过布局校准重新生成原书还原源码")
            changes.update(render_strategy="custom_latex", generated_content_revision=None)
        elif update.render_strategy is not None:
            changes["render_strategy"] = update.render_strategy
        draft = detail.model_copy(update={"pages": [page.model_copy(update=changes)]})
        page_order = next(index + 1 for index, item in enumerate(detail.pages) if item.number == number)
        return await compiled_pdf(draft, print_version, page_order=[page_order])

    @app.get("/api/books/{book_id}/compiled/{digest}.pdf")
    async def get_compiled_pdf(book_id: str, digest: str) -> FileResponse:
        ensure_book(book_id)
        if not COMPILED_DIGEST.fullmatch(digest):
            raise HTTPException(status_code=404, detail="PDF 不存在")
        path = storage.books_root / book_id / "latex-cache" / digest / "document.pdf"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="PDF 不存在，请先生成")
        return FileResponse(
            path, media_type="application/pdf", filename=f"book-{book_id}.pdf",
            content_disposition_type="inline",
        )

    @app.get("/api/books/{book_id}/compiled/{digest}/pages/{output_page}.png")
    async def get_compiled_page(book_id: str, digest: str, output_page: int) -> FileResponse:
        ensure_book(book_id)
        if not COMPILED_DIGEST.fullmatch(digest):
            raise HTTPException(status_code=404, detail="PDF 不存在")
        path = storage.books_root / book_id / "latex-cache" / digest / "document.pdf"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="PDF 不存在，请先生成")
        try:
            async with preview_lock:
                image_path = await asyncio.to_thread(render_output_page, path, output_page)
        except IndexError as error:
            raise HTTPException(status_code=404, detail="输出页面不存在") from error
        return FileResponse(image_path, media_type="image/png")

    @app.get("/api/books/{book_id}/assets/{name}")
    async def get_asset(book_id: str, name: str) -> FileResponse:
        ensure_book(book_id)
        if not ASSET_NAME.fullmatch(name):
            raise HTTPException(status_code=404, detail="资源不存在")
        book_dir = (storage.books_root / book_id).resolve()
        path = (book_dir / name).resolve()
        if path.parent != book_dir or not path.is_file():
            raise HTTPException(status_code=404, detail="资源不存在")
        return FileResponse(path)

    app.state.storage = storage
    app.state.secrets = secrets
    app.state.running = running
    app.state.pause_requests = pause_requests
    app.state.active_run_ids = active_run_ids
    return app


app = create_app()
