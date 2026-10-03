"""Persist independent source-page compilations and assemble their output map."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

import pymupdf as fitz

from .fidelity_rendering import GENERATOR_VERSION, MEASUREMENT_VERSION
from .latex_diagnostics import DIAGNOSTICS_VERSION, diagnose_document, diagnostics_quality_status
from .latex_export import LatexCompileError, LatexDocument, _xelatex_executable, compile_pdf
from .layout_contract import PageSourceMetadata
from .models import BookDetail, Page, PageMapEntry, PdfCompileResult, RenderDiagnostic
from .pipeline import _render_lock


OUTPUT_CHECKS_VERSION = "pdf-glyph-ranges-v2"
_FANDOL = ("FandolSong-Regular.otf", "FandolSong-Bold.otf", "FandolHei-Regular.otf",
           "FandolHei-Bold.otf", "FandolKai-Regular.otf", "FandolFang-Regular.otf")
_TEX_FONTS = _FANDOL + ("lmroman10-regular.otf", "latinmodern-math.otf",
                        "cmr10.pfb", "cmmi10.pfb", "cmsy10.pfb", "cmex10.pfb", "rsfs10.pfb", "rsfs10.tfm")


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
        suggestion="对照源图及当前源码修正问题后重新编译。",
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
    with _render_lock, fitz.open(path) as pdf:
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
    with _render_lock, fitz.open() as merged:
        for path in paths:
            with fitz.open(path) as document:
                merged.insert_pdf(document)
        merged.save(pending)
    pending.replace(target)


def render_output_page(pdf_path: Path, output_page: int) -> Path:
    target = pdf_path.parent / f"page-{output_page:04d}.png"
    with _render_lock, fitz.open(pdf_path) as document:
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
                pdf = await compile_pdf(document.source, unit_dir)
            except LatexCompileError as error:
                return failed_result(detail, LatexCompileError(str(error), code=error.code,
                    page_number=page.number, line_id=error.line_id), positions)
            unit_diagnostics, sizes = await asyncio.to_thread(inspect_pdf, pdf, page, document, detail.book.id)
            unit_diagnostics.extend(diagnose_document(replace(document, page_order=[1]), page, unit_dir,
                book_id=detail.book.id, arrangement_position=1, output_page_start=1, output_page_end=len(sizes)))
            if page.layout_source is not None and page.layout_source.source != source:
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
