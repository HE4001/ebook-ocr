from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Book, Page, Usage
from .prompts import legacy_blocks_to_markdown


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": "https://api.openai.com/v1",
    "responses_path": "/responses",
    "extraction_model": "",
    "reasoning_effort": "",
    "classification_model": "",  # 兼容旧设置；页面代理不会调用它。
    "structured_output": False,  # 兼容旧设置；页面代理始终直接请求 Markdown。
    "timeout_seconds": 120,
    "max_output_tokens": 12_000,
}


class Storage:
    def __init__(self, data_root: Path):
        self.data_root = data_root.resolve()
        self.books_root = self.data_root / "books"
        self.db_path = self.data_root / "app.db"

    def initialize(self) -> None:
        self.books_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                    status TEXT NOT NULL, page_count INTEGER NOT NULL,
                    completed_pages INTEGER NOT NULL DEFAULT 0, error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    image_name TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                    extraction_text TEXT NOT NULL DEFAULT '', extraction_json TEXT,
                    blocks_json TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL DEFAULT '',
                    usage_unknown INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (book_id, number)
                );
                CREATE TABLE IF NOT EXISTS page_attempts (
                    book_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                    attempt INTEGER NOT NULL, returned INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                    PRIMARY KEY (book_id, page_number, attempt),
                    FOREIGN KEY (book_id, page_number) REFERENCES pages(book_id, number)
                        ON DELETE CASCADE
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(pages)")}
            if "usage_unknown" not in columns:
                connection.execute(
                    "ALTER TABLE pages ADD COLUMN usage_unknown INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE pages SET usage_unknown = 1 WHERE status != 'uploaded'"
                )
            if "text" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN text TEXT NOT NULL DEFAULT ''")
                rows = connection.execute(
                    "SELECT book_id, number, blocks_json, extraction_text FROM pages"
                ).fetchall()
                for row in rows:
                    try:
                        blocks = json.loads(row["blocks_json"] or "[]")
                        text = legacy_blocks_to_markdown(blocks) if isinstance(blocks, list) else ""
                    except (ValueError, TypeError, KeyError):
                        text = ""
                    if not text:
                        text = row["extraction_text"] or ""
                    connection.execute(
                        "UPDATE pages SET text = ? WHERE book_id = ? AND number = ?",
                        (text, row["book_id"], row["number"]),
                    )
            if not connection.execute("SELECT 1 FROM settings WHERE id = 1").fetchone():
                connection.execute(
                    "INSERT INTO settings(id, value) VALUES (1, ?)",
                    (json.dumps(DEFAULT_SETTINGS, ensure_ascii=False),),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_interrupted(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'interrupted', error = ? "
                "WHERE status IN ('processing', 'extracting', 'classifying')",
                ("上次处理在程序退出时中断，可重新处理",),
            )
            connection.execute(
                "UPDATE books SET status = 'interrupted', error = ? "
                "WHERE status IN ('processing', 'extracting', 'classifying')",
                ("上次处理在程序退出时中断，可重新处理",),
            )
            connection.execute(
                "UPDATE books SET status = 'paused', error = NULL WHERE status = 'pausing'"
            )

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE id = 1").fetchone()
        stored = json.loads(row["value"]) if row else {}
        return {**DEFAULT_SETTINGS, **stored}

    def get_api_key(self) -> str | None:
        """Return the locally persisted API key, if one has been saved."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM credentials WHERE name = 'api_key'"
            ).fetchone()
        return str(row["value"]) if row else None

    def save_settings(
        self,
        settings: dict[str, Any],
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> None:
        """Persist settings and an optional credential change atomically.

        ``api_key=None`` with ``clear_api_key=False`` leaves the credential
        untouched.  The API layer normalizes blank input to that preserve
        behavior and gives clearing precedence over a supplied key.
        """
        safe = {key: settings[key] for key in DEFAULT_SETTINGS}
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO settings(id, value) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET value = excluded.value",
                (json.dumps(safe, ensure_ascii=False),),
            )
            if clear_api_key:
                connection.execute(
                    "DELETE FROM credentials WHERE name = 'api_key'"
                )
            elif api_key is not None:
                connection.execute(
                    "INSERT INTO credentials(name, value) VALUES ('api_key', ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                    (api_key,),
                )

    def create_book(
        self, book_id: str, title: str, filename: str,
        pages: list[tuple[int, int, int, str]],
    ) -> Book:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO books(id,title,filename,status,page_count,completed_pages,error,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (book_id, title, filename, "uploaded", len(pages), 0, None, created_at),
            )
            connection.executemany(
                "INSERT INTO pages(book_id,number,width,height,image_name,status) VALUES (?,?,?,?,?,?)",
                [(book_id, number, width, height, name, "uploaded")
                 for number, width, height, name in pages],
            )
        book = self.get_book(book_id)
        assert book is not None
        return book

    def list_books(self) -> list[Book]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
            return [self._book(connection, row) for row in rows]

    def get_book(self, book_id: str) -> Book | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            return self._book(connection, row) if row else None

    def delete_book(self, book_id: str) -> bool:
        book_dir = self.books_root / book_id
        if book_dir.resolve() != self.books_root.resolve() / book_id:
            raise ValueError("书籍路径无效")
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
                return False
            connection.execute("DELETE FROM page_attempts WHERE book_id = ?", (book_id,))
            connection.execute("DELETE FROM pages WHERE book_id = ?", (book_id,))
            connection.execute("DELETE FROM books WHERE id = ?", (book_id,))
            if book_dir.exists():
                shutil.rmtree(book_dir)
        return True

    def get_pages(self, book_id: str) -> list[Page]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? ORDER BY number", (book_id,)
            ).fetchall()
            return [self._page(connection, row) for row in rows]

    def get_page_record(self, book_id: str, number: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND number = ?", (book_id, number)
            ).fetchone()
        return dict(row) if row else None

    def get_unfinished_page_records(self, book_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND status != 'ready' ORDER BY number",
                (book_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_book(self, book_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE books SET status = 'processing', error = NULL WHERE id = ?", (book_id,)
            )

    def request_pause(self, book_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE books SET status = 'pausing', error = NULL WHERE id = ?",
                (book_id,),
            )

    def pause_book(self, book_id: str) -> None:
        with self._connect() as connection:
            counts = connection.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready "
                "FROM pages WHERE book_id = ?", (book_id,),
            ).fetchone()
            ready = int(counts["ready"] or 0)
            total = int(counts["total"] or 0)
            connection.execute(
                "UPDATE books SET status = ?, completed_pages = ?, error = NULL WHERE id = ?",
                ("ready" if ready == total else "paused", ready, book_id),
            )

    def set_page_status(self, book_id: str, number: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = ?, error = NULL WHERE book_id = ? AND number = ?",
                (status, book_id, number),
            )

    def begin_attempt(self, book_id: str, number: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM page_attempts "
                "WHERE book_id = ? AND page_number = ?", (book_id, number),
            ).fetchone()
            attempt = int(row["next"])
            connection.execute(
                "INSERT INTO page_attempts(book_id,page_number,attempt) VALUES (?,?,?)",
                (book_id, number, attempt),
            )
        return attempt

    def finish_attempt(
        self, book_id: str, number: int, attempt: int,
        usage: tuple[int | None, int | None, int | None], returned: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE page_attempts SET returned = ?, input_tokens = ?, "
                "output_tokens = ?, total_tokens = ? "
                "WHERE book_id = ? AND page_number = ? AND attempt = ?",
                (int(returned), *usage, book_id, number, attempt),
            )

    def save_page_result(self, book_id: str, number: int, text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ?, extraction_json = NULL "
                "WHERE book_id = ? AND number = ?",
                (text, book_id, number),
            )
        self.refresh_book(book_id, processing=True)

    def fail_page(self, book_id: str, number: int, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'failed', error = ? WHERE book_id = ? AND number = ?",
                (message[:1000], book_id, number),
            )

    def interrupt_page(self, book_id: str, number: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'interrupted', error = ? WHERE book_id = ? AND number = ?",
                ("处理被中断，可重新处理", book_id, number),
            )

    def save_manual_text(self, book_id: str, number: int, text: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ? "
                "WHERE book_id = ? AND number = ?",
                (text, book_id, number),
            )
            if result.rowcount == 0:
                raise KeyError("page")
        self.refresh_book(book_id, processing=False)

    def refresh_book(self, book_id: str, processing: bool) -> None:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM books WHERE id = ?", (book_id,),
            ).fetchone()
            counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                "FROM pages WHERE book_id = ?", (book_id,),
            ).fetchone()
            total = int(counts["total"] or 0)
            ready = int(counts["ready"] or 0)
            failed = int(counts["failed"] or 0)
            if ready == total:
                status, error = "ready", None
            elif processing:
                status, error = "processing", None
            elif current is not None and current["status"] == "paused":
                status, error = "paused", None
            elif failed:
                status, error = "failed", f"{failed} 页处理失败"
            else:
                status, error = "interrupted", "仍有页面未完成"
            connection.execute(
                "UPDATE books SET status = ?, completed_pages = ?, error = ? WHERE id = ?",
                (status, ready, error, book_id),
            )

    def finish_book(self, book_id: str) -> None:
        self.refresh_book(book_id, processing=False)

    @staticmethod
    def _usage(rows: list[sqlite3.Row], historical_unknown: bool = False) -> Usage:
        def summed(column: str) -> int | None:
            values = [row[column] for row in rows if row[column] is not None]
            return sum(values) if values else None

        return Usage(
            input_tokens=summed("input_tokens"),
            output_tokens=summed("output_tokens"),
            total_tokens=summed("total_tokens"),
            complete=bool(rows) and not historical_unknown and all(
                row["returned"] and all(row[key] is not None for key in
                                        ("input_tokens", "output_tokens", "total_tokens"))
                for row in rows
            ),
        )

    @classmethod
    def _book(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> Book:
        attempts = connection.execute(
            "SELECT * FROM page_attempts WHERE book_id = ?", (row["id"],)
        ).fetchall()
        historical_unknown = bool(connection.execute(
            "SELECT 1 FROM pages WHERE book_id = ? AND usage_unknown = 1 LIMIT 1",
            (row["id"],),
        ).fetchone())
        data = dict(row)
        if data["status"] in {"extracting", "classifying"}:
            data["status"] = "interrupted"
        return Book(**data, usage=cls._usage(attempts, historical_unknown))

    @classmethod
    def _page(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> Page:
        attempts = connection.execute(
            "SELECT * FROM page_attempts WHERE book_id = ? AND page_number = ? "
            "ORDER BY attempt", (row["book_id"], row["number"]),
        ).fetchall()
        status = row["status"]
        if status in {"extracting", "classifying"}:
            status = "interrupted"
        return Page(
            number=row["number"], status=status, error=row["error"],
            text=row["text"], usage=cls._usage(attempts, bool(row["usage_unknown"])),
            attempts=len(attempts),
        )
