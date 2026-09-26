import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from backend.gemini_client import GeminiClient, GeminiConfig
from backend.models import StructuredPageResult
from backend.pipeline import BookProcessor
from backend.prompts import MARGIN_SEGMENT_SCHEMA, PAGE_RESPONSE_SCHEMA
from backend.responses_client import ModelServiceError, ResponsesClient, ResponsesConfig
from backend.storage import Storage


def page_result():
    segment = {"kind": "page_number", "text": "2", "alignment": "left", "row": 2,
               "font_size": "normal", "bold": True, "italic": True}
    return {"page_kind": "content", "page_side": "left", "header_segments": [segment],
            "body_markdown": "原文", "footer_segments": [dict(segment)]}


def response(protocol, value):
    text = json.dumps(value, ensure_ascii=False)
    if protocol == "gemini":
        return {"candidates": [{"finishReason": "STOP", "content": {
            "role": "model", "parts": [{"text": text}],
        }}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}}
    return {"id": "response-1", "status": "completed", "output_text": text,
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}


def model_client(protocol):
    if protocol == "gemini":
        return GeminiClient(GeminiConfig("https://model.invalid", "/models", "test", 15,
                                         context_reuse_enabled=True))
    return ResponsesClient(ResponsesConfig("https://model.invalid", "/responses", "test", True, 15,
                                           context_reuse_enabled=True))


PAGE_INPUT = [
    {"role": "system", "content": [{"type": "input_text", "text": "OCR"}]},
    {"role": "user", "content": [{"type": "input_text", "text": "当前页"}]},
]


class ModelResponseValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_fields_fail_without_changing_context(self):
        for protocol in ("openai_responses", "gemini"):
            client = model_client(protocol)
            client._post = AsyncMock(return_value=response(protocol, page_result()))
            result = await client.request_page("test-model", PAGE_INPUT)
            self.assertEqual(result.page_side, "left")
            self.assertEqual(result.footer_segments[0].row, 2)
            original_context = copy.deepcopy(client.contents) if protocol == "gemini" else client.previous_response_id

            cases = []
            for field in PAGE_RESPONSE_SCHEMA["required"]:
                value = page_result()
                del value[field]
                cases.append((field, value))
            for area in ("header_segments", "footer_segments"):
                for field in MARGIN_SEGMENT_SCHEMA["required"]:
                    value = page_result()
                    del value[area][0][field]
                    cases.append((f"{area}.{field}", value))
            for field, value in cases:
                with self.subTest(protocol=protocol, missing=field):
                    client._post.return_value = response(protocol, value)
                    with self.assertRaisesRegex(ModelServiceError, "页面结构无效"):
                        await client.request_page("test-model", PAGE_INPUT)
                    context = client.contents if protocol == "gemini" else client.previous_response_id
                    self.assertEqual(context, original_context)

    async def test_invalid_new_response_preserves_saved_result_and_records_usage(self):
        real_client = httpx.AsyncClient
        for protocol in ("openai_responses", "gemini"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                storage = Storage(Path(directory))
                storage.initialize()
                book_dir = storage.books_root / "test"
                book_dir.mkdir()
                Image.new("RGB", (16, 16), "white").save(book_dir / "page.png")
                storage.create_book("test", "测试", "page.png", [(1, 16, 16, "page.png")])
                storage.save_page_result("test", 1, StructuredPageResult.model_validate(page_result()))
                invalid = page_result()
                del invalid["footer_segments"][0]["alignment"]
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response(protocol, invalid)))
                settings = {**storage.get_settings(), "api_protocol": protocol,
                            "base_url": "https://model.invalid", "extraction_model": "test-model"}
                with patch("httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
                    await BookProcessor(storage).process("test", settings, "test-key")
                page = storage.get_pages("test")[0]
                self.assertEqual((page.status, page.text, page.page_side), ("failed", "原文", "left"))
                self.assertEqual(page.footer_segments[0].alignment, "left")
                self.assertEqual(page.attempts, 1)
                self.assertEqual(page.usage.total_tokens, 5)

    def test_legacy_results_keep_compatibility_defaults(self):
        result = StructuredPageResult.model_validate({
            "header_segments": [{"kind": "text", "text": "旧页眉"}],
            "body_markdown": "旧正文", "footer_segments": [{"kind": "page_number", "text": "2"}],
        })
        self.assertEqual((result.page_kind, result.page_side), ("content", "unknown"))
        for segment in result.header_segments + result.footer_segments:
            self.assertEqual((segment.alignment, segment.row, segment.font_size, segment.bold, segment.italic),
                             ("center", 1, "small", False, False))


if __name__ == "__main__":
    unittest.main()
