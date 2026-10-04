import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from backend.pipeline import BookProcessor
from backend.responses_client import (
    MAX_RESPONSE_BYTES,
    ModelServiceError,
    ResponsesClient,
    ResponsesConfig,
    fetch_models,
)
from backend.storage import Storage
from backend.tests.layout_response_fixtures import page_response


@contextmanager
def mock_upstream(handler):
    real_client = httpx.AsyncClient
    with patch("backend.responses_client.httpx.AsyncClient", side_effect=lambda **kwargs:
               real_client(transport=httpx.MockTransport(handler), **kwargs)):
        yield


def model_client(api_key="test-secret", **changes):
    return ResponsesClient(ResponsesConfig(
        base_url="https://model.invalid/v1", responses_path="/responses",
        api_key=api_key, structured_output=True, timeout_seconds=15, **changes,
    ))


class ResponsesErrorTests(unittest.IsolatedAsyncioTestCase):
    async def error_message(self, response, *, models=False, api_key="test-secret"):
        with mock_upstream(lambda request: response):
            with self.assertRaises(ModelServiceError) as raised:
                if models:
                    await fetch_models(
                        base_url="https://model.invalid/v1", models_path="/models",
                        api_key=api_key, timeout_seconds=15,
                    )
                else:
                    await model_client(api_key).test_connection("test-model")
        return str(raised.exception)

    async def test_page_400_keeps_diagnostics_callbacks_and_context_without_retry(self):
        requests = []
        starts = []
        ends = []
        client = model_client(context_reuse_enabled=True, reasoning_effort="high")
        client.previous_response_id = "previous-success"
        reason = "Invalid schema: 'anyOf' is not supported."

        def handler(request):
            requests.append(request)
            return httpx.Response(400, json={
                "id": "failed-response", "status": "failed",
                "error": {"message": reason, "param": "text.format.schema",
                          "code": "invalid_json_schema", "private": "RAW_RESPONSE_MARKER"},
                "usage": {"input_tokens": 11, "output_tokens": 0, "total_tokens": 11},
                "private": "RAW_RESPONSE_MARKER",
            }, headers={"X-Private": "RESPONSE_HEADER_MARKER"})

        def on_start():
            starts.append(17)
            return 17

        page_input = [{"role": "user", "content": [
            {"type": "input_text", "text": "REQUEST_TEXT_MARKER"},
            {"type": "input_image", "image_url": "data:image/png;base64,REQUEST_IMAGE_MARKER"},
        ]}]
        with mock_upstream(handler), patch("backend.responses_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(ModelServiceError) as raised:
                await client.request_page("test-model", page_input, on_start,
                                          lambda *args: ends.append(args))
        self.assertEqual(str(raised.exception),
                         f"模型服务 HTTP 400：{reason}；param=text.format.schema；code=invalid_json_schema")
        self.assertEqual(len(requests), 1)
        sleep.assert_not_awaited()
        self.assertEqual(starts, [17])
        self.assertEqual(ends, [(17, (11, 0, 11), True)])
        self.assertEqual(client.previous_response_id, "previous-success")
        payload = json.loads(requests[0].content)
        self.assertEqual(str(requests[0].url), "https://model.invalid/v1/responses")
        self.assertEqual(payload["previous_response_id"], "previous-success")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        for marker in ("RAW_RESPONSE_MARKER", "RESPONSE_HEADER_MARKER", "REQUEST_TEXT_MARKER",
                       "REQUEST_IMAGE_MARKER", "Bearer", "test-secret"):
            self.assertNotIn(marker, str(raised.exception))

    async def test_missing_or_invalid_message_and_non_json_use_status_only(self):
        cases = [
            {"text": "RAW_RESPONSE_MARKER test-secret"},
            {"json": {"error": {"param": "text.format.schema", "code": "invalid_schema"}}},
            {"json": {"error": {"message": " \n\t "}}},
            {"json": {"error": {"message": None}}},
            {"json": {"error": {"message": ["RAW_RESPONSE_MARKER"]}}},
            {"json": {"error": "RAW_RESPONSE_MARKER"}},
            {"json": ["RAW_RESPONSE_MARKER"]},
        ]
        for models in (False, True):
            for index, content in enumerate(cases):
                with self.subTest(models=models, case=index):
                    message = await self.error_message(httpx.Response(400, **content), models=models)
                    self.assertEqual(message, "模型服务 HTTP 400")

    async def test_non_string_param_and_code_are_ignored(self):
        for fields in ({"param": {"private": "RAW_RESPONSE_MARKER"}, "code": ["RAW_RESPONSE_MARKER"]},
                       {"param": 123, "code": True}):
            with self.subTest(fields=fields):
                message = await self.error_message(httpx.Response(400, json={
                    "error": {"message": "Unsupported parameter.", **fields},
                }))
                self.assertEqual(message, "模型服务 HTTP 400：Unsupported parameter.")

    async def test_oversized_error_response_uses_status_only(self):
        content = b'{"error":{"message":"RAW_RESPONSE_MARKER"}}' + b" " * MAX_RESPONSE_BYTES
        for models in (False, True):
            with self.subTest(models=models):
                message = await self.error_message(httpx.Response(400, content=content), models=models)
                self.assertEqual(message, "模型服务 HTTP 400")

    async def test_current_key_is_redacted_in_all_displayed_fields(self):
        api_key = "sk-test-redaction-secret"
        for models in (False, True):
            with self.subTest(models=models):
                message = await self.error_message(httpx.Response(401, json={"error": {
                    "message": f"Invalid API key: {api_key}",
                    "param": f"key-{api_key}", "code": f"invalid-{api_key}",
                }}), models=models, api_key=api_key)
                self.assertEqual(message,
                                 "模型服务 HTTP 401：Invalid API key: [已隐藏]；"
                                 "param=key-[已隐藏]；code=invalid-[已隐藏]")
                self.assertNotIn(api_key, message)

    async def test_long_details_are_bounded_and_redacted_before_truncation(self):
        api_key = "sk-test-redaction-secret"
        for models in (False, True):
            with self.subTest(models=models):
                message = await self.error_message(httpx.Response(400, json={"error": {
                    "message": "a" * 595 + api_key + "b" * 4000,
                    "param": "p" * 145 + api_key + "q" * 1000,
                    "code": "c" * 145 + api_key + "d" * 1000,
                }}), models=models, api_key=api_key)
                self.assertLessEqual(len(message), 1000)
                self.assertIn("param=", message)
                self.assertIn("code=", message)
                self.assertNotIn("sk-te", message)
                self.assertNotIn(api_key, message)

    async def test_fetch_models_includes_upstream_reason(self):
        message = await self.error_message(httpx.Response(403, json={"error": {
            "message": "Model catalogue access denied.", "param": "models", "code": "permission_denied",
        }}), models=True)
        self.assertEqual(message, "模型服务 HTTP 403：Model catalogue access denied.；"
                                 "param=models；code=permission_denied")

    async def test_pipeline_400_records_long_diagnostic_and_preserves_saved_page(self):
        requests = []
        api_key = "sk-pipeline-test-secret"

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, json={
                    "id": "saved-response", "status": "completed",
                    "output_text": json.dumps(page_response("旧正文"), ensure_ascii=False),
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                })
            return httpx.Response(400, json={
                "error": {
                    "message": f"Invalid schema: {api_key} " + "detail " * 1000,
                    "param": "text.format.schema", "code": "invalid_json_schema",
                    "private": "RAW_RESPONSE_MARKER",
                },
                "usage": {"input_tokens": 11, "output_tokens": 0, "total_tokens": 11},
            })

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.initialize()
            book_dir = storage.books_root / "error-book"
            book_dir.mkdir()
            Image.new("RGB", (16, 16), "white").save(book_dir / "page.png")
            (book_dir / "source.png").write_bytes((book_dir / "page.png").read_bytes())
            storage.create_book("error-book", "测试", "page.png", [(1, 16, 16, "page.png")])
            settings = {**storage.get_settings(), "api_protocol": "openai_responses",
                        "base_url": "https://model.invalid/v1", "extraction_model": "test-model",
                        "processing_concurrency": 1}
            processor = BookProcessor(storage)
            with mock_upstream(handler):
                await processor.process("error-book", settings, api_key)
                original = storage.get_pages("error-book")[0]
                self.assertEqual(original.status, "ready")
                self.assertEqual(original.attempts, 1)
                self.assertEqual(original.layout_source.lines[0].latex, "旧正文")
                revised = storage.save_manual_text(
                    "error-book", 1, "人工修订正文", expected_content_revision=original.content_revision,
                    expected_layout_revision=original.layout_revision,
                )
                versions = storage.get_page_versions("error-book", 1)
                await processor.process("error-book", settings, api_key)
            page = storage.get_pages("error-book")[0]
            self.assertEqual(len(requests), 2)
            self.assertEqual(page.status, "failed")
            self.assertTrue(page.error.startswith("模型服务 HTTP 400：Invalid schema: [已隐藏]"))
            self.assertIn("param=text.format.schema", page.error)
            self.assertIn("code=invalid_json_schema", page.error)
            self.assertLessEqual(len(page.error), 1000)
            self.assertNotIn(api_key, page.error)
            self.assertNotIn("RAW_RESPONSE_MARKER", page.error)
            self.assertEqual(page.text, "人工修订正文")
            self.assertEqual(page.layout_source, revised.layout_source)
            self.assertEqual(page.source_metadata, revised.source_metadata)
            self.assertEqual((page.content_revision, page.layout_revision, page.generated_content_revision,
                              page.render_strategy),
                             (revised.content_revision, revised.layout_revision,
                              revised.generated_content_revision, revised.render_strategy))
            self.assertEqual(storage.get_page_versions("error-book", 1), versions)
            self.assertEqual(json.loads(versions[-1]["extraction_json"])["body_latex"], "旧正文")
            self.assertEqual(page.attempts, 2)
            self.assertEqual(page.usage.model_dump(), {
                "input_tokens": 13, "output_tokens": 3, "total_tokens": 16, "complete": True,
            })
            self.assertEqual(storage.get_book("error-book").usage, page.usage)

    async def test_transient_retry_keeps_each_attempt_usage_and_final_diagnostic(self):
        requests = []
        starts = []
        ends = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, json={
                    "error": {"message": "Rate limited."},
                    "usage": {"input_tokens": 2, "output_tokens": 0, "total_tokens": 2},
                })
            return httpx.Response(400, json={"error": {"message": "Unsupported reasoning effort."}})

        def on_start():
            starts.append(len(starts) + 1)
            return starts[-1]

        with mock_upstream(handler), patch("backend.responses_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(ModelServiceError, "模型服务 HTTP 400：Unsupported reasoning effort\\."):
                await model_client()._post({"model": "test-model"}, on_start,
                                          lambda *args: ends.append(args))
        self.assertEqual(len(requests), 2)
        sleep.assert_awaited_once_with(1)
        self.assertEqual(starts, [1, 2])
        self.assertEqual(ends, [(1, (2, 0, 2), True), (2, (None, None, None), True)])


if __name__ == "__main__":
    unittest.main()
