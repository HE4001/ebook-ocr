from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Book, CoverField, MarginSegment, Page, PageKind, SourceFile, StructuredPageResult, Usage
from .prompts import legacy_blocks_to_markdown


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": "https://api.openai.com/v1",
    "responses_path": "/responses",
    "models_path": "/models",
    "extraction_model": "",
    "reasoning_effort": "",
    "classification_model": "",  # 兼容旧设置；页面代理不会调用它。
    "structured_output": False,  # 兼容旧设置；页面代理固定请求结构化结果。
    "timeout_seconds": 120,
    "processing_concurrency": 10,
    "context_reuse_enabled": False,
    "context_reuse_max_pages": 10,
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
                    created_at TEXT NOT NULL, selection_confirmed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS pages (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    image_name TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                    extraction_text TEXT NOT NULL DEFAULT '', extraction_json TEXT,
                    blocks_json TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL DEFAULT '',
                    page_kind TEXT NOT NULL DEFAULT 'content',
                    cover_fields_json TEXT NOT NULL DEFAULT '[]',
                    header_segments_json TEXT NOT NULL DEFAULT '[]',
                    footer_segments_json TEXT NOT NULL DEFAULT '[]',
                    usage_unknown INTEGER NOT NULL DEFAULT 0,
                    selected INTEGER NOT NULL DEFAULT 1,
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
                CREATE TABLE IF NOT EXISTS source_files (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    id TEXT NOT NULL, filename TEXT NOT NULL, kind TEXT NOT NULL,
                    page_count INTEGER NOT NULL, position INTEGER NOT NULL,
                    directory TEXT NOT NULL, parent_id TEXT,
                    PRIMARY KEY (book_id, id)
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
            if "header_segments_json" not in columns:
                connection.execute(
                    "ALTER TABLE pages ADD COLUMN header_segments_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "footer_segments_json" not in columns:
                connection.execute(
                    "ALTER TABLE pages ADD COLUMN footer_segments_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "page_kind" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN page_kind TEXT NOT NULL DEFAULT 'content'")
            if "cover_fields_json" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN cover_fields_json TEXT NOT NULL DEFAULT '[]'")
            if "selected" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN selected INTEGER NOT NULL DEFAULT 1")
            if "source_id" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN source_id TEXT NOT NULL DEFAULT 'legacy'")
                connection.execute("ALTER TABLE pages ADD COLUMN source_page INTEGER NOT NULL DEFAULT 1")
                connection.execute("UPDATE pages SET source_page = number")
            if "position" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE pages SET position = number")
            book_columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)")}
            if "selection_confirmed" not in book_columns:
                connection.execute(
                    "ALTER TABLE books ADD COLUMN selection_confirmed INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE books SET selection_confirmed = 1 WHERE status != 'uploaded' OR EXISTS ("
                    "SELECT 1 FROM pages WHERE pages.book_id = books.id AND ("
                    "status != 'uploaded' OR text != '' OR extraction_text != '' "
                    "OR extraction_json IS NOT NULL OR blocks_json != '[]' "
                    "OR header_segments_json != '[]' OR footer_segments_json != '[]')) "
                    "OR EXISTS (SELECT 1 FROM page_attempts WHERE page_attempts.book_id = books.id)"
                )
            if "upload_confirmed" not in book_columns:
                connection.execute("ALTER TABLE books ADD COLUMN upload_confirmed INTEGER NOT NULL DEFAULT 1")
            source_columns = {row["name"] for row in connection.execute("PRAGMA table_info(source_files)")}
            if "parent_id" not in source_columns:
                connection.execute("ALTER TABLE source_files ADD COLUMN parent_id TEXT")
            nested_sources = connection.execute(
                "WITH RECURSIVE file_tree(book_id, id, root_id) AS ("
                "SELECT book_id, id, id FROM source_files WHERE parent_id IS NULL "
                "UNION ALL "
                "SELECT child.book_id, child.id, tree.root_id FROM source_files AS child "
                "JOIN file_tree AS tree ON child.book_id = tree.book_id AND child.parent_id = tree.id"
                ") SELECT tree.book_id, tree.id, tree.root_id FROM file_tree AS tree "
                "JOIN source_files AS source ON source.book_id = tree.book_id AND source.id = tree.id "
                "WHERE source.parent_id IS NOT NULL AND source.parent_id != tree.root_id"
            ).fetchall()
            connection.executemany(
                "UPDATE source_files SET parent_id = ? WHERE book_id = ? AND id = ?",
                [(row["root_id"], row["book_id"], row["id"]) for row in nested_sources],
            )
            connection.execute(
                "INSERT INTO source_files(book_id,id,filename,kind,page_count,position,directory) "
                "SELECT id, 'legacy', filename, CASE WHEN lower(filename) LIKE '%.pdf' "
                "THEN 'pdf' ELSE 'image' END, page_count, 0, '' FROM books "
                "WHERE page_count > 0 AND NOT EXISTS ("
                "SELECT 1 FROM source_files WHERE source_files.book_id = books.id)"
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
        stored.pop("max_output_tokens", None)  # 忽略旧版本保存的输出额度。
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
        safe = {key: settings.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
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
                "INSERT INTO pages(book_id,number,width,height,image_name,status,source_page,position) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(book_id, number, width, height, name, "uploaded", number, number)
                 for number, width, height, name in pages],
            )
            connection.execute(
                "INSERT INTO source_files(book_id,id,filename,kind,page_count,position,directory) "
                "VALUES (?, 'legacy', ?, ?, ?, 0, '')",
                (book_id, filename, "pdf" if filename.lower().endswith(".pdf") else "image", len(pages)),
            )
        book = self.get_book(book_id)
        assert book is not None
        return book

    def create_project(self, book_id: str, title: str) -> Book:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO books(id,title,filename,status,page_count,created_at,upload_confirmed) "
                "VALUES (?,?,'','uploaded',0,?,0)",
                (book_id, title, datetime.now(timezone.utc).isoformat()),
            )
        book = self.get_book(book_id)
        assert book is not None
        return book

    def get_files(self, book_id: str) -> list[SourceFile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_files WHERE book_id = ? ORDER BY position", (book_id,),
            ).fetchall()
        return [SourceFile(**dict(row)) for row in rows]

    def append_files(self, book_id: str, files: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            number = connection.execute(
                "SELECT COALESCE(MAX(number), 0) FROM pages WHERE book_id = ?", (book_id,),
            ).fetchone()[0]
            position = connection.execute(
                "SELECT COALESCE(MAX(position), 0) FROM pages WHERE book_id = ?", (book_id,),
            ).fetchone()[0]
            file_position = connection.execute(
                "SELECT COUNT(*) FROM source_files WHERE book_id = ?", (book_id,),
            ).fetchone()[0]
            for source in files:
                connection.execute(
                    "INSERT INTO source_files(book_id,id,filename,kind,page_count,position,directory) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (book_id, source["id"], source["filename"], source["kind"],
                     len(source["pages"]), file_position, source["directory"]),
                )
                file_position += 1
                for source_page, width, height, image_name in source["pages"]:
                    number += 1
                    position += 1
                    connection.execute(
                        "INSERT INTO pages(book_id,number,width,height,image_name,status,source_id,"
                        "source_page,position) VALUES (?,?,?,?,?,'uploaded',?,?,?)",
                        (book_id, number, width, height,
                         f'{source["directory"]}/{image_name}', source["id"], source_page, position),
                    )
            connection.execute(
                "UPDATE books SET page_count = ?, upload_confirmed = 0, selection_confirmed = 0, "
                "status = 'uploaded', error = NULL WHERE id = ?", (number, book_id),
            )

    def confirm_upload(self, book_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE books SET upload_confirmed = 1 WHERE id = ?", (book_id,))

    def save_arrangement(
        self, book_id: str, file_order: list[str], page_order: list[int],
        file_parents: dict[str, str | None] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE source_files SET position = ? WHERE book_id = ? AND id = ?",
                [(position, book_id, source_id) for position, source_id in enumerate(file_order)],
            )
            if file_parents is not None:
                connection.executemany(
                    "UPDATE source_files SET parent_id = ? WHERE book_id = ? AND id = ?",
                    [(parent_id, book_id, source_id) for source_id, parent_id in file_parents.items()],
                )
            connection.execute("UPDATE pages SET selected = 0 WHERE book_id = ?", (book_id,))
            connection.executemany(
                "UPDATE pages SET selected = 1, position = ? WHERE book_id = ? AND number = ?",
                [(position, book_id, number) for position, number in enumerate(page_order)],
            )
            connection.execute("UPDATE books SET selection_confirmed = 1 WHERE id = ?", (book_id,))
        self.refresh_book(book_id, processing=False)

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

    def get_pages(self, book_id: str, *, all_pages: bool = False) -> list[Page]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT pages.*, source_files.filename AS source_filename FROM pages "
                "JOIN source_files ON pages.book_id = source_files.book_id "
                "AND pages.source_id = source_files.id WHERE pages.book_id = ? "
                + ("" if all_pages else "AND selected = 1 ")
                + "ORDER BY pages.position, pages.number", (book_id,),
            ).fetchall()
            return [self._page(connection, row) for row in rows]

    def get_page_record(self, book_id: str, number: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT pages.*, source_files.filename AS source_filename, source_files.kind AS source_kind, "
                "source_files.directory AS source_directory, source_files.page_count AS source_page_count "
                "FROM pages JOIN source_files ON pages.book_id = source_files.book_id "
                "AND pages.source_id = source_files.id WHERE pages.book_id = ? AND number = ?",
                (book_id, number),
            ).fetchone()
        return dict(row) if row else None

    def get_page_records(self, book_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND selected = 1 ORDER BY position, number", (book_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_page_selection(self, book_id: str, numbers: list[int]) -> None:
        self.save_arrangement(book_id, [source.id for source in self.get_files(book_id)], numbers)

    def add_pages(self, book_id: str, numbers: list[int]) -> list[int]:
        added = []
        with self._connect() as connection:
            position = connection.execute(
                "SELECT COALESCE(MAX(position), 0) FROM pages WHERE book_id = ? AND selected = 1", (book_id,),
            ).fetchone()[0]
            for number in numbers:
                position += 1
                result = connection.execute(
                    "UPDATE pages SET selected = 1, position = ? WHERE book_id = ? AND number = ? AND selected = 0",
                    (position, book_id, number),
                )
                if result.rowcount:
                    added.append(number)
        return added

    def remove_page(self, book_id: str, number: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET selected = 0 WHERE book_id = ? AND number = ?",
                (book_id, number),
            )

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
                "SELECT SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready "
                "FROM pages WHERE book_id = ? AND selected = 1", (book_id,),
            ).fetchone()
            ready = int(counts["ready"] or 0)
            connection.execute(
                "UPDATE books SET status = 'paused', completed_pages = ?, error = NULL WHERE id = ?",
                (ready, book_id),
            )

    def update_page_image(
        self, book_id: str, number: int, width: int, height: int, image_name: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET width = ?, height = ?, image_name = ? "
                "WHERE book_id = ? AND number = ?",
                (width, height, image_name, book_id, number),
            )

    def set_page_status(self, book_id: str, number: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = ?, error = NULL WHERE book_id = ? AND number = ?",
                (status, book_id, number),
            )
        self.refresh_book(book_id, processing=True)

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

    def save_page_result(
        self, book_id: str, number: int, result: StructuredPageResult | str,
    ) -> None:
        if isinstance(result, str):
            result = StructuredPageResult(
                header_segments=[], body_markdown=result, footer_segments=[]
            )
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ?, "
                "page_kind = ?, cover_fields_json = ?, "
                "header_segments_json = ?, footer_segments_json = ?, extraction_json = NULL "
                "WHERE book_id = ? AND number = ?",
                (
                    result.body_markdown,
                    result.page_kind,
                    json.dumps([field.model_dump() for field in result.cover_fields], ensure_ascii=False),
                    json.dumps([segment.model_dump() for segment in result.header_segments], ensure_ascii=False),
                    json.dumps([segment.model_dump() for segment in result.footer_segments], ensure_ascii=False),
                    book_id, number,
                ),
            )
        self.refresh_book(book_id, processing=True)

    def fail_page(self, book_id: str, number: int, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'failed', error = ? WHERE book_id = ? AND number = ?",
                (message[:1000], book_id, number),
            )
        self.refresh_book(book_id, processing=True)

    def interrupt_page(self, book_id: str, number: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pages SET status = 'interrupted', error = ? WHERE book_id = ? AND number = ?",
                ("处理被中断，可重新处理", book_id, number),
            )

    def save_manual_text(
        self, book_id: str, number: int, text: str, *,
        page_kind: PageKind | None = None, cover_fields: list[CoverField] | None = None,
    ) -> None:
        with self._connect() as connection:
            page = connection.execute(
                "SELECT page_kind, cover_fields_json, header_segments_json, footer_segments_json FROM pages "
                "WHERE book_id = ? AND number = ? AND selected = 1",
                (book_id, number),
            ).fetchone()
            if page is None:
                raise KeyError("page")
            kind = page_kind if page_kind is not None else page["page_kind"]
            if kind == "content":
                if cover_fields:
                    raise ValueError("正文页不能包含封面书目信息")
                fields_json = "[]"
                header_json = page["header_segments_json"]
                footer_json = page["footer_segments_json"]
            else:
                if text:
                    raise ValueError("封面和封底不能包含正文")
                fields_json = (
                    json.dumps([field.model_dump() for field in cover_fields], ensure_ascii=False)
                    if cover_fields is not None else page["cover_fields_json"]
                )
                header_json = footer_json = "[]"
            connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ?, page_kind = ?, "
                "cover_fields_json = ?, header_segments_json = ?, footer_segments_json = ? "
                "WHERE book_id = ? AND number = ?",
                (text, kind, fields_json, header_json, footer_json, book_id, number),
            )
        self.refresh_book(book_id, processing=False)

    def refresh_book(self, book_id: str, processing: bool) -> None:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM books WHERE id = ?", (book_id,),
            ).fetchone()
            counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status='interrupted' THEN 1 ELSE 0 END) AS interrupted "
                "FROM pages WHERE book_id = ? AND selected = 1", (book_id,),
            ).fetchone()
            total = int(counts["total"] or 0)
            ready = int(counts["ready"] or 0)
            failed = int(counts["failed"] or 0)
            interrupted = int(counts["interrupted"] or 0)
            if total == 0:
                status, error = "uploaded", None
            elif processing:
                status = "pausing" if current is not None and current["status"] == "pausing" else "processing"
                error = None
            elif ready == total:
                status, error = "ready", None
            elif current is not None and current["status"] == "paused":
                status, error = "paused", None
            elif failed:
                status, error = "failed", f"{failed} 页处理失败"
            elif interrupted:
                status, error = "interrupted", "仍有页面未完成"
            else:
                status, error = "uploaded", None
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
        data["file_count"] = connection.execute(
            "SELECT COUNT(*) FROM source_files WHERE book_id = ?", (row["id"],),
        ).fetchone()[0]
        data["selected_page_count"] = connection.execute(
            "SELECT COUNT(*) FROM pages WHERE book_id = ? AND selected = 1", (row["id"],)
        ).fetchone()[0]
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
            source_id=row["source_id"], source_filename=row["source_filename"], source_page=row["source_page"],
            text=row["text"],
            page_kind=row["page_kind"],
            cover_fields=[CoverField.model_validate(value) for value in json.loads(row["cover_fields_json"])],
            header_segments=[MarginSegment.model_validate(value) for value in
                             json.loads(row["header_segments_json"])],
            footer_segments=[MarginSegment.model_validate(value) for value in
                             json.loads(row["footer_segments_json"])],
            usage=cls._usage(attempts, bool(row["usage_unknown"])),
            attempts=len(attempts),
        )
