import unittest

from backend.fidelity_rendering import fidelity_items, split_math_anchor
from backend.latex_export import LatexCompileError, build_latex, build_latex_documents, generate_source_fidelity_latex
from backend.layout_contract import (
    EquationGroup, EquationNumber, LayoutLine, LayoutRegion, PageSourceMetadata, SourceFidelityLayout,
)
from backend.models import Book, BookDetail, LayoutSettings, Page, Usage


def synthetic_layout() -> SourceFidelityLayout:
    return SourceFidelityLayout(
        source=PageSourceMetadata(
            book_id="synthetic", page_number=1, source_id="source", source_page=1,
            source_kind="image", source_file_fingerprint="image", image_fingerprint="image",
            canonical_width_px=600, canonical_height_px=900, source_width_px=600, source_height_px=900,
            source_coordinate_space="original_image_px", canonical_to_source_affine=(1, 0, 0, 1, 0, 0),
        ),
        content_revision=2, layout_revision=3, canvas_width_bp=300, canvas_height_bp=450,
        canvas_basis="manual", body_font_size_bp=10, body_font_family="songti", body_font_basis="manual",
        body_frame=(0.1, 0.1, 0.9, 0.9),
        regions=[LayoutRegion(region_id="body", kind="body", order=0, bbox=(0.1, 0.1, 0.9, 0.9), basis="manual")],
        lines=[
            LayoutLine(line_id="intro", block_id="body", order=0, latex=r"保留原行及 \(x^2\)。",
                       bbox=(0.1, 0.1, 0.7, 0.14), baseline=0.13, basis="manual"),
            LayoutLine(line_id="eq-a", block_id="body", order=1, kind="equation", latex="x & = 1",
                       bbox=(0.2, 0.2, 0.7, 0.25), baseline=0.24, basis="manual"),
            LayoutLine(line_id="eq-b", block_id="body", order=2, kind="equation", latex=r"& = \frac{2}{2}",
                       bbox=(0.3, 0.3, 0.7, 0.36), baseline=0.34, basis="manual"),
            LayoutLine(line_id="eq-c", block_id="body", order=3, kind="equation", latex=r"& = \sqrt{1}",
                       bbox=(0.3, 0.4, 0.7, 0.45), baseline=0.44, basis="manual"),
        ],
        equation_groups=[EquationGroup(
            group_id="derivation", line_ids=["eq-a", "eq-b", "eq-c"], align_x=0.3, basis="manual",
            number=EquationNumber(latex="(A)", line_id="eq-b", anchor_x=0.88, bbox=(0.8, 0.3, 0.88, 0.36)),
        )],
    )


def synthetic_page(layout: SourceFidelityLayout | None = None, **updates) -> Page:
    values = dict(
        number=1, source_id="source", source_page=1, status="ready", error=None, text="",
        render_strategy="source_fidelity", content_revision=2, layout_revision=3, generated_content_revision=2,
        layout_source=layout, header_segments=[], footer_segments=[],
        usage=Usage(input_tokens=None, output_tokens=None, total_tokens=None, complete=True), attempts=0,
    )
    values.update(updates)
    return Page(**values)


def synthetic_detail(pages: list[Page], paper="source") -> BookDetail:
    return BookDetail(book=Book(
        id="synthetic", title="原创布局夹具", filename="fixture.png", paper_size="a5",
        layout=LayoutSettings(source_fidelity_paper=paper), status="ready", page_count=len(pages),
        selection_confirmed=True, selected_page_count=len(pages), completed_pages=len(pages),
        error=None, created_at="2026-10-03", usage=pages[0].usage,
    ), pages=pages)


