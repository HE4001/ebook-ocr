from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .latex_content import is_latex_document
from .latex_migration import legacy_blocks_to_markdown, markdown_to_latex
from .layout_contract import LayoutObservation, PageLayout, PageSourceMetadata, RenderStrategy, SourceFidelityLayout
from .content_contract import PageContent, content_plain_text
from .models import (
    Assessment, Attempt, Book, CoverField, ExecutionStatus, ExportManifest,
    ExportManifestPage, Issue, LayoutSettings, MarginSegment, Page, PageKind,
    PageResult, PageTask, PaperSize, ResultStatus, Revision, Run, RunCounts,
    RunCreate, RunStatus, SourceFile, StructuredPageResult, Usage, WorkflowStage,
    OutputFormat, OutputSnapshot, OutputSnapshotPage, PageOutcome, PageOutcomeSummary,
    RecognitionResponse, RunPolicy, RunSummary, SelectionDraft, SelectionUpdate, SourcePageSummary, WorkflowError,
)


DEFAULT_SETTINGS: dict[str, Any] = {
    "api_protocol": "openai_responses",
    "base_url": "https://api.openai.com/v1",
    "responses_path": "/responses",
    "models_path": "/models",
    "extraction_model": "",
    "reasoning_effort": "",
    "classification_model": "",  # 兼容旧设置；页面代理不会调用它。
    "structured_output": False,  # 兼容旧设置；页面代理固定请求结构化结果。
    "timeout_seconds": 120,
    "processing_concurrency": 2,
}
_UNSET = object()


def _without_retired_settings(settings: dict[str, Any]) -> dict[str, Any]:
    # 读取旧设置和运行快照时忽略已移除的功能配置，无需迁移数据库。
    return {key: value for key, value in settings.items()
            if key not in {"max_output_tokens", "context_reuse_enabled", "context_reuse_max_pages"}}


class RevisionConflict(ValueError):
    """The caller edited an older content or layout revision."""


class RequestBudgetExceeded(ValueError):
    """The frozen run or page request allowance has been exhausted."""


