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
from backend.prompts import MARGIN_SEGMENT_SCHEMA, PAGE_RESPONSE_SCHEMA, PAGE_RESPONSE_SCHEMA_V1
from backend.responses_client import ModelServiceError, ResponsesClient, ResponsesConfig
from backend.storage import Storage
from backend.tests.layout_response_fixtures import original_printed_page, page_response


def page_result(version=2):
    segment = {"kind": "page_number", "text": "2", "alignment": "left", "row": 2,
               "font_size": "normal", "bold": True, "italic": True}
    result = page_response("原文", version=version,
                           header_segments=[segment], footer_segments=[segment])
    result["page_side"] = "left"
    return result


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
            for field in page_result()["layout"]:
                value = page_result()
                del value["layout"][field]
                cases.append((f"layout.{field}", value))
            for field in page_result()["layout"]["lines"][0]:
                value = page_result()
                del value["layout"]["lines"][0][field]
                cases.append((f"line.{field}", value))
            for field in page_result()["layout"]["lines"][0]["style"]:
                value = page_result()
                del value["layout"]["lines"][0]["style"][field]
                cases.append((f"line.style.{field}", value))
            for field in original_printed_page()["layout"]["equation_groups"][0]["number"]:
                value = original_printed_page()
                del value["layout"]["equation_groups"][0]["number"][field]
                cases.append((f"equation.number.{field}", value))
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
                (book_dir / "source.png").write_bytes((book_dir / "page.png").read_bytes())
                storage.create_book("test", "测试", "page.png", [(1, 16, 16, "page.png")])
                storage.save_page_result("test", 1, StructuredPageResult.model_validate(page_result(version=1)))
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
            "body_latex": "旧正文", "footer_segments": [{"kind": "page_number", "text": "2"}],
        })
        self.assertEqual((result.page_kind, result.page_side), ("content", "unknown"))
        for segment in result.header_segments + result.footer_segments:
            self.assertEqual((segment.alignment, segment.row, segment.font_size, segment.bold, segment.italic),
                             ("center", 1, "small", False, False))

    async def test_requested_version_selects_schema_and_rejects_other_versions(self):
        for protocol in ("openai_responses", "gemini"):
            for version, schema in ((1, PAGE_RESPONSE_SCHEMA_V1), (2, PAGE_RESPONSE_SCHEMA)):
                with self.subTest(protocol=protocol, version=version):
                    client = model_client(protocol)
                    client._post = AsyncMock(return_value=response(protocol, page_result(version)))
                    result = await client.request_page("test-model", PAGE_INPUT, response_version=version)
                    self.assertEqual(result.response_version, version)
                    payload = client._post.call_args.args[1 if protocol == "gemini" else 0]
                    actual_schema = (payload["generationConfig"]["responseJsonSchema"] if protocol == "gemini"
                                     else payload["text"]["format"]["schema"])
                    self.assertEqual(actual_schema, schema)
                    client._post.return_value = response(protocol, page_result(3 - version))
                    with self.assertRaisesRegex(ModelServiceError, "页面结构无效"):
                        await client.request_page("test-model", PAGE_INPUT, response_version=version)
                    if version == 1:
                        for extra in ({"response_version": 1}, {"layout": None}):
                            client._post.return_value = response(protocol, {**page_result(1), **extra})
                            with self.assertRaisesRegex(ModelServiceError, "页面结构无效"):
                                await client.request_page("test-model", PAGE_INPUT, response_version=1)

    async def test_invalid_layout_is_rejected_without_advancing_context(self):
        for protocol in ("openai_responses", "gemini"):
            client = model_client(protocol)
            client._post = AsyncMock(return_value=response(protocol, page_result()))
            await client.request_page("test-model", PAGE_INPUT)
            context = copy.deepcopy(client.contents) if protocol == "gemini" else client.previous_response_id
            invalid_values = []
            for bbox in ([.9, .1, .1, .2], [-.1, .1, .9, .2], [.1, .1, 1.1, .2]):
                value = page_result()
                value["layout"]["lines"][0]["bbox"] = bbox
                invalid_values.append(value)
            value = page_result()
            value["layout"]["lines"].append(copy.deepcopy(value["layout"]["lines"][0]))
            invalid_values.append(value)
            value = page_result()
            value["layout"]["regions"][0]["parent_id"] = "paragraph"
            invalid_values.append(value)
            value = page_result()
            value["layout"]["source_id"] = "model-forged-source"
            invalid_values.append(value)
            value = page_result()
            value["layout"]["lines"][0]["basis"] = "file_metadata"
            invalid_values.append(value)
            value = page_result()
            value["layout"]["review_reasons"] = [f"模型复核项 {index}" for index in range(101)]
            invalid_values.append(value)
            for index, value in enumerate(invalid_values):
                with self.subTest(protocol=protocol, case=index):
                    client._post.return_value = response(protocol, value)
                    with self.assertRaisesRegex(ModelServiceError, "页面结构无效"):
                        await client.request_page("test-model", PAGE_INPUT)
                    self.assertEqual(client.contents if protocol == "gemini" else client.previous_response_id, context)

    def test_wire_schema_reserves_program_review_slots(self):
        self.assertEqual(PAGE_RESPONSE_SCHEMA["$defs"]["LayoutObservation"]["properties"]
                         ["review_reasons"]["maxItems"], 100)


if __name__ == "__main__":
    unittest.main()
