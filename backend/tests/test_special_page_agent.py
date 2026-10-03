import asyncio
import json
import tempfile
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from backend.pipeline import BookProcessor
from backend.prompts import PAGE_CONTEXT_AGENT_PROMPT, PAGE_RESPONSE_SCHEMA
from backend.storage import Storage
from backend.tests.layout_response_fixtures import page_response


def response(value, response_id, tokens=3):
    return httpx.Response(200, json={
        "id": response_id, "status": "completed",
        "usage": {"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1},
        "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps(value, ensure_ascii=False),
        }]}],
    })


@contextmanager
def book(page_count=1, context=False):
    with tempfile.TemporaryDirectory() as directory:
        storage = Storage(Path(directory))
        storage.initialize()
        book_dir = storage.books_root / "special-test"
        book_dir.mkdir()
        image = BytesIO()
        Image.new("RGB", (32, 24), "white").save(image, format="PNG")
        (book_dir / "source.png").write_bytes(image.getvalue())
        pages = []
        for number in range(1, page_count + 1):
            name = f"page-{number:04d}.png"
            (book_dir / name).write_bytes(image.getvalue())
            pages.append((number, 32, 24, name))
        storage.create_book("special-test", "测试书", "source.png", pages)
        settings = storage.get_settings()
        settings.update({
            "base_url": "https://model.invalid/v1", "responses_path": "/responses",
            "extraction_model": "vision-test", "reasoning_effort": "high",
            "processing_concurrency": 1, "context_reuse_enabled": context,
            "context_reuse_max_pages": 10,
        })
        yield storage, settings


def fake_client(responses, requests, on_request=None):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            requests.append((url, kwargs["json"]))
            if on_request is not None:
                on_request(len(requests))
            return responses.pop(0)

    return FakeClient


class SpecialPageAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_uses_only_normal_request(self):
        with book() as (storage, settings):
            requests = []
            value = page_response(text="普通正文", footer_segments=[{
                "kind": "page_number", "text": "2", "alignment": "left", "row": 1,
                "font_size": "small", "bold": False, "italic": False,
            }])
            responses = [response(value, "normal-1")]
            with patch("backend.responses_client.httpx.AsyncClient", fake_client(responses, requests)):
                await BookProcessor(storage).process("special-test", settings, "secret")
            page = storage.get_pages("special-test")[0]
            self.assertEqual((page.status, page.page_kind, page.attempts), ("ready", "content", 1))
            self.assertIn("普通正文", page.text)
            self.assertEqual(page.layout_source.lines[0].latex, "普通正文")
            self.assertEqual(page.cover_fields, [])
            self.assertEqual(page.page_side, "left")
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][1]["text"]["format"]["schema"], PAGE_RESPONSE_SCHEMA)

    async def test_covers_use_one_request_and_keep_context_chain(self):
        for kind in ("front_cover", "back_cover"):
            with self.subTest(kind=kind), book(2, context=True) as (storage, settings):
                requests = []
                fields = [{"kind": "title", "text": "图中书名"},
                          {"kind": "publisher", "text": "图中出版社"}]
                responses = [
                    response(page_response(kind=kind, cover_fields=fields), "cover-1", 5),
                    response(page_response(text="下一页正文"), "normal-2"),
                ]
                with patch("backend.responses_client.httpx.AsyncClient", fake_client(responses, requests)):
                    await BookProcessor(storage).process("special-test", settings, "secret")
                self.assertEqual(len(requests), 2)
                cover_request, following = [payload for _, payload in requests]
                self.assertEqual([url for url, _ in requests],
                                 ["https://model.invalid/v1/responses"] * 2)
                self.assertTrue(cover_request["store"])
                self.assertNotIn("previous_response_id", cover_request)
                self.assertEqual(following["previous_response_id"], "cover-1")
                self.assertEqual(len(cover_request["input"]), 2)
                self.assertEqual(cover_request["input"][0]["content"][0]["text"], PAGE_CONTEXT_AGENT_PROMPT)
                self.assertIn("source.png", cover_request["input"][1]["content"][0]["text"])
                self.assertEqual(cover_request["reasoning"], {"effort": "high"})
                self.assertEqual(cover_request["text"]["format"]["schema"], PAGE_RESPONSE_SCHEMA)
                cover, content = storage.get_pages("special-test")
                self.assertEqual((cover.status, cover.page_kind, cover.text, cover.attempts),
                                 ("ready", kind, "", 1))
                self.assertEqual([field.model_dump() for field in cover.cover_fields], fields)
                self.assertEqual(cover.page_side, "unknown")
                self.assertEqual(cover.usage.model_dump(), {
                    "input_tokens": 5, "output_tokens": 1, "total_tokens": 6, "complete": True,
                })
                self.assertEqual((content.status, content.attempts), ("ready", 1))
                self.assertIn("下一页正文", content.text)

    async def test_page_failure_preserves_saved_result_and_counts_retry_usage(self):
        with book() as (storage, settings):
            storage.save_manual_text("special-test", 1, "此前已校对正文")
            requests = []
            responses = [httpx.Response(status, json={"usage": {
                "input_tokens": 4, "output_tokens": 1, "total_tokens": 5,
            }}) for status in (503, 400)]

            def assert_old_result(_request_number):
                page = storage.get_pages("special-test")[0]
                self.assertEqual((page.text, page.page_kind), ("此前已校对正文", "content"))

            with patch("backend.responses_client.httpx.AsyncClient",
                       fake_client(responses, requests, assert_old_result)), patch(
                "backend.responses_client.asyncio.sleep", return_value=None,
            ):
                await BookProcessor(storage).process("special-test", settings, "secret")
            page = storage.get_pages("special-test")[0]
            self.assertEqual((page.status, page.text, page.page_kind, page.attempts),
                             ("failed", "此前已校对正文", "content", 2))
            self.assertEqual(page.usage.model_dump(), {
                "input_tokens": 8, "output_tokens": 2, "total_tokens": 10, "complete": True,
            })
            self.assertEqual(len(requests), 2)

    async def test_pause_finishes_special_page_before_stopping(self):
        with book(2, context=True) as (storage, settings):
            pause = asyncio.Event()
            requests = []
            responses = [
                response(page_response(kind="back_cover"), "cover-1"),
            ]

            def request_pause(number):
                if number == 1:
                    pause.set()

            with patch("backend.responses_client.httpx.AsyncClient",
                       fake_client(responses, requests, request_pause)):
                await BookProcessor(storage).process("special-test", settings, "secret", pause)
            first, second = storage.get_pages("special-test")
            self.assertEqual((first.status, first.page_kind, first.attempts),
                             ("ready", "back_cover", 1))
            self.assertEqual((second.status, second.attempts), ("uploaded", 0))
            self.assertEqual(storage.get_book("special-test").status, "paused")
            self.assertEqual(len(requests), 1)
