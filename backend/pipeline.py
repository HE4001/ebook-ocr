from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from .importers import ensure_pdf_pages
from .models import StructuredPageResult
from .prompts import PAGE_AGENT_PROMPT, page_context
from .responses_client import ModelServiceError, ResponsesClient, ResponsesConfig
from .storage import Storage


class PageAgent:
    """单页、单次 Responses 请求；不携带相邻页面或历史对话。"""

    def __init__(self, client: ResponsesClient, storage: Storage):
        self.client = client
        self.storage = storage

    async def run(
        self, book_id: str, number: int, total: int, filename: str,
        model: str, image_path: Path,
    ) -> StructuredPageResult:
        image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        input_value = [
            {"role": "system", "content": [{"type": "input_text", "text": PAGE_AGENT_PROMPT}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": page_context(filename, number, total)},
                {"type": "input_image", "image_url": f"data:image/png;base64,{image_data}",
                 "detail": "high"},
            ]},
        ]
        return await self.client.request_page(
            model, input_value,
            on_attempt_start=lambda: self.storage.begin_attempt(book_id, number),
            on_attempt_end=lambda attempt, usage, returned: self.storage.finish_attempt(
                book_id, number, attempt, usage, returned
            ),
        )


class BookProcessor:
    def __init__(self, storage: Storage):
        self.storage = storage

    async def process(
        self, book_id: str, settings: dict[str, Any], api_key: str,
        pause_requested: asyncio.Event | None = None,
        records: list[dict[str, Any]] | None = None,
    ) -> None:
        client = ResponsesClient(ResponsesConfig(
            base_url=settings["base_url"],
            responses_path=settings["responses_path"],
            api_key=api_key,
            structured_output=settings["structured_output"],
            timeout_seconds=settings["timeout_seconds"],
            max_output_tokens=settings["max_output_tokens"],
            reasoning_effort=settings["reasoning_effort"],
        ))
        if pause_requested is None or not pause_requested.is_set():
            self.storage.begin_book(book_id)
        if records is None:
            records = self.storage.get_page_records(book_id)
        book = self.storage.get_book(book_id)
        assert book is not None
        current_number: int | None = None
        try:
            completed = 0
            for record in records:
                if pause_requested is not None and pause_requested.is_set():
                    break
                current_number = record["number"]
                try:
                    self.storage.set_page_status(book_id, current_number, "processing")
                    book_dir = self.storage.books_root / book_id
                    if book.filename.lower().endswith(".pdf"):
                        single_page = book_dir / f"page-{current_number:04d}.pdf"
                        if not single_page.is_file():
                            await asyncio.to_thread(ensure_pdf_pages, book_dir, [current_number])
                    result = await PageAgent(client, self.storage).run(
                        book_id, current_number, book.page_count, book.filename,
                        settings["extraction_model"], book_dir / record["image_name"],
                    )
                    self.storage.save_page_result(book_id, current_number, result)
                except (ModelServiceError, ValueError, OSError) as exc:
                    self.storage.fail_page(book_id, current_number, _safe_error(exc))
                completed += 1
            if pause_requested is not None and pause_requested.is_set() and completed < len(records):
                self.storage.pause_book(book_id)
            else:
                self.storage.finish_book(book_id)
        except asyncio.CancelledError:
            if current_number is not None:
                self.storage.interrupt_page(book_id, current_number)
            self.storage.finish_book(book_id)
            raise


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip()
    if isinstance(exc, (ModelServiceError, ValueError)) and text:
        return text[:1000]
    return "页面处理失败"
