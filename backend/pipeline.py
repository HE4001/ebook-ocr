from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from typing import Any

from .importers import prepare_source_page, read_page_source_metadata
from .gemini_client import GeminiClient
from .latex_content import is_latex_document
from .latex_export import GENERATOR_VERSION, PAPER_SIZES, FidelityLayoutError, generate_source_fidelity_latex
from .layout_contract import PageSourceMetadata, SourceFidelityLayout
from .model_client import create_model_client
from .models import Book, StructuredPageResult
from .prompts import PAGE_AGENT_PROMPT, PAGE_CONTEXT_AGENT_PROMPT, PAGE_RESPONSE_VERSION, page_context
from .responses_client import ModelServiceError, ResponsesClient
from .storage import Storage


_render_lock = threading.Lock()


def _prepare_page(book_dir: Path, record: dict[str, Any]) -> tuple[int, int, str, PageSourceMetadata]:
    # PyMuPDF cannot render concurrently, including across different projects.
    with _render_lock:
        width, height, image_name = prepare_source_page(book_dir, record)
        metadata = read_page_source_metadata(
            book_dir, {**record, "width": width, "height": height, "image_name": image_name},
        )
        return width, height, image_name, metadata


def _source_layout(
    result: StructuredPageResult, metadata: PageSourceMetadata, book: Book,
    content_revision: int, layout_revision: int,
) -> SourceFidelityLayout:
    observation = result.layout
    assert observation is not None
    paper_width_mm, _, _, default_font_size = PAPER_SIZES[book.paper_size]
    # At most two program reasons here, plus one missing-geometry reason below.
    reasons = list(observation.review_reasons)
    if metadata.pdf_geometry is not None:
        geometry = metadata.pdf_geometry
        width, height = geometry.width_bp, geometry.height_bp
        if geometry.rotation in (90, 270):
            width, height = height, width
        canvas_basis = "file_metadata"
    else:
        width = paper_width_mm * 72 / 25.4
        height = width * metadata.canonical_height_px / metadata.canonical_width_px
        canvas_basis = "project"
        reasons.append("原图没有可信物理尺寸：画布采用项目纸宽并保持原图长宽比，待人工校准。")
    reasons.append("正文基准字体及字号采用项目设置，原书字体与字号待人工校准。")
    observation.review_reasons = reasons
    return SourceFidelityLayout(
        **observation.model_dump(),
        source=metadata, content_revision=content_revision, layout_revision=layout_revision,
        generated_content_revision=None if is_latex_document(result.body_latex) else content_revision,
        generator_version=GENERATOR_VERSION,
        canvas_width_bp=width, canvas_height_bp=height, canvas_basis=canvas_basis,
        body_font_size_bp=book.layout.font_size_pt or default_font_size,
        body_font_family=book.layout.font_family, body_font_basis="project",
    )


class PageAgent:
    """每次只提交当前页；实验模式的历史由组内 client 管理。"""

    def __init__(
        self, client: ResponsesClient | GeminiClient, storage: Storage, context_reuse_enabled: bool = False,
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
        source_context = page_context(filename, source_page or number, total)
        input_value = [
            {"role": "system", "content": [{"type": "input_text", "text": self.prompt}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": source_context},
                {"type": "input_image", "image_url": f"data:image/png;base64,{image_data}",
                 "detail": "high"},
            ]},
        ]
        result = await self.client.request_page(
            model, input_value,
            on_attempt_start=lambda: self.storage.begin_attempt(book_id, number),
            on_attempt_end=lambda attempt, usage, returned: self.storage.finish_attempt(
                book_id, number, attempt, usage, returned
            ),
            response_version=PAGE_RESPONSE_VERSION,
        )
        return result


class BookProcessor:
    def __init__(self, storage: Storage):
        self.storage = storage

    async def process(
        self, book_id: str, settings: dict[str, Any], api_key: str,
        pause_requested: asyncio.Event | None = None,
        records: list[dict[str, Any]] | None = None,
        pending_pages: list[int] | None = None,
        manually_saved_pages: set[int] | None = None,
    ) -> None:
        context_reuse_enabled = settings.get("context_reuse_enabled", False)
        group_size = settings.get("context_reuse_max_pages", 10) if context_reuse_enabled else 1
        concurrency = settings.get("processing_concurrency", 10)
        if manually_saved_pages is None:
            manually_saved_pages = set()
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
                client = create_model_client(settings, api_key)
                agent = PageAgent(client, self.storage, context_reuse_enabled)
                for number in group:
                    if paused():
                        return
                    if reserved_pages.get(number) is not group:
                        continue
                    reserved_pages.pop(number)
                    record = self.storage.get_page_record(book_id, number)
                    # A removed and re-added page belongs to its new queue position.
                    # A manual save supersedes this run's queued OCR, including
                    # pages already reserved by a context-reuse group.
                    if (number in pending_pages or number in manually_saved_pages
                            or record is None or not record["selected"]):
                        continue
                    revision = record["content_revision"]
                    try:
                        if not self.storage.set_page_status(
                            book_id, number, "processing", expected_content_revision=revision,
                        ):
                            continue
                        width, height, image_name, metadata = await asyncio.to_thread(
                            _prepare_page, book_dir, record
                        )
                        self.storage.update_page_image(book_id, number, width, height, image_name)
                        result = await agent.run(
                            book_id, number, record["source_page_count"], record["source_filename"],
                            settings["extraction_model"], book_dir / image_name, record["source_page"],
                        )
                        layout_source = None
                        generated_text = None
                        strategy = record["render_strategy"] if result.page_kind == "content" else "legacy_template"
                        if result.page_kind == "content" and result.layout is not None:
                            book = self.storage.get_book(book_id)
                            assert book is not None
                            layout_source = _source_layout(
                                result, metadata, book, revision + 1, record["layout_revision"] + 1,
                            )
                            if is_latex_document(result.body_latex):
                                strategy = "custom_latex"
                            else:
                                strategy = "source_fidelity"
                                try:
                                    generated_text = generate_source_fidelity_latex(layout_source)
                                except FidelityLayoutError as exc:
                                    # Valid observations with unknown positions remain editable for calibration.
                                    result.layout.review_reasons.append(str(exc))
                                    layout_source.review_reasons = list(result.layout.review_reasons)
                                    layout_source.generated_content_revision = None
                        elif is_latex_document(result.body_latex):
                            strategy = "custom_latex"
                        saved = self.storage.save_page_result(
                            book_id, number, result, expected_content_revision=revision,
                            source_metadata=metadata, generated_text=generated_text,
                            generator_version=GENERATOR_VERSION if layout_source is not None else None,
                            render_strategy=strategy, layout_source=layout_source,
                        )
                        if not saved and context_reuse_enabled:
                            client.reset_context()
                    except (ModelServiceError, ValueError, OSError) as exc:
                        self.storage.fail_page(
                            book_id, number, _safe_error(exc), expected_content_revision=revision,
                        )
                        if context_reuse_enabled:
                            client.reset_context()
                    except asyncio.CancelledError:
                        self.storage.interrupt_page(book_id, number, expected_content_revision=revision)
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
