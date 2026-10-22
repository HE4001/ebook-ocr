from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from uuid import uuid4

from .credentials import migrate_credentials
from .latex_migration import legacy_blocks_to_markdown, markdown_to_latex
from .models import SelectionDraft


class StorageMigrations:
    """Schema and historical data migration helpers used by Storage."""

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
                    (json.dumps(self.DEFAULT_SETTINGS, ensure_ascii=False),),
                )
            self._migrate_latex_content(connection)
            self._migrate_workflow(connection)
            self._migrate_v2(connection)
            migrate_credentials(connection)

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
            "CREATE INDEX IF NOT EXISTS workflow_attempts_run ON workflow_attempts(run_id)",
            "CREATE INDEX IF NOT EXISTS workflow_assessments_revision ON workflow_assessments(revision_id)",
            "CREATE INDEX IF NOT EXISTS workflow_tasks_run_position "
            "ON workflow_tasks(run_id,json_extract(task_json,'$.position'),page_id)",
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

