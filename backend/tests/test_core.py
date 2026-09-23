import json
import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.main import create_app
from backend.models import PageResult
from backend.pipeline import BookProcessor
from backend.prompts import sections_to_markdown
from backend.responses_client import ModelServiceError, ResponsesClient
from backend.storage import Storage


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), "white").save(output, format="PNG")
    return output.getvalue()


class BackendTests(unittest.TestCase):
    def test_pause_finishes_current_page_and_resumes_remaining_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
            storage = client.app.state.storage
            book_id = str(uuid4())
            book_dir = storage.books_root / book_id
            book_dir.mkdir()
            for number in (1, 2):
                (book_dir / f"page-{number:04d}.png").write_bytes(png_bytes())
            storage.create_book(book_id, "两页", "two.png", [
                (1, 32, 24, "page-0001.png"),
                (2, 32, 24, "page-0002.png"),
            ])
            client.put("/api/settings", json={"api_key": "secret", "extraction_model": "vision"})
            first_started = threading.Event()
            release_first = threading.Event()
            called: list[int] = []

            async def fake_run(_self, _book_id, number, *_args):
                called.append(number)
                if number == 1:
                    first_started.set()
                    self.assertTrue(await asyncio.to_thread(release_first.wait, 5))
                return f"第 {number} 页"

            def wait_for_status(expected: str) -> None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if client.get(f"/api/books/{book_id}").json()["book"]["status"] == expected:
                        return
                    time.sleep(0.02)
                self.fail(f"书籍未进入 {expected} 状态")

            with patch("backend.pipeline.PageAgent.run", fake_run):
                self.assertEqual(client.post(f"/api/books/{book_id}/pause").status_code, 409)
                self.assertEqual(client.post(f"/api/books/{book_id}/process").status_code, 200)
                self.assertTrue(first_started.wait(5))
                self.assertEqual(client.post(f"/api/books/{book_id}/pause").json(), {
                    "requested": True,
                })
                wait_for_status("pausing")
                self.assertEqual(client.post(f"/api/books/{book_id}/process").status_code, 409)
                release_first.set()
                wait_for_status("paused")
                self.assertEqual(called, [1])
                self.assertEqual([p.status for p in storage.get_pages(book_id)], ["ready", "uploaded"])
                deadline = time.monotonic() + 5
                while book_id in client.app.state.running and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertNotIn(book_id, client.app.state.running)
                storage.recover_interrupted()
                self.assertEqual(storage.get_book(book_id).status, "paused")
                self.assertEqual(client.put(f"/api/books/{book_id}/pages/1", json={
                    "text": "已校对的第 1 页",
                }).status_code, 200)
                self.assertEqual(storage.get_book(book_id).status, "paused")
                self.assertEqual(client.post(f"/api/books/{book_id}/process").json(), {
                    "started": True,
                })
                wait_for_status("ready")
                self.assertEqual(called, [1, 2])
                self.assertEqual([p.text for p in storage.get_pages(book_id)], [
                    "已校对的第 1 页", "第 2 页",
                ])

    def test_display_equation_delimiters_and_inline_math(self) -> None:
        formula = r"E=mc^2\tag{1}"
        multiline = "$$\n" + "\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}" + "\n$$"
        result = PageResult.model_validate({"sections": [
            {"type": "equation", "text": f"$${formula}$$"},
            {"type": "equation", "text": multiline},
            {"type": "paragraph", "text": r"正文 $x^2$ 继续"},
            {"type": "equation", "text": "旧公式说明"},
        ]})
        self.assertEqual(sections_to_markdown(result),
                         f"$$\n{formula}\n$$\n\n{multiline}\n\n正文 $x^2$ 继续\n\n旧公式说明")

    def test_markdown_response_and_usage_across_retries(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                storage = Storage(Path(directory))
                storage.initialize()
                book_id = str(uuid4())
                book_dir = storage.books_root / book_id
                book_dir.mkdir()
                (book_dir / "page-0001.png").write_bytes(png_bytes())
                storage.create_book(book_id, "测试", "page.png", [(1, 32, 24, "page-0001.png")])
                response_values = [
                    httpx.Response(503, json={"usage": {
                        "input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}),
                    httpx.Response(200, json={
                        "status": "completed", "usage": {
                            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                            "output_tokens_details": {"reasoning_tokens": 2}},
                        "output": [
                            {"type": "reasoning", "content": [{
                                "type": "reasoning_text", "text": "不应出现在OCR正文中的推理"
                            }]},
                            {"type": "message", "content": [{
                                "type": "output_text", "text": '# 标题\n\n正文 **原文**'
                            }]},
                        ],
                    }),
                ]
                sent_payloads = []
                sent_urls = []

                class FakeClient:
                    def __init__(self, **_kwargs):
                        pass

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_args):
                        pass

                    async def post(self, *_args, **_kwargs):
                        sent_urls.append(_args[0])
                        sent_payloads.append(_kwargs["json"])
                        return response_values.pop(0)

                settings = storage.get_settings()
                settings["base_url"] = "https://api.deepseek.com"
                settings["extraction_model"] = "deepseek-flash"
                settings["reasoning_effort"] = "high"
                settings["max_output_tokens"] = 24_000
                with patch("backend.responses_client.httpx.AsyncClient", FakeClient), patch(
                    "backend.responses_client.asyncio.sleep", return_value=None
                ):
                    await BookProcessor(storage).process(book_id, settings, "secret")
                page = storage.get_pages(book_id)[0]
                self.assertEqual(page.text, "# 标题\n\n正文 **原文**")
                self.assertEqual(page.attempts, 2)
                self.assertEqual(sent_urls[0], "https://api.deepseek.com/responses")
                self.assertEqual(sent_payloads[0]["reasoning"], {"effort": "high"})
                self.assertEqual(sent_payloads[0]["max_output_tokens"], 24_000)
                self.assertEqual(sent_payloads[0]["model"], "deepseek-flash")
                self.assertNotIn("text", sent_payloads[0])
                self.assertIn("直接输出", sent_payloads[0]["input"][0]["content"][0]["text"])
                image = sent_payloads[0]["input"][1]["content"][1]
                self.assertEqual(image["type"], "input_image")
                self.assertEqual(image["detail"], "high")
                self.assertTrue(image["image_url"].startswith("data:image/png;base64,"))
                self.assertEqual(page.usage.model_dump(), {
                    "input_tokens": 13, "output_tokens": 9, "total_tokens": 22,
                    "complete": True,
                })

                # 重跑失败页只追加用量；无正文但有 usage 的请求仍先入账。
                storage.set_page_status(book_id, 1, "failed")
                response_values.append(httpx.Response(200, json={
                    "status": "completed", "usage": {
                        "input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                    "output": [],
                }))
                with patch("backend.responses_client.httpx.AsyncClient", FakeClient):
                    await BookProcessor(storage).process(book_id, settings, "secret")
                page = storage.get_pages(book_id)[0]
                self.assertEqual(page.status, "failed")
                self.assertEqual(page.attempts, 3)
                self.assertEqual(page.usage.total_tokens, 25)

                response_values.append(httpx.Response(200, json={
                    "status": "incomplete", "usage": {
                        "input_tokens": 6, "output_tokens": 8,
                        "total_tokens": 14,
                        "output_tokens_details": {"reasoning_tokens": 6}},
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                }))
                with patch("backend.responses_client.httpx.AsyncClient", FakeClient):
                    await BookProcessor(storage).process(book_id, settings, "secret")
                page = storage.get_pages(book_id)[0]
                self.assertEqual(page.attempts, 4)
                self.assertEqual(page.usage.total_tokens, 39)
                self.assertEqual(page.usage.output_tokens, 18)
                self.assertTrue(page.usage.complete)
                self.assertIn("输出上限", page.error)

                response_values.append(httpx.Response(200, json={
                    "status": "completed", "usage": {
                        "input_tokens": 2, "output_tokens": 0, "total_tokens": 2},
                    "output": [{"type": "message", "content": [{
                        "type": "output_text", "text": ""}]}],
                }))
                with patch("backend.responses_client.httpx.AsyncClient", FakeClient):
                    await BookProcessor(storage).process(book_id, settings, "secret")
                page = storage.get_pages(book_id)[0]
                self.assertEqual(page.status, "ready")
                self.assertEqual(page.text, "")

                with self.assertRaisesRegex(ModelServiceError, "内容过滤器"):
                    ResponsesClient.extract_output_text({
                        "status": "incomplete",
                        "incomplete_details": {"reason": "content_filter"},
                    })

        import asyncio
        asyncio.run(run())

    def test_legacy_migration_keeps_ready_page_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "books").mkdir()
            book_id = str(uuid4())
            connection = sqlite3.connect(path / "app.db")
            connection.executescript("""
                CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT, filename TEXT,
                    status TEXT, page_count INTEGER, completed_pages INTEGER,
                    error TEXT, created_at TEXT);
                CREATE TABLE pages (book_id TEXT, number INTEGER, width INTEGER,
                    height INTEGER, image_name TEXT, status TEXT, error TEXT,
                    extraction_text TEXT, extraction_json TEXT, blocks_json TEXT,
                    PRIMARY KEY(book_id, number));
            """)
            connection.execute(
                "INSERT INTO books VALUES (?,?,?,?,?,?,?,?)",
                (book_id, "旧书", "old.pdf", "ready", 1, 1, None, "2026-01-01"),
            )
            blocks = [
                {"type": "heading", "level": 2, "text": "标题"},
                {"type": "table", "text": "销售表", "rows": [["年", "值"], ["一", "二"]]},
                {"type": "equation", "text": "公式", "latex": "x^2"},
            ]
            connection.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?)",
                (book_id, 1, 10, 10, "page-0001.png", "ready", None,
                 "旧提取", None, json.dumps(blocks, ensure_ascii=False)),
            )
            connection.commit()
            connection.close()
            storage = Storage(path)
            storage.initialize()
            page = storage.get_pages(book_id)[0]
            self.assertIn("## 标题", page.text)
            self.assertIn("销售表", page.text)
            self.assertIn("一 | 二", page.text)
            self.assertIn("x^2", page.text)
            self.assertEqual(storage.get_unfinished_page_records(book_id), [])
            self.assertEqual(page.attempts, 0)
            self.assertIsNone(page.usage.total_tokens)
            with storage._connect() as connection:
                connection.execute(
                    "INSERT INTO pages(book_id,number,width,height,image_name,status) "
                    "VALUES (?,?,?,?,?,?)",
                    (book_id, 2, 10, 10, "page-0002.png", "ready"),
                )
            attempt = storage.begin_attempt(book_id, 2)
            storage.finish_attempt(book_id, 2, attempt, (5, 2, 7), True)
            book = storage.get_book(book_id)
            self.assertEqual(book.usage.total_tokens, 7)
            self.assertFalse(book.usage.complete)
            self.assertTrue(storage.delete_book(book_id))
            with storage._connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_attempts").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM books").fetchone()[0], 0)

    def test_reasoning_setting_and_connection_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
            self.assertEqual(client.get("/api/settings").json()["reasoning_effort"], "")
            self.assertEqual(client.get("/api/settings").json()["max_output_tokens"], 12_000)
            saved = client.put("/api/settings", json={
                "api_key": "secret", "extraction_model": "vision",
                "reasoning_effort": "  custom_depth  ",
                "max_output_tokens": 24_000,
            })
            self.assertEqual(saved.json()["reasoning_effort"], "custom_depth")
            self.assertEqual(saved.json()["max_output_tokens"], 24_000)
            self.assertEqual(client.put("/api/settings", json={"max_output_tokens": 0}).status_code, 422)
            payloads = []

            async def fake_post(_self, payload, *_args):
                payloads.append(payload)
                return {"status": "completed", "output_text": "OK"}

            with patch.object(ResponsesClient, "_post", fake_post):
                self.assertTrue(client.post("/api/settings/test").json()["ok"])
            self.assertEqual(payloads[0]["reasoning"], {"effort": "custom_depth"})
            self.assertEqual(payloads[0]["max_output_tokens"], 24_000)
            client.put("/api/settings", json={"reasoning_effort": "  "})
            with patch.object(ResponsesClient, "_post", fake_post):
                self.assertTrue(client.post("/api/settings/test").json()["ok"])
            self.assertNotIn("reasoning", payloads[1])
            self.assertEqual(client.app.state.storage.get_settings()["max_output_tokens"], 24_000)

    def test_delete_book_isolated_and_running_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
            client.put("/api/settings", json={"api_key": "secret"})
            first = client.post("/api/books", files={
                "file": ("first.png", png_bytes(), "image/png")}).json()["id"]
            second = client.post("/api/books", files={
                "file": ("second.png", png_bytes(), "image/png")}).json()["id"]
            storage = client.app.state.storage
            storage.begin_attempt(first, 1)
            client.app.state.running[first] = object()
            self.assertEqual(client.delete(f"/api/books/{first}").status_code, 409)
            client.app.state.running.pop(first)
            with patch("backend.storage.shutil.rmtree", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    client.delete(f"/api/books/{first}")
            self.assertIsNotNone(storage.get_book(first))
            self.assertEqual(client.delete(f"/api/books/{first}").status_code, 204)
            self.assertFalse((storage.books_root / first).exists())
            self.assertEqual(client.get(f"/api/books/{first}").status_code, 404)
            self.assertEqual(client.delete(f"/api/books/{first}").status_code, 404)
            self.assertEqual(client.get(f"/api/books/{second}").status_code, 200)
            self.assertTrue((storage.books_root / second).exists())
            self.assertTrue(client.get("/api/settings").json()["has_api_key"])

    def test_api_upload_and_manual_text_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
            settings = client.put("/api/settings", json={
                "api_key": "secret", "extraction_model": "vision",
                "classification_model": "", "responses_path": "",
            }).json()
            self.assertTrue(settings["has_api_key"])
            self.assertEqual(settings["responses_path"], "")
            self.assertNotIn("api_key", settings)
            uploaded = client.post("/api/books", files={
                "file": ("page.png", png_bytes(), "image/png")})
            self.assertEqual(uploaded.status_code, 201)
            book_id = uploaded.json()["id"]
            detail = client.get(f"/api/books/{book_id}").json()
            self.assertEqual(set(detail["pages"][0]), {
                "number", "status", "error", "text", "usage", "attempts"})
            saved = client.put(f"/api/books/{book_id}/pages/1", json={
                "text": "**人工校对**"})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["text"], "**人工校对**")
            self.assertEqual(saved.json()["attempts"], 0)
            self.assertEqual(client.post(f"/api/books/{book_id}/process").json(), {
                "started": False})
            export = client.get(f"/api/books/{book_id}/export").json()
            self.assertEqual(export["pages"][0]["text"], "**人工校对**")
            markdown = client.get(f"/api/books/{book_id}/export.md")
            self.assertEqual(markdown.status_code, 200)
            self.assertIn("text/markdown", markdown.headers["content-type"])
            self.assertIn('.md"', markdown.headers["content-disposition"])
            self.assertEqual(markdown.text, "# page\n\n## 第 1 页\n\n**人工校对**\n")

    def test_api_key_persistence_lifecycle_and_failed_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            with TestClient(create_app(data_root)) as client:
                first = client.put("/api/settings", json={
                    "api_key": "fictional-first-key", "extraction_model": "vision",
                })
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first.json()["has_api_key"])

                # Blank input preserves the persisted key and is never echoed.
                blank = client.put("/api/settings", json={"api_key": "  "})
                self.assertEqual(blank.status_code, 200)
                self.assertTrue(blank.json()["has_api_key"])
                self.assertNotIn("api_key", client.get("/api/settings").json())
                omitted = client.put("/api/settings", json={"extraction_model": "vision-2"})
                self.assertEqual(omitted.status_code, 200)
                self.assertTrue(omitted.json()["has_api_key"])

                replacement = client.put("/api/settings", json={
                    "api_key": "fictional-replacement-key",
                })
                self.assertEqual(replacement.status_code, 200)
                self.assertTrue(replacement.json()["has_api_key"])

                uploaded = client.post("/api/books", files={
                    "file": ("page.png", png_bytes(), "image/png")})
                self.assertEqual(uploaded.status_code, 201)
                book_id = uploaded.json()["id"]
                exported = client.get(f"/api/books/{book_id}/export")
                self.assertEqual(exported.status_code, 200)
                self.assertNotIn("fictional-replacement-key", exported.text)
                self.assertNotIn("api_key", exported.text)

                # A failed transaction must leave the in-memory key unchanged.
                storage = client.app.state.storage
                with patch.object(storage, "save_settings", side_effect=RuntimeError("disk")):
                    with self.assertRaises(RuntimeError):
                        client.put("/api/settings", json={"api_key": "fictional-failed-key"})
                self.assertEqual(client.app.state.secrets.api_key, "fictional-replacement-key")

            # A new app instance loads the committed replacement from SQLite.
            with TestClient(create_app(data_root)) as restarted:
                self.assertTrue(restarted.get("/api/settings").json()["has_api_key"])
                self.assertEqual(restarted.app.state.secrets.api_key, "fictional-replacement-key")
                cleared = restarted.put("/api/settings", json={
                    "api_key": "fictional-ignored-key", "clear_api_key": True,
                })
                self.assertEqual(cleared.status_code, 200)
                self.assertFalse(cleared.json()["has_api_key"])

            # Clearing is persisted, so a later restart remains keyless.
            with TestClient(create_app(data_root)) as cleared_restart:
                self.assertFalse(cleared_restart.get("/api/settings").json()["has_api_key"])


if __name__ == "__main__":
    unittest.main()