class FidelityRenderingTests(unittest.TestCase):
    def test_absolute_baselines_group_anchor_and_number_attachment(self):
        layout = synthetic_layout()
        items = fidelity_items(layout, 300, 450)
        equations = [item for item in items if item.line.kind == "equation" and not item.is_number]
        self.assertEqual([item.line.line_id for item in equations], ["eq-a", "eq-b", "eq-c"])
        self.assertEqual([item.anchor_x_bp for item in equations], [90, 90, 90])
        self.assertEqual([item.baseline_bp for item in equations], [108, 153, 198])
        label = next(item for item in items if item.is_number)
        self.assertEqual((label.line.line_id, label.anchor_x_bp, label.baseline_bp), ("eq-b", 264, 153))
        source = generate_source_fidelity_latex(layout)
        self.assertEqual(source.count(r"\shipout\vbox"), 1)
        self.assertIn("measurement-v2", source)
        self.assertNotIn(r"\resizebox", source)
        self.assertNotIn(r"\clip", source)

    def test_fidelity_layout_wins_over_generated_full_document(self):
        page = synthetic_page(synthetic_layout(), text=r"\documentclass{article}\begin{document}stale body\end{document}")
        document = build_latex_documents(synthetic_detail([page]))[0]
        self.assertEqual(document.render_strategy, "source_fidelity")
        self.assertIn("source-fidelity-v1", document.source)
        self.assertNotIn("stale body", document.source)

    def test_mixed_sources_are_independent_and_custom_source_exact(self):
        custom = "% preserve CRLF\r\n\\documentclass{article}\r\n\\begin{document}custom\\end{document}\r\n"
        pages = [synthetic_page(synthetic_layout()),
                 synthetic_page(None, number=2, render_strategy="custom_latex", text=custom),
                 synthetic_page(None, number=3, render_strategy="legacy_template", text="片段")]
        documents = build_latex_documents(synthetic_detail(pages))
        self.assertEqual([document.page_order for document in documents], [[1], [2], [3]])
        self.assertEqual(documents[1].source, custom)
        self.assertEqual(documents[2].render_strategy, "legacy_template")

    def test_project_mapping_preserves_aspect_and_only_scales_whole_canvas(self):
        page = synthetic_page(synthetic_layout(), page_side="right")
        document = build_latex_documents(synthetic_detail([page], "project"), True)[0]
        a, b, c, d, x, y = document.source_to_output_affine
        self.assertAlmostEqual(a / d, 300 / 450)
        self.assertEqual((b, c), (0, 0))
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)
        self.assertAlmostEqual(a / 300, document.canvas_scale)
        self.assertEqual(document.source.count(r"\scalebox{"), 1)

    def test_blank_source_remains_one_empty_canvas(self):
        layout = synthetic_layout().model_copy(update={"lines": [], "equation_groups": [], "body_frame": None})
        source = generate_source_fidelity_latex(layout)
        self.assertEqual(source.count(r"\shipout\vbox"), 1)
        self.assertNotIn(r"\thepage", source)
        self.assertNotIn(r"\EbookMeasuredLine{line", source)

    def test_missing_positions_are_explicit_errors(self):
        with self.assertRaises(LatexCompileError) as missing:
            build_latex_documents(synthetic_detail([synthetic_page()]))
        self.assertEqual(missing.exception.code, "LAYOUT_UNVERIFIED")
        layout = synthetic_layout()
        layout.lines[0].baseline = None
        with self.assertRaises(LatexCompileError) as missing_line:
            build_latex_documents(synthetic_detail([synthetic_page(layout)]))
        self.assertEqual(missing_line.exception.line_id, "intro")

    def test_template_reapplies_configured_baseline_after_each_newgeometry(self):
        pages = [synthetic_page(render_strategy="legacy_template", text="甲乙丙丁"),
                 synthetic_page(number=2, render_strategy="legacy_template", text="戊己庚辛")]
        source_lines = build_latex(synthetic_detail(pages)).splitlines()
        starts = [index for index,line in enumerate(source_lines) if line.startswith(r"\EbookPage{")]
        self.assertEqual(len(starts), 2)
        for index in starts:
            self.assertEqual(source_lines[index+1], r"\normalsize")

    def test_math_anchor_excludes_nested_alignment_and_escaped_ampersands(self):
        self.assertIsNone(split_math_anchor(r"\begin{matrix}a&b\\c&d\end{matrix}"))
        self.assertEqual(split_math_anchor(r"\text{a\&b} &= c"), (r"\text{a\&b} ", "= c"))
        self.assertEqual(split_math_anchor("x % ignored &\n &= c"), ("x % ignored &\n ", "= c"))