class Storage:
    def __init__(self, data_root: Path):
        self.data_root = data_root.resolve()
        self.books_root = self.data_root / "books"
        self.db_path = self.data_root / "app.db"

    def initialize(self) -> None:
        self.books_root.mkdir(parents=True, exist_ok=True)
        self._backup_before_latex_migration()
        self._backup_before_layout_migration()
        self._backup_before_workflow_migration()
        self._backup_before_v2_migration()
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
                    created_at TEXT NOT NULL, selection_confirmed INTEGER NOT NULL DEFAULT 0,
                    paper_size TEXT NOT NULL DEFAULT 'a4',
                    layout_json TEXT NOT NULL DEFAULT '{}',
                    content_format TEXT NOT NULL DEFAULT 'latex',
                    render_strategy TEXT NOT NULL DEFAULT 'source_fidelity'
                );
                CREATE TABLE IF NOT EXISTS pages (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    image_name TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                    extraction_text TEXT NOT NULL DEFAULT '', extraction_json TEXT,
                    blocks_json TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL DEFAULT '',
                    legacy_markdown TEXT,
                    page_kind TEXT NOT NULL DEFAULT 'content',
                    page_side TEXT NOT NULL DEFAULT 'unknown',
                    cover_fields_json TEXT NOT NULL DEFAULT '[]',
                    header_segments_json TEXT NOT NULL DEFAULT '[]',
                    footer_segments_json TEXT NOT NULL DEFAULT '[]',
                    render_strategy TEXT NOT NULL DEFAULT 'legacy_template',
                    content_revision INTEGER NOT NULL DEFAULT 0,
                    layout_revision INTEGER NOT NULL DEFAULT 0,
                    generated_content_revision INTEGER,
                    layout_schema_version INTEGER,
                    layout_source_json TEXT,
                    source_metadata_json TEXT,
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
                CREATE TABLE IF NOT EXISTS page_versions (
                    book_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                    content_revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (book_id, page_number, content_revision),
                    FOREIGN KEY (book_id, page_number) REFERENCES pages(book_id, number)
                        ON DELETE CASCADE
                );
                """
            )
            connection.execute("BEGIN")
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
                    blocks = json.loads(row["blocks_json"])
                    text = legacy_blocks_to_markdown(blocks)
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
            if "page_side" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN page_side TEXT NOT NULL DEFAULT 'unknown'")
            if "cover_fields_json" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN cover_fields_json TEXT NOT NULL DEFAULT '[]'")
            if "legacy_markdown" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN legacy_markdown TEXT")
            if "selected" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN selected INTEGER NOT NULL DEFAULT 1")
            if "source_id" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN source_id TEXT NOT NULL DEFAULT 'legacy'")
                connection.execute("ALTER TABLE pages ADD COLUMN source_page INTEGER NOT NULL DEFAULT 1")
                connection.execute("UPDATE pages SET source_page = number")
            if "position" not in columns:
                connection.execute("ALTER TABLE pages ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE pages SET position = number")
            layout_columns = {
                "render_strategy": "TEXT NOT NULL DEFAULT 'legacy_template'",
                "content_revision": "INTEGER NOT NULL DEFAULT 0",
                "layout_revision": "INTEGER NOT NULL DEFAULT 0",
                "generated_content_revision": "INTEGER",
                "layout_schema_version": "INTEGER",
                "layout_source_json": "TEXT",
                "source_metadata_json": "TEXT",
            }
            for name, definition in layout_columns.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE pages ADD COLUMN {name} {definition}")
            book_columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)")}
            if "paper_size" not in book_columns:
                connection.execute("ALTER TABLE books ADD COLUMN paper_size TEXT NOT NULL DEFAULT 'a4'")
            if "layout_json" not in book_columns:
                connection.execute("ALTER TABLE books ADD COLUMN layout_json TEXT NOT NULL DEFAULT '{}'")
            if "content_format" not in book_columns:
                connection.execute("ALTER TABLE books ADD COLUMN content_format TEXT NOT NULL DEFAULT 'markdown'")
            if "render_strategy" not in book_columns:
                connection.execute("ALTER TABLE books ADD COLUMN render_strategy TEXT NOT NULL DEFAULT 'legacy_template'")
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
            self._migrate_latex_content(connection)
            self._migrate_workflow(connection)
            self._migrate_v2(connection)

    def _backup_before_v2_migration(self) -> None:
        backup_path = self.data_root / "app-before-ocr-v2.db"
        if not self.db_path.is_file() or backup_path.exists():
            return
        with closing(sqlite3.connect(self.db_path)) as source:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='books'").fetchone():
                return
            if "selection_json" in {row[1] for row in source.execute("PRAGMA table_info(books)")}:
                return
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)

    def _migrate_v2(self, connection: sqlite3.Connection) -> None:
        """Incremental only; existing revisions and run stage data are untouched."""
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)")}
        if "selection_revision" not in columns:
            connection.execute("ALTER TABLE books ADD COLUMN selection_revision INTEGER NOT NULL DEFAULT 0")
        if "selection_json" not in columns:
            connection.execute("ALTER TABLE books ADD COLUMN selection_json TEXT")
        for book in connection.execute("SELECT id FROM books WHERE selection_json IS NULL").fetchall():
            pages = connection.execute(
                "SELECT page_id,source_version FROM pages WHERE book_id=? AND selected=1 ORDER BY position,number", (book["id"],),
            ).fetchall()
            draft = SelectionDraft(book_id=book["id"], selection_revision=0,
                                   page_ids=[page["page_id"] for page in pages],
                                   source_versions={page["page_id"]: page["source_version"] for page in pages})
            connection.execute("UPDATE books SET selection_json=? WHERE id=?", (draft.model_dump_json(), book["id"]))
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(workflow_runs)")}
        if "workflow_version" not in columns:
            connection.execute("ALTER TABLE workflow_runs ADD COLUMN workflow_version INTEGER NOT NULL DEFAULT 1")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(workflow_attempts)")}
        if "response_json" not in columns:
            connection.execute("ALTER TABLE workflow_attempts ADD COLUMN response_json TEXT")

    def _backup_before_workflow_migration(self) -> None:
        backup_path = self.data_root / "app-before-workflow.db"
        if not self.db_path.is_file() or backup_path.exists():
            return
        with closing(sqlite3.connect(self.db_path)) as source:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pages'").fetchone():
                return
            if "page_id" in {row[1] for row in source.execute("PRAGMA table_info(pages)")}:
                return
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)

    def _migrate_workflow(self, connection: sqlite3.Connection) -> None:
        page_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pages)")}
        for name, definition in {
            "page_id": "TEXT", "source_version": "INTEGER NOT NULL DEFAULT 1",
            "current_revision_id": "TEXT", "result_status": "TEXT",
            "manual_protected": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in page_columns:
                connection.execute(f"ALTER TABLE pages ADD COLUMN {name} {definition}")
        book_columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)")}
        for name in ("arrangement_revision", "output_settings_version"):
            if name not in book_columns:
                connection.execute(f"ALTER TABLE books ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        for statement in (
            "CREATE UNIQUE INDEX IF NOT EXISTS pages_page_id ON pages(page_id)",
            "CREATE TABLE IF NOT EXISTS workflow_revisions (revision_id TEXT PRIMARY KEY, "
            "book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, page_id TEXT NOT NULL, "
            "revision_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS workflow_runs (run_id TEXT PRIMARY KEY, "
            "book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, client_request_id TEXT NOT NULL, "
            "status TEXT NOT NULL, request_limit INTEGER NOT NULL, request_count INTEGER NOT NULL DEFAULT 0, "
            "snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, "
            "UNIQUE(book_id,client_request_id))",
            "CREATE TABLE IF NOT EXISTS workflow_tasks (run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) "
            "ON DELETE CASCADE, page_id TEXT NOT NULL, task_json TEXT NOT NULL, PRIMARY KEY(run_id,page_id))",
            "CREATE TABLE IF NOT EXISTS workflow_attempts (attempt_id TEXT PRIMARY KEY, "
            "run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE, "
            "page_id TEXT NOT NULL, legacy_attempt INTEGER NOT NULL, attempt_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS workflow_assessments (assessment_id TEXT PRIMARY KEY, "
            "book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, revision_id TEXT NOT NULL, "
            "assessment_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS export_manifests (manifest_id TEXT PRIMARY KEY, "
            "book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, manifest_json TEXT NOT NULL)",
        ):
            connection.execute(statement)
        rows = connection.execute("SELECT * FROM pages WHERE page_id IS NULL OR current_revision_id IS NULL").fetchall()
        for old_page in rows:
            page_id = old_page["page_id"] or str(uuid4())
            # Historical ready pages have no independent assessment. Preserve old work by default.
            protected = bool(old_page["text"] or old_page["status"] == "ready")
            connection.execute(
                "UPDATE pages SET page_id = ?, manual_protected = ? WHERE book_id = ? AND number = ?",
                (page_id, int(protected), old_page["book_id"], old_page["number"]),
            )
            page = connection.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            self._persist_current_revision(connection, page, origin="legacy")

    def _backup_before_layout_migration(self) -> None:
        backup_path = self.data_root / "app-before-layout.db"
        if not self.db_path.is_file() or backup_path.exists():
            return
        with closing(sqlite3.connect(self.db_path)) as source:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pages'").fetchone():
                return
            columns = {row[1] for row in source.execute("PRAGMA table_info(pages)")}
            if "layout_source_json" in columns:
                return
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)

    def _backup_before_latex_migration(self) -> None:
        backup_path = self.data_root / "app-before-latex.db"
        if not self.db_path.is_file() or backup_path.exists():
            return
        with closing(sqlite3.connect(self.db_path)) as source:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'books'").fetchone():
                return
            columns = {row[1] for row in source.execute("PRAGMA table_info(books)")}
            if "content_format" in columns and not source.execute(
                "SELECT 1 FROM books WHERE content_format != 'latex' LIMIT 1"
            ).fetchone():
                return
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)

    @staticmethod
    def _migrate_latex_content(connection: sqlite3.Connection) -> None:
        books = connection.execute("SELECT id FROM books WHERE content_format != 'latex'").fetchall()
        for book in books:
            pages = connection.execute(
                "SELECT number, text, page_kind FROM pages WHERE book_id = ?", (book["id"],)
            ).fetchall()
            for page in pages:
                latex = markdown_to_latex(page["text"]) if page["page_kind"] == "content" else ""
                connection.execute(
                    "UPDATE pages SET legacy_markdown = text, text = ?, extraction_json = NULL "
                    "WHERE book_id = ? AND number = ?",
                    (latex, book["id"], page["number"]),
                )
            connection.execute("UPDATE books SET content_format = 'latex' WHERE id = ?", (book["id"],))

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
            connection.execute("BEGIN IMMEDIATE")
            active_runs = connection.execute(
                "SELECT run_id,status FROM workflow_runs WHERE status IN ('queued','running','pausing')",
            ).fetchall()
            for run in active_runs:
                for task in self._tasks(connection, run["run_id"]):
                    if task.state == "running":
                        task.state = "interrupted"
                        task.error = "程序退出中断本阶段；已发送但未结算的请求不会自动重发"
                        self._write_task(connection, task)
                connection.execute(
                    "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    ("paused" if run["status"] == "pausing" else "interrupted", self._now(), run["run_id"]),
                )
            for row in connection.execute("SELECT * FROM workflow_attempts").fetchall():
                attempt = Attempt.model_validate_json(row["attempt_json"])
                if attempt.state == "reserved":
                    version = connection.execute("SELECT workflow_version FROM workflow_runs WHERE run_id=?", (attempt.run_id,)).fetchone()
                    if version["workflow_version"] == 2 and attempt.sent_at is None:
                        # A grouped repair may reserve its review before any send.
                        continue
                    if row["response_json"]:
                        response = RecognitionResponse.model_validate_json(row["response_json"])
                        attempt.state = "succeeded"
                        attempt.usage = response.usage
                        attempt.provider_request_id = response.provider_request_id
                        attempt.finished_at = self._now()
                        connection.execute("UPDATE workflow_attempts SET attempt_json=? WHERE attempt_id=?", (attempt.model_dump_json(), attempt.attempt_id))
                        continue
                    attempt.state = "unknown"
                    attempt.error = "请求已预留且可能发送，程序退出前未取得结算结果"
                    attempt.finished_at = self._now()
                    connection.execute(
                        "UPDATE workflow_attempts SET attempt_json = ? WHERE attempt_id = ?",
                        (attempt.model_dump_json(), attempt.attempt_id),
                    )
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
        return {**DEFAULT_SETTINGS, **_without_retired_settings(stored)}

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
                "INSERT INTO books(id,title,filename,status,page_count,completed_pages,error,created_at,content_format,render_strategy) "
                "VALUES (?,?,?,?,?,?,?,?,'latex','source_fidelity')",
                (book_id, title, filename, "uploaded", len(pages), 0, None, created_at),
            )
            connection.executemany(
                "INSERT INTO pages(book_id,number,width,height,image_name,status,source_page,position,render_strategy) "
                "VALUES (?,?,?,?,?,?,?,?,'source_fidelity')",
                [(book_id, number, width, height, name, "uploaded", number, number)
                 for number, width, height, name in pages],
            )
            connection.execute(
                "INSERT INTO source_files(book_id,id,filename,kind,page_count,position,directory) "
                "VALUES (?, 'legacy', ?, ?, ?, 0, '')",
                (book_id, filename, "pdf" if filename.lower().endswith(".pdf") else "image", len(pages)),
            )
            self._initialize_new_pages(connection, book_id)
            draft = self._selection(connection, book_id).model_copy(update={"selection_revision": 1})
            connection.execute("UPDATE books SET selection_json=?,selection_revision=1 WHERE id=?", (draft.model_dump_json(), book_id))
        book = self.get_book(book_id)
        assert book is not None
        return book

    def create_project(self, book_id: str, title: str) -> Book:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO books(id,title,filename,status,page_count,created_at,upload_confirmed,content_format,render_strategy,layout_json) "
                "VALUES (?,?,'','uploaded',0,?,0,'latex','source_fidelity',?)",
                (book_id, title, datetime.now(timezone.utc).isoformat(), LayoutSettings(source_fidelity_paper="source").model_dump_json()),
            )
            connection.execute("UPDATE books SET selection_json=? WHERE id=?", (SelectionDraft(book_id=book_id, selection_revision=0).model_dump_json(), book_id))
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
            connection.execute("BEGIN IMMEDIATE")
            selection = self._selection(connection, book_id)
            strategy = connection.execute(
                "SELECT render_strategy FROM books WHERE id = ?", (book_id,),
            ).fetchone()["render_strategy"]
            number = connection.execute(
                "SELECT COALESCE(MAX(number), 0) FROM pages WHERE book_id = ?", (book_id,),
            ).fetchone()[0]
            previous_number = number
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
                        "source_page,position,render_strategy) VALUES (?,?,?,?,?,'uploaded',?,?,?,?)",
                        (book_id, number, width, height,
                         f'{source["directory"]}/{image_name}', source["id"], source_page, position, strategy),
                    )
            connection.execute(
                "UPDATE books SET page_count = ?, upload_confirmed = 0, selection_confirmed = 0, "
                "status = 'uploaded', error = NULL, arrangement_revision = arrangement_revision + 1 "
                "WHERE id = ?", (number, book_id),
            )
            self._initialize_new_pages(connection, book_id)
            imported = connection.execute("SELECT page_id,source_version FROM pages WHERE book_id=? AND number>? ORDER BY position,number", (book_id, previous_number)).fetchall()
            if imported:
                draft = SelectionDraft(book_id=book_id, selection_revision=selection.selection_revision + 1,
                                       page_ids=selection.page_ids + [page["page_id"] for page in imported],
                                       source_versions={**selection.source_versions, **{page["page_id"]: page["source_version"] for page in imported}})
                connection.execute("UPDATE books SET selection_json=?,selection_revision=? WHERE id=?", (draft.model_dump_json(), draft.selection_revision, book_id))

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
            connection.execute(
                "UPDATE books SET selection_confirmed = 1, arrangement_revision = arrangement_revision + 1 WHERE id = ?",
                (book_id,),
            )
        self.refresh_book(book_id, processing=False)

    def list_books(self) -> list[Book]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
            return [self._book(connection, row) for row in rows]

    def get_book(self, book_id: str) -> Book | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            return self._book(connection, row) if row else None

    def save_layout(
        self, book_id: str, paper_size: PaperSize | None = None, layout: LayoutSettings | None = None,
        render_strategy: RenderStrategy | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if paper_size is not None:
                connection.execute("UPDATE books SET paper_size = ? WHERE id = ?", (paper_size, book_id))
            if layout is not None:
                connection.execute(
                    "UPDATE books SET layout_json = ? WHERE id = ?", (layout.model_dump_json(), book_id),
                )
            if render_strategy is not None:
                previous = connection.execute(
                    "SELECT render_strategy FROM books WHERE id = ?", (book_id,),
                ).fetchone()
                if previous is None:
                    raise KeyError("book")
                changed_pages = connection.execute(
                    "SELECT * FROM pages WHERE book_id = ? AND render_strategy = ? AND render_strategy != ?",
                    (book_id, previous["render_strategy"], render_strategy),
                ).fetchall()
                for page in changed_pages:
                    self._archive_page(connection, page)
                    connection.execute(
                        "UPDATE pages SET render_strategy = ?,layout_revision = layout_revision + 1 WHERE page_id = ?",
                        (render_strategy, page["page_id"]),
                    )
                    updated = connection.execute("SELECT * FROM pages WHERE page_id = ?", (page["page_id"],)).fetchone()
                    current = self._revision(connection, page["current_revision_id"])
                    self._persist_current_revision(connection, updated, origin=current.origin if current else "legacy")
                connection.execute(
                    "UPDATE books SET render_strategy = ? WHERE id = ?", (render_strategy, book_id),
                )
            connection.execute(
                "UPDATE books SET output_settings_version = output_settings_version + 1 WHERE id = ?", (book_id,),
            )
            connection.execute("UPDATE pages SET result_status = NULL WHERE book_id = ?", (book_id,))

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
            if added:
                connection.execute(
                    "UPDATE books SET arrangement_revision = arrangement_revision + 1 WHERE id = ?", (book_id,),
                )
        return added

    def remove_page(self, book_id: str, number: int) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE pages SET selected = 0 WHERE book_id = ? AND number = ?",
                (book_id, number),
            )
            if result.rowcount:
                connection.execute(
                    "UPDATE books SET arrangement_revision = arrangement_revision + 1 WHERE id = ?", (book_id,),
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

    def set_page_status(
        self, book_id: str, number: int, status: str, *,
        expected_content_revision: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE pages SET status = ?, error = NULL WHERE book_id = ? AND number = ? "
                "AND (? IS NULL OR content_revision = ?)",
                (status, book_id, number, expected_content_revision, expected_content_revision),
            )
        if result.rowcount:
            self.refresh_book(book_id, processing=True)
        return bool(result.rowcount)

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
        self, book_id: str, number: int, result: StructuredPageResult,
        *, expected_content_revision: int | None = None,
        source_metadata: PageSourceMetadata | None = None,
        generated_text: str | None = None,
        generator_version: str | None = None,
        render_strategy: RenderStrategy | None = None,
        layout_source: SourceFidelityLayout | None = None,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            page = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND number = ?", (book_id, number),
            ).fetchone()
            if page is None or page["manual_protected"] or (
                expected_content_revision is not None
                and expected_content_revision != page["content_revision"]
            ):
                return False
            content_revision = page["content_revision"] + 1
            layout_revision = page["layout_revision"]
            text = result.body_latex
            strategy = render_strategy or page["render_strategy"]
            generated_revision = None
            layout_json = page["layout_source_json"]
            schema_version = page["layout_schema_version"]
            metadata_json = page["source_metadata_json"]
            if source_metadata is not None:
                self._check_source_identity(source_metadata, page)
                metadata_json = source_metadata.model_dump_json()
            if is_latex_document(text):
                strategy = "custom_latex"
            if result.layout is not None:
                if source_metadata is None:
                    raise ValueError("保存布局必须提供程序来源元数据")
                if layout_source is not None and layout_source.model_dump(
                    include=set(LayoutObservation.model_fields),
                ) != result.layout.model_dump():
                    raise ValueError("生成布局与识别原行内容不一致")
                layout_revision += 1
                if strategy == "source_fidelity" and generated_text is not None:
                    text = generated_text
                    generated_revision = content_revision
                layout = layout_source or SourceFidelityLayout(
                    **result.layout.model_dump(), source=source_metadata,
                    content_revision=content_revision, layout_revision=layout_revision,
                )
                layout = layout.model_copy(update={
                    "source": source_metadata,
                    "content_revision": content_revision,
                    "layout_revision": layout_revision,
                    "generated_content_revision": generated_revision,
                    "generator_version": generator_version,
                })
                layout_json = layout.model_dump_json()
                schema_version = layout.schema_version
            elif layout_source is not None:
                raise ValueError("没有观察布局时不能保存布局来源")
            self._archive_page(connection, page)
            connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ?, "
                "page_kind = ?, page_side = ?, cover_fields_json = ?, "
                "header_segments_json = ?, footer_segments_json = ?, extraction_json = ?, "
                "render_strategy = ?, content_revision = ?, layout_revision = ?, "
                "generated_content_revision = ?, layout_schema_version = ?, "
                "layout_source_json = ?, source_metadata_json = ? "
                "WHERE book_id = ? AND number = ?",
                (
                    text,
                    result.page_kind,
                    result.page_side,
                    json.dumps([field.model_dump() for field in result.cover_fields], ensure_ascii=False),
                    json.dumps([segment.model_dump() for segment in result.header_segments], ensure_ascii=False),
                    json.dumps([segment.model_dump() for segment in result.footer_segments], ensure_ascii=False),
                    result.model_dump_json(), strategy, content_revision, layout_revision,
                    generated_revision, schema_version, layout_json, metadata_json,
                    book_id, number,
                ),
            )
            self._persist_current_revision(
                connection, connection.execute("SELECT * FROM pages WHERE page_id = ?", (page["page_id"],)).fetchone(),
                origin="automatic",
            )
        self.refresh_book(book_id, processing=True)
        return True

    def fail_page(
        self, book_id: str, number: int, message: str, *,
        expected_content_revision: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE pages SET status = 'failed', error = ? WHERE book_id = ? AND number = ? "
                "AND (? IS NULL OR content_revision = ?)",
                (message[:1000], book_id, number, expected_content_revision, expected_content_revision),
            )
        if result.rowcount:
            self.refresh_book(book_id, processing=True)
        return bool(result.rowcount)

    def interrupt_page(
        self, book_id: str, number: int, *, expected_content_revision: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE pages SET status = 'interrupted', error = ? WHERE book_id = ? AND number = ? "
                "AND (? IS NULL OR content_revision = ?)",
                ("处理被中断，可重新处理", book_id, number,
                 expected_content_revision, expected_content_revision),
            )
        return bool(result.rowcount)

    def save_manual_text(
        self, book_id: str, number: int, text: str, *,
        page_kind: PageKind | None = None, cover_fields: list[CoverField] | None = None,
        processing: bool = False,
        expected_content_revision: int | None = None,
        expected_layout_revision: int | None = None,
        render_strategy: RenderStrategy | None = None,
    ) -> Page:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            page = connection.execute(
                "SELECT * FROM pages "
                "WHERE book_id = ? AND number = ? AND selected = 1",
                (book_id, number),
            ).fetchone()
            if page is None:
                raise KeyError("page")
            self._check_revisions(page, expected_content_revision, expected_layout_revision)
            kind = page_kind if page_kind is not None else page["page_kind"]
            strategy = render_strategy or page["render_strategy"]
            detached = text != page["text"] or kind != page["page_kind"]
            if kind == "content":
                if cover_fields:
                    raise ValueError("正文页不能包含封面书目信息")
                fields_json = "[]"
                page_side = page["page_side"]
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
                page_side = "unknown"
            if kind == "content" and detached:
                if render_strategy == "source_fidelity":
                    raise ValueError("自由源码已修改，请通过布局校准重新生成原书还原源码")
                strategy = "custom_latex"
            content_revision = page["content_revision"] + 1
            generated_revision = None if detached else page["generated_content_revision"]
            layout_json = page["layout_source_json"]
            layout = (SourceFidelityLayout.model_validate_json(layout_json)
                      if not detached and layout_json is not None else None)
            if strategy == "source_fidelity" and layout is not None:
                if layout.content_revision != page["content_revision"]:
                    raise ValueError("布局与当前源码已脱离，请先保存布局校准")
            if generated_revision is not None:
                generated_revision = content_revision
            if layout is not None and layout.content_revision == page["content_revision"]:
                layout = layout.model_copy(update={
                    "content_revision": content_revision,
                    "generated_content_revision": generated_revision,
                })
                layout_json = layout.model_dump_json()
            self._archive_page(connection, page)
            connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ?, page_kind = ?, page_side = ?, "
                "cover_fields_json = ?, header_segments_json = ?, footer_segments_json = ?, "
                "render_strategy = ?, content_revision = ?, generated_content_revision = ?, layout_source_json = ? "
                "WHERE book_id = ? AND number = ?",
                (text, kind, page_side, fields_json, header_json, footer_json, strategy,
                 content_revision, generated_revision, layout_json, book_id, number),
            )
            connection.execute(
                "UPDATE pages SET manual_protected = 1, result_status = NULL WHERE page_id = ?", (page["page_id"],),
            )
            self._persist_current_revision(
                connection, connection.execute("SELECT * FROM pages WHERE page_id = ?", (page["page_id"],)).fetchone(),
                origin="manual",
            )
            saved = self._saved_page(connection, book_id, number)
        self.refresh_book(book_id, processing=processing)
        return saved

    def save_page_layout(
        self, book_id: str, number: int, layout: SourceFidelityLayout, generated_text: str,
        expected_content_revision: int, expected_layout_revision: int,
    ) -> Page:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            page = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND number = ? AND selected = 1",
                (book_id, number),
            ).fetchone()
            if page is None:
                raise KeyError("page")
            self._check_revisions(page, expected_content_revision, expected_layout_revision)
            self._check_source_identity(layout.source, page)
            if page["page_kind"] != "content":
                raise ValueError("封面和封底不能保存正文布局")
            content_revision = page["content_revision"] + 1
            layout_revision = page["layout_revision"] + 1
            layout = layout.model_copy(update={
                "content_revision": content_revision,
                "layout_revision": layout_revision,
                "generated_content_revision": content_revision,
            })
            self._archive_page(connection, page)
            connection.execute(
                "UPDATE pages SET status = 'ready', error = NULL, text = ?, render_strategy = 'source_fidelity', "
                "content_revision = ?, layout_revision = ?, generated_content_revision = ?, "
                "layout_schema_version = ?, layout_source_json = ?, source_metadata_json = ? "
                "WHERE book_id = ? AND number = ?",
                (generated_text, content_revision, layout_revision, content_revision,
                 layout.schema_version, layout.model_dump_json(), layout.source.model_dump_json(), book_id, number),
            )
            connection.execute(
                "UPDATE pages SET manual_protected = 1, result_status = NULL WHERE page_id = ?", (page["page_id"],),
            )
            self._persist_current_revision(
                connection, connection.execute("SELECT * FROM pages WHERE page_id = ?", (page["page_id"],)).fetchone(),
                origin="manual",
            )
            saved = self._saved_page(connection, book_id, number)
            processing = connection.execute(
                "SELECT status FROM books WHERE id = ?", (book_id,),
            ).fetchone()["status"] in {"processing", "pausing"}
        self.refresh_book(book_id, processing=processing)
        return saved

    @staticmethod
    def _check_revisions(
        page: sqlite3.Row, content_revision: int | None, layout_revision: int | None,
    ) -> None:
        if (content_revision is not None and content_revision != page["content_revision"]) or (
            layout_revision is not None and layout_revision != page["layout_revision"]
        ):
            raise RevisionConflict("页面已更新，请加载最新版本后再保存")

    @staticmethod
    def _check_source_identity(source: PageSourceMetadata, page: sqlite3.Row) -> None:
        if (source.book_id, source.page_number, source.source_id, source.source_page) != (
            page["book_id"], page["number"], page["source_id"], page["source_page"],
        ):
            raise ValueError("布局来源与项目页面不一致")
        if (source.page_id and source.page_id != page["page_id"]) or source.source_version != page["source_version"]:
            raise ValueError("布局来源版本与项目页面不一致")
        if source.source_file_id and source.source_file_id != page["source_id"]:
            raise ValueError("布局源资产与项目页面不一致")

    @staticmethod
    def _archive_page(connection: sqlite3.Connection, page: sqlite3.Row) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO page_versions(book_id,page_number,content_revision,snapshot_json) VALUES (?,?,?,?)",
            (page["book_id"], page["number"], page["content_revision"],
             json.dumps(dict(page), ensure_ascii=False)),
        )

    @classmethod
    def _saved_page(cls, connection: sqlite3.Connection, book_id: str, number: int) -> Page:
        row = connection.execute(
            "SELECT pages.*, source_files.filename AS source_filename FROM pages JOIN source_files "
            "ON pages.book_id = source_files.book_id AND pages.source_id = source_files.id "
            "WHERE pages.book_id = ? AND number = ?", (book_id, number),
        ).fetchone()
        return cls._page(connection, row)

    def get_page_versions(self, book_id: str, number: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM page_versions WHERE book_id = ? AND page_number = ? "
                "ORDER BY content_revision", (book_id, number),
            ).fetchall()
        return [json.loads(row["snapshot_json"]) for row in rows]

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
        data["layout"] = LayoutSettings.model_validate_json(data.pop("layout_json"))
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
            page_id=row["page_id"], source_version=row["source_version"],
            current_revision_id=row["current_revision_id"], result_status=row["result_status"],
            manual_protected=bool(row["manual_protected"]),
            source_id=row["source_id"], source_filename=row["source_filename"], source_page=row["source_page"],
            text=row["text"],
            render_strategy=row["render_strategy"],
            content_revision=row["content_revision"],
            layout_revision=row["layout_revision"],
            generated_content_revision=row["generated_content_revision"],
            layout_source=(SourceFidelityLayout.model_validate_json(row["layout_source_json"])
                           if row["layout_source_json"] is not None else None),
            source_metadata=(PageSourceMetadata.model_validate_json(row["source_metadata_json"])
                             if row["source_metadata_json"] is not None else None),
            page_kind=row["page_kind"],
            page_side=row["page_side"],
            cover_fields=[CoverField.model_validate(value) for value in json.loads(row["cover_fields_json"])],
            header_segments=[MarginSegment.model_validate(value) for value in
                             json.loads(row["header_segments_json"])],
            footer_segments=[MarginSegment.model_validate(value) for value in
                             json.loads(row["footer_segments_json"])],
            usage=cls._usage(attempts, bool(row["usage_unknown"])),
            attempts=len(attempts),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _initialize_new_pages(self, connection: sqlite3.Connection, book_id: str) -> None:
        for row in connection.execute(
            "SELECT number FROM pages WHERE book_id = ? AND page_id IS NULL", (book_id,),
        ).fetchall():
            connection.execute(
                "UPDATE pages SET page_id = ? WHERE book_id = ? AND number = ?",
                (str(uuid4()), book_id, row["number"]),
            )
            page = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND number = ?", (book_id, row["number"]),
            ).fetchone()
            self._persist_current_revision(connection, page, origin="legacy")

    def _persist_current_revision(
        self, connection: sqlite3.Connection, page: sqlite3.Row, *, origin: str,
    ) -> Revision:
        metadata = (PageSourceMetadata.model_validate_json(page["source_metadata_json"])
                    if page["source_metadata_json"] else None)
        if metadata is not None:
            metadata = metadata.model_copy(update={
                "page_id": page["page_id"], "source_file_id": page["source_id"],
                "source_version": page["source_version"],
            })
        layout = (SourceFidelityLayout.model_validate_json(page["layout_source_json"])
                  if page["layout_source_json"] else None)
        if layout is not None and metadata is not None:
            layout = layout.model_copy(update={"source": metadata})
        if layout is not None and layout.content_revision == page["content_revision"]:
            layout = layout.model_copy(update={"layout_revision": page["layout_revision"]})
        revision = Revision(
            revision_id=str(uuid4()), parent_revision_id=page["current_revision_id"],
            book_id=page["book_id"], page_id=page["page_id"], page_number=page["number"],
            source_version=page["source_version"], content_revision=page["content_revision"],
            layout_revision=page["layout_revision"], origin=origin, run_id=None,
            text=page["text"], render_strategy=page["render_strategy"], layout_source=layout,
            source_metadata=metadata, page_kind=page["page_kind"], page_side=page["page_side"],
            cover_fields=[CoverField.model_validate(item) for item in json.loads(page["cover_fields_json"])],
            header_segments=[MarginSegment.model_validate(item) for item in json.loads(page["header_segments_json"])],
            footer_segments=[MarginSegment.model_validate(item) for item in json.loads(page["footer_segments_json"])],
            generated_content_revision=page["generated_content_revision"],
            generator_version=layout.generator_version if layout else None, created_at=self._now(),
        )
        self._insert_revision(connection, revision)
        connection.execute(
            "UPDATE pages SET current_revision_id = ?, result_status = NULL,source_metadata_json = ?,layout_source_json = ? WHERE page_id = ?",
            (revision.revision_id, metadata.model_dump_json() if metadata else None,
             layout.model_dump_json() if layout else None, revision.page_id),
        )
        return revision

    @staticmethod
    def _insert_revision(connection: sqlite3.Connection, revision: Revision) -> None:
        connection.execute(
            "INSERT INTO workflow_revisions(revision_id,book_id,page_id,revision_json) VALUES (?,?,?,?)",
            (revision.revision_id, revision.book_id, revision.page_id, revision.model_dump_json()),
        )

    @staticmethod
    def _revision(connection: sqlite3.Connection, revision_id: str) -> Revision | None:
        row = connection.execute(
            "SELECT revision_json FROM workflow_revisions WHERE revision_id = ?", (revision_id,),
        ).fetchone()
        return Revision.model_validate_json(row["revision_json"]) if row else None

    def get_revision(self, revision_id: str) -> Revision | None:
        with self._connect() as connection:
            return self._revision(connection, revision_id)

    @staticmethod
    def _tasks(connection: sqlite3.Connection, run_id: str) -> list[PageTask]:
        tasks = [PageTask.model_validate_json(row["task_json"]) for row in connection.execute(
            "SELECT task_json FROM workflow_tasks WHERE run_id = ?", (run_id,),
        ).fetchall()]
        return sorted(tasks, key=lambda task: task.position)

    @staticmethod
    def _task(connection: sqlite3.Connection, run_id: str, page_id: str) -> PageTask:
        row = connection.execute(
            "SELECT task_json FROM workflow_tasks WHERE run_id = ? AND page_id = ?", (run_id, page_id),
        ).fetchone()
        if row is None:
            raise KeyError("task")
        return PageTask.model_validate_json(row["task_json"])

    @staticmethod
    def _write_task(connection: sqlite3.Connection, task: PageTask) -> None:
        connection.execute(
            "UPDATE workflow_tasks SET task_json = ? WHERE run_id = ? AND page_id = ?",
            (task.model_dump_json(), task.run_id, task.page_id),
        )

    @classmethod
    def _run(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> Run:
        snapshot = json.loads(row["snapshot_json"])
        tasks = cls._tasks(connection, row["run_id"])
        attempts = [Attempt.model_validate_json(item["attempt_json"]) for item in connection.execute(
            "SELECT attempt_json FROM workflow_attempts WHERE run_id = ?", (row["run_id"],),
        ).fetchall()]
        usage_attempts = [attempt for attempt in attempts if attempt.state != "cancelled"]
        def total(field: str) -> int | None:
            values = [getattr(item.usage, field) for item in usage_attempts if getattr(item.usage, field) is not None]
            return sum(values) if values else None
        return Run(
            workflow_version=snapshot.get("workflow_version", 1),
            recognition_scope=snapshot.get("recognition_scope", "legacy_all_visible"),
            run_id=row["run_id"], book_id=row["book_id"],
            arrangement_revision=snapshot["arrangement_revision"], page_ids=snapshot["page_ids"],
            settings_snapshot=_without_retired_settings(snapshot["settings"]), policy=snapshot["policy"], status=row["status"],
            request_limit=row["request_limit"], request_count=row["request_count"],
            generator_version=snapshot["generator_version"], created_at=row["created_at"],
            updated_at=row["updated_at"], error=row["error"], tasks=tasks,
            export_manifest_id=snapshot.get("export_manifest_id"),
            selection_revision=snapshot.get("selection_revision"),
            continuation_run_id=snapshot.get("continuation_run_id"),
            output_snapshot_id=snapshot.get("output_snapshot_id"),
            continuation_outputs_needed=snapshot.get("continuation_outputs_needed", False),
            usage=Usage(input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
                        total_tokens=total("total_tokens"),
                        complete=bool(usage_attempts) and all(item.usage.complete and item.state not in {"reserved", "unknown"} for item in usage_attempts)),
            counts=RunCounts(
                total=len(tasks), completed=sum(task.outcome is not None if snapshot.get("workflow_version", 1) == 2 else task.result_status is not None for task in tasks),
                auto_passed=sum(task.result_status == "auto_passed" for task in tasks),
                completed_with_issues=sum(task.result_status == "completed_with_issues" for task in tasks),
                failed=sum(task.result_status == "failed" for task in tasks),
                editable=sum(cls._outcome_category(task.outcome) == "editable" for task in tasks),
                partial_content=sum(cls._outcome_category(task.outcome) == "partial_content" for task in tasks),
                regions_preserved=sum(cls._outcome_category(task.outcome) == "regions_preserved" for task in tasks),
                page_preserved=sum(cls._outcome_category(task.outcome) == "page_preserved" for task in tasks),
                no_result=sum(cls._outcome_category(task.outcome) == "no_result" for task in tasks),
                protected_existing=sum(bool(task.outcome and task.outcome.protected_existing) for task in tasks),
            ),
        )

    def create_run(
        self, book_id: str, request: RunCreate, *, settings_snapshot: dict[str, Any] | None = None,
        generator_version: str = "workflow-v1",
    ) -> Run:
        settings = self.get_settings() if settings_snapshot is None else settings_snapshot
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM workflow_runs WHERE book_id = ? AND client_request_id = ?",
                (book_id, request.client_request_id),
            ).fetchone()
            if previous:
                return self._run(connection, previous)
            book = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            if book is None:
                raise KeyError("book")
            workflow_version = 2
            if request.selection_revision is None and book["arrangement_revision"] != request.expected_arrangement_revision:
                raise RevisionConflict("页序已更新，请使用最新编排启动任务")
            if connection.execute(
                "SELECT 1 FROM workflow_runs WHERE book_id = ? AND status IN ('queued','running','pausing','paused')",
                (book_id,),
            ).fetchone():
                raise RevisionConflict("项目已有未结束任务，请先完成或恢复该任务")
            records = connection.execute(
                "SELECT pages.*, source_files.filename AS source_filename, source_files.kind AS source_kind, "
                "source_files.directory AS source_directory, source_files.page_count AS source_page_count "
                "FROM pages JOIN source_files ON pages.book_id = source_files.book_id AND pages.source_id = source_files.id "
                "WHERE pages.book_id = ?" + (" AND selected=1" if request.selection_revision is None else "") + " ORDER BY pages.position,pages.number", (book_id,),
            ).fetchall()
            continuation = None
            continuation_outputs_needed = False
            if workflow_version == 2:
                if generator_version == "workflow-v1":
                    generator_version = "workflow-v2"
                draft = self._selection(connection, book_id)
                if request.selection_revision is not None and (draft.selection_revision != request.selection_revision or not draft.valid):
                    raise RevisionConflict("来源或选页已更新，请回到选页保存当前选择")
                by_id = {row["page_id"]: row for row in records}
                if request.selection_revision is not None:
                    records = [by_id[page_id] for page_id in draft.page_ids]
                if request.continuation_run_id is not None:
                    old = connection.execute("SELECT * FROM workflow_runs WHERE book_id=? AND run_id=?", (book_id, request.continuation_run_id)).fetchone()
                    if old is None or old["workflow_version"] != 2 or old["status"] not in {"finished", "failed", "interrupted"}:
                        raise ValueError("补做只能关联已结束或中断的新版运行")
                    continuation = {task.page_id: task for task in self._tasks(connection, old["run_id"])}
                    old_frozen = json.loads(old["snapshot_json"])
                    old_output = connection.execute("SELECT manifest_json FROM export_manifests WHERE manifest_id=?", (old_frozen.get("output_snapshot_id"),)).fetchone()
                    continuation_outputs_needed = not old_output or any(
                        value.status != "available" for value in OutputSnapshot.model_validate_json(old_output["manifest_json"]).formats.values()
                    )
                    # The new snapshot includes the saved selection, including
                    # successful references. Only missing phases receive work.
                    if any(row["page_id"] not in continuation for row in records):
                        raise RevisionConflict("补做选择必须来自关联运行；新增页面请启动新的识别")
            selected_ids = set(request.page_ids) if request.page_ids is not None else None
            selected_numbers = set(request.pages) if request.pages is not None else None
            if selected_ids is not None:
                if not selected_ids <= {row["page_id"] for row in records}:
                    raise ValueError("处理范围包含未编排页面")
                records = [row for row in records if row["page_id"] in selected_ids]
            if selected_numbers is not None:
                if not selected_numbers <= {row["number"] for row in records}:
                    raise ValueError("处理范围包含未编排页面")
                records = [row for row in records if row["number"] in selected_numbers]
            if not records:
                raise ValueError("处理范围不能为空")
            page_ids = [row["page_id"] for row in records]
            if not set(request.policy.replace_page_ids) <= set(page_ids):
                raise ValueError("人工稿替换范围必须属于本轮处理范围")
            safe_settings = {key: settings.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
            safe_settings["recognition_scope"] = "printed_original_only" if workflow_version == 2 else "legacy_all_visible"
            safe_settings.update(paper_size=book["paper_size"], layout=json.loads(book["layout_json"]),
                                 render_strategy=book["render_strategy"], output_settings_version=book["output_settings_version"],
                                 title=book["title"], filename=book["filename"])
            source_fields = ("page_id", "number", "source_id", "source_page", "source_version", "width", "height",
                             "image_name", "source_filename", "source_kind", "source_directory", "source_page_count")
            run_id, now = str(uuid4()), self._now()
            snapshot = {"workflow_version": workflow_version,
                        "recognition_scope": safe_settings["recognition_scope"],
                        "selection_revision": draft.selection_revision if workflow_version == 2 else request.selection_revision,
                        "continuation_run_id": request.continuation_run_id,
                        "continuation_outputs_needed": continuation_outputs_needed,
                        "arrangement_revision": book["arrangement_revision"], "page_ids": page_ids,
                        "settings": safe_settings, "policy": request.policy.model_dump(),
                        "generator_version": generator_version,
                        "sources": {row["page_id"]: {key: row[key] for key in source_fields} for row in records}}
            connection.execute(
                "INSERT INTO workflow_runs(run_id,book_id,client_request_id,status,request_limit,snapshot_json,created_at,updated_at,workflow_version) "
                "VALUES (?,?,?,'queued',?,?,?,?,?)",
                (run_id, book_id, request.client_request_id,
                 request.request_limit if request.request_limit is not None else (request.policy.requests_per_page if workflow_version == 2 else 3) * len(records),
                 json.dumps(snapshot, ensure_ascii=False), now, now, workflow_version),
            )
            for position, row in enumerate(records):
                task = PageTask(
                    run_id=run_id, page_id=row["page_id"], page_number=row["number"], source_version=row["source_version"],
                    position=position, base_revision_id=row["current_revision_id"],
                    base_content_revision=row["content_revision"], base_layout_revision=row["layout_revision"],
                )
                task.stage_data["recognition_scope"] = safe_settings["recognition_scope"]
                if workflow_version == 2:
                    task.stage_data["protected_existing"] = bool(row["manual_protected"] and row["page_id"] not in request.policy.replace_page_ids)
                    if continuation is not None:
                        self._inherit_v2_candidate(connection, task, continuation[row["page_id"]], book_id)
                        old_task = continuation[row["page_id"]]
                        same_output_settings = old_frozen["settings"]["output_settings_version"] == book["output_settings_version"]
                        if not same_output_settings and task.candidate_revision_id is not None:
                            inherited = self._revision(connection, task.candidate_revision_id)
                            if inherited:
                                # Output changes invalidate every inherited layout,
                                # independently of the previous quality conclusion.
                                updated = inherited.model_copy(update={"revision_id": str(uuid4()), "parent_revision_id": inherited.revision_id,
                                                                       "page_layout": None, "layout_source": None,
                                                                       "generated_content_revision": None, "created_at": self._now()})
                                self._insert_revision(connection, updated)
                                task.candidate_revision_id = updated.revision_id
                                task.completed_stages = [stage for stage in task.completed_stages if stage != "layout"]
                                task.stage = "layout" if task.stage_data.get("basic_review_complete") else "review"
                        if (same_output_settings and task.candidate_revision_id is not None
                            and old_task.outcome is not None and old_task.outcome.content == "usable"
                            and old_task.outcome.layout == "faithful" and old_task.source_version == row["source_version"]):
                            task.outcome = old_task.outcome
                            task.state, task.stage = "finished", "export"
                            task.stage_data["reused_complete_result"] = True
                elif row["manual_protected"] and row["page_id"] not in request.policy.replace_page_ids:
                    task.stage, task.state, task.result_status = "finalize", "succeeded", "completed_with_issues"
                    task.stage_data = {"protected_manual": True, "final_revision_id": row["current_revision_id"],
                                       "candidate_not_adopted_reason": "启动时默认保护已有人工稿；历史内容未经过本轮自动复核"}
                connection.execute(
                    "INSERT INTO workflow_tasks(run_id,page_id,task_json) VALUES (?,?,?)",
                    (run_id, row["page_id"], task.model_dump_json()),
                )
            return self._run(connection, connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_run(self, book_id: str, run_id: str) -> Run | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workflow_runs WHERE book_id = ? AND run_id = ?", (book_id, run_id)).fetchone()
            return self._run(connection, row) if row else None

    def get_run_book(self, book_id: str, run_id: str) -> Book:
        run = self.get_run(book_id, run_id)
        book = self.get_book(book_id)
        if run is None or book is None:
            raise KeyError("run or book")
        settings = run.settings_snapshot
        return book.model_copy(update={
            "paper_size": settings["paper_size"], "layout": LayoutSettings.model_validate(settings["layout"]),
            "render_strategy": settings["render_strategy"], "arrangement_revision": run.arrangement_revision,
            "output_settings_version": settings["output_settings_version"], "title": settings["title"],
            "filename": settings["filename"], "page_count": len(run.page_ids), "selected_page_count": len(run.page_ids),
        })

    def list_runs(self, book_id: str) -> list[Run]:
        with self._connect() as connection:
            return [self._run(connection, row) for row in connection.execute(
                "SELECT * FROM workflow_runs WHERE book_id = ? ORDER BY created_at DESC", (book_id,),
            ).fetchall()]

    def get_run_tasks(self, run_id: str) -> list[PageTask]:
        with self._connect() as connection:
            return self._tasks(connection, run_id)

    def get_task(self, run_id: str, page_id: str) -> PageTask | None:
        with self._connect() as connection:
            try:
                return self._task(connection, run_id, page_id)
            except KeyError:
                return None

    def update_task(
        self, run_id: str, page_id: str, *, stage: WorkflowStage | None = None,
        state: ExecutionStatus | None = None, candidate_revision_id: str | None = None,
        completed_stage: WorkflowStage | None = None, stage_data: dict | None = None,
        error: str | None | object = _UNSET, result_status: ResultStatus | None = None,
        workflow_error: WorkflowError | None = None,
    ) -> PageTask:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._task(connection, run_id, page_id)
            for name, value in (("stage", stage), ("state", state), ("candidate_revision_id", candidate_revision_id),
                                ("result_status", result_status)):
                if value is not None:
                    setattr(task, name, value)
            if error is not _UNSET:
                task.error = error
            if workflow_error is not None and workflow_error not in task.errors:
                task.errors.append(workflow_error)
            if completed_stage is not None and completed_stage not in task.completed_stages:
                task.completed_stages.append(completed_stage)
            if completed_stage == "review":
                task.stage_data["basic_review_complete"] = True
            if stage_data is not None:
                task.stage_data.update(stage_data)
            self._write_task(connection, task)
            connection.execute("UPDATE workflow_runs SET updated_at = ? WHERE run_id = ?", (self._now(), run_id))
            return task

    def set_run_status(self, book_id: str, run_id: str, status: RunStatus, error: str | None = None) -> Run:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM workflow_runs WHERE book_id = ? AND run_id = ?", (book_id, run_id)).fetchone():
                raise KeyError("run")
            tasks = self._tasks(connection, run_id)
            if status == "succeeded":
                if any(task.result_status is None for task in tasks):
                    raise ValueError("页面尚未全部形成终态，不能结束为成功")
                failed = sum(task.result_status == "failed" for task in tasks)
                if failed:
                    status, error = "failed", error or f"{failed} 页发生技术失败，输出不完整"
            if status == "finished" and any(task.outcome is None for task in tasks):
                raise ValueError("页面尚未形成明确结果，不能结束处理")
            connection.execute("UPDATE workflow_runs SET status = ?,error = ?,updated_at = ? WHERE run_id = ?",
                               (status, error, self._now(), run_id))
            legacy_status = {"queued": "processing", "running": "processing", "pausing": "pausing", "paused": "paused",
                             "succeeded": "ready", "failed": "failed", "interrupted": "interrupted", "finished": "ready"}[status]
            if status == "finished" and not any(task.outcome and task.outcome.content == "usable" for task in tasks):
                legacy_status = "failed"
            completed = sum(task.outcome is not None or task.result_status is not None for task in tasks)
            connection.execute("UPDATE books SET status = ?,completed_pages = ?,error = ? WHERE id = ?",
                               (legacy_status, completed, error, book_id))
            return self._run(connection, connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_run_page_record(self, run_id: str, page_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run")
            task = self._task(connection, run_id, page_id)
            snapshot = json.loads(row["snapshot_json"])
            record = connection.execute("SELECT * FROM pages WHERE page_id = ? AND book_id = ?", (page_id, row["book_id"])).fetchone()
            if record is None:
                raise KeyError("page")
            data = dict(record)
            data.update(snapshot["sources"][page_id])
            data.update(book_id=row["book_id"], content_revision=task.base_content_revision,
                        layout_revision=task.base_layout_revision, current_revision_id=task.base_revision_id,
                        settings_snapshot=_without_retired_settings(snapshot["settings"]))
            base = self._revision(connection, task.base_revision_id)
            if base is not None:
                data.update(text=base.text, render_strategy=base.render_strategy, page_kind=base.page_kind, page_side=base.page_side,
                            generated_content_revision=base.generated_content_revision,
                            layout_source_json=base.layout_source.model_dump_json() if base.layout_source else None,
                            source_metadata_json=base.source_metadata.model_dump_json() if base.source_metadata else None,
                            cover_fields_json=json.dumps([item.model_dump() for item in base.cover_fields], ensure_ascii=False),
                            header_segments_json=json.dumps([item.model_dump() for item in base.header_segments], ensure_ascii=False),
                            footer_segments_json=json.dumps([item.model_dump() for item in base.footer_segments], ensure_ascii=False))
            return data

    def reserve_attempt(
        self, run_id: str, page_id: str, stage: WorkflowStage, *, retry: bool = False,
        purpose: str | None = None, recovery_round: int | None = None,
        block_ids: list[str] | None = None, reservation_key: str | None = None,
        retry_of_attempt_id: str | None = None,
    ) -> Attempt:
        with self._connect() as connection:
            version = connection.execute("SELECT workflow_version FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if version is None:
            raise KeyError("run")
        if version["workflow_version"] == 2:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._reserve_v2_attempts(connection, run_id, page_id, [stage], retry=retry,
                                                 purpose=purpose, recovery_round=recovery_round,
                                                 block_ids=block_ids or [], reservation_key=reservation_key,
                                                 retry_of_attempt_id=retry_of_attempt_id)[0]
        if stage not in {"recognize", "verify", "repair"}:
            raise ValueError("只有识别、复核和修复阶段可预留模型请求")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError("run")
            if run["status"] != "running":
                raise RequestBudgetExceeded("任务已暂停或结束，不能启动新请求")
            task = self._task(connection, run_id, page_id)
            if task.result_status is not None or task.request_count >= 6:
                raise RequestBudgetExceeded("本页已结束或已达到六次物理请求上限")
            counter = {"recognize": "recognize_count", "verify": "review_count", "repair": "repair_count"}[stage]
            logical_limit = 2 if stage == "verify" else 1
            if retry:
                last = [attempt for attempt in self._attempts(connection, run_id, page_id) if attempt.stage == stage]
                if task.retry_count >= 2 or not last or last[-1].state != "failed":
                    raise RequestBudgetExceeded("暂时错误重试名额已用尽，或上次请求结果不能安全重发")
            elif getattr(task, counter) >= logical_limit:
                raise RequestBudgetExceeded("本阶段已达到逻辑调用轮数上限")
            elif stage == "verify" and task.review_count == 1 and task.repair_count == 0:
                raise RequestBudgetExceeded("第二次复核仅用于本页唯一一轮内容修复之后")
            # First requests earmark their basic allowance. Retry/repair requests may
            # only use the remainder after every unfinished page's first review.
            basic_needed = 0
            for other in self._tasks(connection, run_id):
                if other.result_status is not None or other.state == "failed":
                    continue
                recognize_needed = int(other.recognize_count == 0)
                review_needed = int(other.review_count == 0)
                if other.page_id == page_id and not retry:
                    if stage == "recognize":
                        recognize_needed = 0
                    elif stage == "verify":
                        review_needed = 0
                basic_needed += recognize_needed + review_needed
            basic_request = not retry and stage in {"recognize", "verify"} and getattr(task, counter) == 0
            reserved_for_other_stages = 0 if basic_request else basic_needed
            if run["request_count"] + 1 + reserved_for_other_stages > run["request_limit"]:
                raise RequestBudgetExceeded("本轮额度不足；已为未处理页面保留基本识别与复核名额")
            task.request_count += 1
            if retry:
                task.retry_count += 1
            else:
                setattr(task, counter, getattr(task, counter) + 1)
            self._write_task(connection, task)
            now = self._now()
            attempt = Attempt(attempt_id=str(uuid4()), run_id=run_id, page_id=page_id,
                              stage=stage, ordinal=task.request_count, retry=retry, created_at=now)
            legacy_attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM page_attempts WHERE book_id = ? AND page_number = ?",
                (run["book_id"], task.page_number),
            ).fetchone()[0]
            connection.execute("INSERT INTO page_attempts(book_id,page_number,attempt) VALUES (?,?,?)",
                               (run["book_id"], task.page_number, legacy_attempt))
            connection.execute(
                "INSERT INTO workflow_attempts(attempt_id,run_id,page_id,legacy_attempt,attempt_json) VALUES (?,?,?,?,?)",
                (attempt.attempt_id, run_id, page_id, legacy_attempt, attempt.model_dump_json()),
            )
            connection.execute("UPDATE workflow_runs SET request_count = request_count + 1,updated_at = ? WHERE run_id = ?",
                               (now, run_id))
            return attempt

    @staticmethod
    def _attempts(connection: sqlite3.Connection, run_id: str, page_id: str | None = None) -> list[Attempt]:
        rows = connection.execute(
            "SELECT attempt_json FROM workflow_attempts WHERE run_id = ?" + (" AND page_id = ?" if page_id else "") + " ORDER BY rowid",
            (run_id, page_id) if page_id else (run_id,),
        ).fetchall()
        return sorted((Attempt.model_validate_json(row["attempt_json"]) for row in rows), key=lambda attempt: attempt.created_at)

    def get_attempts(self, run_id: str, page_id: str | None = None) -> list[Attempt]:
        with self._connect() as connection:
            return self._attempts(connection, run_id, page_id)

    def finish_run_attempt(
        self, attempt_id: str, *, state: str, usage: Usage | None = None,
        provider_request_id: str | None = None, error: str | None = None, retryable: bool = False,
    ) -> Attempt:
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("请求结算状态不正确")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError("attempt")
            attempt = Attempt.model_validate_json(row["attempt_json"])
            if attempt.settled_at is not None or (attempt.state != "reserved" and not row["response_json"]):
                return attempt
            attempt.state, attempt.finished_at = state, self._now()
            attempt.settled_at = attempt.finished_at
            if usage is not None:
                attempt.usage = usage
            attempt.provider_request_id, attempt.error = provider_request_id, error
            attempt.retryable = state == "failed" and retryable
            connection.execute("UPDATE workflow_attempts SET attempt_json = ? WHERE attempt_id = ?",
                               (attempt.model_dump_json(), attempt_id))
            task = self._task(connection, attempt.run_id, attempt.page_id)
            run = connection.execute("SELECT book_id FROM workflow_runs WHERE run_id = ?", (attempt.run_id,)).fetchone()
            connection.execute(
                "UPDATE page_attempts SET returned = ?,input_tokens = ?,output_tokens = ?,total_tokens = ? "
                "WHERE book_id = ? AND page_number = ? AND attempt = ?",
                (int(state != "unknown"), attempt.usage.input_tokens, attempt.usage.output_tokens,
                 attempt.usage.total_tokens, run["book_id"], task.page_number, row["legacy_attempt"]),
            )
            return attempt

    def reserve_compile(self, run_id: str, page_id: str) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._task(connection, run_id, page_id)
            row = connection.execute("SELECT status,workflow_version,snapshot_json FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run")
            limit = RunPolicy.model_validate(json.loads(row["snapshot_json"])["policy"]).compile_limit if row["workflow_version"] == 2 else 4
            if row["status"] != "running" or task.compile_count >= limit:
                raise RequestBudgetExceeded(f"任务已停止或本页已达到 {limit} 次候选编译上限")
            task.compile_count += 1
            self._write_task(connection, task)
            return task.compile_count

    def save_candidate(
        self, run_id: str, page_id: str, *, layout_source: SourceFidelityLayout | None,
        text: str, page_kind: PageKind = "content", page_side: str = "unknown",
        cover_fields: list[CoverField] | None = None, header_segments: list[MarginSegment] | None = None,
        footer_segments: list[MarginSegment] | None = None, generator_version: str | None = None,
        parent_revision_id: str | None = None,
    ) -> Revision:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._task(connection, run_id, page_id)
            run = connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None or run["workflow_version"] != 1:
                raise ValueError("旧候选接口只用于旧运行；新版内容和布局必须分别保存")
            snapshot = json.loads(run["snapshot_json"])
            source = snapshot["sources"][page_id]
            if parent_revision_id is not None:
                parent = self._revision(connection, parent_revision_id)
                if parent is None or parent.page_id != page_id or parent.book_id != run["book_id"]:
                    raise ValueError("候选父修订不属于本页")
            content_revision, layout_revision = task.base_content_revision + 1, task.base_layout_revision + 1
            if layout_source is not None:
                metadata = layout_source.source
                if (metadata.book_id, metadata.page_id, metadata.source_id, metadata.source_page, metadata.source_version) != (
                    run["book_id"], page_id, source["source_id"], source["source_page"], task.source_version,
                ):
                    raise ValueError("候选布局与冻结源资产不一致")
                layout_source = layout_source.model_copy(update={
                    "content_revision": content_revision, "layout_revision": layout_revision,
                    "generated_content_revision": content_revision if text else None,
                    "generator_version": generator_version or snapshot["generator_version"],
                })
            revision = Revision(
                revision_id=str(uuid4()), parent_revision_id=parent_revision_id or task.candidate_revision_id or task.base_revision_id,
                book_id=run["book_id"], page_id=page_id, page_number=task.page_number,
                source_version=task.source_version, content_revision=content_revision, layout_revision=layout_revision,
                origin="automatic", run_id=run_id, text=text, render_strategy="source_fidelity",
                layout_source=layout_source, source_metadata=layout_source.source if layout_source else None,
                page_kind=page_kind, page_side=page_side, cover_fields=cover_fields or [],
                header_segments=header_segments or [], footer_segments=footer_segments or [],
                generated_content_revision=content_revision if layout_source is not None and text else None,
                generator_version=generator_version or snapshot["generator_version"], created_at=self._now(),
            )
            self._insert_revision(connection, revision)
            task.candidate_revision_id = revision.revision_id
            self._write_task(connection, task)
            return revision

    def save_assessment(self, assessment: Assessment) -> Assessment:
        with self._connect() as connection:
            revision = self._revision(connection, assessment.revision_id)
            task = self._task(connection, assessment.run_id, assessment.page_id)
            run = connection.execute("SELECT book_id FROM workflow_runs WHERE run_id = ?", (assessment.run_id,)).fetchone()
            related_revision = assessment.revision_id in {task.base_revision_id, task.stage_data.get("final_revision_id")}
            if (revision is None or revision.page_id != assessment.page_id or revision.book_id != run["book_id"]
                or (revision.run_id != assessment.run_id and not related_revision)):
                raise ValueError("复核必须关联本轮本页候选或已保留修订")
            if any((issue.run_id, issue.page_id, issue.revision_id) != (
                assessment.run_id, assessment.page_id, assessment.revision_id,
            ) for issue in assessment.issues):
                raise ValueError("问题必须关联同一候选修订")
            connection.execute(
                "INSERT INTO workflow_assessments(assessment_id,book_id,revision_id,assessment_json) VALUES (?,?,?,?)",
                (assessment.assessment_id, revision.book_id, revision.revision_id, assessment.model_dump_json()),
            )
            return assessment

    @staticmethod
    def _assessment(connection: sqlite3.Connection, revision_id: str, *, run_id: str | None = None) -> Assessment | None:
        query = "SELECT assessment_json FROM workflow_assessments WHERE revision_id = ? ORDER BY rowid DESC"
        if run_id is None:
            query += " LIMIT 1"
        # A retained revision may have conclusions from several runs.
        for row in connection.execute(query, (revision_id,)):
            assessment = Assessment.model_validate_json(row["assessment_json"])
            if run_id is None or assessment.run_id == run_id:
                return assessment
        return None

    def get_assessment(self, revision_id: str) -> Assessment | None:
        with self._connect() as connection:
            return self._assessment(connection, revision_id)

    def adopt_candidate(self, run_id: str, page_id: str, revision_id: str, result_status: ResultStatus) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._task(connection, run_id, page_id)
            run = connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None or run["workflow_version"] != 1:
                raise ValueError("新版运行应保存独立页面结果，不使用旧 success 采用接口")
            snapshot = json.loads(run["snapshot_json"])
            revision = self._revision(connection, revision_id)
            if revision is None or (revision.page_id, revision.run_id) != (page_id, run_id):
                raise ValueError("只能采用本轮本页候选")
            page = connection.execute("SELECT * FROM pages WHERE page_id = ? AND book_id = ?", (page_id, run["book_id"])).fetchone()
            if page is None:
                raise KeyError("page")
            authorized = not page["manual_protected"] or page_id in snapshot["policy"]["replace_page_ids"]
            matches = (page["current_revision_id"] == task.base_revision_id and page["content_revision"] == task.base_content_revision
                       and page["layout_revision"] == task.base_layout_revision and page["source_version"] == task.source_version)
            if not authorized or not matches or result_status == "failed":
                current_source = connection.execute(
                    "SELECT pages.*,source_files.filename AS source_filename,source_files.kind AS source_kind,"
                    "source_files.directory AS source_directory,source_files.page_count AS source_page_count "
                    "FROM pages JOIN source_files ON pages.book_id = source_files.book_id AND pages.source_id = source_files.id "
                    "WHERE pages.page_id = ?", (page_id,),
                ).fetchone()
                source_fields = ("page_id", "number", "source_id", "source_page", "source_version", "width", "height",
                                 "image_name", "source_filename", "source_kind", "source_directory", "source_page_count")
                task.stage_data.update(final_revision_id=page["current_revision_id"],
                                       final_source={key: current_source[key] for key in source_fields},
                                       candidate_not_adopted_reason=("候选技术失败；保留已有采用修订" if result_status == "failed"
                                                                    else "运行中页面已编辑或启动时未授权替换人工稿；保留当前采用修订"))
                task.result_status = "failed" if result_status == "failed" else "completed_with_issues"
                self._write_task(connection, task)
                return False
            assessment = self._assessment(connection, revision_id)
            if result_status == "auto_passed" and (assessment is None or any(
                getattr(assessment, name) != "passed" for name in ("content", "layout", "coverage")
            ) or any(issue.severity in {"error", "warning"} for issue in assessment.issues)
                or (revision.layout_source is not None and revision.layout_source.source_disposition != "transcribed")):
                raise ValueError("自动通过必须有内容、布局和覆盖结论，且不能包含不确定源图保留")
            self._archive_page(connection, page)
            layout = revision.layout_source
            current_output_version = connection.execute(
                "SELECT output_settings_version FROM books WHERE id = ?", (run["book_id"],),
            ).fetchone()[0]
            current_result_status = result_status if current_output_version == snapshot["settings"]["output_settings_version"] else None
            connection.execute(
                "UPDATE pages SET current_revision_id = ?,result_status = ?,status = ?,error = NULL,text = ?, "
                "render_strategy = ?,content_revision = ?,layout_revision = ?,generated_content_revision = ?, "
                "layout_schema_version = ?,layout_source_json = ?,source_metadata_json = ?,page_kind = ?,page_side = ?, "
                "cover_fields_json = ?,header_segments_json = ?,footer_segments_json = ?,manual_protected = 0 WHERE page_id = ?",
                (revision_id, current_result_status, "failed" if result_status == "failed" else "ready", revision.text,
                 revision.render_strategy, revision.content_revision, revision.layout_revision, revision.generated_content_revision,
                 layout.schema_version if layout else None, layout.model_dump_json() if layout else None,
                 revision.source_metadata.model_dump_json() if revision.source_metadata else None,
                 revision.page_kind, revision.page_side,
                 json.dumps([item.model_dump() for item in revision.cover_fields], ensure_ascii=False),
                 json.dumps([item.model_dump() for item in revision.header_segments], ensure_ascii=False),
                 json.dumps([item.model_dump() for item in revision.footer_segments], ensure_ascii=False), page_id),
            )
            task.stage_data["final_revision_id"] = revision_id
            task.result_status = result_status
            self._write_task(connection, task)
            return True

    def create_export_manifest(
        self, book_id: str, *, expected_arrangement_revision: int, page_ids: list[str] | None = None,
        run_id: str | None = None, generator_version: str = "workflow-v1",
    ) -> ExportManifest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            book = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            if book is None:
                raise KeyError("book")
            tasks: dict[str, PageTask] = {}
            if run_id is not None:
                run = connection.execute("SELECT * FROM workflow_runs WHERE book_id = ? AND run_id = ?", (book_id, run_id)).fetchone()
                if run is None:
                    raise KeyError("run")
                if run["workflow_version"] != 1:
                    raise ValueError("新版运行必须冻结 OutputSnapshot，不能生成旧导出清单")
                snapshot = json.loads(run["snapshot_json"])
                arrangement_revision = snapshot["arrangement_revision"]
                settings = snapshot["settings"]
                generator_version = snapshot["generator_version"]
                tasks = {task.page_id: task for task in self._tasks(connection, run_id)}
                order = snapshot["page_ids"]
                sources = snapshot["sources"]
            else:
                arrangement_revision = book["arrangement_revision"]
                settings = {"paper_size": book["paper_size"], "layout": json.loads(book["layout_json"]),
                            "render_strategy": book["render_strategy"], "output_settings_version": book["output_settings_version"],
                            "title": book["title"], "filename": book["filename"]}
                rows = connection.execute(
                    "SELECT pages.*,source_files.filename AS source_filename FROM pages JOIN source_files "
                    "ON pages.book_id = source_files.book_id AND pages.source_id = source_files.id "
                    "WHERE pages.book_id = ? AND selected = 1 ORDER BY pages.position,pages.number", (book_id,),
                ).fetchall()
                sources = {row["page_id"]: dict(row) for row in rows}
                order = [row["page_id"] for row in rows]
            if arrangement_revision != expected_arrangement_revision:
                raise RevisionConflict("导出页序修订与所选快照不一致")
            if page_ids is not None:
                if not set(page_ids) <= set(order):
                    raise ValueError("导出范围包含快照之外的页面")
                scope = set(page_ids)
                order = [page_id for page_id in order if page_id in scope]
            if not order:
                raise ValueError("导出范围不能为空")
            entries, issues = [], []
            for position, page_id in enumerate(order):
                task = tasks.get(page_id)
                source = task.stage_data.get("final_source", sources[page_id]) if task else sources[page_id]
                revision_id = (task.stage_data.get("final_revision_id") or task.base_revision_id) if task else source["current_revision_id"]
                revision = self._revision(connection, revision_id)
                if revision is None:
                    raise ValueError("输出页面缺少已保存修订")
                if run_id is None and ((revision.page_content and revision.page_content.recognition_scope == "printed_original_only")
                    or (revision.layout_source and revision.layout_source.recognition_scope == "printed_original_only")):
                    settings["recognition_scope"] = "printed_original_only"
                status = task.result_status if task else source["result_status"]
                assessment = self._assessment(connection, revision_id, run_id=run_id)
                explanation = task.stage_data.get("candidate_not_adopted_reason") if task else None
                if task and (task.stage_data.get("protected_manual") or explanation):
                    if status != "failed":
                        status = "completed_with_issues"
                    preserved_issues = [Issue(
                        issue_id=str(uuid4()), run_id=task.run_id, page_id=page_id, revision_id=revision_id,
                        category="preserved_revision", severity="warning", reason=explanation or "保留已有稿件",
                        disposition="保留采用版本；本轮候选检查不代表保留稿已复核；历史ready不作为本轮质量通过",
                    )]
                    if status == "failed" and task.error and not (
                        assessment and any(issue.reason == task.error for issue in assessment.issues)
                    ):
                        preserved_issues.append(Issue(
                            issue_id=str(uuid4()), run_id=task.run_id, page_id=page_id, revision_id=revision_id,
                            category="execution_failed", severity="error", reason=task.error,
                            disposition="本轮技术失败；保留采用版本，输出不完整",
                        ))
                    if assessment is None:
                        assessment = Assessment(
                            assessment_id=str(uuid4()), revision_id=revision_id, run_id=task.run_id, page_id=page_id,
                            layout="failed" if status == "failed" else "unverified",
                            coverage="failed" if status == "failed" else "unverified",
                            rule_version="preserved-v1", issues=preserved_issues, created_at=self._now(),
                        )
                    else:
                        # Keep this run's errors and incomplete checks on the retained revision.
                        assessment = assessment.model_copy(update={"issues": [*assessment.issues, *preserved_issues]})
                if assessment:
                    issues.extend(assessment.issues)
                entries.append(ExportManifestPage(
                    page_id=page_id, page_number=source["number"], position=position, revision_id=revision_id,
                    source_file_id=source["source_id"], source_page=source["source_page"], source_version=revision.source_version,
                    source_filename=source["source_filename"], image_name=source["image_name"], result_status=status,
                    assessment=assessment,
                ))
            manifest = ExportManifest(
                recognition_scope=settings.get("recognition_scope", "legacy_all_visible"), manifest_id=str(uuid4()), book_id=book_id, run_id=run_id, arrangement_revision=arrangement_revision,
                output_settings_version=settings["output_settings_version"], settings_snapshot=settings,
                generator_version=generator_version, pages=entries,
                complete=all(entry.result_status != "failed" and (run_id is None or entry.result_status is not None) for entry in entries),
                issues=issues, created_at=self._now(),
            )
            connection.execute("INSERT INTO export_manifests(manifest_id,book_id,manifest_json) VALUES (?,?,?)",
                               (manifest.manifest_id, book_id, manifest.model_dump_json()))
            if run_id is not None:
                snapshot["export_manifest_id"] = manifest.manifest_id
                connection.execute("UPDATE workflow_runs SET snapshot_json = ?,updated_at = ? WHERE run_id = ?",
                                   (json.dumps(snapshot, ensure_ascii=False), self._now(), run_id))
            return manifest

    def get_export_manifest(self, book_id: str, manifest_id: str) -> ExportManifest | None:
        with self._connect() as connection:
            row = connection.execute("SELECT manifest_json FROM export_manifests WHERE book_id = ? AND manifest_id = ?",
                                     (book_id, manifest_id)).fetchone()
            if row is None or json.loads(row["manifest_json"]).get("workflow_version") == 2:
                return None
            return ExportManifest.model_validate_json(row["manifest_json"])

    def record_manifest_outputs(self, book_id: str, manifest_id: str, outputs: dict[str, str]) -> ExportManifest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT manifest_json FROM export_manifests WHERE book_id = ? AND manifest_id = ?",
                                     (book_id, manifest_id)).fetchone()
            if row is None:
                raise KeyError("manifest")
            if json.loads(row["manifest_json"]).get("workflow_version") == 2:
                raise ValueError("新版各格式输出必须分别记录状态")
            manifest = ExportManifest.model_validate_json(row["manifest_json"])
            manifest = manifest.model_copy(update={"outputs": {**manifest.outputs, **outputs}})
            connection.execute("UPDATE export_manifests SET manifest_json = ? WHERE manifest_id = ?",
                               (manifest.model_dump_json(), manifest_id))
            return manifest

    def set_run_manifest(self, book_id: str, run_id: str, manifest_id: str) -> Run:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_runs WHERE book_id = ? AND run_id = ?", (book_id, run_id)).fetchone()
            manifest = connection.execute("SELECT 1 FROM export_manifests WHERE book_id = ? AND manifest_id = ?",
                                          (book_id, manifest_id)).fetchone()
            if row is None or manifest is None:
                raise KeyError("run or manifest")
            if row["workflow_version"] != 1:
                raise ValueError("新版运行不使用旧 export_manifest_id")
            snapshot = json.loads(row["snapshot_json"])
            snapshot["export_manifest_id"] = manifest_id
            connection.execute("UPDATE workflow_runs SET snapshot_json = ?,updated_at = ? WHERE run_id = ?",
                               (json.dumps(snapshot, ensure_ascii=False), self._now(), run_id))
            return self._run(connection, connection.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_manifest_pages(self, book_id: str, manifest_id: str) -> list[Page]:
        manifest = self.get_export_manifest(book_id, manifest_id)
        if manifest is None:
            raise KeyError("manifest")
        with self._connect() as connection:
            pages = []
            for entry in manifest.pages:
                revision = self._revision(connection, entry.revision_id)
                if revision is None:
                    raise KeyError("revision")
                pages.append(Page(
                    number=entry.page_number, page_id=entry.page_id, source_id=entry.source_file_id,
                    source_filename=entry.source_filename, source_page=entry.source_page, source_version=entry.source_version,
                    current_revision_id=revision.revision_id, result_status=entry.result_status,
                    manual_protected=revision.origin in {"manual", "legacy"},
                    status="failed" if entry.result_status == "failed" else ("ready" if revision.text or revision.layout_source else "uploaded"),
                    error=None, text=revision.text, render_strategy=revision.render_strategy,
                    content_revision=revision.content_revision, layout_revision=revision.layout_revision,
                    generated_content_revision=revision.generated_content_revision, layout_source=revision.layout_source,
                    source_metadata=revision.source_metadata, page_kind=revision.page_kind, page_side=revision.page_side,
                    cover_fields=revision.cover_fields, header_segments=revision.header_segments, footer_segments=revision.footer_segments,
                    usage=Usage(input_tokens=None, output_tokens=None, total_tokens=None, complete=False), attempts=0,
                ))
            return pages

    def get_manifest_book(self, book_id: str, manifest_id: str) -> Book:
        manifest = self.get_export_manifest(book_id, manifest_id)
        book = self.get_book(book_id)
        if manifest is None or book is None:
            raise KeyError("manifest or book")
        settings = manifest.settings_snapshot
        return book.model_copy(update={
            "paper_size": settings["paper_size"], "layout": LayoutSettings.model_validate(settings["layout"]),
            "render_strategy": settings["render_strategy"], "arrangement_revision": manifest.arrangement_revision,
            "output_settings_version": manifest.output_settings_version, "title": settings.get("title", book.title),
            "filename": settings.get("filename", book.filename),
            "page_count": len(manifest.pages), "selected_page_count": len(manifest.pages),
        })

    def list_results(self, book_id: str, offset: int = 0, limit: int = 100) -> list[PageResult]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pages WHERE book_id = ? AND selected = 1 ORDER BY position,number LIMIT ? OFFSET ?",
                (book_id, limit, offset),
            ).fetchall()
            results = []
            for row in rows:
                assessment = self._assessment(connection, row["current_revision_id"])
                layout = SourceFidelityLayout.model_validate_json(row["layout_source_json"]) if row["layout_source_json"] else None
                results.append(PageResult(
                    page_id=row["page_id"], page_number=row["number"], current_revision_id=row["current_revision_id"],
                    result_status=row["result_status"],
                    content=assessment.content if assessment else "unverified",
                    layout=assessment.layout if assessment and row["result_status"] is not None else "unverified",
                    coverage=assessment.coverage if assessment else "unverified",
                    source_disposition=layout.source_disposition if layout else None, issues=assessment.issues if assessment else [],
                ))
            return results

    def list_issues(self, book_id: str, offset: int = 0, limit: int = 100) -> list[Issue]:
        with self._connect() as connection:
            latest = connection.execute("SELECT snapshot_json FROM workflow_runs WHERE book_id = ? ORDER BY created_at DESC LIMIT 1",
                                        (book_id,)).fetchone()
            if latest:
                manifest_id = json.loads(latest["snapshot_json"]).get("export_manifest_id")
                if manifest_id:
                    row = connection.execute("SELECT manifest_json FROM export_manifests WHERE manifest_id = ?", (manifest_id,)).fetchone()
                    if row:
                        return ExportManifest.model_validate_json(row["manifest_json"]).issues[offset:offset + limit]
            issues = []
            for row in connection.execute("SELECT current_revision_id FROM pages WHERE book_id = ? AND selected = 1 ORDER BY position,number",
                                          (book_id,)).fetchall():
                assessment = self._assessment(connection, row["current_revision_id"])
                if assessment:
                    issues.extend(assessment.issues)
            return issues[offset:offset + limit]

    # V2 stores content/layout in the existing immutable revision table, response
    # metadata on existing attempts, and output snapshots in existing manifests.
    @staticmethod
    def _selection(connection: sqlite3.Connection, book_id: str) -> SelectionDraft:
        book = connection.execute("SELECT selection_json,selection_revision FROM books WHERE id=?", (book_id,)).fetchone()
        if book is None:
            raise KeyError("book")
        if book["selection_json"]:
            draft = SelectionDraft.model_validate_json(book["selection_json"])
        else:
            rows = connection.execute("SELECT page_id,source_version FROM pages WHERE book_id=? AND selected=1 ORDER BY position,number", (book_id,)).fetchall()
            draft = SelectionDraft(book_id=book_id, selection_revision=book["selection_revision"],
                                   page_ids=[row["page_id"] for row in rows],
                                   source_versions={row["page_id"]: row["source_version"] for row in rows})
        versions = {row["page_id"]: row["source_version"] for row in connection.execute("SELECT page_id,source_version FROM pages WHERE book_id=?", (book_id,))}
        return draft.model_copy(update={"valid": all(versions.get(page_id) == draft.source_versions.get(page_id) for page_id in draft.page_ids)})

    def get_selection(self, book_id: str) -> SelectionDraft:
        with self._connect() as connection:
            return self._selection(connection, book_id)

    def list_source_pages(self, book_id: str, offset: int = 0, limit: int = 100, source_id: str | None = None) -> list[SourcePageSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.page_id,p.source_version,p.number,p.source_id,p.source_page,f.filename AS source_filename,p.width,p.height "
                "FROM pages AS p JOIN source_files AS f ON p.book_id=f.book_id AND p.source_id=f.id "
                "WHERE p.book_id=?" + (" AND p.source_id=?" if source_id else "") + " ORDER BY f.position,p.source_page,p.number LIMIT ? OFFSET ?",
                (book_id, source_id, min(limit, 500), offset) if source_id else (book_id, min(limit, 500), offset),
            ).fetchall()
            return [SourcePageSummary(**dict(row)) for row in rows]

    def count_source_pages(self, book_id: str, source_id: str | None = None) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM pages WHERE book_id=?" + (" AND source_id=?" if source_id else ""),
                                      (book_id, source_id) if source_id else (book_id,)).fetchone()[0]

    def get_source_page_record(self, book_id: str, page_id: str) -> dict[str, Any] | None:
        """Local preview/import input, independent of selected/editor content."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT p.book_id,p.page_id,p.number,p.source_version,p.source_id,p.source_page,p.width,p.height,p.image_name,"
                "f.filename AS source_filename,f.kind AS source_kind,f.directory AS source_directory,f.page_count AS source_page_count "
                "FROM pages AS p JOIN source_files AS f ON p.book_id=f.book_id AND p.source_id=f.id WHERE p.book_id=? AND p.page_id=?",
                (book_id, page_id),
            ).fetchone()
            return dict(row) if row else None

    def save_selection(self, book_id: str, update: SelectionUpdate) -> SelectionDraft:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._selection(connection, book_id)
            if current.selection_revision != update.expected_selection_revision:
                raise RevisionConflict("选页已被更新，请加载当前选择")
            versions = {row["page_id"]: row["source_version"] for row in connection.execute("SELECT page_id,source_version FROM pages WHERE book_id=?", (book_id,))}
            if any(versions.get(page_id) != update.source_versions[page_id] for page_id in update.page_ids):
                raise RevisionConflict("所选页面来源已变化，请重新保存选页")
            if current.page_ids == update.page_ids and current.source_versions == update.source_versions:
                connection.execute("UPDATE books SET selection_confirmed=1 WHERE id=?", (book_id,))
                return current
            draft = SelectionDraft(book_id=book_id, selection_revision=current.selection_revision + 1,
                                   page_ids=update.page_ids, source_versions=update.source_versions)
            connection.execute("UPDATE books SET selection_json=?,selection_revision=?,selection_confirmed=1 WHERE id=?",
                               (draft.model_dump_json(), draft.selection_revision, book_id))
            # Legacy editor selection is a projection; ordered V2 authority lives
            # in the draft, independently of every source page's existence.
            connection.execute("UPDATE pages SET selected=0 WHERE book_id=?", (book_id,))
            connection.executemany("UPDATE pages SET selected=1 WHERE book_id=? AND page_id=?", [(book_id, page_id) for page_id in draft.page_ids])
            return draft

    @staticmethod
    def _outcome_category(outcome: PageOutcome | None) -> str | None:
        if outcome is None:
            return None
        if not outcome.source_readable:
            return "no_result"
        if outcome.source_disposition == "page_preserved":
            return "page_preserved"
        if outcome.source_disposition == "regions_preserved":
            return "regions_preserved"
        if outcome.content_revision_id and outcome.content == "usable":
            return "editable"
        if outcome.content_revision_id and outcome.content in {"uncertain", "unverified"}:
            return "partial_content"
        return "no_result"

    @staticmethod
    def _v2_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        run = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError("run")
        if run["workflow_version"] != 2:
            raise ValueError("旧任务不能使用新版内容处理器；请通过新版选择建立补做运行")
        return run

    def _inherit_v2_candidate(self, connection: sqlite3.Connection, task: PageTask, old: PageTask, book_id: str) -> None:
        if old.source_version != task.source_version or old.candidate_revision_id is None:
            return
        parent = self._revision(connection, old.candidate_revision_id)
        if parent is None or parent.page_content is None:
            return
        if parent.page_content.recognition_scope != task.stage_data.get("recognition_scope", "legacy_all_visible"):
            return
        revision = parent.model_copy(update={"revision_id": str(uuid4()), "parent_revision_id": parent.revision_id,
                                              "run_id": task.run_id, "created_at": self._now()})
        self._insert_revision(connection, revision)
        task.candidate_revision_id = revision.revision_id
        task.stage_data["inherited_from_run_id"] = old.run_id
        if parent.page_content.recognition_scope == "printed_original_only":
            task.stage_data["region_plan_complete"] = True
            task.stage_data["initial_batches_finished"] = True
            task.stage_data["region_plan"] = [region.model_dump(mode="json") for region in parent.page_content.regions]
            task.stage_data["region_adoptions"] = dict(old.stage_data.get("region_adoptions", {}))
        task.stage_data["basic_recognition_complete"] = bool(parent.page_content.blocks or parent.page_content.blank or parent.page_content.recognition_scope == "printed_original_only")
        task.stage_data["basic_review_complete"] = bool(old.stage_data.get("basic_review_complete"))
        task.completed_stages = [stage for stage in old.completed_stages if stage in {"prepare", "recognize", "review", "layout"}]
        task.stage = "render" if parent.page_layout is not None else ("layout" if task.stage_data["basic_review_complete"] else "review")
        task.stage_data["inherited_errors"] = [error.model_dump() for error in old.errors]

    def _basic_v2_needs(self, connection: sqlite3.Connection, tasks: list[PageTask]) -> dict[str, int]:
        needed = {}
        for task in tasks:
            if task.outcome is not None or task.state in {"failed", "finished"}:
                needed[task.page_id] = 0
                continue
            attempts = self._attempts(connection, task.run_id, task.page_id)
            recognition = int(not task.stage_data.get("basic_recognition_complete") and not task.stage_data.get("basic_recognition_unavailable") and not any(
                attempt.purpose == "basic_recognition" and attempt.state in {"reserved", "unknown"} for attempt in attempts
            ))
            if "recognition_batches" in task.stage_data and not task.stage_data.get("initial_batches_finished"):
                done = set(task.stage_data.get("recognition_batches_done", []))
                operations = task.stage_data.get("v2_operations", {})
                counted = {attempt.attempt_id for attempt in attempts if attempt.state != "cancelled"}
                recognition = sum(1 for index in range(len(task.stage_data["recognition_batches"]))
                    if index not in done and operations.get(f"recognize-batch-{index}", {}).get("attempt_id") not in counted)
            review = int(not task.stage_data.get("basic_review_complete") and not task.stage_data.get("basic_review_unavailable") and not any(
                attempt.purpose in {"basic_review", "recovery_review"} and attempt.state in {"reserved", "unknown"} for attempt in attempts
            ))
            plan = int(task.stage_data.get("recognition_scope") == "printed_original_only" and not task.stage_data.get("region_plan_complete") and not task.stage_data.get("region_plan_unavailable") and not any(attempt.purpose == "region_plan" and attempt.state in {"reserved", "unknown"} for attempt in attempts))
            needed[task.page_id] = plan + recognition + review
        return needed

    def _reserve_v2_attempts(
        self, connection: sqlite3.Connection, run_id: str, page_id: str, stages: list[WorkflowStage], *,
        retry: bool, purpose: str | None, recovery_round: int | None, block_ids: list[str], reservation_key: str | None,
        retry_of_attempt_id: str | None = None,
    ) -> list[Attempt]:
        run = self._v2_run(connection, run_id)
        task = self._task(connection, run_id, page_id)
        previous = self._attempts(connection, run_id, page_id)
        count = len(stages)
        if not stages or any(stage not in {"recognize", "review", "recover"} for stage in stages):
            raise ValueError("新版模型请求只允许 recognize/review/recover 职责")
        retry_source = None
        if retry:
            if count != 1 or not retry_of_attempt_id:
                raise ValueError("新版暂时重试必须绑定一个具体失败物理请求")
            retry_source = next((attempt for attempt in previous if attempt.attempt_id == retry_of_attempt_id), None)
            if retry_source is None or (retry_source.run_id, retry_source.page_id, retry_source.stage) != (run_id, page_id, stages[0]):
                raise ValueError("重试目标不属于当前运行、页面和模型职责")
            if (block_ids and block_ids != retry_source.block_ids) or (recovery_round is not None and recovery_round != retry_source.recovery_round):
                raise RevisionConflict("重试目标块或恢复轮与绑定的失败请求不一致")
            block_ids, recovery_round = list(retry_source.block_ids), retry_source.recovery_round
            purpose = "retry"
        elif retry_of_attempt_id is not None:
            raise ValueError("只有暂时重试可以绑定失败物理请求")
        if reservation_key:
            same = [attempt for attempt in previous if attempt.reservation_key == reservation_key]
            if same:
                expected_targets = [block_ids]
                if len(stages) > 1:
                    requests = len(stages) - 1
                    expected_targets = [block_ids[index * len(block_ids) // requests:(index + 1) * len(block_ids) // requests]
                                        for index in range(requests)] + [block_ids]
                if (len(same) != count or [attempt.stage for attempt in same] != stages
                        or [attempt.block_ids for attempt in same] != expected_targets
                        or any(attempt.retry != retry or attempt.retry_of_attempt_id != retry_of_attempt_id for attempt in same)
                        or (retry and any(attempt.recovery_round != recovery_round for attempt in same))):
                    raise RevisionConflict("预约身份已用于不同模型职责")
                return same
        if run["status"] != "running" or task.outcome is not None:
            raise RequestBudgetExceeded("任务已暂停或本页已结束，不能预约新请求")
        if any(attempt.state == "unknown" for attempt in previous):
            raise RequestBudgetExceeded("本页已有消费未知请求，保存已有内容后结束；自动流程不能重复发送")
        policy = RunPolicy.model_validate(json.loads(run["snapshot_json"])["policy"])
        if task.request_count + count > policy.page_request_limit:
            raise RequestBudgetExceeded("本页物理请求安全上限已用尽")
        if retry:
            if (task.retry_count >= policy.temporary_retry_limit or retry_source is None
                    or retry_source.state != "failed" or not retry_source.retryable):
                raise RequestBudgetExceeded("暂时重试额度已用尽，或绑定请求并非可安全重试的已知失败")
            if any(attempt.retry_of_attempt_id == retry_of_attempt_id and attempt.state != "cancelled" for attempt in previous):
                raise RevisionConflict("该失败请求已经派生重试；后续重试必须绑定当前失败重试请求")
        elif len(stages) > 1:
            if not reservation_key or not block_ids or len(set(block_ids)) != len(block_ids):
                raise ValueError("局部恢复必须有幂等身份和不重复目标块/证据 ID")
            if task.recovery_round_count >= policy.local_recovery_limit:
                raise RequestBudgetExceeded("本页局部恢复轮数已用尽")
            recovery_round = task.recovery_round_count + 1
            purpose = "recovery"
        else:
            if stages[0] == "recover":
                raise ValueError("内容修复及修后复核必须一起原子预约")
            if purpose is None:
                purpose = "basic_recognition" if stages[0] == "recognize" and not task.stage_data.get("basic_recognition_complete") else (
                    "basic_review" if stages[0] == "review" and not task.stage_data.get("basic_review_complete") else "local_recognition")
            if (purpose == "basic_recognition" and stages[0] != "recognize") or (purpose == "basic_review" and stages[0] != "review"):
                raise ValueError("基础预约用途与模型职责不一致")
            if purpose not in {"region_plan", "basic_recognition", "basic_review", "local_recognition"}:
                raise ValueError("修复和修后复核不能使用单次预约绕过完整额度保护")
            if purpose == "local_recognition" and (stages[0] != "recognize" or not block_ids):
                raise ValueError("局部识别必须有明确目标区域；重复复核须使用恢复轮已预约请求")
            if purpose == "region_plan" and (stages[0] != "recognize" or any(attempt.purpose == purpose and attempt.state != "cancelled" for attempt in previous)):
                raise RequestBudgetExceeded("区域计划已发出；只能解析保存响应或走安全暂时重试")
            if purpose == "basic_recognition" and any(attempt.purpose == purpose and attempt.state != "cancelled" for attempt in previous):
                raise RequestBudgetExceeded("基础识别已发出；保存响应需本地解析，已知暂时错误须走共享重试")
            if purpose == "basic_review" and any(attempt.purpose == purpose and attempt.state != "cancelled" for attempt in previous):
                raise RequestBudgetExceeded("首次复核已发出；不能把复核结构错误当作新的首次请求")
            if purpose == "basic_review" and not task.stage_data.get("basic_recognition_complete"):
                raise ValueError("内容复核必须基于已取得的有效内容或明确空白观察")
        tasks = self._tasks(connection, run_id)
        needed = self._basic_v2_needs(connection, tasks)
        initial_batch = purpose == "local_recognition" and "recognition_batches" in task.stage_data and not task.stage_data.get("initial_batches_finished")
        basic_request = count == 1 and not retry and (purpose in {"region_plan", "basic_recognition", "basic_review"} or initial_batch)
        if basic_request:
            # When the budget cannot cover everyone, preserve frozen page order.
            protected = sum(needed[other.page_id] for other in tasks if other.position < task.position)
            own_remaining = max(0, needed[task.page_id] - 1)
            if initial_batch:
                own_remaining = int(not task.stage_data.get("basic_review_complete") and not task.stage_data.get("basic_review_unavailable"))
            if task.request_count + count + own_remaining > policy.page_request_limit:
                raise RequestBudgetExceeded("本页额度不足以完成区域计划、转录与独立核验")
            protected += own_remaining
        else:
            protected = sum(needed.values())
        if run["request_count"] + count + protected > run["request_limit"]:
            raise RequestBudgetExceeded("本轮共享额度不足；基础识别及首次复核额度按冻结页序保留")
        if retry:
            task.retry_count += 1
        elif count > 1:
            task.recovery_round_count += 1
        result, now = [], self._now()
        legacy_ordinal = connection.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM page_attempts WHERE book_id=? AND page_number=?", (run["book_id"], task.page_number)).fetchone()[0]
        for index, stage in enumerate(stages):
            task.request_count += 1
            if not retry:
                counter = {"recognize": "recognize_count", "review": "review_count", "recover": "repair_count"}[stage]
                setattr(task, counter, getattr(task, counter) + 1)
            attempt = Attempt(attempt_id=str(uuid4()), run_id=run_id, page_id=page_id, stage=stage,
                              ordinal=max((old.ordinal for old in previous), default=0) + index + 1,
                              retry=retry, purpose="recovery_review" if count > 1 and stage == "review" else purpose,
                              recovery_round=recovery_round,
                              block_ids=block_ids[(index * len(block_ids)) // (count - 1):((index + 1) * len(block_ids)) // (count - 1)] if count > 1 and stage == "recover" else block_ids,
                              reservation_key=reservation_key, retry_of_attempt_id=retry_of_attempt_id, created_at=now)
            connection.execute("INSERT INTO page_attempts(book_id,page_number,attempt) VALUES (?,?,?)", (run["book_id"], task.page_number, legacy_ordinal + index))
            connection.execute("INSERT INTO workflow_attempts(attempt_id,run_id,page_id,legacy_attempt,attempt_json) VALUES (?,?,?,?,?)",
                               (attempt.attempt_id, run_id, page_id, legacy_ordinal + index, attempt.model_dump_json()))
            result.append(attempt)
        self._write_task(connection, task)
        connection.execute("UPDATE workflow_runs SET request_count=request_count+?,updated_at=? WHERE run_id=?", (count, now, run_id))
        return result

    def reserve_recovery_round(self, run_id: str, page_id: str, *, block_ids: list[str], request_count: int, reservation_key: str) -> list[Attempt]:
        if request_count < 1 or request_count > len(set(block_ids)):
            raise ValueError("每块每轮最多一个局部候选；恢复请求数必须与目标范围匹配")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reserve_v2_attempts(connection, run_id, page_id, ["recover"] * request_count + ["review"],
                                             retry=False, purpose="recovery", recovery_round=None,
                                             block_ids=block_ids, reservation_key=reservation_key)

    def mark_attempt_sent(self, attempt_id: str) -> Attempt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT attempt_json,response_json FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError("attempt")
            attempt = Attempt.model_validate_json(row["attempt_json"])
            run = self._v2_run(connection, attempt.run_id)
            if run["status"] != "running" or attempt.state != "reserved" or attempt.sent_at is not None or row["response_json"]:
                raise RevisionConflict("预约已发送、已结算或任务已暂停，不能重复发送")
            attempt.sent_at = self._now()
            connection.execute("UPDATE workflow_attempts SET attempt_json=? WHERE attempt_id=?", (attempt.model_dump_json(), attempt_id))
            return attempt

    def cancel_unsent_attempt(self, attempt_id: str) -> Attempt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError("attempt")
            attempt = Attempt.model_validate_json(row["attempt_json"])
            self._v2_run(connection, attempt.run_id)
            if attempt.state == "cancelled":
                return attempt
            if attempt.state != "reserved" or attempt.sent_at is not None or row["response_json"]:
                raise RevisionConflict("已发送或消费未知的请求不能退还额度")
            attempt.state, attempt.finished_at = "cancelled", self._now()
            attempt.usage = Usage(input_tokens=0, output_tokens=0, total_tokens=0, complete=True)
            task = self._task(connection, attempt.run_id, attempt.page_id)
            task.request_count -= 1
            if attempt.retry:
                task.retry_count -= 1
            else:
                counter = {"recognize": "recognize_count", "review": "review_count", "recover": "repair_count"}[attempt.stage]
                setattr(task, counter, max(0, getattr(task, counter) - 1))
            self._write_task(connection, task)
            connection.execute("UPDATE workflow_attempts SET attempt_json=? WHERE attempt_id=?", (attempt.model_dump_json(), attempt_id))
            run = self._v2_run(connection, attempt.run_id)
            connection.execute("UPDATE page_attempts SET returned=1,input_tokens=0,output_tokens=0,total_tokens=0 WHERE book_id=? AND page_number=? AND attempt=?",
                               (run["book_id"], task.page_number, row["legacy_attempt"]))
            if not attempt.retry and attempt.recovery_round is not None:
                group = [other for other in self._attempts(connection, attempt.run_id, attempt.page_id) if other.reservation_key == attempt.reservation_key]
                if group and all(other.state == "cancelled" and other.sent_at is None for other in group):
                    task.recovery_round_count = max(0, task.recovery_round_count - 1)
                    self._write_task(connection, task)
            connection.execute("UPDATE workflow_runs SET request_count=request_count-1,updated_at=? WHERE run_id=?", (self._now(), attempt.run_id))
            return attempt

    def save_recognition_response(
        self, attempt_id: str, body: str, *, completion_status: str, usage: Usage | None = None,
        provider_request_id: str | None = None,
    ) -> RecognitionResponse:
        """Persist bounded received text before any semantic/schema parsing."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError("attempt")
            if row["response_json"]:
                return RecognitionResponse.model_validate_json(row["response_json"])
            attempt = Attempt.model_validate_json(row["attempt_json"])
            run = self._v2_run(connection, attempt.run_id)
            policy = RunPolicy.model_validate(json.loads(run["snapshot_json"])["policy"])
            raw = body.encode("utf-8")
            bounded = raw[:policy.response_body_limit_bytes].decode("utf-8", errors="ignore").encode("utf-8")
            relative = Path("books") / run["book_id"] / "responses" / f"{attempt_id}.txt"
            target = self.data_root / relative
            response = RecognitionResponse(attempt_id=attempt_id, run_id=attempt.run_id, page_id=attempt.page_id,
                                           body_asset=relative.as_posix(), body_storage="saved", body_bytes=len(bounded),
                                           body_truncated=len(raw) > len(bounded), completion_status=completion_status,
                                           usage=usage or attempt.usage, provider_request_id=provider_request_id, created_at=self._now())
            if response.body_truncated:
                response.completion_status = "truncated"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".tmp")
                with temporary.open("wb") as output:
                    output.write(bounded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            except OSError as error:
                response.body_storage, response.body_asset = "failed", None
                response.storage_error = f"响应已收到但正文未保存：{error.__class__.__name__}"
            connection.execute("UPDATE workflow_attempts SET response_json=? WHERE attempt_id=?", (response.model_dump_json(), attempt_id))
            # Transport completion is retained even when later content parsing
            # fails. A received response must never become an unknown resend.
            attempt.state, attempt.finished_at = "succeeded", self._now()
            attempt.usage, attempt.provider_request_id = response.usage, provider_request_id
            connection.execute("UPDATE workflow_attempts SET attempt_json=? WHERE attempt_id=?", (attempt.model_dump_json(), attempt_id))
            task = self._task(connection, attempt.run_id, attempt.page_id)
            connection.execute("UPDATE page_attempts SET returned=1,input_tokens=?,output_tokens=?,total_tokens=? WHERE book_id=? AND page_number=? AND attempt=?",
                               (response.usage.input_tokens, response.usage.output_tokens, response.usage.total_tokens, run["book_id"], task.page_number, row["legacy_attempt"]))
            return response

    def get_recognition_response(self, attempt_id: str) -> RecognitionResponse | None:
        with self._connect() as connection:
            row = connection.execute("SELECT response_json FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return RecognitionResponse.model_validate_json(row["response_json"]) if row and row["response_json"] else None

    def read_recognition_body(self, attempt_id: str) -> str:
        response = self.get_recognition_response(attempt_id)
        if response is None or response.body_storage != "saved" or response.body_asset is None:
            raise ValueError("响应正文未保存，不能本地重新解析，也不能自动重发未知消费请求")
        target = (self.data_root / response.body_asset).resolve()
        if not target.is_relative_to(self.books_root):
            raise ValueError("响应资产不属于本地资料目录")
        return target.read_text(encoding="utf-8")

    def record_response_parse_errors(self, attempt_id: str, errors: list[WorkflowError]) -> RecognitionResponse:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT response_json FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or not row["response_json"]:
                raise KeyError("response")
            response = RecognitionResponse.model_validate_json(row["response_json"])
            response.parse_errors = errors
            connection.execute("UPDATE workflow_attempts SET response_json=? WHERE attempt_id=?", (response.model_dump_json(), attempt_id))
            return response

    def save_content_candidate(
        self, run_id: str, page_id: str, content: PageContent, *, expected_candidate_revision_id: str | None = None,
    ) -> Revision:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._v2_run(connection, run_id)
            task = self._task(connection, run_id, page_id)
            if (content.book_id, content.page_id, content.source_version) != (run["book_id"], page_id, task.source_version):
                raise ValueError("内容与本轮冻结来源不一致")
            if task.candidate_revision_id != expected_candidate_revision_id:
                previous = self._revision(connection, task.candidate_revision_id) if task.candidate_revision_id else None
                if previous and previous.page_content and previous.page_content.content_revision_id == content.content_revision_id and previous.page_content == content:
                    return previous
                raise RevisionConflict("候选已变化，请基于当前候选保存内容")
            for saved in connection.execute("SELECT revision_json FROM workflow_revisions WHERE book_id=? AND page_id=?", (run["book_id"], page_id)):
                existing = Revision.model_validate_json(saved["revision_json"])
                if existing.page_content and existing.page_content.content_revision_id == content.content_revision_id and existing.page_content != content:
                    raise RevisionConflict("内容修订 ID 已冻结；修改块或复核状态时必须建立新的内容修订")
            for response_id in content.response_ids:
                response = connection.execute("SELECT page_id,response_json FROM workflow_attempts WHERE attempt_id=?", (response_id,)).fetchone()
                if response is None or response["page_id"] != page_id or not response["response_json"]:
                    raise ValueError("内容必须引用已持久化的同页响应")
                received = RecognitionResponse.model_validate_json(response["response_json"])
                response_run = connection.execute("SELECT snapshot_json FROM workflow_runs WHERE run_id=?", (received.run_id,)).fetchone()
                response_source = json.loads(response_run["snapshot_json"])["sources"][page_id] if response_run else None
                if received.body_storage != "saved" or response_source is None or response_source["source_version"] != task.source_version:
                    raise ValueError("内容响应正文未保存，或响应属于不同来源版本")
            parent = self._revision(connection, task.candidate_revision_id) if task.candidate_revision_id else None
            metadata = parent.source_metadata if parent else None
            content_revision = max(task.base_content_revision, parent.content_revision if parent else 0) + 1
            revision = Revision(revision_id=str(uuid4()), parent_revision_id=task.candidate_revision_id or task.base_revision_id,
                                book_id=run["book_id"], page_id=page_id, page_number=task.page_number, source_version=task.source_version,
                                content_revision=content_revision, layout_revision=task.base_layout_revision,
                                origin="automatic", run_id=run_id, text=content_plain_text(content), render_strategy="source_fidelity",
                                source_metadata=metadata, created_at=self._now(), workflow_version=2, page_content=content,
                                generator_version=json.loads(run["snapshot_json"])["generator_version"])
            self._insert_revision(connection, revision)
            task.candidate_revision_id = revision.revision_id
            task.stage_data["basic_recognition_complete"] = bool(content.blocks or content.blank or (content.recognition_scope == "printed_original_only" and task.stage_data.get("initial_batches_finished")))
            self._write_task(connection, task)
            return revision

    def save_layout_candidate(
        self, run_id: str, page_id: str, layout: PageLayout, *, expected_candidate_revision_id: str,
        render_layout: SourceFidelityLayout | None = None, generated_text: str | None = None,
    ) -> Revision:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._v2_run(connection, run_id)
            task = self._task(connection, run_id, page_id)
            parent = self._revision(connection, task.candidate_revision_id) if task.candidate_revision_id else None
            if parent and parent.page_layout == layout:
                return parent
            if task.candidate_revision_id != expected_candidate_revision_id or parent is None or parent.page_content is None:
                raise RevisionConflict("布局必须基于当前已保存内容候选")
            if (layout.page_id, layout.source_version, layout.content_revision_id, layout.source.book_id) != (
                page_id, task.source_version, parent.page_content.content_revision_id, run["book_id"],
            ):
                raise ValueError("派生布局输入修订与来源不一致")
            line_ids = {line.line_id for block in parent.page_content.blocks for line in getattr(block, "lines", [])}
            line_ids.update(line.line_id for block in parent.page_content.blocks for cell in getattr(block, "cells", []) for line in cell.lines)
            block_ids = {block.block_id for block in parent.page_content.blocks}
            if any(line.line_id not in line_ids or line.block_id not in block_ids for line in layout.lines):
                raise ValueError("派生布局必须引用已有内容块和原行")
            layout_revision = parent.layout_revision + 1
            for saved in connection.execute("SELECT revision_json FROM workflow_revisions WHERE book_id=? AND page_id=?", (run["book_id"], page_id)):
                existing = Revision.model_validate_json(saved["revision_json"])
                if existing.page_layout and existing.page_layout.layout_revision_id == layout.layout_revision_id and existing.page_layout != layout:
                    raise RevisionConflict("布局修订 ID 已冻结；调整布局时必须建立新布局修订")
            if render_layout is not None:
                if render_layout.source != layout.source:
                    raise ValueError("渲染转换与派生布局来源不一致")
                render_layout = render_layout.model_copy(update={"content_revision": parent.content_revision,
                                                                 "layout_revision": layout_revision,
                                                                 "generated_content_revision": parent.content_revision})
            revision = parent.model_copy(update={"revision_id": str(uuid4()), "parent_revision_id": parent.revision_id,
                                                 "page_layout": layout, "layout_source": render_layout,
                                                 "source_metadata": layout.source, "layout_revision": layout_revision,
                                                 "text": generated_text if generated_text is not None else parent.text,
                                                 "generated_content_revision": parent.content_revision if render_layout else None,
                                                 "created_at": self._now()})
            self._insert_revision(connection, revision)
            task.candidate_revision_id = revision.revision_id
            self._write_task(connection, task)
            return revision

    def get_page_content(self, revision_id: str) -> PageContent | None:
        revision = self.get_revision(revision_id)
        return revision.page_content if revision else None

    def get_page_layout(self, revision_id: str) -> PageLayout | None:
        revision = self.get_revision(revision_id)
        return revision.page_layout if revision else None

    def save_page_outcome(self, run_id: str, page_id: str, outcome: PageOutcome, *, adopt: bool = True) -> bool:
        """Always retain the run's content; CAS controls only the current draft."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._v2_run(connection, run_id)
            task = self._task(connection, run_id, page_id)
            if (outcome.page_id, outcome.source_version) != (page_id, task.source_version):
                raise ValueError("结果来源与冻结页面不一致")
            revision = self._revision(connection, task.candidate_revision_id) if task.candidate_revision_id else None
            if revision is not None:
                outcome = outcome.model_copy(update={
                    "content_revision_id": outcome.content_revision_id or (revision.page_content.content_revision_id if revision.page_content else None),
                    "layout_revision_id": outcome.layout_revision_id or (revision.page_layout.layout_revision_id if revision.page_layout else None),
                }, deep=True)
            if outcome.content == "usable" and (outcome.content_revision_id is None or revision is None or revision.page_content is None or not (revision.page_content.blocks or revision.page_content.blank)):
                raise ValueError("可用内容结论必须引用已保存的有效块或明确空白观察")
            if outcome.content_revision_id is not None and (revision is None or revision.page_content is None or revision.page_content.content_revision_id != outcome.content_revision_id):
                raise ValueError("结果必须引用本轮已保存内容")
            if outcome.layout_revision_id is not None and (revision is None or revision.page_layout is None or revision.page_layout.layout_revision_id != outcome.layout_revision_id):
                raise ValueError("结果必须引用本轮已保存布局")
            page = connection.execute("SELECT * FROM pages WHERE book_id=? AND page_id=?", (run["book_id"], page_id)).fetchone()
            if page is None:
                raise KeyError("page")
            policy = RunPolicy.model_validate(json.loads(run["snapshot_json"])["policy"])
            authorized = not page["manual_protected"] or page_id in policy.replace_page_ids
            matches = (page["current_revision_id"], page["content_revision"], page["layout_revision"], page["source_version"]) == (
                task.base_revision_id, task.base_content_revision, task.base_layout_revision, task.source_version,
            )
            already_adopted = bool(revision and page["current_revision_id"] == revision.revision_id)
            adopted = already_adopted or bool(adopt and authorized and matches and revision and revision.page_content)
            if adopted and not already_adopted:
                self._archive_page(connection, page)
                connection.execute("UPDATE pages SET current_revision_id=?,content_revision=?,layout_revision=?,text=?,render_strategy=?,source_metadata_json=?,layout_source_json=?,layout_schema_version=?,generated_content_revision=?,status='ready',error=NULL,result_status=NULL,manual_protected=0 WHERE page_id=?",
                                   (revision.revision_id, revision.content_revision, revision.layout_revision, revision.text, revision.render_strategy,
                                    revision.source_metadata.model_dump_json() if revision.source_metadata else None,
                                    revision.layout_source.model_dump_json() if revision.layout_source else None,
                                    revision.layout_source.schema_version if revision.layout_source else None,
                                    revision.generated_content_revision, page_id))
            outcome = outcome.model_copy(update={"adopted_revision_id": revision.revision_id if adopted else page["current_revision_id"],
                                                 "protected_existing": not adopted and bool(page["manual_protected"] or not matches)})
            for error in task.errors:
                if error not in outcome.errors:
                    outcome.errors.append(error)
            task.outcome, task.state = outcome, "finished"
            task.stage_data["final_revision_id"] = revision.revision_id if revision else None
            task.stage_data["adopted"] = adopted
            self._write_task(connection, task)
            connection.execute("UPDATE workflow_runs SET updated_at=? WHERE run_id=?", (self._now(), run_id))
            return adopted

    def get_run_outcomes(self, run_id: str, offset: int = 0, limit: int = 100) -> list[PageOutcomeSummary]:
        with self._connect() as connection:
            return [PageOutcomeSummary(page_id=task.page_id, page_number=task.page_number, position=task.position,
                                       stage=task.stage, state=task.state, outcome=task.outcome)
                    for task in self._tasks(connection, run_id)[offset:offset + min(limit, 500)]]

    def get_run_summary(self, book_id: str, run_id: str) -> RunSummary | None:
        run = self.get_run(book_id, run_id)
        if run is None:
            return None
        active: dict[str, int] = {}
        errors = []
        for task in run.tasks:
            if task.state == "running":
                active[task.stage] = active.get(task.stage, 0) + 1
            errors.extend(task.errors)
        snapshot = self.get_output_snapshot(book_id, run.output_snapshot_id) if run.output_snapshot_id else None
        waiting = [attempt.sent_at for attempt in self.get_attempts(run_id)
                   if attempt.sent_at is not None and attempt.settled_at is None and attempt.state in {"reserved", "succeeded"}]
        return RunSummary(recognition_scope=run.recognition_scope, workflow_version=run.workflow_version, run_id=run_id, book_id=book_id,
                          selection_revision=run.selection_revision, status=run.status, counts=run.counts,
                          active_stages=active, updated_at=run.updated_at, error=run.error, recent_errors=errors[-5:],
                          output_snapshot_id=run.output_snapshot_id, formats=snapshot.formats if snapshot else {},
                          request_limit=run.request_limit, request_count=run.request_count, usage=run.usage,
                          model_wait_started_at=min(waiting) if waiting else None)

    def create_output_snapshot(self, book_id: str, run_id: str) -> OutputSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._v2_run(connection, run_id)
            if run["book_id"] != book_id:
                raise KeyError("run")
            frozen = json.loads(run["snapshot_json"])
            if frozen.get("output_snapshot_id"):
                saved = connection.execute("SELECT manifest_json FROM export_manifests WHERE manifest_id=? AND book_id=?", (frozen["output_snapshot_id"], book_id)).fetchone()
                if saved:
                    return OutputSnapshot.model_validate_json(saved["manifest_json"])
            entries = []
            for task in self._tasks(connection, run_id):
                if task.outcome is None:
                    raise ValueError("页面处理尚未结束，不能冻结输出快照")
                source = frozen["sources"][task.page_id]
                revision = self._revision(connection, task.candidate_revision_id) if task.candidate_revision_id else None
                prepared = task.stage_data.get("prepared", {})
                metadata = revision.source_metadata if revision else None
                if metadata is None and isinstance(prepared, dict) and prepared.get("metadata"):
                    metadata = PageSourceMetadata.model_validate(prepared["metadata"])
                if metadata is not None and (metadata.page_id, metadata.source_version) != (task.page_id, task.source_version):
                    raise ValueError("输出源几何与冻结来源版本不一致")
                entries.append(OutputSnapshotPage(page_id=task.page_id, position=task.position, source_version=task.source_version,
                                                  source_file_id=source["source_id"], source_page=source["source_page"],
                                                  source_filename=source["source_filename"], image_name=source["image_name"],
                                                  source_metadata=metadata, revision_id=revision.revision_id if revision else None,
                                                  content_revision_id=task.outcome.content_revision_id, layout_revision_id=task.outcome.layout_revision_id,
                                                  outcome=task.outcome))
            snapshot = OutputSnapshot(recognition_scope=frozen.get("recognition_scope", "legacy_all_visible"), output_snapshot_id=str(uuid4()), book_id=book_id, run_id=run_id,
                                      selection_revision=frozen["selection_revision"], page_ids=frozen["page_ids"],
                                      output_settings_version=frozen["settings"]["output_settings_version"], settings_snapshot=frozen["settings"],
                                      generator_version=frozen["generator_version"], pages=entries, created_at=self._now())
            if frozen.get("continuation_run_id") and all(task.stage_data.get("reused_complete_result") for task in self._tasks(connection, run_id)):
                previous_run = connection.execute("SELECT snapshot_json FROM workflow_runs WHERE run_id=? AND book_id=?", (frozen["continuation_run_id"], book_id)).fetchone()
                previous_id = json.loads(previous_run["snapshot_json"]).get("output_snapshot_id") if previous_run else None
                previous_row = connection.execute("SELECT manifest_json FROM export_manifests WHERE manifest_id=?", (previous_id,)).fetchone()
                if previous_row:
                    previous_snapshot = OutputSnapshot.model_validate_json(previous_row["manifest_json"])
                    if previous_snapshot.page_ids == snapshot.page_ids and previous_snapshot.output_settings_version == snapshot.output_settings_version and all(
                        (old.content_revision_id, old.layout_revision_id, old.source_version) == (new.content_revision_id, new.layout_revision_id, new.source_version)
                        for old, new in zip(previous_snapshot.pages, snapshot.pages)
                    ):
                        snapshot = snapshot.model_copy(update={"formats": {
                            name: value if name == "pdf" and value.status == "available" else OutputFormat()
                            for name, value in previous_snapshot.formats.items()
                        }})
            connection.execute("INSERT INTO export_manifests(manifest_id,book_id,manifest_json) VALUES (?,?,?)", (snapshot.output_snapshot_id, book_id, snapshot.model_dump_json()))
            frozen["output_snapshot_id"] = snapshot.output_snapshot_id
            connection.execute("UPDATE workflow_runs SET snapshot_json=?,updated_at=? WHERE run_id=?", (json.dumps(frozen, ensure_ascii=False), self._now(), run_id))
            return snapshot

    def get_output_snapshot(self, book_id: str, snapshot_id: str) -> OutputSnapshot | None:
        with self._connect() as connection:
            row = connection.execute("SELECT manifest_json FROM export_manifests WHERE book_id=? AND manifest_id=?", (book_id, snapshot_id)).fetchone()
            if row is None or json.loads(row["manifest_json"]).get("workflow_version") != 2:
                return None
            return OutputSnapshot.model_validate_json(row["manifest_json"])

    def record_output_format(self, book_id: str, snapshot_id: str, format_name: str, *, status: str, asset: str | None = None, error: str | None = None) -> OutputSnapshot:
        if format_name not in {"json", "pdf", "latex"}:
            raise ValueError("未知输出格式")
        value = OutputFormat(status=status, asset=asset, error=error)
        if status == "available" and not asset:
            raise ValueError("可用输出必须引用已生成资产")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT manifest_json FROM export_manifests WHERE book_id=? AND manifest_id=?", (book_id, snapshot_id)).fetchone()
            if row is None:
                raise KeyError("snapshot")
            snapshot = OutputSnapshot.model_validate_json(row["manifest_json"])
            current = snapshot.formats[format_name]
            if current.status == "available" and status != "available":
                return snapshot
            snapshot = snapshot.model_copy(update={"formats": {**snapshot.formats, format_name: value}})
            connection.execute("UPDATE export_manifests SET manifest_json=? WHERE manifest_id=?", (snapshot.model_dump_json(), snapshot_id))
            return snapshot

    def get_snapshot_content(self, book_id: str, snapshot_id: str, page_id: str) -> PageContent | None:
        revision = self.get_snapshot_revision(book_id, snapshot_id, page_id)
        return revision.page_content if revision else None

    def get_snapshot_revision(self, book_id: str, snapshot_id: str, page_id: str) -> Revision | None:
        snapshot = self.get_output_snapshot(book_id, snapshot_id)
        if snapshot is None or page_id not in snapshot.page_ids:
            raise KeyError("snapshot page")
        entry = next(entry for entry in snapshot.pages if entry.page_id == page_id)
        if entry.revision_id is None:
            return None
        with self._connect() as connection:
            revision = self._revision(connection, entry.revision_id)
            if revision is None or (revision.book_id, revision.page_id, revision.source_version) != (book_id, page_id, entry.source_version):
                raise ValueError("输出修订与冻结页面不一致")
            if (revision.page_content.content_revision_id if revision.page_content else None) != entry.content_revision_id or (
                revision.page_layout.layout_revision_id if revision.page_layout else None
            ) != entry.layout_revision_id:
                raise ValueError("输出内容/布局引用与精确修订不一致")
            return revision

    def replace_page_source(
        self, book_id: str, page_id: str, *, source_file_id: str, source_page: int,
        width: int, height: int, image_name: str,
    ) -> None:
        """Register a new asset version; the caller retains the previous asset files."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            page = connection.execute("SELECT * FROM pages WHERE book_id = ? AND page_id = ?", (book_id, page_id)).fetchone()
            if page is None:
                raise KeyError("page")
            source = connection.execute("SELECT page_count FROM source_files WHERE book_id = ? AND id = ?",
                                        (book_id, source_file_id)).fetchone()
            if source is None or not 1 <= source_page <= source["page_count"] or width <= 0 or height <= 0:
                raise ValueError("替换源资产或页面尺寸不正确")
            self._archive_page(connection, page)
            connection.execute(
                "UPDATE pages SET source_id = ?,source_page = ?,source_version = source_version + 1,width = ?,height = ?,"
                "image_name = ?,source_metadata_json = NULL,layout_source_json = NULL,layout_schema_version = NULL,"
                "generated_content_revision = NULL,render_strategy = 'custom_latex',content_revision = content_revision + 1,"
                "layout_revision = layout_revision + 1,manual_protected = 1,result_status = NULL WHERE page_id = ?",
                (source_file_id, source_page, width, height, image_name, page_id),
            )
            updated = connection.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            self._persist_current_revision(connection, updated, origin="manual")
