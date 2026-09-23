from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
from PIL import Image

from backend.importers import ensure_pdf_pages, import_document


def _sample_pdf() -> bytes:
    document = fitz.open()
    first = document.new_page(width=200, height=300)
    first.set_cropbox(fitz.Rect(10, 20, 190, 280))
    first.set_rotation(90)
    first.insert_text((30, 50), "first")
    second = document.new_page(width=400, height=200)
    second.insert_text((30, 50), "second")
    data = document.tobytes()
    document.close()
    return data


def test_pdf_import_splits_pages_and_renders_from_single_page_pdf(tmp_path: Path) -> None:
    source_data = _sample_pdf()
    pages = import_document(source_data, "sample.pdf", tmp_path)

    assert [page[0] for page in pages] == [1, 2]
    assert (tmp_path / "source.pdf").read_bytes() == source_data
    source = fitz.open(tmp_path / "source.pdf")
    try:
        assert source.page_count == 2
        for number, source_page in enumerate(source, start=1):
            page_pdf = tmp_path / f"page-{number:04d}.pdf"
            assert page_pdf.is_file()
            split = fitz.open(page_pdf)
            try:
                assert split.page_count == 1
                split_page = split[0]
                assert split_page.rotation == source_page.rotation
                assert split_page.mediabox == source_page.mediabox
                assert split_page.cropbox == source_page.cropbox
                assert split_page.rect == source_page.rect
            finally:
                split.close()
    finally:
        source.close()

    for number, width, height, image_name in pages:
        with Image.open(tmp_path / image_name) as image:
            assert image.size == (width, height)
            assert width > 0 and height > 0


def test_ensure_pdf_pages_only_fills_requested_missing_pages(tmp_path: Path) -> None:
    (tmp_path / "source.pdf").write_bytes(_sample_pdf())

    ensure_pdf_pages(tmp_path, [2])

    assert not (tmp_path / "page-0001.pdf").exists()
    second = fitz.open(tmp_path / "page-0002.pdf")
    try:
        assert second.page_count == 1
    finally:
        second.close()

