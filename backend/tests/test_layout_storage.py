from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.layout_contract import LayoutObservation, PageSourceMetadata, SourceFidelityLayout
from backend.models import StructuredPageResult
from backend.storage import RevisionConflict, Storage


class LayoutStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = Storage(Path(self.temporary.name))
        self.storage.initialize()
        self.storage.create_book("b", "书", "source.png", [(1, 100, 200, "page-0001.png")])
        self.source = PageSourceMetadata(
            book_id="b", page_number=1, source_id="legacy", source_page=1, source_kind="image",
            source_file_fingerprint="source", image_fingerprint="image",
            canonical_width_px=100, canonical_height_px=200, source_width_px=100, source_height_px=200,
            source_coordinate_space="original_image_px", canonical_to_source_affine=(1, 0, 0, 1, 0, 0),
        )
        self.observation = LayoutObservation.model_validate({
            "regions": [{"region_id": "body", "kind": "body", "order": 0}],
            "lines": [{"line_id": "l1", "block_id": "body", "order": 0, "latex": "原文"}],
        })
        self.result = StructuredPageResult(
            header_segments=[], footer_segments=[], body_latex="原文", response_version=2,
            layout=self.observation,
        )
        self.layout = SourceFidelityLayout(
            **self.observation.model_dump(), source=self.source, content_revision=1, layout_revision=1,
            canvas_width_bp=300, canvas_height_bp=600, canvas_basis="project",
            body_font_size_bp=12, body_font_basis="project",
        )

    def save_result(self, text: str = "生成源码") -> bool:
        return self.storage.save_page_result(
            "b", 1, self.result, expected_content_revision=0, source_metadata=self.source,
            generated_text=text, generator_version="v1", layout_source=self.layout,
        )

    def test_content_layout_and_export_fields_commit_together(self) -> None:
        self.assertTrue(self.save_result())
        page = self.storage.get_pages("b")[0]
        self.assertEqual(page.text, "生成源码")
        self.assertEqual((page.content_revision, page.layout_revision, page.generated_content_revision), (1, 1, 1))
        self.assertEqual(page.layout_source.source, page.source_metadata)
        self.assertEqual(page.layout_source.canvas_width_bp, 300)
        self.assertEqual(page.layout_source.body_font_size_bp, 12)
        self.assertEqual(page.layout_source.generator_version, "v1")
        self.assertEqual(page.model_dump(mode="json")["layout_source"]["schema_version"], 1)
        self.assertEqual(self.storage.get_book("b").render_strategy, "source_fidelity")

    def test_unchanged_generated_document_save_remains_layout_authoritative(self) -> None:
        text = r"\documentclass{article}\begin{document}generated\end{document}"
        self.assertTrue(self.save_result(text))
        page = self.storage.save_manual_text(
            "b", 1, text, expected_content_revision=1, expected_layout_revision=1,
            render_strategy="source_fidelity",
        )
        self.assertEqual(page.render_strategy, "source_fidelity")
        self.assertEqual(page.generated_content_revision, page.content_revision)
        self.assertEqual(page.layout_source.content_revision, page.content_revision)

    def test_manual_save_wins_against_old_task_status_and_result(self) -> None:
        self.assertTrue(self.save_result())
        page = self.storage.save_manual_text("b", 1, "人工源码", expected_content_revision=1)
        self.assertEqual(page.render_strategy, "custom_latex")
        self.assertEqual(page.layout_revision, 1)
        self.assertIsNone(page.generated_content_revision)
        self.assertFalse(self.storage.save_page_result("b", 1, self.result, expected_content_revision=1))
        self.assertFalse(self.storage.set_page_status("b", 1, "processing", expected_content_revision=1))
        self.assertFalse(self.storage.fail_page("b", 1, "old failure", expected_content_revision=1))
        self.assertFalse(self.storage.interrupt_page("b", 1, expected_content_revision=1))
        current = self.storage.get_pages("b")[0]
        self.assertEqual((current.text, current.status, current.error), ("人工源码", "ready", None))
        with self.assertRaises(RevisionConflict):
            self.storage.save_manual_text("b", 1, "旧草稿", expected_content_revision=1)
        with self.assertRaises(RevisionConflict):
            self.storage.save_page_layout("b", 1, self.layout, "新生成", 2, 0)
        calibrated = self.storage.save_page_layout("b", 1, self.layout, "新生成", 2, 1)
        self.assertEqual((calibrated.content_revision, calibrated.layout_revision), (3, 2))
        self.assertEqual(calibrated.generated_content_revision, 3)
        self.assertEqual(calibrated.render_strategy, "source_fidelity")
        self.assertEqual(self.storage.get_page_versions("b", 1)[1]["text"], "生成源码")

    def test_legacy_response_does_not_downgrade_a_fidelity_page(self) -> None:
        result = StructuredPageResult(header_segments=[], footer_segments=[], body_latex="旧协议正文")
        self.assertTrue(self.storage.save_page_result("b", 1, result, expected_content_revision=0))
        page = self.storage.get_pages("b")[0]
        self.assertEqual(page.render_strategy, "source_fidelity")
        self.assertIsNone(page.layout_source)
        self.assertIsNone(page.generated_content_revision)

    def test_unknown_geometry_is_saved_for_calibration_without_fake_generated_revision(self) -> None:
        self.assertTrue(self.storage.save_page_result(
            "b", 1, self.result, expected_content_revision=0, source_metadata=self.source,
            generated_text=None, layout_source=self.layout,
        ))
        page = self.storage.get_pages("b")[0]
        self.assertEqual(page.render_strategy, "source_fidelity")
        self.assertEqual(page.text, "原文")
        self.assertIsNone(page.layout_source.lines[0].bbox)
        self.assertIsNone(page.generated_content_revision)
        self.assertIsNone(page.layout_source.generated_content_revision)

    def test_inconsistent_layout_cannot_change_effective_content(self) -> None:
        mismatched = self.layout.model_copy(update={"lines": [
            self.layout.lines[0].model_copy(update={"latex": "另一正文"}),
        ]})
        with self.assertRaises(ValueError):
            self.storage.save_page_result(
                "b", 1, self.result, expected_content_revision=0,
                source_metadata=self.source, generated_text="生成源码", layout_source=mismatched,
            )
        self.assertEqual(self.storage.get_pages("b")[0].content_revision, 0)
        self.assertEqual(self.storage.get_page_versions("b", 1), [])

    def test_complete_ocr_document_keeps_preamble_and_observed_layout(self) -> None:
        document = r"\documentclass{article}\newcommand{\mine}{kept}\begin{document}\mine\end{document}"
        result = self.result.model_copy(update={"body_latex": document})
        self.assertTrue(self.storage.save_page_result(
            "b", 1, result, expected_content_revision=0,
            source_metadata=self.source, generated_text="replacement", layout_source=self.layout,
        ))
        page = self.storage.get_pages("b")[0]
        self.assertEqual(page.text, document)
        self.assertEqual(page.render_strategy, "custom_latex")
        self.assertIsNotNone(page.layout_source)
        self.assertIsNone(page.generated_content_revision)


