import tempfile
import unittest
from pathlib import Path

from backend.latex_diagnostics import diagnose_document, diagnostics_quality_status, read_measurements
from backend.latex_export import build_latex_documents
from backend.tests.test_fidelity_rendering import synthetic_detail, synthetic_layout, synthetic_page


def write_sidecars(path: Path, *, width=100, second_height=10, log=""):
    (path / "compiled.log").write_text(log, encoding="utf-8")
    (path / "compiled.ebook-metrics.csv").write_text(
        "measurement-v2\ntoken,natural_width_bp,height_bp,depth_bp,anchor_prefix_bp\n"
        f"line0001,{width},8,2,0\n"
        "line0002,50,8,2,10\n"
        f"line0003,45,{second_height},3,0\n"
        "number0003,15,8,2,0\n"
        "line0004,40,9,2,0\n", encoding="utf-8",
    )


class LatexDiagnosticsTests(unittest.TestCase):
    def diagnostics(self, path: Path, layout=None, **kwargs):
        page = synthetic_page(layout or synthetic_layout())
        document = build_latex_documents(synthetic_detail([page]))[0]
        return diagnose_document(document, page, path, book_id="synthetic", arrangement_position=1, **kwargs)

    def test_natural_width_detects_paper_overflow_without_overfull(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            # A fixed picture/line holder creates no Overfull warning; its natural body still exceeds the paper.
            write_sidecars(path, width=400)
            diagnostics = self.diagnostics(path, output_page_start=4, output_page_end=4)
        outside = next(diagnostic for diagnostic in diagnostics if diagnostic.code == "CONTENT_OUTSIDE_PAGE")
        self.assertEqual((outside.line_id, outside.block_id, outside.output_page_start), ("intro", "body", 4))
        self.assertAlmostEqual(outside.overflow_bp, 130)
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "needs_review")

    def test_tall_formula_collides_with_neighbor_but_internal_fraction_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_sidecars(path)
            self.assertFalse(any(item.code == "BLOCK_OVERLAP" for item in self.diagnostics(path)))
            write_sidecars(path, second_height=60)
            self.assertTrue(any(item.code == "BLOCK_OVERLAP" for item in self.diagnostics(path)))

    def test_missing_measurements_never_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "compiled.log").write_text("", encoding="utf-8")
            diagnostics = self.diagnostics(path)
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "unverified")

    def test_missing_glyph_and_font_substitution_are_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_sidecars(path, log="Missing character: There is no 字 in font Test!\n\nLaTeX Font Warning: Font shape undefined\nusing another font instead.\n\n")
            diagnostics = self.diagnostics(path)
        self.assertTrue({"MISSING_GLYPH", "FONT_SUBSTITUTION"}.issubset({item.code for item in diagnostics}))
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "needs_review")

    def test_unknown_font_and_model_geometry_remain_unverified(self):
        layout = synthetic_layout().model_copy(update={"body_font_basis": "model_estimate"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_sidecars(path)
            diagnostics = self.diagnostics(path, layout)
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "unverified")

    def test_revision_mismatch_is_not_a_current_pass(self):
        layout = synthetic_layout().model_copy(update={"content_revision": 1})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_sidecars(path)
            diagnostics = self.diagnostics(path, layout)
        self.assertIn("LAYOUT_SOURCE_MISMATCH", [item.code for item in diagnostics])

    def test_complete_measurement_still_requires_output_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_sidecars(path)
            diagnostics = self.diagnostics(path)
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=False), "unverified")
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "passed")

    def test_invalid_sidecar_is_reported_and_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_sidecars(path)
            with (path / "compiled.ebook-metrics.csv").open("a", encoding="utf-8") as stream:
                stream.write("line0001,1,1,1,0\n")
            with self.assertRaises(ValueError):
                read_measurements(path / "compiled.ebook-metrics.csv")
            diagnostics = self.diagnostics(path)
        self.assertEqual(diagnostics_quality_status(diagnostics, output_checks_complete=True), "unverified")

    def test_space_separated_header_is_not_a_protocol_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"compiled.ebook-metrics.csv"
            path.write_text("measurement-v2\ntoken natural_width_bp height_bp depth_bp anchor_prefix_bp\n",encoding="utf-8")
            with self.assertRaises(ValueError):
                read_measurements(path)
