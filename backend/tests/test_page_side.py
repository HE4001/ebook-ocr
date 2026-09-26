import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import MarginSegment, StructuredPageResult
from backend.prompts import PAGE_RESPONSE_SCHEMA, SPECIAL_PAGE_RESPONSE_SCHEMA
from backend.storage import Storage


def content(side="unknown", footer=None):
    return StructuredPageResult(
        page_side=side, header_segments=[], body_markdown="正文",
        footer_segments=footer if footer is not None else [
            MarginSegment(kind="page_number", text="2", alignment="left")],
    )


class PageSideTests(unittest.TestCase):
    def test_schema_and_unknown_boundaries(self):
        self.assertIn("page_side", PAGE_RESPONSE_SCHEMA["required"])
        self.assertEqual(PAGE_RESPONSE_SCHEMA["properties"]["page_side"]["enum"],
                         ["left", "right", "unknown"])
        self.assertEqual(set(SPECIAL_PAGE_RESPONSE_SCHEMA["properties"]),
                         {"page_kind", "cover_fields"})
        self.assertEqual(content().page_side, "unknown")
        for footer in ([], [MarginSegment(kind="text", text="  ", alignment="left")]):
            with self.subTest(footer=footer):
                self.assertEqual(content("left", footer).page_side, "unknown")
        for kind in ("front_cover", "back_cover"):
            with self.subTest(kind=kind):
                result = StructuredPageResult(page_kind=kind, page_side="right",
                                              header_segments=[], body_markdown="", footer_segments=[])
                self.assertEqual(result.page_side, "unknown")

    def test_old_database_migrates_unknown_without_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            footer = [MarginSegment(kind="page_number", text="2", alignment="left").model_dump()]
            with closing(sqlite3.connect(root / "app.db")) as connection, connection:
                connection.executescript("""
                    CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT, filename TEXT,
                        status TEXT, page_count INTEGER, completed_pages INTEGER,
                        error TEXT, created_at TEXT);
                    CREATE TABLE pages (book_id TEXT, number INTEGER, width INTEGER,
                        height INTEGER, image_name TEXT, status TEXT, error TEXT,
                        extraction_text TEXT, extraction_json TEXT, blocks_json TEXT,
                        text TEXT, footer_segments_json TEXT, PRIMARY KEY(book_id, number));
                """)
                connection.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?)",
                                   ("old", "旧书", "old.pdf", "ready", 1, 1, None, "2026-01-01"))
                connection.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    "old", 1, 10, 10, "page.png", "ready", None, "", None, "[]",
                    "旧正文", json.dumps(footer),
                ))
            storage = Storage(root)
            storage.initialize()
            page = storage.get_pages("old")[0]
            self.assertEqual((page.page_side, page.text), ("unknown", "旧正文"))
            self.assertEqual([item.model_dump() for item in page.footer_segments], footer)
            storage.save_page_result("old", 1, content("left"))
            restarted = Storage(root)
            restarted.initialize()
            self.assertEqual(restarted.get_pages("old")[0].page_side, "left")

    def test_api_read_export_manual_edit_and_cover_clear(self):
        with tempfile.TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
            storage = client.app.state.storage
            book_id = str(uuid4())
            storage.create_book(book_id, "左右页", "page.png", [(1, 10, 10, "page.png")])
            book_url = f"/api/books/{book_id}"
            arranged = client.put(f"{book_url}/arrangement", json={
                "file_order": ["legacy"], "page_order": [1],
            })
            self.assertEqual(arranged.status_code, 200)
            page_url = f"{book_url}/pages/1"
            for side in ("left", "right", "unknown"):
                with self.subTest(side=side):
                    storage.save_page_result(book_id, 1, content(side))
                    for url in (book_url, f"{book_url}/export"):
                        response = client.get(url)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["pages"][0]["page_side"], side)
                    edited = client.put(page_url, json={"text": "人工校对正文"})
                    self.assertEqual(edited.status_code, 200)
                    self.assertEqual((edited.json()["page_side"], edited.json()["text"]),
                                     (side, "人工校对正文"))
            for kind in ("front_cover", "back_cover"):
                with self.subTest(kind=kind):
                    storage.save_page_result(book_id, 1, content("right"))
                    cover = client.put(page_url, json={"text": "", "page_kind": kind, "cover_fields": []})
                    self.assertEqual(cover.status_code, 200)
                    self.assertEqual((cover.json()["page_side"], cover.json()["footer_segments"]),
                                     ("unknown", []))
                    restored = client.put(page_url, json={"text": "正文", "page_kind": "content"})
                    self.assertEqual(restored.status_code, 200)
                    self.assertEqual(restored.json()["page_side"], "unknown")
            storage.save_page_result(book_id, 1, content("left", []))
            self.assertEqual(client.get(f"{book_url}/export").json()["pages"][0]["page_side"],
                             "unknown")
