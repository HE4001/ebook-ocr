from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from typing import Any

from .importers import prepare_source_page
from .models import StructuredPageResult
from .prompts import PAGE_AGENT_PROMPT, PAGE_CONTEXT_AGENT_PROMPT, page_context
from .responses_client import ModelServiceError, ResponsesClient, ResponsesConfig
from .storage import Storage


_render_lock = threading.Lock()


def _prepare_page(book_dir: Path, record: dict[str, Any]) -> tuple[int, int, str]:
    # PyMuPDF cannot render concurrently, including across different projects.
    with _render_lock:
        return prepare_source_page(book_dir, record)


class PageAgent:
    """每次只提交当前页；实验模式的历史由组内 client 管理。"""

    def __init__(
        self, client: ResponsesClient, storage: Storage, context_reuse_enabled: bool = False,
    ):
        self.client = client
        self.storage = storage
        self.prompt = PAGE_CONTEXT_AGENT_PROMPT if context_reuse_enabled else PAGE_AGENT_PROMPT

    async def run(
        self, book_id: str, number: int, total: int, filename: str,
        model: str, image_path: Path, source_page: int | None = None,
    ) -> StructuredPageResult:
        image_data = await asyncio.to_thread(
            lambda: base64.b64encode(image_path.read_bytes()).decode("ascii")
        )
        input_value = [
            {"role": "system", "content": [{"type": "input_text", "text": self.prompt}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": page_context(filename, source_page or number, total)},
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
        pending_pages: list[int] | None = None,
    ) -> None:
        context_reuse_enabled = settings.get("context_reuse_enabled", False)
        group_size = settings.get("context_reuse_max_pages", 10) if context_reuse_enabled else 1
        concurrency = settings.get("processing_concurrency", 10)
        config = ResponsesConfig(
            base_url=settings["base_url"],
            responses_path=settings["responses_path"],
            api_key=api_key,
            structured_output=settings["structured_output"],
            timeout_seconds=settings["timeout_seconds"],
            reasoning_effort=settings["reasoning_effort"],
            context_reuse_enabled=context_reuse_enabled,
        )
        if pause_requested is None or not pause_requested.is_set():
            self.storage.begin_book(book_id)
        if pending_pages is None:
            if records is None:
                records = self.storage.get_page_records(book_id)
            pending_pages = [record["number"] for record in records]
        book_dir = self.storage.books_root / book_id
        reserved_pages: dict[int, list[int]] = {}

        def paused() -> bool:
            return pause_requested is not None and pause_requested.is_set()

        async def worker() -> None:
            while pending_pages:
                if paused():
                    return
                # Reserve consecutive pages before yielding so each session follows
                # the arranged order, rather than alternating pages between workers.
                group = pending_pages[:group_size]
                del pending_pages[:group_size]
                reserved_pages.update((number, group) for number in group)
                client = ResponsesClient(config)
                agent = PageAgent(client, self.storage, context_reuse_enabled)
                for number in group:
                    if paused():
                        return
                    if reserved_pages.get(number) is not group:
                        continue
                    reserved_pages.pop(number)
                    record = self.storage.get_page_record(book_id, number)
                    # A removed and re-added page belongs to its new queue position.
                    if number in pending_pages or record is None or not record["selected"]:
                        continue
                    try:
                        self.storage.set_page_status(book_id, number, "processing")
                        width, height, image_name = await asyncio.to_thread(
                            _prepare_page, book_dir, record
                        )
                        self.storage.update_page_image(book_id, number, width, height, image_name)
                        result = await agent.run(
                            book_id, number, record["source_page_count"], record["source_filename"],
                            settings["extraction_model"], book_dir / image_name, record["source_page"],
                        )
                        self.storage.save_page_result(book_id, number, result)
                    except (ModelServiceError, ValueError, OSError) as exc:
                        self.storage.fail_page(book_id, number, _safe_error(exc))
                        if context_reuse_enabled:
                            client.reset_context()
                    except asyncio.CancelledError:
                        self.storage.interrupt_page(book_id, number)
                        raise

        try:
            async with asyncio.TaskGroup() as tasks:
                for _ in range(min(concurrency, len(pending_pages))):
                    tasks.create_task(worker())
        except BaseException:
            self.storage.finish_book(book_id)
            raise
        else:
            if (paused()
                    and (pending_pages or any(
                        page["number"] in reserved_pages or page["status"] != "ready"
                        for page in self.storage.get_page_records(book_id)
                    ))):
                self.storage.pause_book(book_id)
            else:
                self.storage.finish_book(book_id)


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip()
    if isinstance(exc, (ModelServiceError, ValueError)) and text:
        return text[:1000]
    return "页面处理失败"
