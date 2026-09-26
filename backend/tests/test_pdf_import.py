from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
from PIL import Image

from backend.importers import ensure_pdf_pages, import_document, prepare_pdf_page


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


def test_pdf_import_defers_assets_until_selected_page_is_prepared(tmp_path: Path) -> None:
    source_data = _sample_pdf()
    pages = import_document(source_data, "sample.pdf", tmp_path)

    assert pages == [(1, 0, 0, "page-0001.png"), (2, 0, 0, "page-0002.png")]
    assert (tmp_path / "source.pdf").read_bytes() == source_data
    assert {path.name for path in tmp_path.iterdir()} == {"source.pdf"}

    number, width, height, image_name = prepare_pdf_page(tmp_path, 1)

    assert number == 1
    assert image_name == "page-0001.png"
    assert not (tmp_path / "page-0002.pdf").exists()
    assert not (tmp_path / "page-0002.png").exists()
    source = fitz.open(tmp_path / "source.pdf")
    try:
        assert source.page_count == 2
        source_page = source[0]
        split = fitz.open(tmp_path / "page-0001.pdf")
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

