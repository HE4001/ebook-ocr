from __future__ import annotations

import asyncio
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from .importers import ImportFailure, import_document
from .models import (
    Book,
    BookDetail,
    ConnectionTestResult,
    Page,
    PageUpdate,
    PauseResult,
    ProcessResult,
    SettingsOut,
    SettingsUpdate,
)
from .pipeline import BookProcessor
from .responses_client import ModelServiceError, ResponsesClient, ResponsesConfig
from .storage import Storage


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class RuntimeSecrets:
    api_key: str = ""


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
        settings.update(values)
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

    @app.post("/api/settings/test", response_model=ConnectionTestResult)
    async def test_settings() -> ConnectionTestResult:
        settings = storage.get_settings()
        model = settings["extraction_model"]
        if not secrets.api_key:
            return ConnectionTestResult(ok=False, message="请先输入 API 密钥")
        if not model:
            return ConnectionTestResult(ok=False, message="请先填写模型名称")
        client = ResponsesClient(
            ResponsesConfig(
                base_url=settings["base_url"],
                responses_path=settings["responses_path"],
                api_key=secrets.api_key,
                structured_output=settings["structured_output"],
                timeout_seconds=settings["timeout_seconds"],
                max_output_tokens=settings["max_output_tokens"],
                reasoning_effort=settings["reasoning_effort"],
            )
        )
        try:
            await client.test_connection(model)
        except ModelServiceError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        return ConnectionTestResult(ok=True, message="连接成功（文本请求，不代表视觉识别效果）")

    @app.get("/api/books", response_model=list[Book])
    async def list_books() -> list[Book]:
        return storage.list_books()

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
        book = ensure_book(book_id)
        return BookDetail(book=book, pages=storage.get_pages(book_id))

    @app.delete("/api/books/{book_id}", status_code=204)
    async def delete_book(book_id: str) -> Response:
        ensure_book(book_id)
        if book_id in running:
            raise HTTPException(status_code=409, detail="该书籍正在处理")
        storage.delete_book(book_id)
        return Response(status_code=204)

    @app.post("/api/books/{book_id}/process", response_model=ProcessResult)
    async def process_book(book_id: str) -> ProcessResult:
        ensure_book(book_id)
        if book_id in running:
            raise HTTPException(status_code=409, detail="该书籍正在处理")
        if not storage.get_unfinished_page_records(book_id):
            return ProcessResult(started=False)
        settings = storage.get_settings()
        if not secrets.api_key:
            raise HTTPException(status_code=400, detail="请先设置 API 密钥")
        if not settings["extraction_model"]:
            raise HTTPException(status_code=400, detail="请先填写页面代理模型")
        processor = BookProcessor(storage)
        pause_requested = asyncio.Event()
        storage.begin_book(book_id)
        task = asyncio.create_task(processor.process(
            book_id, settings, secrets.api_key, pause_requested,
        ))
        running[book_id] = task
        pause_requests[book_id] = pause_requested

        def remove_finished(_task: asyncio.Task[None]) -> None:
            running.pop(book_id, None)
            pause_requests.pop(book_id, None)
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

    @app.put("/api/books/{book_id}/pages/{number}", response_model=Page)
    async def save_page(book_id: str, number: int, update: PageUpdate) -> Page:
        ensure_book(book_id)
        if book_id in running:
            raise HTTPException(status_code=409, detail="处理运行中，暂不能修改")
        try:
            storage.save_manual_text(book_id, number, update.text)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="页面不存在") from exc
        pages = storage.get_pages(book_id)
        return next(page for page in pages if page.number == number)

    @app.get("/api/books/{book_id}/export")
    async def export_book(book_id: str) -> JSONResponse:
        book = ensure_book(book_id)
        payload = BookDetail(book=book, pages=storage.get_pages(book_id)).model_dump(mode="json")
        return JSONResponse(
            content=payload,
            headers={
                "Content-Disposition": f'attachment; filename="book-{book_id}.json"'
            },
        )

    @app.get("/api/books/{book_id}/export.md")
    async def export_markdown(book_id: str) -> Response:
        book = ensure_book(book_id)
        pages = storage.get_pages(book_id)
        body = "# " + book.title + "\n\n" + "\n\n---\n\n".join(
            f"## 第 {page.number} 页\n\n{page.text}" for page in pages
        ) + "\n"
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="book-{book_id}.md"'},
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
    return app


app = create_app()
