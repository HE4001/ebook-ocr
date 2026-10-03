from __future__ import annotations

import copy
import io
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from PIL import Image
from pydantic import ValidationError

from backend.importers import import_document, prepare_pdf_page, read_page_source_metadata
from backend.layout_contract import (
    LayoutObservation, PageSourceMetadata, SourceFidelityLayout,
    equation_alignment_index, transform_point,
)
from backend.models import StructuredPageResult
from backend.storage import Storage


def observation() -> dict:
    return {
        "regions": [{"region_id": "body", "kind": "body", "order": 0}],
        "lines": [{"line_id": "l1", "block_id": "body", "order": 0,
                   "latex": "正文", "bbox": [0.1, 0.1, 0.9, 0.2], "baseline": 0.18}],
    }


def response(version: int) -> dict:
    value = {"page_kind": "content", "page_side": "unknown", "cover_fields": [],
             "header_segments": [], "body_latex": "正文", "footer_segments": []}
    if version == 2:
        value.update(response_version=2, layout=LayoutObservation.model_validate(observation()).model_dump())
    return value


class LayoutContractTests(unittest.TestCase):
    def test_layout_rejects_invalid_geometry_and_structure(self) -> None:
        invalid = []
        for box in ([0.9, 0.1, 0.1, 0.2], [-0.1, 0.1, 0.9, 0.2],
                    [0.1, 0.1, float("nan"), 0.2], [0.1, 0.1, float("inf"), 0.2]):
            value = observation()
            value["lines"][0]["bbox"] = box
            invalid.append(value)
        value = observation()
        value["lines"].append(copy.deepcopy(value["lines"][0]))
        invalid.append(value)
        value = observation()
        value["lines"][0]["block_id"] = "missing"
        invalid.append(value)
        value = observation()
        value["regions"][0]["parent_id"] = "body"
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                LayoutObservation.model_validate(value)

    def test_response_versions_and_model_evidence_are_explicit(self) -> None:
        StructuredPageResult.from_model_response(response(1), 1)
        StructuredPageResult.from_model_response(response(2), 2)
        for field, content in (("response_version", 1), ("layout", None)):
            value = response(1)
            value[field] = content
            with self.subTest(field=field), self.assertRaises(ValueError):
                StructuredPageResult.from_model_response(value, 1)
        value = response(2)
        value["layout"]["lines"][0]["style"]["basis"] = "manual"
        with self.assertRaises(ValueError):
            StructuredPageResult.from_model_response(value, 2)
        LayoutObservation.model_validate(value["layout"])  # Manual calibration accepts manual evidence.
        for path in (("schema_version",), ("lines",), ("lines", 0, "style"),
                     ("lines", 0, "style", "bold")):
            value = response(2)
            parent = value["layout"]
            for key in path[:-1]:
                parent = parent[key]
            del parent[path[-1]]
            with self.subTest(path=path), self.assertRaises(ValueError):
                StructuredPageResult.from_model_response(value, 2)

    def test_equations_use_one_math_body_and_one_group_anchor(self) -> None:
        self.assertEqual(equation_alignment_index(r"x&=\frac{a}{b}"), 1)
        self.assertIsNone(equation_alignment_index(r"\begin{cases}a&b\\c&d\end{cases}"))
        self.assertIsNone(equation_alignment_index(r"x\&y"))
        for latex in (r"\[x\]", "$x$", r"\begin{align}x\end{align}", "x&=&y"):
            with self.subTest(latex=latex), self.assertRaises(ValueError):
                equation_alignment_index(latex)
        value = observation()
        value["lines"][0].update(kind="equation", latex="x&=y")
        with self.assertRaises(ValidationError):
            LayoutObservation.model_validate(value)
        value["equation_groups"] = [{"group_id": "g1", "line_ids": ["l1"], "align_x": 0.5}]
        LayoutObservation.model_validate(value)

    def test_model_review_reasons_leave_three_internal_program_slots(self) -> None:
        value = response(2)
        value["layout"]["review_reasons"] = ["模型复核"] * 100
        StructuredPageResult.from_model_response(value, 2)
        value["layout"]["review_reasons"].append("程序复核")
        with self.assertRaises(ValueError):
            StructuredPageResult.from_model_response(value, 2)
        value["layout"]["review_reasons"].extend(["尺寸复核", "字体复核"])
        LayoutObservation.model_validate(value["layout"])
        value["layout"]["review_reasons"].append("超量")
        with self.assertRaises(ValidationError):
            LayoutObservation.model_validate(value["layout"])

    def test_pdf_metadata_keeps_crop_rotation_and_pixel_mapping(self) -> None:
        expected_origin = {0: (0, 0), 90: (0, 260), 180: (180, 260), 270: (180, 0)}
        for rotation in expected_origin:
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                with fitz.open() as document:
                    page = document.new_page(width=200, height=300)
                    page.set_cropbox(fitz.Rect(10, 20, 190, 280))
                    page.set_rotation(rotation)
                    data = document.tobytes()
                import_document(data, "source.pdf", directory)
                prepare_pdf_page(directory, 1)
                metadata = read_page_source_metadata(directory, {
                    "book_id": "b", "number": 7, "source_id": "s", "source_page": 1,
                    "source_kind": "pdf", "source_directory": "", "image_name": "page-0001.png",
                    "source_filename": "source.pdf",
                })
                self.assertEqual(metadata.pdf_geometry.crop_box_bp, (10, 20, 190, 280))
                self.assertEqual((metadata.pdf_geometry.width_bp, metadata.pdf_geometry.height_bp), (180, 260))
                self.assertEqual(metadata.pdf_geometry.rotation, rotation)
                self.assertEqual(transform_point(metadata.canonical_to_source_affine, 0, 0), expected_origin[rotation])

    def test_wire_length_boundary_survives_three_program_review_reasons_and_storage(self) -> None:
        for body, reasons in (("x" * 1_000_000, []), ("", ["r" * 10_000] * 100)):
            with self.subTest(body_length=len(body)), tempfile.TemporaryDirectory() as temporary:
                value = response(2)
                value["body_latex"] = ""
                value["layout"]["lines"][0]["latex"] = body
                value["layout"]["review_reasons"] = reasons
                result = StructuredPageResult.from_model_response(value, 2)
                result.layout.review_reasons.extend(["p" * 1_000] * 3)
                source = PageSourceMetadata(
                    book_id="b", page_number=1, source_id="legacy", source_page=1, source_kind="image",
                    source_file_fingerprint="source", image_fingerprint="image",
                    canonical_width_px=1, canonical_height_px=1,
                    source_coordinate_space="original_image_px", canonical_to_source_affine=(1, 0, 0, 1, 0, 0),
                )
                layout = SourceFidelityLayout(
                    **result.layout.model_dump(), source=source, content_revision=1, layout_revision=1,
                )
                storage = Storage(Path(temporary))
                storage.initialize()
                storage.create_book("b", "书", "source.png", [(1, 1, 1, "page-0001.png")])
                self.assertTrue(storage.save_page_result(
                    "b", 1, result, expected_content_revision=0, source_metadata=source,
                    layout_source=layout, generated_text=None,
                ))
                saved = storage.get_pages("b")[0]
                self.assertEqual(saved.layout_source.lines[0].latex, body)
                self.assertEqual(saved.layout_source.review_reasons[-3:], ["p" * 1_000] * 3)

    def test_wire_and_internal_length_limits_reject_overlong_content(self) -> None:
        value = response(2)
        value["body_latex"] = ""
        value["layout"]["lines"][0]["latex"] = "x" * 999_999
        value["layout"]["review_reasons"] = ["rr"]
        LayoutObservation.model_validate(value["layout"])
        with self.assertRaisesRegex(ValueError, "模型页面内容过长"):
            StructuredPageResult.from_model_response(value, 2)
        value["layout"]["lines"][0]["latex"] = ""
        value["layout"]["review_reasons"] = ["r" * 1_003_001]
        with self.assertRaisesRegex(ValidationError, "布局复核原因过长"):
            LayoutObservation.model_validate(value["layout"])
        value = observation()
        value["lines"][0]["latex"] = "x" * 600_000
        second = copy.deepcopy(value["lines"][0])
        second.update(line_id="l2", order=1, latex="x" * 400_001)
        value["lines"].append(second)
        with self.assertRaisesRegex(ValidationError, "布局内容过长"):
            LayoutObservation.model_validate(value)

    def test_image_exif_inverse_preserves_original_pixel_coordinates(self) -> None:
        expected_origin = {1: (0, 0), 2: (12, 0), 3: (12, 8), 4: (0, 8),
                           5: (0, 0), 6: (0, 8), 7: (12, 8), 8: (12, 0)}
        for orientation in expected_origin:
            with self.subTest(orientation=orientation), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                image = Image.new("RGB", (12, 8), "white")
                exif = image.getexif()
                exif[274] = orientation
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", exif=exif)
                import_document(buffer.getvalue(), "source.jpg", directory)
                metadata = read_page_source_metadata(directory, {
                    "book_id": "b", "number": 1, "source_id": "s", "source_page": 1,
                    "source_kind": "image", "source_directory": "", "image_name": "page-0001.png",
                    "source_filename": "source.jpg",
                })
                self.assertIsNone(metadata.pdf_geometry)
                self.assertEqual((metadata.source_width_px, metadata.source_height_px), (12, 8))
                size = (8, 12) if orientation >= 5 else (12, 8)
                self.assertEqual((metadata.canonical_width_px, metadata.canonical_height_px), size)
                self.assertEqual(transform_point(metadata.canonical_to_source_affine, 0, 0), expected_origin[orientation])


if __name__ == "__main__":
    unittest.main()
