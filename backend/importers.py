from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image, ImageOps


MAX_IMAGE_PIXELS = 40_000_000
MAX_RENDER_PIXELS = 20_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImportFailure(ValueError):
    pass


def import_document(data: bytes, filename: str, book_dir: Path) -> list[tuple[int, int, int, str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _import_pdf(data, book_dir)
    if suffix in {".png", ".jpg", ".jpeg"}:
        return _import_image(data, suffix, book_dir)
    raise ImportFailure("仅支持 PDF、PNG 和 JPEG 文件")


def _import_pdf(data: bytes, book_dir: Path) -> list[tuple[int, int, int, str]]:
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ImportFailure("无法读取 PDF 文件") from exc
    try:
        if document.needs_pass:
            raise ImportFailure("暂不支持加密 PDF")
        if document.page_count < 1:
            raise ImportFailure("PDF 没有页面")
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / "source.pdf").write_bytes(data)
        return [
            (number, 0, 0, f"page-{number:04d}.png")
            for number in range(1, document.page_count + 1)
        ]
    finally:
        document.close()


def _save_single_pdf_page(document: fitz.Document, index: int, target: Path) -> None:
    """Save one source page as a one-page PDF without changing its page geometry."""
    single_page_document = fitz.open()
    try:
        single_page_document.insert_pdf(document, from_page=index, to_page=index)
        single_page_document.save(target)
    except Exception as exc:
        raise ImportFailure("无法保存 PDF 单页文件") from exc
    finally:
        single_page_document.close()


def ensure_pdf_pages(book_dir: Path, page_numbers: list[int]) -> None:
    """Create missing single-page PDFs for an already imported source PDF.

    Only the requested missing pages are created; existing files and rendered
    images are left untouched, including assets from earlier imports.
    """
    requested = sorted(set(page_numbers))
    if not requested:
        return
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 1
        for number in requested
    ):
        raise ImportFailure("PDF 页面编号无效")

    source_path = book_dir / "source.pdf"
    if not source_path.is_file():
        raise ImportFailure("源 PDF 不存在")
    try:
        document = fitz.open(source_path)
    except Exception as exc:
        raise ImportFailure("无法读取源 PDF 文件") from exc
    try:
        if document.needs_pass:
            raise ImportFailure("暂不支持加密 PDF")
        if document.page_count < 1 or any(
            number > document.page_count for number in requested
        ):
            raise ImportFailure("PDF 页面编号超出范围")
        for number in requested:
            target = book_dir / f"page-{number:04d}.pdf"
            if not target.is_file():
                _save_single_pdf_page(document, number - 1, target)
    finally:
        document.close()


def prepare_pdf_page(book_dir: Path, number: int) -> tuple[int, int, int, str]:
    """Prepare and reuse assets for one selected source PDF page."""
    ensure_pdf_pages(book_dir, [number])
    image_name = f"page-{number:04d}.png"
    image_path = book_dir / image_name
    try:
        if image_path.is_file():
            with Image.open(image_path) as image:
                width, height = image.size
            return number, width, height, image_name

        # Render the saved single-page PDF to preserve its crop and rotation.
        with fitz.open(book_dir / f"page-{number:04d}.pdf") as document:
            if document.page_count != 1:
                raise ImportFailure("拆分后的 PDF 页面数量无效")
            page = document[0]
            base_width, base_height = page.rect.width, page.rect.height
            if base_width <= 0 or base_height <= 0:
                raise ImportFailure("PDF 页面尺寸无效")
            scale = 2.0
            pixels = base_width * base_height * scale * scale
            if pixels > MAX_RENDER_PIXELS:
                scale = math.sqrt(MAX_RENDER_PIXELS / (base_width * base_height))
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False
            )
        if pixmap.width * pixmap.height > MAX_RENDER_PIXELS + 10_000:
            raise ImportFailure("PDF 页面尺寸过大")
        pixmap.save(image_path)
        return number, pixmap.width, pixmap.height, image_name
    except ImportFailure:
        raise
    except Exception as exc:
        raise ImportFailure("无法渲染 PDF 页面") from exc


def _import_image(
    data: bytes, suffix: str, book_dir: Path
) -> list[tuple[int, int, int, str]]:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ImportFailure("图片像素尺寸过大")
            if image.mode in {"RGBA", "LA"}:
                rgba = image.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                image = rgb
            else:
                image = image.convert("RGB")
            (book_dir / f"source{suffix}").write_bytes(data)
            image_name = "page-0001.png"
            image.save(book_dir / image_name, format="PNG", optimize=True)
            return [(1, width, height, image_name)]
    except ImportFailure:
        raise
    except Exception as exc:
        raise ImportFailure("无法读取图片文件") from exc


def prepare_source_page(book_dir: Path, record: dict[str, Any]) -> tuple[int, int, str]:
    """Resolve a stable project page to its original file and local source page."""
    if record["source_kind"] == "pdf":
        _, width, height, name = prepare_pdf_page(
            book_dir / record["source_directory"], record["source_page"]
        )
        image_name = str(Path(record["source_directory"]) / name).replace("\\", "/")
        return width, height, image_name
    return record["width"], record["height"], record["image_name"]


def prepare_source_preview(book_dir: Path, record: dict[str, Any]) -> Path:
    """Cache a small preview of only the requested page, without preparing OCR assets."""
    directory = book_dir / record["source_directory"]
    target = directory / f'preview-{record["source_page"]:04d}.png'
    if target.is_file():
        return target
    try:
        if record["source_kind"] == "pdf":
            with fitz.open(directory / "source.pdf") as document:
                page = document[record["source_page"] - 1]
                width, height = page.rect.width, page.rect.height
                if width <= 0 or height <= 0:
                    raise ImportFailure("PDF 页面尺寸无效")
                scale = min(1.5, 1200 / max(width, height))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                pixmap.save(target)
        else:
            with Image.open(book_dir / record["image_name"]) as image:
                image.thumbnail((1200, 1200))
                image.save(target, format="PNG")
        return target
    except ImportFailure:
        raise
    except Exception as exc:
        raise ImportFailure("无法预览源页面") from exc


def create_source_assets(
    page_path: Path,
    book_id: str,
    page_number: int,
    blocks: list[dict],
    visual_kinds: dict[str, str] | None = None,
) -> list[dict]:
    visual_kinds = visual_kinds or {}
    targets = [
        block
        for block in blocks
        if block.get("bbox")
        and (
            visual_kinds.get(block["id"], block.get("type")) in {"figure", "equation"}
            or (
                visual_kinds.get(block["id"], block.get("type")) == "table"
                and not block.get("rows")
            )
        )
    ]
    if not targets:
        return blocks
    with Image.open(page_path) as image:
        width, height = image.size
        for block in targets:
            x0, y0, x1, y1 = block["bbox"]
            box = (
                max(0, int(x0 * width)),
                max(0, int(y0 * height)),
                min(width, int(math.ceil(x1 * width))),
                min(height, int(math.ceil(y1 * height))),
            )
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                continue
            safe_id = "".join(char if char.isalnum() or char in "_.-" else "_" for char in block["id"])
            name = f"asset-{page_number:04d}-{safe_id}.png"
            image.crop(box).save(page_path.parent / name, format="PNG", optimize=True)
            block["asset_url"] = f"/api/books/{book_id}/assets/{name}"
    return blocks
