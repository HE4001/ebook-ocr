import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.gemini_client import GeminiClient, GeminiConfig
from backend.main import create_app
from backend.model_client import create_model_client
from backend.pipeline import BookProcessor
from backend.responses_client import ModelServiceError, ResponsesClient
from backend.storage import Storage
from backend.tests.layout_response_fixtures import page_response


@contextmanager
def upstream(handler):
    real_client = httpx.AsyncClient
    with patch("backend.gemini_client.httpx.AsyncClient", side_effect=lambda **kwargs:
               real_client(transport=httpx.MockTransport(handler), **kwargs)):
        yield


def normal(kind="content", text="正文"):
    return page_response(text, kind)


def answer(value, signature="opaque-signature"):
    return {"candidates": [{"finishReason": "STOP", "content": {
        "role": "model", "parts": [
            {"thought": True, "text": "private thinking", "thoughtSignature": signature},
            {"text": json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value},
        ],
    }}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3,
                            "thoughtsTokenCount": 4, "totalTokenCount": 9}}


def draft(**changes):
    return {"api_protocol": "gemini", "base_url": "https://gemini.invalid/v1beta",
            "models_path": "/models", "api_key": "test-key", "timeout_seconds": 15, **changes}


def page_input():
    return [{"role": "system", "content": [{"type": "input_text", "text": "OCR"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "page 1"},
             {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="}]}]


@contextmanager
def temporary_book(count=1, context=False):
    with tempfile.TemporaryDirectory() as directory:
        storage = Storage(Path(directory))
        storage.initialize()
        root = storage.books_root / "gemini-test"
        root.mkdir()
        pages = []
        for number in range(1, count + 1):
            name = f"page-{number:04d}.png"
            Image.new("RGB", (16, 16), "white").save(root / name)
            pages.append((number, 16, 16, name))
        (root / "source.png").write_bytes((root / "page-0001.png").read_bytes())
        storage.create_book("gemini-test", "测试", "input.png", pages)
        settings = {**storage.get_settings(), "api_protocol": "gemini",
                    "base_url": "https://gemini.invalid/v1beta", "models_path": "/models",
                    "extraction_model": "models/manual-model", "processing_concurrency": 1,
                    "context_reuse_enabled": context, "context_reuse_max_pages": 10}
        yield storage, settings


class GeminiApiTests(unittest.TestCase):
    def test_native_models_pagination_filter_and_draft_is_not_saved(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, json={"models": [
                    {"name": "models/vision", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
                ], "nextPageToken": "page two+"})
            return httpx.Response(200, json={"models": [
                {"name": "models/manual-model", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/vision", "supportedGenerationMethods": ["generateContent"]},
            ]})

        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
            client.put("/api/settings", json={"api_key": "saved-key"})
            before = client.get("/api/settings").json()
            with upstream(handler):
                result = client.post("/api/settings/models", json=draft())
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {"models": ["models/vision", "models/manual-model"]})
            self.assertEqual(client.get("/api/settings").json(), before)
            self.assertEqual(client.app.state.storage.get_api_key(), "saved-key")
        self.assertEqual(len(requests), 2)
        self.assertEqual(dict(requests[0].url.params), {})
        self.assertEqual(dict(requests[1].url.params), {"pageToken": "page two+"})
        for request in requests:
            self.assertEqual((request.method, request.url.path), ("GET", "/v1beta/models"))
            self.assertEqual(request.headers["x-goog-api-key"], "test-key")
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.content, b"")

    def test_legacy_default_and_connection_changes_cannot_reuse_saved_key(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.initialize()
            legacy = storage.get_settings()
            legacy.pop("api_protocol")
            with storage._connect() as connection:
                connection.execute("UPDATE settings SET value = ? WHERE id = 1", (json.dumps(legacy),))
            self.assertIsInstance(create_model_client(legacy, "test-key"), ResponsesClient)
            with TestClient(create_app(Path(directory))) as client:
                self.assertEqual(client.get("/api/settings").json()["api_protocol"], "openai_responses")
                client.put("/api/settings", json={"base_url": draft()["base_url"], "api_key": "saved-key"})
                before = client.get("/api/settings").json()
                for changes in ({"api_protocol": "gemini"}, {"base_url": "https://other.invalid/v1"}):
                    for empty in (None, "", "  "):
                        with self.subTest(changes=changes, empty=empty):
                            body = {**before, **changes, "api_key": empty}
                            body = {k: body[k] for k in draft()}
                            self.assertEqual(client.post("/api/settings/models", json=body).status_code, 400)
                            result = client.put("/api/settings", json={**changes, "api_key": empty})
                            self.assertEqual(result.status_code, 400)
                            self.assertNotIn("saved-key", result.text)
                            self.assertEqual(client.get("/api/settings").json(), before)
                saved = client.put("/api/settings", json={"api_protocol": "gemini", "api_key": "new-key"})
                self.assertEqual(saved.status_code, 200)
                self.assertNotIn("new-key", saved.text)
                self.assertNotIn("api_key", saved.json())
                requests = []
                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={"models": []})
                with upstream(handler):
                    self.assertEqual(client.post("/api/settings/models", json=draft(api_key=" ")).status_code, 200)
                    self.assertEqual(client.post("/api/settings/models", json=draft(
                        base_url="https://other.invalid/v1", api_key="explicit-key")).status_code, 200)
                self.assertEqual([r.headers["x-goog-api-key"] for r in requests], ["new-key", "explicit-key"])
                cleared = client.put("/api/settings", json={"api_protocol": "openai_responses", "clear_api_key": True})
                self.assertEqual(cleared.status_code, 200)
                self.assertFalse(cleared.json()["has_api_key"])
                self.assertFalse(client.app.state.storage.get_api_key())

    def test_connection_uses_native_text_and_optional_thinking_without_schema(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=answer("OK"))
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client, upstream(handler):
            self.assertEqual(client.put("/api/settings", json={**draft(), "extraction_model": "models/manual-model"}).status_code, 200)
            for effort, expected in (("", None), ("low", {"thinkingLevel": "LOW"}),
                                     ("128", {"thinkingBudget": 128}), ("-1", {"thinkingBudget": -1})):
                with self.subTest(effort=effort):
                    client.put("/api/settings", json={"reasoning_effort": effort})
                    self.assertTrue(client.post("/api/settings/test").json()["ok"])
                    payload = json.loads(requests[-1].content)
                    self.assertEqual(payload["contents"], [{"role": "user", "parts": [{"text": "只回复 OK。"}]}])
                    self.assertEqual(payload.get("generationConfig"), {"thinkingConfig": expected} if expected else None)
                    self.assertEqual(requests[-1].url.path, "/v1beta/models/manual-model:generateContent")
                    self.assertEqual(requests[-1].headers["x-goog-api-key"], "test-key")
            for invalid in ("xhigh", "-2"):
                client.put("/api/settings", json={"reasoning_effort": invalid})
                self.assertFalse(client.post("/api/settings/test").json()["ok"])
            self.assertEqual(len(requests), 4)


class GeminiPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_pipeline_native_schema_retry_attempts_and_thought_usage(self):
        requests = []
        value = page_response(footer_segments=[{
            "kind": "page_number", "text": "3", "alignment": "right", "row": 1,
            "font_size": "small", "bold": False, "italic": False,
        }])
        def handler(request):
            requests.append(request)
            return httpx.Response(503 if len(requests) == 1 else 200, json=answer(value))
        with temporary_book() as (storage, settings), upstream(handler), patch("backend.gemini_client.asyncio.sleep", return_value=None):
            await BookProcessor(storage).process("gemini-test", settings, "test-key")
            page = storage.get_pages("gemini-test")[0]
            self.assertEqual((page.status, page.attempts), ("ready", 2))
            self.assertIn("正文", page.text)
            self.assertEqual(page.render_strategy, "source_fidelity")
            self.assertEqual(page.layout_source.lines[0].latex, "正文")
            self.assertEqual(page.page_side, "right")
            self.assertEqual(page.usage.model_dump(), {"input_tokens": 4, "output_tokens": 14, "total_tokens": 18, "complete": True})
        payload = json.loads(requests[-1].content)
        self.assertEqual(set(payload), {"systemInstruction", "contents", "generationConfig"})
        self.assertEqual(payload["contents"][0]["parts"][1]["inlineData"]["mimeType"], "image/png")
        self.assertTrue(payload["contents"][0]["parts"][1]["inlineData"]["data"].startswith("iVBOR"))
        self.assertEqual(set(payload["generationConfig"]["responseJsonSchema"]["properties"]), set(normal()))
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(json.loads(requests[0].content), payload)

    async def test_special_pages_use_one_request_and_keep_signed_history(self):
        for kind in ("front_cover", "back_cover"):
            with self.subTest(kind=kind), temporary_book(3, context=True) as (storage, settings):
                fields = [{"kind": "title", "text": "原图书名"}]
                values = [answer(normal()), answer(page_response(kind=kind, cover_fields=fields)),
                          answer(normal(text="后页"))]
                requests = []
                def handler(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json=values[len(requests) - 1])
                with upstream(handler):
                    await BookProcessor(storage).process("gemini-test", settings, "test-key")
                first, cover_request, following = requests
                self.assertEqual([len(p["contents"]) for p in requests], [1, 3, 5])
                self.assertEqual(cover_request["contents"][1], values[0]["candidates"][0]["content"])
                self.assertEqual(following["contents"][3], values[1]["candidates"][0]["content"])
                self.assertEqual(cover_request["systemInstruction"], first["systemInstruction"])
                self.assertEqual(cover_request["generationConfig"]["responseJsonSchema"],
                                 first["generationConfig"]["responseJsonSchema"])
                cover = storage.get_pages("gemini-test")[1]
                self.assertEqual((cover.status, cover.page_kind, cover.text, cover.attempts), ("ready", kind, "", 1))
                self.assertEqual([field.model_dump() for field in cover.cover_fields], fields)
                self.assertEqual(cover.page_side, "unknown")
                self.assertEqual(cover.usage.model_dump(), {"input_tokens": 2, "output_tokens": 7, "total_tokens": 9, "complete": True})
                self.assertIn("后页", storage.get_pages("gemini-test")[2].text)

    async def test_failures_do_not_pollute_context_or_leak_upstream_details_and_reset(self):
        client = GeminiClient(GeminiConfig("https://gemini.invalid/v1beta", "/models", "test-key", 15, context_reuse_enabled=True))
        truncated = answer(normal())
        truncated["candidates"][0]["finishReason"] = "MAX_TOKENS"
        marker = "private-upstream-test-key"
        outcomes = [httpx.Response(200, json=body) for body in (
            truncated, {"promptFeedback": {"blockReason": marker}}, {"candidates": []},
            answer({"page_kind": "content"}), answer("not JSON"),
        )] + [httpx.Response(302, headers={"Location": "https://redirect.invalid"}, text=marker),
              httpx.Response(401, text=marker)]
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return outcomes.pop(0)
        with upstream(lambda request: httpx.Response(200, json=answer(normal()))):
            await client.request_page("models/manual-model", page_input())
        history = copy.deepcopy(client.contents)
        with upstream(handler):
            for index in range(7):
                with self.subTest(index=index), self.assertRaises(ModelServiceError) as raised:
                    await client.request_page("models/manual-model", page_input())
                self.assertTrue(str(raised.exception))
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn("test-key", str(raised.exception))
                self.assertEqual(client.contents, history)
        self.assertEqual(len(requests), 7)
        client.reset_context()
        self.assertEqual(client.contents, [])
        with upstream(lambda request: requests.append(json.loads(request.content)) or httpx.Response(200, json=answer(normal()))):
            await client.request_page("models/manual-model", page_input())
        self.assertEqual(len(requests[-1]["contents"]), 1)

    async def test_cover_with_content_layout_is_visible_pipeline_failure(self):
        invalid = normal("front_cover")
        invalid["layout"] = normal()["layout"]
        outcomes = [answer(invalid)]
        with temporary_book(context=True) as (storage, settings), upstream(lambda request: httpx.Response(200, json=outcomes.pop(0))):
            await BookProcessor(storage).process("gemini-test", settings, "test-key")
            page = storage.get_pages("gemini-test")[0]
            self.assertEqual((page.status, page.attempts), ("failed", 1))
            self.assertIn("页面结构无效", page.error)
            self.assertEqual(page.cover_fields, [])


if __name__ == "__main__":
    unittest.main()