class LayoutMigrationTests(unittest.TestCase):
    def test_incremental_migration_and_repeat_initialize_preserve_legacy_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with closing(sqlite3.connect(root / "app.db")) as connection, connection:
                connection.executescript("""
                    CREATE TABLE books(id TEXT PRIMARY KEY, title TEXT, filename TEXT, status TEXT,
                        page_count INTEGER, completed_pages INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT,
                        content_format TEXT, layout_json TEXT NOT NULL DEFAULT '{}');
                    INSERT INTO books VALUES ('old','旧书','old.pdf','ready',1,1,NULL,'2020','latex','{}');
                    CREATE TABLE pages(book_id TEXT, number INTEGER, width INTEGER, height INTEGER,
                        image_name TEXT, status TEXT, error TEXT, extraction_text TEXT, extraction_json TEXT,
                        blocks_json TEXT, text TEXT, page_kind TEXT, page_side TEXT,
                        cover_fields_json TEXT, header_segments_json TEXT, footer_segments_json TEXT,
                        usage_unknown INTEGER, PRIMARY KEY(book_id,number));
                    INSERT INTO pages VALUES ('old',7,300,500,'page-0007.png','ready',NULL,'raw','{}',
                        '[]','人工旧源码','content','right','[]','[]',
                        '[{"kind":"page_number","text":"7","alignment":"right"}]',0);
                    CREATE TABLE page_attempts(book_id TEXT, page_number INTEGER, attempt INTEGER,
                        returned INTEGER, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                        PRIMARY KEY(book_id,page_number,attempt));
                    INSERT INTO page_attempts VALUES ('old',7,1,1,10,20,30);
                """)
            storage = Storage(root)
            storage.initialize()
            first = storage.get_pages("old")[0]
            self.assertEqual((first.text, first.page_side, first.attempts), ("人工旧源码", "right", 1))
            self.assertEqual(first.usage.total_tokens, 30)
            self.assertEqual(first.footer_segments[0].text, "7")
            self.assertEqual(first.render_strategy, "legacy_template")
            self.assertIsNone(first.layout_source)
            self.assertEqual(storage.get_book("old").render_strategy, "legacy_template")
            self.assertTrue((root / "app-before-layout.db").is_file())
            storage.initialize()
            self.assertEqual(storage.get_pages("old")[0].model_dump(), first.model_dump())
            self.assertEqual(storage.create_project("new", "新书").render_strategy, "source_fidelity")


if __name__ == "__main__":
    unittest.main()
