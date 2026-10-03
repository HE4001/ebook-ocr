"""Isolated API and cache fixtures; XeLaTeX is mocked for the API suite."""

import json
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf as fitz
from fastapi.testclient import TestClient
from PIL import Image

from backend.main import create_app
from backend.fidelity_rendering import MEASUREMENT_VERSION
from backend.compile_service import OUTPUT_CHECKS_VERSION, inspect_pdf
from backend.latex_export import LatexDocument
from backend.latex_diagnostics import diagnostics_quality_status


def image_bytes(color="white"):
    stream = BytesIO()
    Image.new("RGB", (300, 500), color).save(stream, format="PNG")
    return stream.getvalue()


def calibration(page, text="Original line"):
    return {
        "expected_content_revision": page["content_revision"],
        "expected_layout_revision": page["layout_revision"],
        "observation": {
            "schema_version": 1, "body_frame": [.1, .1, .9, .9],
            "regions": [{"region_id": "body", "kind": "body", "order": 0,
                         "bbox": [.1, .1, .9, .9], "parent_id": None, "basis": "manual"}],
            "lines": [{"line_id": "line-1", "block_id": "body", "order": 0,
                       "kind": "text", "latex": text, "bbox": [.1, .2, .9, .25],
                       "baseline": .24, "basis": "manual", "style": {
                           "font_family": None, "font_size_bp": None, "font_size_ratio": None,
                           "bold": None, "italic": None, "basis": None}}],
            "equation_groups": [], "review_reasons": [],
        },
        "canvas_width_bp": 300, "canvas_height_bp": 500,
        "body_font_size_bp": 10, "body_font_family": "songti",
    }


class CompileApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = TestClient(create_app(self.root))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.calls = []
        self.page_count = 1
        self.compiler_log = ""
        self.outside = False

        async def compiler(source, output_dir):
            self.calls.append(source)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "document.tex").write_text(source, encoding="utf-8")
            (output_dir / "compiled.log").write_text(self.compiler_log, encoding="utf-8")
            tokens = re.findall(r"\\EbookMeasuredLine\{([^{}]+)\}", source)
            (output_dir / "compiled.ebook-metrics.csv").write_text(
                f"{MEASUREMENT_VERSION}\ntoken,natural_width_bp,height_bp,depth_bp,anchor_prefix_bp\n"
                + "".join(f"{token},20,6,2,0\n" for token in tokens), encoding="utf-8")
            with fitz.open() as document:
                count = 2 if "MULTIPAGE" in source else self.page_count
                paper = re.search(r"paperwidth=([\d.]+)(bp|mm),paperheight=([\d.]+)(bp|mm)", source)
                width, height = (float(paper[1]) * (72 / 25.4 if paper[2] == "mm" else 1),
                                 float(paper[3]) * (72 / 25.4 if paper[4] == "mm" else 1)) if paper else (420, 595)
                for _ in range(count):
                    page = document.new_page(width=width, height=height)
                    page.insert_text((width - 5 if self.outside else 40, 50), "Original line", fontsize=12)
                document.save(output_dir / "document.pdf")
            return output_dir / "document.pdf"

        self.runtime_identity = {"engine": "mock", "fonts": "mock"}
        runtime = patch("backend.compile_service.compiler_identity", side_effect=lambda: self.runtime_identity)
        compile_mock = patch("backend.compile_service.compile_pdf", side_effect=compiler)
        runtime.start()
        compile_mock.start()
        self.addCleanup(runtime.stop)
        self.addCleanup(compile_mock.stop)

    def project(self, count=1):
        book = self.client.post("/api/projects", json={"title": "Synthetic"}).json()
        self.url = f'/api/books/{book["id"]}'
        upload = self.client.post(f"{self.url}/files", files=[
            ("files", (f"page-{index}.png", image_bytes(), "image/png")) for index in range(count)
        ])
        self.assertEqual(upload.status_code, 200)
        self.files = upload.json()["files"]
        self.client.post(f"{self.url}/confirm-upload")
        self.client.put(f"{self.url}/arrangement", json={
            "file_order": [item["id"] for item in self.files], "page_order": list(range(1, count + 1))})
        return self.client.get(self.url).json()["pages"]

    def save_calibration(self, number, page, **changes):
        response = self.client.put(f"{self.url}/pages/{number}/layout", json={**calibration(page), **changes})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_calibration_draft_save_cas_and_source_edit_authority(self):
        page = self.project()[0]
        payload = calibration(page)
        draft = self.client.post(f"{self.url}/pages/1/compile-layout-pdf", json=payload).json()
        self.assertEqual(draft["quality_status"], "passed")
        self.assertEqual(self.client.get(self.url).json()["pages"][0], page)
        saved = self.save_calibration(1, page)
        self.assertEqual((saved["content_revision"], saved["layout_revision"]), (1, 1))
        self.assertEqual(saved["render_strategy"], "source_fidelity")
        self.assertEqual(self.client.put(f"{self.url}/pages/1/layout", json=payload).status_code, 409)
        unchanged = self.client.post(f"{self.url}/pages/1/compile", json={"text": saved["text"]}).json()
        self.assertEqual(unchanged["page_map"][0]["render_strategy"], "source_fidelity")
        exact = r"\documentclass{article}" + "\n" + r"\newcommand{\custom}{MULTIPAGE}" + "\n" + r"\begin{document}\custom\end{document}"
        source_draft = self.client.post(f"{self.url}/pages/1/compile", json={"text": exact}).json()
        self.assertEqual(self.calls[-1], exact)
        self.assertEqual(source_draft["page_map"][0]["render_strategy"], "custom_latex")
        self.assertEqual(source_draft["page_map"][0]["output_page_end"], 2)
        self.assertEqual(source_draft["quality_status"], "unverified")
        edited = self.client.put(f"{self.url}/pages/1", json={
            "text": exact, "expected_content_revision": 1, "expected_layout_revision": 1}).json()
        self.assertEqual(edited["render_strategy"], "custom_latex")
        self.assertEqual(edited["layout_source"], saved["layout_source"])
        self.assertEqual(self.client.put(f"{self.url}/pages/1", json={
            "text": "stale", "expected_content_revision": 1}).status_code, 409)
        self.assertEqual(self.client.put(f"{self.url}/pages/1/layout", json={
            **calibration(edited), "render_strategy": "legacy_template"}).status_code, 422)

    def test_unknown_and_omitted_calibration_parameters_keep_their_basis(self):
        page = self.project()[0]
        payload = calibration(page)
        for name in ("canvas_width_bp", "canvas_height_bp", "body_font_size_bp", "body_font_family"):
            payload.pop(name)
        saved = self.client.put(f"{self.url}/pages/1/layout", json=payload).json()
        layout = saved["layout_source"]
        for name in ("canvas_width_bp", "canvas_height_bp", "canvas_basis", "body_font_size_bp",
                     "body_font_family", "body_font_basis"):
            self.assertIsNone(layout[name])
        confirmed = self.save_calibration(1, saved)
        payload.update(expected_content_revision=confirmed["content_revision"],
                       expected_layout_revision=confirmed["layout_revision"])
        again = self.client.put(f"{self.url}/pages/1/layout", json=payload).json()
        for name in ("canvas_width_bp", "canvas_height_bp", "canvas_basis", "body_font_size_bp",
                     "body_font_family", "body_font_basis"):
            self.assertEqual(again["layout_source"][name], confirmed["layout_source"][name])
        unknown = {**payload, "expected_content_revision": again["content_revision"],
                   "expected_layout_revision": again["layout_revision"],
                   "canvas_width_bp": None, "canvas_height_bp": None,
                   "body_font_size_bp": None, "body_font_family": None}
        cleared = self.client.put(f"{self.url}/pages/1/layout", json=unknown).json()
        for name in ("canvas_width_bp", "canvas_height_bp", "canvas_basis", "body_font_size_bp",
                     "body_font_family", "body_font_basis"):
            self.assertIsNone(cleared["layout_source"][name])
        one_font = {**payload, "expected_content_revision": cleared["content_revision"],
                    "expected_layout_revision": cleared["layout_revision"], "body_font_size_bp": 10}
        partial = self.client.post(f"{self.url}/pages/1/compile-layout-pdf", json=one_font).json()
        self.assertEqual(partial["quality_status"], "needs_review")
        partial_saved = self.client.put(f"{self.url}/pages/1/layout", json=one_font).json()
        self.assertEqual(partial_saved["layout_source"]["body_font_basis"], "project")
        self.assertIsNone(partial_saved["layout_source"]["body_font_family"])
        omission = {**payload, "expected_content_revision": partial_saved["content_revision"],
                    "expected_layout_revision": partial_saved["layout_revision"]}
        self.client.post(f"{self.url}/pages/1/compile-layout-pdf", json=omission)
        self.assertEqual(self.client.get(self.url).json()["pages"][0], partial_saved)
        single_canvas = {**omission, "canvas_width_bp": None}
        self.assertEqual(self.client.put(f"{self.url}/pages/1/layout", json=single_canvas).status_code, 422)

    def test_unit_cache_reorder_single_page_edit_and_current_source_fingerprint(self):
        pages = self.project(2)
        saved = [self.save_calibration(index, page) for index, page in enumerate(pages, 1)]
        first = self.client.post(f"{self.url}/compile").json()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.client.post(f"{self.url}/compile").json(), first)
        self.assertEqual(len(self.calls), 2)
        self.client.put(f"{self.url}/arrangement", json={
            "file_order": [item["id"] for item in self.files], "page_order": [2, 1]})
        reordered = self.client.post(f"{self.url}/compile").json()
        self.assertNotEqual(reordered["pdf_url"], first["pdf_url"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([(item["page_number"], item["output_page_start"]) for item in reordered["page_map"]], [(2, 1), (1, 2)])
        self.client.put(f"{self.url}/pages/1", json={"text": "Edited source"})
        edited = self.client.post(f"{self.url}/compile").json()
        self.assertEqual(len(self.calls), 3)
        book_id = self.url.rsplit("/", 1)[1]
        source = self.root / "books" / book_id / "sources" / self.files[1]["id"] / "source.png"
        source.write_bytes(image_bytes("red"))
        changed_source = self.client.post(f"{self.url}/compile").json()
        self.assertEqual(len(self.calls), 4)
        self.assertIn("LAYOUT_SOURCE_MISMATCH", [item["code"] for item in changed_source["diagnostics"]])
        self.assertNotEqual(changed_source["pdf_url"], edited["pdf_url"])
        digest = changed_source["pdf_url"].rsplit("/", 1)[1][:-4]
        cache = self.root / "books" / book_id / "latex-cache" / digest / "result.json"
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["result"], changed_source)

    def test_paper_print_runtime_and_diagnostic_versions_invalidate_cache(self):
        page = self.project()[0]
        self.save_calibration(1, page)
        first = self.client.post(f"{self.url}/compile").json()
        self.client.put(f"{self.url}/layout", json={"paper_size": "a5"})
        paper = self.client.post(f"{self.url}/compile").json()
        printed = self.client.post(f"{self.url}/compile?print_version=true").json()
        self.runtime_identity = {"engine": "mock-v2", "fonts": "mock-v2"}
        runtime = self.client.post(f"{self.url}/compile?print_version=true").json()
        with patch("backend.compile_service.DIAGNOSTICS_VERSION", "diagnostics-test-v2"):
            diagnostics = self.client.post(f"{self.url}/compile?print_version=true").json()
        self.assertEqual(len({item["pdf_url"] for item in (first, paper, printed, runtime, diagnostics)}), 5)
        self.assertEqual(len(self.calls), 5)
        with TestClient(create_app(self.root)) as restarted:
            self.assertEqual(restarted.post(f"{self.url}/compile?print_version=true").json(), runtime)
        self.assertEqual(len(self.calls), 5)

    def test_actual_pdf_overflow_missing_glyph_font_substitution_and_continuation(self):
        page = self.project()[0]
        self.save_calibration(1, page)
        self.outside = True
        self.page_count = 2
        self.compiler_log = "Missing character: There is no X in font Y!\nLaTeX Font Warning: Font shape undefined, using replacement instead.\n\n"
        result = self.client.post(f"{self.url}/compile").json()
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertTrue({"CONTENT_OUTSIDE_PAGE", "UNEXPECTED_PAGE_COUNT", "MISSING_GLYPH", "FONT_SUBSTITUTION"} <= codes)
        self.assertEqual(result["quality_status"], "needs_review")
        self.assertEqual(result["page_map"][0]["output_page_end"], 2)
        self.assertNotIn("Overfull", self.compiler_log)
        digest = result["pdf_url"].rsplit("/", 1)[1][:-4]
        self.assertEqual(self.client.get(f"{self.url}/compiled/{digest}/pages/2.png").headers["content-type"], "image/png")
        self.assertEqual(self.client.get(f"{self.url}/compiled/{digest}/pages/3.png").status_code, 404)

    def test_fixed_makebox_without_overfull_is_reported_from_pdf_glyphs(self):
        self.project()
        self.outside = True
        source = r"\noindent\makebox[10pt][l]{A long original line remains intact}"
        result = self.client.post(f"{self.url}/pages/1/compile", json={"text": source}).json()
        outside = [item for item in result["diagnostics"] if item["code"] == "CONTENT_OUTSIDE_PAGE"]
        self.assertEqual(len(outside), 1)
        self.assertEqual(outside[0]["basis"], OUTPUT_CHECKS_VERSION)
        self.assertGreater(outside[0]["overflow_bp"], 0)
        self.assertEqual(self.compiler_log, "")
        self.assertEqual(result["quality_status"], "needs_review")
        self.assertIn(source, self.calls[-1])

    def test_extended_math_glyphs_with_unmapped_unicode_are_not_missing(self):
        self.project()
        model = self.client.app.state.storage.get_pages(self.url.rsplit("/", 1)[1])[0]
        output = MagicMock()
        output.rect = fitz.Rect(0, 0, 300, 450)
        output.rotation_matrix = fitz.Matrix(1, 1)
        output.get_texttrace.return_value = [{"type": 0, "chars": [
            (0xfffd, glyph, (20, 30 + glyph), (20, 20 + glyph, 30, 30 + glyph)) for glyph in (1, 2, 3)
        ]}]
        pdf = MagicMock()
        pdf.__len__.return_value = 1
        pdf.__iter__.side_effect = lambda: iter([output])
        pdf.__enter__.return_value = pdf
        document = LatexDocument("", [1], "source_fidelity", output_width_bp=300, output_height_bp=450)
        with patch("backend.compile_service.fitz.open", return_value=pdf):
            diagnostics, sizes = inspect_pdf(Path("synthetic.pdf"), model, document, self.url.rsplit("/", 1)[1])
            self.assertEqual(sizes, [(300, 450)])
            self.assertEqual([item.code for item in diagnostics], ["TEXT_MAPPING_INCOMPLETE"])
            self.assertEqual((diagnostics[0].severity, diagnostics[0].coverage), ("info", "partial"))
            self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "unverified")
            output.get_texttrace.return_value = [{"type": 0, "chars": [
                (ord("X"), 0, (20, 30), (20, 20, 30, 30)),
                (0, 1, (30, 30), (30, 20, 40, 30)),
            ]}]
            diagnostics, _ = inspect_pdf(Path("synthetic.pdf"), model, document, self.url.rsplit("/", 1)[1])
            self.assertEqual([item.code for item in diagnostics], ["MISSING_GLYPH"])

    def test_mixed_custom_paper_multiple_pages_fidelity_and_blank_template(self):
        pages = self.project(3)
        self.save_calibration(1, pages[0])
        custom = (r"\documentclass{article}\usepackage{geometry}"
                  r"\geometry{paperwidth=200bp,paperheight=400bp}" "\n"
                  r"\newcommand{\sample}{MULTIPAGE}\begin{document}\sample\end{document}")
        self.client.put(f"{self.url}/pages/2", json={"text": custom})
        self.client.put(f"{self.url}/pages/3", json={"text": "", "render_strategy": "legacy_template"})
        result = self.client.post(f"{self.url}/compile").json()
        self.assertEqual([(item["page_number"], item["output_page_start"], item["output_page_end"])
                          for item in result["page_map"]], [(1, 1, 1), (2, 2, 3), (3, 4, 4)])
        self.assertEqual([item["render_strategy"] for item in result["page_map"]],
                         ["source_fidelity", "custom_latex", "legacy_template"])
        self.assertEqual((result["page_map"][1]["output_width_bp"], result["page_map"][1]["output_height_bp"]), (200, 400))
        self.assertEqual(self.calls[1], custom)
        self.assertIn(r"\null", self.calls[2])
        self.assertEqual(result["quality_status"], "unverified")

    def test_missing_layout_and_syntax_failure_return_no_current_pdf(self):
        page = self.project()[0]
        missing = self.client.post(f"{self.url}/compile").json()
        self.assertEqual((missing["quality_status"], missing["pdf_url"]), ("compile_failed", None))
        self.assertIn("LAYOUT_UNVERIFIED", [item["code"] for item in missing["diagnostics"]])
        self.assertEqual(self.client.get(f"{self.url}/export.tex").status_code, 422)
        unknown = calibration(page)
        unknown["observation"]["lines"][0].update(bbox=None, baseline=None)
        draft = self.client.post(f"{self.url}/pages/1/compile-layout-pdf", json=unknown).json()
        self.assertEqual((draft["quality_status"], draft["pdf_url"]), ("compile_failed", None))
        self.assertEqual(draft["diagnostics"][0]["line_id"], "line-1")
        self.assertEqual(self.client.get(self.url).json()["pages"][0], page)
        self.save_calibration(1, page)
        from backend.latex_export import LatexCompileError
        with patch("backend.compile_service.compile_pdf", side_effect=LatexCompileError("invalid source")):
            failed = self.client.post(f"{self.url}/pages/1/compile", json={"text": r"\badcommand"}).json()
        self.assertEqual((failed["quality_status"], failed["pdf_url"]), ("compile_failed", None))
        self.assertEqual(failed["diagnostics"][0]["page_number"], 1)
        self.assertEqual(failed["diagnostics"][0]["code"], "COMPILE_FAILED")

    def test_rotated_pdf_mapping_binding_and_blank_legacy_template(self):
        with fitz.open() as source:
            source_page = source.new_page(width=300, height=500)
            source_page.set_rotation(90)
            pdf = source.tobytes()
        book = self.client.post("/api/books", files={"file": ("rotated.pdf", pdf, "application/pdf")}).json()
        self.url = f'/api/books/{book["id"]}'
        self.client.put(f"{self.url}/arrangement", json={"file_order": ["legacy"], "page_order": [1]})
        page = self.client.get(self.url).json()["pages"][0]
        saved = self.save_calibration(1, page)
        self.assertEqual(saved["source_metadata"]["pdf_geometry"]["rotation"], 90)
        self.assertEqual(saved["source_metadata"]["canonical_to_source_affine"][0], 0)
        ordinary = self.client.post(f"{self.url}/compile").json()
        self.assertIsNotNone(ordinary["page_map"][0]["source_to_output_affine"])
        storage = self.client.app.state.storage
        with storage._connect() as connection:
            connection.execute("UPDATE pages SET page_side='left' WHERE book_id=?", (book["id"],))
        left = self.client.post(f"{self.url}/compile?print_version=true").json()
        with storage._connect() as connection:
            connection.execute("UPDATE pages SET page_side='right' WHERE book_id=?", (book["id"],))
        right = self.client.post(f"{self.url}/compile?print_version=true").json()
        self.assertLess(left["page_map"][0]["source_to_output_affine"][4], right["page_map"][0]["source_to_output_affine"][4])
        self.assertEqual(self.client.put(f"{self.url}/layout", json={"render_strategy": "legacy_template"}).status_code, 200)
        blank = self.client.post(f"{self.url}/pages/1/compile", json={"text": "", "render_strategy": "legacy_template"}).json()
        self.assertEqual(blank["page_map"][0]["render_strategy"], "custom_latex")
        self.assertIn(r"\null", self.calls[-1])
        self.assertEqual(blank["quality_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
