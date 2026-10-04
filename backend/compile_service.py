"""Persist independent source-page compilations and assemble their output map."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from .fidelity_rendering import (
    GENERATOR_VERSION, MEASUREMENT_VERSION, fidelity_items, source_preserved_line_ids,
)
from .latex_diagnostics import (
    DIAGNOSTICS_VERSION, NaturalMeasurement, diagnose_document, diagnostics_quality_status,
    local_geometry_adjustment, measured_item_bbox, read_measurements,
)
from .latex_export import (
    LatexCompileError, LatexDocument, _xelatex_executable, build_latex_documents,
    compile_pdf, copy_document_resources,
)
from .layout_contract import PageSourceMetadata, SourceFidelityLayout
from .models import (
    Book, BookDetail, ExportManifest, LayoutSettings, Page, PageMapEntry,
    PdfCompileResult, RenderDiagnostic, Revision, Usage,
)
from .importers import IMAGE_LOCK, prepare_page
from .latex_content import source_resource_name
from .source_analysis import persist_source_region


OUTPUT_CHECKS_VERSION = "pdf-glyph-ranges-v2"
MANIFEST_OUTPUT_VERSION = "manifest-output-v2"
_FANDOL = ("FandolSong-Regular.otf", "FandolSong-Bold.otf", "FandolHei-Regular.otf",
           "FandolHei-Bold.otf", "FandolKai-Regular.otf", "FandolFang-Regular.otf")
_TEX_FONTS = _FANDOL + ("lmroman10-regular.otf", "latinmodern-math.otf",
                        "cmr10.pfb", "cmmi10.pfb", "cmsy10.pfb", "cmex10.pfb", "rsfs10.pfb", "rsfs10.tfm")


@dataclass(frozen=True)
class CandidateRenderResult:
    pdf_path: Path | None = None
    png_path: Path | None = None
    diagnostics: list[RenderDiagnostic] = field(default_factory=list)
    measurements: dict[str, NaturalMeasurement] = field(default_factory=dict)
    document: LatexDocument | None = None
    layout: SourceFidelityLayout | None = None
    source_disposition: str = "transcribed"
    error: str | None = None


def same_source_identity(left: PageSourceMetadata, right: PageSourceMetadata) -> bool:
    """Program-owned identity and transforms survive the retired fingerprint fields."""
    fields = (
        "book_id", "page_id", "source_id", "source_file_id", "source_page", "source_version",
        "source_kind", "canonical_width_px", "canonical_height_px", "source_width_px",
        "source_height_px", "source_coordinate_space", "canonical_to_source_affine", "pdf_geometry",
    )
    return all(getattr(left, name) == getattr(right, name) for name in fields)


def _revision_page(revision: Revision) -> Page:
    metadata = revision.source_metadata or (revision.layout_source.source if revision.layout_source else None)
    return Page(
        number=revision.page_number, page_id=revision.page_id, source_version=revision.source_version,
        current_revision_id=revision.revision_id, source_id=metadata.source_id if metadata else "legacy",
        source_page=metadata.source_page if metadata else 1, status="ready", error=None, text=revision.text,
        render_strategy=revision.render_strategy, content_revision=revision.content_revision,
        layout_revision=revision.layout_revision, generated_content_revision=revision.generated_content_revision,
        layout_source=revision.layout_source, source_metadata=metadata, page_kind=revision.page_kind,
        page_side=revision.page_side, cover_fields=revision.cover_fields,
        header_segments=revision.header_segments, footer_segments=revision.footer_segments,
        usage=Usage(input_tokens=None, output_tokens=None, total_tokens=None, complete=False), attempts=0,
    )


def _safe_segment(value: str) -> str:
    if not value or value in {".", ".."} or any(character in value for character in '/\\:'):
        raise ValueError("渲染身份必须是有效的本地目录名称")
    return value


def _candidate_directory(book: Book, revision: Revision, book_dir: Path) -> Path:
    source = revision.source_metadata or (revision.layout_source.source if revision.layout_source else None)
    asset_id = source.source_file_id or source.source_id if source else "legacy"
    return (book_dir / "workflow-render" / _safe_segment(asset_id) / _safe_segment(revision.page_id)
            / f"v{revision.source_version}" / _safe_segment(revision.revision_id)
            / f"settings-{book.output_settings_version}" / GENERATOR_VERSION)


def _candidate_identity(book: Book, revision: Revision) -> dict:
    source = revision.source_metadata or (revision.layout_source.source if revision.layout_source else None)
    return {
        "source": source.model_dump(mode="json", exclude={"source_file_fingerprint", "image_fingerprint"}) if source else None,
        "source_version": revision.source_version, "revision_id": revision.revision_id,
        "assets": [asset.model_dump(mode="json") for asset in revision.layout_source.source_assets] if revision.layout_source else [],
        "output_settings_version": book.output_settings_version, "generator_version": GENERATOR_VERSION,
    }


def _cached_candidate(book: Book, revision: Revision, book_dir: Path) -> CandidateRenderResult | None:
    directory = _candidate_directory(book, revision, book_dir)
    path = directory / "result.json"
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["identity"] != _candidate_identity(book, revision):
        return None
    pdf = directory / "document.pdf" if record.get("pdf") else None
    png = directory / "page-0001.png" if record.get("png") else None
    if (pdf is not None and not pdf.is_file()) or (png is not None and not png.is_file()):
        return None
    document = record.get("document")
    if document is not None:
        document["resource_names"] = tuple(document.get("resource_names", ()))
    return CandidateRenderResult(
        pdf_path=pdf, png_path=png, diagnostics=[RenderDiagnostic.model_validate(item) for item in record["diagnostics"]],
        measurements={key: NaturalMeasurement(**value) for key, value in record.get("measurements", {}).items()},
        document=LatexDocument(**document) if document else None, layout=revision.layout_source,
        source_disposition=record["source_disposition"], error=record.get("error"),
    )


def load_candidate_render(book: Book, revision: Revision, book_dir: Path) -> CandidateRenderResult | None:
    """Resume a settled candidate by immutable revision and frozen output settings."""
    return _cached_candidate(book, revision, book_dir)


async def cache_candidate_render(
    book: Book, revision: Revision, book_dir: Path, *, result: CandidateRenderResult,
) -> CandidateRenderResult:
    """Bind unchanged rendered content to its final saved revision without compiling."""
    if result.pdf_path is None:
        _record_candidate(book, revision, book_dir, result)
        return result
    document = build_latex_documents(BookDetail(book=book, pages=[_revision_page(revision)]))[0]
    if result.document is None or document.source != result.document.source:
        raise ValueError("新修订的输出内容已变化，不能复用旧候选 PDF")
    directory = _candidate_directory(book, revision, book_dir)
    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / "document.pdf"
    if result.pdf_path.resolve() != pdf.resolve():
        shutil.copyfile(result.pdf_path, pdf)
    png = directory / "page-0001.png" if result.png_path else None
    if png is not None and result.png_path.resolve() != png.resolve():
        shutil.copyfile(result.png_path, png)
    copy_document_resources(document, book_dir, directory)
    (directory / "document.tex").write_text(document.source, encoding="utf-8", newline="")
    cached = replace(result, pdf_path=pdf, png_path=png, document=document, layout=revision.layout_source)
    _record_candidate(book, revision, book_dir, cached)
    return cached


def _record_candidate(book: Book, revision: Revision, book_dir: Path, result: CandidateRenderResult) -> None:
    directory = _candidate_directory(book, revision, book_dir)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "identity": _candidate_identity(book, revision), "pdf": result.pdf_path is not None,
        "png": result.png_path is not None, "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
        "measurements": {key: asdict(value) for key, value in result.measurements.items()},
        "document": asdict(result.document) if result.document else None,
        "source_disposition": result.source_disposition, "error": result.error,
    }
    (directory / "result.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _candidate_failure(book: Book, revision: Revision, error: Exception, *, code: str = "COMPILE_FAILED") -> CandidateRenderResult:
    page = _revision_page(revision)
    return CandidateRenderResult(
        diagnostics=[diagnostic(page, book.id, 1, getattr(error, "code", None) or code,
                                str(error), coverage="none", line_id=getattr(error, "line_id", None))],
        layout=revision.layout_source,
        source_disposition=revision.layout_source.source_disposition if revision.layout_source else "transcribed",
        error=str(error),
    )


async def render_candidate(
    book: Book, revision: Revision, book_dir: Path, *, run_id: str, page_id: str, compile_index: int,
) -> CandidateRenderResult:
    """Render one saved revision after the workflow reserves one of four compilations."""
    if page_id != revision.page_id or revision.book_id != book.id or not 1 <= compile_index <= 4:
        raise ValueError("候选渲染身份或编译序号不符合运行快照")
    cached = _cached_candidate(book, revision, book_dir)
    if cached is not None:
        return cached
    directory = _candidate_directory(book, revision, book_dir)
    page = _revision_page(revision)
    try:
        document = build_latex_documents(BookDetail(book=book, pages=[page]))[0]
        copy_document_resources(document, book_dir, directory)
        (directory / "page-0001.png").unlink(missing_ok=True)
        pdf = await compile_pdf(document.source, directory)
        diagnostics, sizes = await asyncio.to_thread(inspect_pdf, pdf, page, document, book.id)
        diagnostics.extend(diagnose_document(document, page, directory, book_id=book.id,
                                            arrangement_position=1, output_page_start=1, output_page_end=len(sizes)))
        if page.layout_source and page.source_metadata and not same_source_identity(page.layout_source.source, page.source_metadata):
            diagnostics.append(diagnostic(page, book.id, 1, "LAYOUT_SOURCE_MISMATCH", "候选布局与源资产版本或坐标变换不一致。"))
        measurements = {}
        metrics = directory / "compiled.ebook-metrics.csv"
        if metrics.is_file():
            try:
                measurements = read_measurements(metrics)
            except ValueError:
                pass  # diagnose_document already records the unusable measurements.
        png = await asyncio.to_thread(render_output_page, pdf, 1) if sizes else None
        if not sizes:
            raise LatexCompileError("候选 PDF 没有输出页面", code="UNEXPECTED_PAGE_COUNT")
        result = CandidateRenderResult(pdf, png, diagnostics, measurements, document, page.layout_source,
                                       page.layout_source.source_disposition if page.layout_source else "transcribed")
    except (LatexCompileError, OSError, ValueError, fitz.FileDataError) as error:
        result = _candidate_failure(book, revision, error)
    _record_candidate(book, revision, book_dir, result)
    return result


def _write_preserved_pdf(document: LatexDocument, layout: SourceFidelityLayout, book_dir: Path,
                         target: Path, base: Path | None = None,
                         clear_boxes: list[tuple[float, float, float, float]] | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name("preserved.pdf")
    with IMAGE_LOCK, (fitz.open(base) if base else fitz.open()) as pdf:
        if base is None:
            pdf.new_page(width=document.output_width_bp, height=document.output_height_bp)
        elif len(pdf) != 1:
            raise ValueError("源区域替代要求最佳候选只有一张输出页")
        output = pdf[0]
        for box in clear_boxes or []:
            output.add_redact_annot(fitz.Rect(box), fill=(1, 1, 1))
        if clear_boxes:
            # Formula rules and other painted paths must disappear with the replaced line.
            output.apply_redactions(images=0, graphics=2)
        a, _, _, d, x, y = document.source_to_output_affine
        for asset in layout.source_assets:
            left, top, right, bottom = asset.bbox
            rect = fitz.Rect(left * a + x, top * d + y, right * a + x, bottom * d + y)
            output.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)
            output.insert_image(rect, filename=str(book_dir / asset.image_name), keep_proportion=False, overlay=True)
        pdf.save(pending)
    pending.replace(target)
    (target.parent / "page-0001.png").unlink(missing_ok=True)


def _region_clear_boxes(layout: SourceFidelityLayout, best: CandidateRenderResult) -> list[tuple[float, float, float, float]]:
    preserved = source_preserved_line_ids(layout)
    if not preserved:
        return []
    if best.document is None or best.layout is None or best.document.source_to_output_affine is None:
        raise ValueError("缺少最佳候选的自然尺寸映射，无法安全替换源区域")
    old_lines = {line.line_id: line for line in best.layout.lines}
    for line in layout.lines:
        if line.line_id not in preserved and (line.line_id not in old_lines or
                line.model_dump(exclude={"basis"}) != old_lines[line.line_id].model_dump(exclude={"basis"})):
            raise ValueError("源区域以外的候选原行已经变化，无法复用最佳 PDF")
    a, _, _, d, x, y = best.document.source_to_output_affine
    scale = best.document.canvas_scale
    width, height = a / scale, d / scale
    affected, unchanged = [], []
    for item in fidelity_items(best.layout, width, height):
        measured = best.measurements.get(item.token)
        if measured is None:
            raise ValueError("缺少候选原行自然尺寸，无法安全清除被替代的文字")
        box = measured_item_bbox(item, measured)
        transformed = tuple(value * scale + (x if index % 2 == 0 else y) for index, value in enumerate(box))
        (affected if item.line.line_id in preserved else unchanged).append(transformed)
    for box in affected:
        if any(min(box[2], other[2]) > max(box[0], other[0])
               and min(box[3], other[3]) > max(box[1], other[1]) for other in unchanged):
            raise ValueError("候选替换范围与其它原行碰撞，必须保留整张源页以避免裁掉无关文字")
        for asset in best.layout.source_assets:
            if asset.purpose != "figure":
                continue
            rect = (asset.bbox[0] * a + x, asset.bbox[1] * d + y,
                    asset.bbox[2] * a + x, asset.bbox[3] * d + y)
            if min(box[2], rect[2]) > max(box[0], rect[0]) and min(box[3], rect[3]) > max(box[1], rect[1]):
                if not any(item.asset_id == asset.asset_id for item in layout.source_assets):
                    raise ValueError("候选清除范围涉及未保留的原图形，必须保留源页")
    return affected


async def preserve_source_regions(
    book: Book, revision: Revision, book_dir: Path, *, best_candidate: CandidateRenderResult,
) -> CandidateRenderResult:
    """Overlay recorded source regions on the best PDF without another TeX invocation."""
    layout = revision.layout_source
    if layout is None or not layout.source_assets or best_candidate.pdf_path is None:
        return _candidate_failure(book, revision, ValueError("缺少可保留的源区域或已生成候选 PDF"), code="SOURCE_PRESERVATION_FAILED")
    cached = _cached_candidate(book, revision, book_dir)
    if cached is not None and cached.pdf_path is not None and cached.source_disposition == layout.source_disposition:
        return cached
    directory = _candidate_directory(book, revision, book_dir)
    page = _revision_page(revision)
    try:
        document = build_latex_documents(BookDetail(book=book, pages=[page]))[0]
        if best_candidate.document is None or document.source_to_output_affine != best_candidate.document.source_to_output_affine:
            raise ValueError("源区域保留与最佳候选的输出画布不一致")
        copy_document_resources(document, book_dir, directory)
        (directory / "document.tex").write_text(document.source, encoding="utf-8", newline="")
        pdf = directory / "document.pdf"
        clear_boxes = _region_clear_boxes(layout, best_candidate)
        await asyncio.to_thread(_write_preserved_pdf, document, layout, book_dir, pdf, best_candidate.pdf_path, clear_boxes)
        diagnostics, sizes = await asyncio.to_thread(inspect_pdf, pdf, page, document, book.id)
        diagnostics.extend(_preservation_diagnostics(book, page, layout, len(sizes)))
        # Keep unresolved output evidence, but do not carry a resolved compiler failure.
        hidden = source_preserved_line_ids(layout)
        diagnostics.extend(item for item in best_candidate.diagnostics
                           if item.line_id not in hidden and item.code not in {
                               "COMPILE_FAILED", "SOURCE_CONTENT_REVIEW", "SOURCE_REGION_PRESERVED",
                           })
        png = await asyncio.to_thread(render_output_page, pdf, 1)
        result = CandidateRenderResult(pdf, png, diagnostics, best_candidate.measurements, document, layout,
                                       layout.source_disposition)
    except (LatexCompileError, OSError, ValueError, fitz.FileDataError) as error:
        result = _candidate_failure(book, revision, error, code="SOURCE_PRESERVATION_FAILED")
    _record_candidate(book, revision, book_dir, result)
    return result


def _preservation_diagnostics(book: Book, page: Page, layout: SourceFidelityLayout, count: int) -> list[RenderDiagnostic]:
    return [diagnostic(
        page, book.id, 1, "SOURCE_PAGE_PRESERVED" if asset.purpose == "source_page" else "SOURCE_REGION_PRESERVED",
        asset.reason or "未完成可靠转录的内容已保留源图。", severity="warning", coverage="partial", basis="source_asset",
        start=1, end=count, block_id=asset.region_id, source_bbox=asset.bbox,
    ) for asset in layout.source_assets if asset.purpose != "figure"]


async def _preserve_source_page_in_directory(
    book: Book, revision: Revision, book_dir: Path, *, image_path: Path, metadata: PageSourceMetadata,
    reason: str, directory: Path,
) -> CandidateRenderResult:
    """Form a derived source-page output without changing the immutable revision cache."""
    try:
        # Reading the source image is required; an unreadable original yields an explicit failure.
        with IMAGE_LOCK, Image.open(image_path) as image:
            image.load()
        old = revision.layout_source
        if old and old.source_disposition == "source_page_preserved" and any(
            asset.purpose == "source_page" and asset.bbox == (0., 0., 1., 1.) for asset in old.source_assets
        ):
            layout = old
        else:
            asset = await asyncio.to_thread(persist_source_region, book_dir, image_path, metadata,
                                            (0., 0., 1., 1.), None, reason, purpose="source_page")
            layout = old.model_copy(update={
                "source": metadata, "source_assets": [asset], "source_disposition": "source_page_preserved",
                "disposition_reason": reason[:2_000],
            }) if old else SourceFidelityLayout(
                source=metadata, content_revision=revision.content_revision, layout_revision=revision.layout_revision,
                source_assets=[asset], source_disposition="source_page_preserved", disposition_reason=reason[:2_000],
            )
        preserved = revision.model_copy(update={"layout_source": layout, "source_metadata": metadata,
                                                 "render_strategy": "source_fidelity"})
        page = _revision_page(preserved)
        document = build_latex_documents(BookDetail(book=book, pages=[page]))[0]
        copy_document_resources(document, book_dir, directory)
        (directory / "document.tex").write_text(document.source, encoding="utf-8", newline="")
        pdf = directory / "document.pdf"
        await asyncio.to_thread(_write_preserved_pdf, document, layout, book_dir, pdf)
        diagnostics, sizes = await asyncio.to_thread(inspect_pdf, pdf, page, document, book.id)
        diagnostics.extend(_preservation_diagnostics(book, page, layout, len(sizes)))
        png = await asyncio.to_thread(render_output_page, pdf, 1)
        return CandidateRenderResult(pdf, png, diagnostics, {}, document, layout, "source_page_preserved")
    except (LatexCompileError, OSError, ValueError, fitz.FileDataError) as error:
        return _candidate_failure(book, revision, error, code="SOURCE_UNREADABLE")


async def preserve_source_page(
    book: Book, revision: Revision, book_dir: Path, *, image_path: Path, metadata: PageSourceMetadata,
    reason: str, run_id: str, page_id: str,
) -> CandidateRenderResult:
    """Retain the source; the workflow binds this output after saving its new revision."""
    if page_id != revision.page_id or metadata.source_version != revision.source_version:
        raise ValueError("源页保留身份与当前修订不一致")
    directory = _candidate_directory(book, revision, book_dir) / "source-preservation" / _safe_segment(run_id)
    return await _preserve_source_page_in_directory(
        book, revision, book_dir, image_path=image_path, metadata=metadata, reason=reason, directory=directory,
    )


def _manifest_revision(page: Page, manifest: ExportManifest) -> Revision:
    return Revision(
        revision_id=page.current_revision_id, parent_revision_id=None, book_id=manifest.book_id,
        page_id=page.page_id, page_number=page.number, source_version=page.source_version,
        content_revision=page.content_revision, layout_revision=page.layout_revision,
        origin="manual" if page.manual_protected else "automatic", run_id=manifest.run_id, text=page.text,
        render_strategy=page.render_strategy, layout_source=page.layout_source, source_metadata=page.source_metadata,
        page_kind=page.page_kind, page_side=page.page_side, cover_fields=page.cover_fields,
        header_segments=page.header_segments, footer_segments=page.footer_segments,
        generated_content_revision=page.generated_content_revision, generator_version=manifest.generator_version,
        created_at=manifest.created_at,
    )


def _manifest_source(book_dir: Path, entry, page: Page, book_id: str) -> tuple[Path, PageSourceMetadata]:
    metadata = page.source_metadata or (page.layout_source.source if page.layout_source else None)
    if page.layout_source and page.layout_source.source_disposition == "source_page_preserved":
        for asset in page.layout_source.source_assets:
            if asset.purpose == "source_page" and asset.bbox == (0., 0., 1., 1.):
                return book_dir / source_resource_name(asset.image_name), metadata
    image_name = source_resource_name(entry.image_name)
    if metadata is not None:
        return book_dir / image_name, metadata
    # Old protected revisions may predate source metadata. Prepare only this frozen source page.
    record = {
        "book_id": book_id, "page_id": entry.page_id, "number": entry.page_number,
        "source_id": entry.source_file_id, "source_file_id": entry.source_file_id,
        "source_version": entry.source_version, "source_page": entry.source_page,
        "source_filename": entry.source_filename,
        "source_kind": "pdf" if Path(entry.source_filename).suffix.lower() == ".pdf" else "image",
        "source_directory": Path(image_name).parent.as_posix(), "image_name": image_name,
        "width": 0, "height": 0,
    }
    _, _, prepared_name, metadata = prepare_page(book_dir, record)
    return book_dir / prepared_name, metadata


async def generate_manifest_outputs(
    book: Book, manifest: ExportManifest, pages: list[Page], book_dir: Path,
) -> dict[str, str]:
    """Produce PDF, resource ZIP and JSON from one frozen order and revision set."""
    if manifest.book_id != book.id or len(pages) != len(manifest.pages):
        raise ValueError("导出页面与冻结清单范围不一致")
    for page, entry in zip(pages, manifest.pages):
        if page.page_id != entry.page_id or page.current_revision_id != entry.revision_id:
            raise ValueError("导出页面顺序或修订与冻结清单不一致")
    settings = manifest.settings_snapshot
    book = book.model_copy(update={
        "paper_size": settings["paper_size"], "layout": LayoutSettings.model_validate(settings["layout"]),
        "render_strategy": settings.get("render_strategy", book.render_strategy),
        "output_settings_version": manifest.output_settings_version,
    })
    relative_dir = Path("exports") / _safe_segment(manifest.manifest_id)
    directory = book_dir / relative_dir
    directory.mkdir(parents=True, exist_ok=True)
    json_path, zip_path = directory / "result.json", directory / "latex.zip"
    if json_path.is_file() and zip_path.is_file():
        previous = json.loads(json_path.read_text(encoding="utf-8"))
        if (previous.get("manifest_id") == manifest.manifest_id and previous.get("generator_version") == GENERATOR_VERSION
                and previous.get("output_version") == MANIFEST_OUTPUT_VERSION
                and all((book_dir / value).is_file() for key, value in previous.get("outputs", {}).items()
                        if key in {"pdf", "partial_pdf", "latex", "json"})):
            return previous["outputs"]
    paths, documents, page_records, pdf_map = [], [], [], []
    missing_page_ids, missing_resources, errors = [], [], []
    output_start = 1
    for position, (page, entry) in enumerate(zip(pages, manifest.pages), 1):
        revision = _manifest_revision(page, manifest)
        output_issues = []
        fallback_reason = None
        try:
            result = load_candidate_render(book, revision, book_dir)
        except (OSError, ValueError, KeyError, TypeError) as error:
            result = _candidate_failure(book, revision, error, code="OUTPUT_UNREADABLE")
        if result is None:
            if manifest.run_id is not None and not page.manual_protected:
                result = _candidate_failure(book, revision, ValueError("本轮候选产物未生成或不可读取"), code="OUTPUT_MISSING")
            else:
                # Exporting an old/manual manuscript preserves its full source and is not an OCR candidate retry.
                result = await render_candidate(book, revision, book_dir, run_id=manifest.run_id or "export",
                                                page_id=page.page_id, compile_index=1)
        if result.pdf_path is not None:
            try:
                with IMAGE_LOCK, fitz.open(result.pdf_path) as pdf:
                    if not len(pdf):
                        raise ValueError("已结算候选 PDF 没有输出页")
                if result.document is None:
                    raise ValueError("已结算候选缺少与 PDF 同修订的源码记录")
            except (OSError, ValueError, fitz.FileDataError) as error:
                result = _candidate_failure(book, revision, error, code="OUTPUT_UNREADABLE")
        if result.pdf_path is None:
            fallback_reason = result.error or "输出候选不可读取"
            errors.append({"page_id": page.page_id, "reason": fallback_reason})
            output_issues.append({
                "page_id": page.page_id, "revision_id": entry.revision_id, "category": "output_fallback",
                "reason": fallback_reason, "disposition": "source_page_preserved",
            })
            try:
                image_path, metadata = await asyncio.to_thread(_manifest_source, book_dir, entry, page, book.id)
                result = await _preserve_source_page_in_directory(
                    book, revision, book_dir, image_path=image_path, metadata=metadata,
                    reason=fallback_reason, directory=directory / "pages" / f"{position:04d}",
                )
            except (LatexCompileError, OSError, ValueError, fitz.FileDataError) as error:
                result = _candidate_failure(book, revision, error, code="SOURCE_UNREADABLE")
        if result.pdf_path is None:
            output_status = "failed"
            if output_issues:
                output_issues[0]["disposition"] = "failed"
        else:
            has_issues = (fallback_reason is not None or result.source_disposition != "transcribed"
                          or entry.result_status != "auto_passed" or any(
                              item.severity in {"warning", "error"} or item.coverage != "complete"
                              for item in result.diagnostics))
            output_status = "completed_with_issues" if has_issues else "auto_passed"
        output_issues.extend({
            "page_id": page.page_id, "revision_id": entry.revision_id, "category": item.code,
            "reason": item.message, "disposition": result.source_disposition if result.pdf_path else "failed",
        } for item in result.diagnostics if item.severity in {"warning", "error"} or item.coverage != "complete")
        record = {
            "position": position, "page_id": page.page_id, "revision_id": entry.revision_id,
            "source_file_id": entry.source_file_id, "source_page": entry.source_page,
            "source_version": entry.source_version, "revision_result_status": entry.result_status,
            "result_status": output_status, "output_status": output_status,
            "output_scope": "manifest", "derived_output": fallback_reason is not None,
            "assessment": entry.assessment.model_dump(mode="json") if entry.assessment else None,
            "assessment_scope": "revision",
            "source_disposition": result.source_disposition,
            "transcription_complete": output_status == "auto_passed" and result.source_disposition == "transcribed",
            "page": page.model_dump(mode="json"),
            "rendered_layout": result.layout.model_dump(mode="json") if result.layout else None,
            "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
            "output_issues": output_issues,
            "error": "；".join(dict.fromkeys(reason for reason in (fallback_reason, result.error) if reason)) or None,
        }
        if result.pdf_path is None:
            missing_page_ids.append(page.page_id)
            errors.append({"page_id": page.page_id, "reason": result.error or "该源页无法生成且无法读取"})
            documents.append(None)
        else:
            with IMAGE_LOCK, fitz.open(result.pdf_path) as pdf:
                count = len(pdf)
            paths.append(result.pdf_path)
            mapping = {"page_id": page.page_id, "revision_id": entry.revision_id, "position": position,
                       "output_page_start": output_start, "output_page_end": output_start + count - 1,
                       "source_disposition": result.source_disposition}
            pdf_map.append(mapping)
            record["pdf_map"] = mapping
            output_start += count
            documents.append(result.document)
        page_records.append(record)
    outputs = {"latex": (relative_dir / "latex.zip").as_posix(), "json": (relative_dir / "result.json").as_posix()}
    if paths:
        filename = "partial.pdf" if missing_page_ids else "document.pdf"
        try:
            await asyncio.to_thread(_merge_pdfs, paths, directory / filename)
            outputs["partial_pdf" if missing_page_ids else "pdf"] = (relative_dir / filename).as_posix()
        except (OSError, ValueError, fitz.FileDataError) as error:
            errors.append({"page_id": None, "reason": f"PDF 合并失败：{error}"})
    if missing_page_ids:
        (directory / "document.pdf").unlink(missing_ok=True)
    instructions = [
        "本包与 PDF、result.json 使用相同冻结页序和修订。",
        "在解压目录运行 XeLaTeX 编译各个编号 .tex，再按以下顺序合并 PDF。",
        "source-assets 等资源保持书目录相对路径；已保留源图的区域不计为成功文字化。",
        "导出时的源页兜底是原修订的派生处置，详见 result.json 的 output_status、derived_output 和 output_issues。",
        "问题说明仅见 result.json；正文没有附加水印或猜测文字。", "", "文件\t页ID\t修订ID\t处置",
    ]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        written = set()
        for position, (page, entry, document, record) in enumerate(zip(pages, manifest.pages, documents, page_records), 1):
            if document is None:
                instructions.append(f"缺失第{position}页\t{page.page_id}\t{entry.revision_id}\t{record['error']}")
                if page.text:
                    package.writestr(f"best-candidates/{position:04d}.tex", page.text)
                continue
            filename = f"{position:04d}.tex"
            package.writestr(filename, document.source)
            instructions.append(f"{filename}\t{page.page_id}\t{entry.revision_id}\t{record['source_disposition']}")
            if record["source_disposition"] != "transcribed" and page.text:
                package.writestr(f"best-candidates/{position:04d}.tex", page.text)
            for name in document.resource_names:
                relative = source_resource_name(name)
                if relative in written:
                    continue
                resource = (book_dir / relative).resolve()
                if not resource.is_relative_to(book_dir.resolve()) or not resource.is_file():
                    missing_resources.append(relative)
                    reason = f"源码资源缺失：{relative}"
                    errors.append({"page_id": page.page_id, "reason": reason})
                    record["output_issues"].append({
                        "page_id": page.page_id, "revision_id": entry.revision_id, "category": "source_resource_missing",
                        "reason": reason, "disposition": "incomplete_resource_package",
                    })
                    record["output_status"] = record["result_status"] = "completed_with_issues"
                    record["transcription_complete"] = False
                    continue
                package.write(resource, arcname=relative)
                written.add(relative)
        complete = manifest.complete and not missing_page_ids and not missing_resources and "pdf" in outputs
        outputs["complete"] = "true" if complete else "false"
        outputs["quality"] = ("degraded" if not complete else "completed_with_issues" if (
            errors or manifest.issues or any(record["output_status"] != "auto_passed" for record in page_records)
        ) else "auto_passed")
        explanations = [item["reason"] for item in errors]
        explanations.extend(issue["reason"] for record in page_records for issue in record["output_issues"]
                            if issue["category"] in {"SOURCE_PAGE_PRESERVED", "SOURCE_REGION_PRESERVED"})
        outputs["error"] = "；".join(dict.fromkeys(explanations))
        outputs["missing_pages"] = ", ".join(str(index) for index, entry in enumerate(manifest.pages, 1)
                                              if entry.page_id in missing_page_ids)
        output_manifest = manifest.model_copy(update={"outputs": outputs})
        payload = {
            "schema_version": 1, "manifest_id": manifest.manifest_id,
            "manifest": output_manifest.model_dump(mode="json"), "generator_version": GENERATOR_VERSION,
            "output_version": MANIFEST_OUTPUT_VERSION, "quality": outputs["quality"],
            "complete": complete, "missing_page_ids": missing_page_ids,
            "missing_resource_names": list(dict.fromkeys(missing_resources)), "errors": errors,
            "output_issues": [issue for record in page_records for issue in record["output_issues"]],
            "pages": page_records, "pdf_map": pdf_map, "outputs": outputs,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        package.writestr("manifest.json", output_manifest.model_dump_json(indent=2))
        package.writestr("result.json", json.dumps(payload, ensure_ascii=False, indent=2))
        package.writestr("README.txt", "\n".join(instructions) + "\n")
    return outputs


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


@lru_cache(maxsize=2048)
def _file_digest(path: str, mtime_ns: int, size: int) -> str:
    with Path(path).open("rb") as stream:
        import hashlib
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_digest(path: Path) -> str:
    stat = path.stat()
    return _file_digest(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def _tool_output(arguments: list[str]) -> str:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run(arguments, capture_output=True, text=True, errors="replace",
                            timeout=30, **options)
    return result.stdout.strip()


@lru_cache(maxsize=4)
def _engine_identity(executable: str, mtime_ns: int, size: int) -> tuple[str, list[str]]:
    kpsewhich = shutil.which(str(Path(executable).with_name("kpsewhich.exe" if os.name == "nt" else "kpsewhich")))
    paths = [_tool_output([kpsewhich, name]) for name in _TEX_FONTS] if kpsewhich else []
    return _tool_output([executable, "--version"]), paths


def compiler_identity() -> dict:
    """Fingerprint the active engine and template fonts once per engine revision."""
    executable = _xelatex_executable()
    stat = Path(executable).stat()
    version, paths = _engine_identity(executable, stat.st_mtime_ns, stat.st_size)
    fonts = {}
    font_metadata = {}
    for name, resolved in zip(_TEX_FONTS, paths):
        fonts[name] = file_digest(Path(resolved)) if resolved else None
        if resolved:
            directory = Path(resolved).parent
            if str(directory) not in font_metadata:
                font_metadata[str(directory)] = [(path.name, path.stat().st_mtime_ns, path.stat().st_size)
                    for path in sorted(directory.iterdir()) if path.suffix.lower() in {".otf", ".pfb", ".tfm"}]
    return {"executable": executable, "engine": version,
            "engine_file": file_digest(Path(executable)), "fonts": fonts,
            "tex_font_metadata": font_metadata,
            "pdf_inspector": fitz.VersionBind}


def diagnostic(page: Page, book_id: str, position: int, code: str, message: str,
               *, severity: str = "error", coverage: str = "complete",
               basis: str = OUTPUT_CHECKS_VERSION, start: int | None = None,
               end: int | None = None, **location) -> RenderDiagnostic:
    return RenderDiagnostic(
        code=code, severity=severity, message=message,
        suggestion="系统根据实际输出诊断执行有限修复；不能可靠生成时保留源内容并记录原因。",
        basis=basis, book_id=book_id, page_number=page.number,
        source_id=page.source_id, source_page=page.source_page,
        arrangement_position=position, output_page_start=start, output_page_end=end,
        content_revision=page.content_revision, layout_revision=page.layout_revision,
        coverage=coverage, **location,
    )


def failed_result(detail: BookDetail, error: LatexCompileError, positions: list[int]) -> PdfCompileResult:
    index = next((index for index, page in enumerate(detail.pages)
                  if page.number == error.page_number), 0)
    page = detail.pages[index]
    diagnostics = [diagnostic(page, detail.book.id, positions[index], "COMPILE_FAILED",
                              str(error), basis="compiler", coverage="none", line_id=error.line_id)]
    if error.code and error.code != "COMPILE_FAILED":
        diagnostics.append(diagnostic(page, detail.book.id, positions[index], error.code,
                                      str(error), basis="generator", coverage="none", line_id=error.line_id))
    return PdfCompileResult(pdf_url=None, warnings=[str(error)], diagnostics=diagnostics,
                            quality_status="compile_failed", generator_version=GENERATOR_VERSION,
                            diagnostics_version=DIAGNOSTICS_VERSION)


def inspect_pdf(path: Path, page: Page, document: LatexDocument, book_id: str) -> tuple[list[RenderDiagnostic], list[tuple[float, float]]]:
    """Texttrace reads unclipped glyph boxes, including text outside the page crop."""
    diagnostics = []
    sizes = []
    with IMAGE_LOCK, fitz.open(path) as pdf:
        count = len(pdf)
        if document.render_strategy != "custom_latex" and count != 1:
            diagnostics.append(diagnostic(page, book_id, 1, "UNEXPECTED_PAGE_COUNT",
                f"此源页实际输出 {count} 张，当前策略要求一源页一张。", start=1, end=count))
        for number, output in enumerate(pdf, 1):
            rect = output.rect
            sizes.append((rect.width, rect.height))
            if (document.output_width_bp is not None and document.output_height_bp is not None
                    and (abs(rect.width - document.output_width_bp) > 0.5
                         or abs(rect.height - document.output_height_bp) > 0.5)):
                diagnostics.append(diagnostic(page, book_id, 1, "OUTPUT_GEOMETRY_MISMATCH",
                    f"PDF 第 {number} 张的实际纸张尺寸与本次画布映射不同。", start=number, end=number))
            outside = []
            missing = 0
            unmapped = 0
            for span in output.get_texttrace():
                if span["type"] == 3:  # Explicitly invisible text paints no glyphs.
                    continue
                for unicode, glyph, _origin, box in span["chars"]:
                    if chr(unicode).isspace():
                        continue
                    if unicode == 0 or glyph == 0:
                        missing += 1
                    elif unicode == 0xfffd:
                        unmapped += 1
                    box = tuple(fitz.Rect(box) * output.rotation_matrix)
                    overflow = max(rect.x0 - box[0], rect.y0 - box[1],
                                   box[2] - rect.x1, box[3] - rect.y1, 0)
                    if overflow > 0.5:
                        outside.append((box, overflow))
            if outside:
                box = (min(item[0][0] for item in outside), min(item[0][1] for item in outside),
                       max(item[0][2] for item in outside), max(item[0][3] for item in outside))
                diagnostics.append(diagnostic(page, book_id, 1, "CONTENT_OUTSIDE_PAGE",
                    f"PDF 第 {number} 张有 {len(outside)} 个字形伸出纸张，固定行盒即使没有 Overfull 也会被裁切。",
                    start=number, end=number, output_bbox_bp=box,
                    overflow_bp=max(item[1] for item in outside)))
            if missing:
                diagnostics.append(diagnostic(page, book_id, 1, "MISSING_GLYPH",
                    f"PDF 第 {number} 张存在缺失字形（glyph 0 或 Unicode NUL）。",
                    start=number, end=number, coverage="partial"))
            if unmapped:
                diagnostics.append(diagnostic(page, book_id, 1, "TEXT_MAPPING_INCOMPLETE",
                    f"PDF 第 {number} 张有 {unmapped} 个已绘制字形缺少 Unicode 映射；字形范围已测量，文字映射覆盖不完整。",
                    severity="info", start=number, end=number, coverage="partial", basis="pdf_inspection"))
    return diagnostics, sizes


def _merge_pdfs(paths: list[Path], target: Path) -> None:
    pending = target.with_name("merged.pdf")
    pending.unlink(missing_ok=True)
    with IMAGE_LOCK, fitz.open() as merged:
        for path in paths:
            with fitz.open(path) as document:
                merged.insert_pdf(document)
        merged.save(pending)
    pending.replace(target)


def render_output_page(pdf_path: Path, output_page: int) -> Path:
    target = pdf_path.parent / f"page-{output_page:04d}.png"
    with IMAGE_LOCK, fitz.open(pdf_path) as document:
        if output_page < 1 or output_page > len(document):
            raise IndexError("输出页面不存在")
        if not target.is_file():
            page = document[output_page - 1]
            scale = min(2, 1800 / max(page.rect.width, page.rect.height))
            page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB,
                            alpha=False).save(target)
    return target


async def compile_documents(detail: BookDetail, documents: list[LatexDocument], book_dir: Path,
                            current_sources: list[PageSourceMetadata], print_version: bool,
                            positions: list[int]) -> PdfCompileResult:
    try:
        runtime = await asyncio.to_thread(compiler_identity)
    except LatexCompileError as error:
        return failed_result(detail, error, positions)
    versions = {"generator": GENERATOR_VERSION, "measurement": MEASUREMENT_VERSION,
                "diagnostics": DIAGNOSTICS_VERSION, "output_checks": OUTPUT_CHECKS_VERSION}
    keys = []
    for page, document, source in zip(detail.pages, documents, current_sources):
        keys.append(_digest({
            "document": {key: value for key, value in asdict(document).items() if key != "page_order"},
            "page": page.model_dump(mode="json", exclude={"status", "error", "usage", "attempts"}),
            "current_source": source.model_dump(mode="json"), "paper_size": detail.book.paper_size,
            "layout": detail.book.layout.model_dump(mode="json"), "print_version": print_version,
            "runtime": runtime, "versions": versions,
        }))
    digest = _digest({"units": keys, "positions": positions, "versions": versions})
    merged_dir = book_dir / "latex-cache" / digest
    merged_record = merged_dir / "result.json"
    if merged_record.is_file():
        cached = json.loads(merged_record.read_text(encoding="utf-8"))
        pdf = merged_dir / "document.pdf"
        if pdf.is_file() and file_digest(pdf) == cached["pdf_fingerprint"]:
            return PdfCompileResult.model_validate(cached["result"])
    paths = []
    page_map = []
    diagnostics = []
    start = 1
    for page, document, source, key, position in zip(detail.pages, documents, current_sources, keys, positions):
        unit_dir = book_dir / "latex-units" / key
        record_path = unit_dir / "result.json"
        pdf = unit_dir / "document.pdf"
        cached = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else None
        if cached is None or not pdf.is_file() or file_digest(pdf) != cached["pdf_fingerprint"]:
            try:
                copy_document_resources(document, book_dir, unit_dir)
                pdf = await compile_pdf(document.source, unit_dir)
            except LatexCompileError as error:
                return failed_result(detail, LatexCompileError(str(error), code=error.code,
                    page_number=page.number, line_id=error.line_id), positions)
            unit_diagnostics, sizes = await asyncio.to_thread(inspect_pdf, pdf, page, document, detail.book.id)
            unit_diagnostics.extend(diagnose_document(replace(document, page_order=[1]), page, unit_dir,
                book_id=detail.book.id, arrangement_position=1, output_page_start=1, output_page_end=len(sizes)))
            if page.layout_source is not None and not same_source_identity(page.layout_source.source, source):
                unit_diagnostics.append(diagnostic(page, detail.book.id, 1, "LAYOUT_SOURCE_MISMATCH",
                    "来源文件、规范图或坐标变换已变化，当前布局仍依据旧来源。", start=1, end=len(sizes)))
            cached = {"pdf_fingerprint": file_digest(pdf), "sizes": sizes,
                      "diagnostics": [item.model_dump(mode="json") for item in unit_diagnostics],
                      "runtime": runtime, "versions": versions}
            record_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        count = len(cached["sizes"])
        end = start + count - 1
        for value in cached["diagnostics"]:
            item = RenderDiagnostic.model_validate(value)
            diagnostics.append(item.model_copy(update={
                "arrangement_position": position,
                "output_page_start": start + item.output_page_start - 1 if item.output_page_start is not None else None,
                "output_page_end": start + item.output_page_end - 1 if item.output_page_end is not None else None,
            }))
        width, height = cached["sizes"][0]
        page_map.append(PageMapEntry(
            page_number=page.number, source_id=page.source_id, source_page=page.source_page,
            arrangement_position=position, output_page_start=start, output_page_end=end,
            content_revision=page.content_revision, layout_revision=page.layout_revision,
            render_strategy=document.render_strategy, output_width_bp=width, output_height_bp=height,
            source_to_output_affine=document.source_to_output_affine, canvas_scale=document.canvas_scale,
        ))
        paths.append(pdf)
        start = end + 1
    merged_dir.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        shutil.copyfile(paths[0], merged_dir / "document.pdf")
    else:
        await asyncio.to_thread(_merge_pdfs, paths, merged_dir / "document.pdf")
    result = PdfCompileResult(
        pdf_url=f"/api/books/{detail.book.id}/compiled/{digest}.pdf",
        warnings=list(dict.fromkeys(item.message for item in diagnostics if item.severity != "info")),
        diagnostics=diagnostics, page_map=page_map,
        quality_status=diagnostics_quality_status(diagnostics, output_checks_complete=True),
        generator_version=GENERATOR_VERSION, diagnostics_version=DIAGNOSTICS_VERSION,
    )
    merged_record.write_text(json.dumps({"result": result.model_dump(mode="json"),
        "pdf_fingerprint": file_digest(merged_dir / "document.pdf"), "units": keys,
        "versions": versions, "runtime": runtime}, ensure_ascii=False), encoding="utf-8")
    return result
