from __future__ import annotations

import asyncio
import os
import re
import shutil
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .importers import ImportFailure, import_document, prepare_source_preview
from .latex_diagnostics import layout_warnings
from .latex_export import LatexCompileError, build_latex, compile_pdf
from .models import (
    Arrangement,
    ArrangementUpdate,
    Book,
    BookDetail,
    ConnectionTestResult,
    LayoutUpdate,
    ModelsRequest,
    ModelsResult,
    Page,
    PageUpdate,
    PagesRequest,
    PauseResult,
    ProcessRequest,
    ProcessResult,
    ProjectCreate,
    SettingsOut,
    SettingsUpdate,
)
from .pipeline import BookProcessor
from .gemini_client import fetch_gemini_models
from .model_client import create_model_client
from .responses_client import ModelServiceError, fetch_models
from .storage import Storage


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
COMPILED_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RuntimeSecrets:
    api_key: str = ""


class PdfCompileResult(BaseModel):
    pdf_url: str
    warnings: list[str]


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
    pending_pages: dict[str, list[int]] = {}
    manually_saved_pages: dict[str, set[int]] = {}
    uploading: set[str] = set()
    preview_lock = asyncio.Lock()
    compile_locks: dict[str, asyncio.Lock] = {}

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
        if is_running(book_id) or book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在处理或上传，请稍后操作")

    def ensure_arranged(book: Book) -> None:
        if not book.upload_confirmed or not book.selection_confirmed:
            raise HTTPException(status_code=409, detail="请先确认上传并完成页面编排")

    def latex_source(detail: BookDetail, print_version: bool) -> str:
        try:
            return build_latex(detail, print_version=print_version)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def compiled_pdf(
        detail: BookDetail, print_version: bool, page_order: list[int] | None = None,
    ) -> PdfCompileResult:
        source = latex_source(detail, print_version)
        digest = sha256(source.encode("utf-8")).hexdigest()
        output_dir = storage.books_root / detail.book.id / "latex-cache" / digest
        async with compile_locks.setdefault(detail.book.id, asyncio.Lock()):
            if not (output_dir / "document.pdf").is_file():
                try:
                    await compile_pdf(source, output_dir)
                except LatexCompileError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
        return PdfCompileResult(
            pdf_url=f"/api/books/{detail.book.id}/compiled/{digest}.pdf",
            warnings=layout_warnings(
                output_dir / "compiled.log", source,
                page_order if page_order is not None else list(range(1, len(detail.pages) + 1)),
            ),
        )

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
        try:
            client = create_model_client(settings, secrets.api_key, context_reuse_enabled=False)
            await client.test_connection(model)
        except ModelServiceError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        return ConnectionTestResult(ok=True, message="连接成功（文本请求，不代表视觉识别效果）")

    @app.get("/api/books", response_model=list[Book])
    async def list_books() -> list[Book]:
        return storage.list_books()

    @app.post("/api/projects", response_model=Book, status_code=201)
    async def create_project(request: ProjectCreate) -> Book:
        return storage.create_project(str(uuid4()), request.title)

    @app.get("/api/books/{book_id}/arrangement", response_model=Arrangement)
    async def get_arrangement(book_id: str) -> Arrangement:
        return arrangement(book_id)

    @app.post("/api/books/{book_id}/files", response_model=Arrangement)
    async def upload_files(book_id: str, files: list[UploadFile] = File(...)) -> Arrangement:
        ensure_book(book_id)
        ensure_idle(book_id)
        if not files:
            raise HTTPException(status_code=400, detail="请选择上传文件")
        uploading.add(book_id)
        book_dir = storage.books_root / book_id
        staging = book_dir / f".upload-{uuid4()}"
        moved: list[Path] = []
        try:
            staging.mkdir(parents=True)
            imported = []
            for file in files:
                filename, data = await read_upload(file)
                source_id = str(uuid4())
                directory = staging / source_id
                directory.mkdir()
                pages = await asyncio.to_thread(import_document, data, filename, directory)
                imported.append({
                    "id": source_id, "filename": filename,
                    "kind": "pdf" if filename.lower().endswith(".pdf") else "image",
                    "directory": f"sources/{source_id}", "pages": pages,
                })
            (book_dir / "sources").mkdir(exist_ok=True)
            for source in imported:
                target = book_dir / source["directory"]
                (staging / source["id"]).rename(target)
                moved.append(target)
            storage.append_files(book_id, imported)
        except BaseException as exc:
            for directory in moved:
                shutil.rmtree(directory)
            if isinstance(exc, ImportFailure):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            uploading.discard(book_id)
        return arrangement(book_id)

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
        if not book.upload_confirmed:
            raise HTTPException(status_code=409, detail="请先确认上传文件")
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

    @app.put("/api/books/{book_id}/layout", response_model=Book)
    async def put_layout(book_id: str, request: LayoutUpdate) -> Book:
        ensure_book(book_id)
        storage.save_layout(book_id, request.paper_size, request.layout)
        return ensure_book(book_id)

    @app.delete("/api/books/{book_id}", status_code=204)
    async def delete_book(book_id: str) -> Response:
        ensure_book(book_id)
        ensure_idle(book_id)
        storage.delete_book(book_id)
        return Response(status_code=204)

    @app.post("/api/books/{book_id}/process", response_model=ProcessResult)
    async def process_book(book_id: str, request: ProcessRequest | None = None) -> ProcessResult:
        book = ensure_book(book_id)
        ensure_idle(book_id)
        legacy_upload = all(source.id == "legacy" for source in storage.get_files(book_id)) and book.file_count == 1
        if not legacy_upload:
            ensure_arranged(book)
        records = storage.get_page_records(book_id)
        if request is not None and request.pages is not None:
            numbers = set(request.pages)
            if any(number < 1 or number > book.page_count for number in numbers):
                raise HTTPException(status_code=400, detail="页码超出源文件范围")
            records = [record for record in records if record["number"] in numbers]
            if len(records) != len(numbers):
                raise HTTPException(status_code=400, detail="所选页面不在当前清单中，请先添加页面")
        if not records:
            return ProcessResult(started=False)
        settings = storage.get_settings()
        if not secrets.api_key:
            raise HTTPException(status_code=400, detail="请先设置 API 密钥")
        if not settings["extraction_model"]:
            raise HTTPException(status_code=400, detail="请先填写页面代理模型")
        numbers = [record["number"] for record in records]
        if not book.selection_confirmed:
            storage.confirm_page_selection(book_id, numbers)
        processor = BookProcessor(storage)
        pause_requested = asyncio.Event()
        manually_saved_pages[book_id] = set()
        storage.begin_book(book_id)
        task = asyncio.create_task(processor.process(
            book_id, settings, secrets.api_key, pause_requested, records,
            pending_pages=numbers,
            manually_saved_pages=manually_saved_pages[book_id],
        ))
        running[book_id] = task
        pause_requests[book_id] = pause_requested
        pending_pages[book_id] = numbers

        def remove_finished(_task: asyncio.Task[None]) -> None:
            if running.get(book_id) is _task:
                running.pop(book_id, None)
                pause_requests.pop(book_id, None)
                pending_pages.pop(book_id, None)
                manually_saved_pages.pop(book_id, None)
            if not _task.cancelled():
                _task.exception()

        task.add_done_callback(remove_finished)
        return ProcessResult(started=True)

    @app.post("/api/books/{book_id}/pause", response_model=PauseResult)
    async def pause_book(book_id: str) -> PauseResult:
        ensure_book(book_id)
        task = running.get(book_id)
        if task is None or task.done():
            raise HTTPException(status_code=409, detail="该书籍未在处理")
        pause_requested = pause_requests[book_id]
        if not pause_requested.is_set():
            pause_requested.set()
            storage.request_pause(book_id)
        return PauseResult(requested=True)

    @app.post("/api/books/{book_id}/pages", response_model=BookDetail)
    async def add_pages(book_id: str, request: PagesRequest) -> BookDetail:
        book = ensure_book(book_id)
        if not book.selection_confirmed:
            raise HTTPException(status_code=400, detail="请先选择处理范围")
        numbers = sorted(set(request.pages))
        if any(number < 1 or number > book.page_count for number in numbers):
            raise HTTPException(status_code=400, detail="页码超出源文件范围")
        added = storage.add_pages(book_id, numbers)
        active = is_running(book_id)
        if active and not pause_requests[book_id].is_set():
            for number in added:
                record = storage.get_page_record(book_id, number)
                if record is not None and record["status"] != "ready":
                    pending_pages[book_id].append(number)
        storage.refresh_book(book_id, processing=active)
        return book_detail(book_id)

    @app.delete("/api/books/{book_id}/pages/{number}", response_model=BookDetail)
    async def delete_page(book_id: str, number: int) -> BookDetail:
        book = ensure_book(book_id)
        if not book.selection_confirmed:
            raise HTTPException(status_code=400, detail="请先选择处理范围")
        record = storage.get_page_record(book_id, number)
        if record is None or not record["selected"]:
            raise HTTPException(status_code=404, detail="页面不在当前清单中")
        if record["status"] == "processing":
            raise HTTPException(status_code=409, detail="当前页面正在处理，暂不能删除")
        storage.remove_page(book_id, number)
        if book_id in pending_pages:
            pending_pages[book_id][:] = [page for page in pending_pages[book_id] if page != number]
        storage.refresh_book(book_id, processing=is_running(book_id))
        return book_detail(book_id)

    @app.put("/api/books/{book_id}/pages/{number}", response_model=Page)
    async def save_page(book_id: str, number: int, update: PageUpdate) -> Page:
        ensure_arranged(ensure_book(book_id))
        if book_id in uploading:
            raise HTTPException(status_code=409, detail="项目正在上传，请稍后保存")
        record = storage.get_page_record(book_id, number)
        if record is None or not record["selected"]:
            raise HTTPException(status_code=404, detail="页面不存在")
        if record["status"] == "processing":
            raise HTTPException(status_code=409, detail="本页正在识别，暂不能保存；可校对其他页面")
        active = is_running(book_id)
        try:
            storage.save_manual_text(
                book_id, number, update.text,
                page_kind=update.page_kind, cover_fields=update.cover_fields,
                processing=active,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="页面不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if active:
            manually_saved_pages[book_id].add(number)
        pages = storage.get_pages(book_id)
        return next(page for page in pages if page.number == number)

    @app.get("/api/books/{book_id}/export")
    async def export_book(book_id: str) -> JSONResponse:
        book = ensure_book(book_id)
        ensure_arranged(book)
        payload = book_detail(book_id).model_dump(mode="json")
        return JSONResponse(
            content=payload,
            headers={
                "Content-Disposition": f'attachment; filename="book-{book_id}.json"'
            },
        )

    @app.get("/api/books/{book_id}/export.tex")
    async def export_latex(book_id: str, print_version: bool = False) -> Response:
        book = ensure_book(book_id)
        ensure_arranged(book)
        return Response(
            content=latex_source(book_detail(book_id), print_version),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="book-{book_id}.tex"'},
        )

    @app.post("/api/books/{book_id}/compile")
    async def compile_book(book_id: str, print_version: bool = False) -> PdfCompileResult:
        ensure_arranged(ensure_book(book_id))
        return await compiled_pdf(book_detail(book_id), print_version)

    @app.post("/api/books/{book_id}/pages/{number}/compile")
    async def compile_page(
        book_id: str, number: int, update: PageUpdate, print_version: bool = False,
    ) -> PdfCompileResult:
        detail = book_detail(book_id)
        ensure_arranged(detail.book)
        page = next((page for page in detail.pages if page.number == number), None)
        if page is None:
            raise HTTPException(status_code=404, detail="页面不存在")
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
    app.state.pending_pages = pending_pages
    return app


app = create_app()
