"""Recover source geometry and shared styles without changing recognized text."""

from __future__ import annotations

import math
import os
import re
import shutil
import statistics
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from .fidelity_rendering import GENERATOR_VERSION
from .content_contract import EquationBlock, FigureBlock, MathSpan, PageContent, TableBlock, TextBlock
from .latex_content import escape_latex, is_latex_document
from .latex_export import PAPER_SIZES, printed_content_notices
from .layout_contract import (
    BBox, EquationGroup, EquationNumber, LayoutLine, LayoutRegion, LineStyle, PageLayout,
    PageLinePlacement, PageSourceMetadata, SourceFidelityLayout, equation_alignment_index, tex_pt_to_bp, output_source_assets,
)
from .models import Book, StructuredPageResult
from .source_analysis import InkBand, PdfTextLine, SourceAnalysis, analyze_source, persist_source_region


def _plain_text(latex: str) -> str:
    latex = re.sub(r"(?<!\\)%[^\n]*", "", latex)
    latex = re.sub(r"\\[A-Za-z]+\*?", "", latex)
    return re.sub(r"[\s{}$&_^\\]", "", latex)


def _pdf_match(line: LayoutLine, evidence: list[PdfTextLine]) -> PdfTextLine | None:
    text = _plain_text(line.latex)
    if len(text) < 4 or line.kind == "equation":
        return None
    candidates = []
    for item in evidence:
        if not item.visible_ink:
            continue
        if line.bbox is not None and _band_score(line.bbox, InkBand(item.bbox, item.baseline, 0., 0)) < .3:
            continue
        source_text = _plain_text(item.text)
        if abs(len(text) - len(source_text)) > max(2, len(text) * .12):
            continue
        score = SequenceMatcher(None, text, source_text, autojunk=False).ratio()
        if score >= .92:
            candidates.append((score, item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if not candidates or (len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .04):
        return None
    return candidates[0][1]


def _band_score(box: BBox, band: InkBand) -> float:
    other = band.bbox
    vertical = max(0., min(box[3], other[3]) - max(box[1], other[1])) / min(box[3] - box[1], other[3] - other[1])
    horizontal = max(0., min(box[2], other[2]) - max(box[0], other[0])) / min(box[2] - box[0], other[2] - other[0])
    distance = abs((box[1] + box[3] - other[1] - other[3]) / 2) / max(box[3] - box[1], other[3] - other[1])
    return vertical * horizontal - distance * .3


def _set_geometry(line: LayoutLine, band: InkBand, analysis: SourceAnalysis) -> None:
    # One analysis pixel of breathing room includes antialiased edge strokes.
    x0, y0, x1, y1 = band.bbox
    line.bbox = (max(0., x0 - 1 / analysis.width), max(0., y0 - 1 / analysis.height),
                 min(1., x1 + 1 / analysis.width), min(1., y1 + 1 / analysis.height))
    line.baseline = min(line.bbox[3], max(line.bbox[1], band.baseline))
    line.basis = "local_measurement"
    line.match_status = "matched"


def _match_region(
    lines: list[LayoutLine], box: BBox, analysis: SourceAnalysis,
    pdf_matches: dict[str, PdfTextLine], measurements: dict[str, InkBand], *, printed_only: bool = False,
) -> list[str]:
    """Anchor first; only fill a gap when its two sequences have equal length."""
    bands = analysis.region_bands(box)
    equation_boxes = [line.bbox for line in lines if line.kind == "equation" and line.bbox is not None]
    bands = [band for band in bands if not any(_band_score(eq_box, band) > .3 for eq_box in equation_boxes)]
    ordinary = [line for line in sorted(lines, key=lambda item: item.order) if line.kind != "equation"]
    assigned: dict[int, int] = {}
    used: set[int] = set()
    previous = -1
    for index, line in enumerate(ordinary):
        evidence = pdf_matches.get(line.line_id)
        hint = evidence.bbox if evidence is not None else line.bbox
        if hint is None:
            continue
        candidates = sorted(((_band_score(hint, band), band_index) for band_index, band in enumerate(bands)
                             if band_index not in used and band_index > previous), reverse=True)
        if not candidates or candidates[0][0] < .45:
            continue
        if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .12:
            continue
        score, band_index = candidates[0]
        # A paragraph-sized observation cannot anchor one arbitrary source row.
        if hint[3] - hint[1] > (bands[band_index].bbox[3] - bands[band_index].bbox[1]) * 2.2:
            continue
        assigned[index] = band_index
        used.add(band_index)
        previous = band_index
    anchors = [(-1, -1), *sorted(assigned.items()), (len(ordinary), len(bands))]
    for (line_start, band_start), (line_end, band_end) in zip(anchors, anchors[1:]):
        unknown_lines = list(range(line_start + 1, line_end))
        unmatched_bands = [index for index in range(band_start + 1, band_end) if index not in used]
        if len(unknown_lines) == len(unmatched_bands):
            for line_index, band_index in zip(unknown_lines, unmatched_bands):
                assigned[line_index] = band_index
                used.add(band_index)
    unresolved = []
    for index, line in enumerate(ordinary):
        if index in assigned:
            band = bands[assigned[index]]
            measurements[line.line_id] = band
            _set_geometry(line, band, analysis)
        else:
            line.baseline = None
            unresolved.append(line.line_id)
    for line in lines:
        if line.kind != "equation":
            continue
        if not printed_only:
            band = analysis.measure_bbox(line.bbox, equation=True) if line.bbox is not None else None
            if band is not None:
                measurements[line.line_id] = band
                _set_geometry(line, band, analysis)
                continue
        # Ink within a model box cannot establish formula identity. No local
        # formula-component matcher exists yet; retain the text and unknown geometry.
        line.baseline = None
        line.match_status = "unknown"
        unresolved.append(line.line_id)
    return unresolved


@lru_cache(maxsize=1)
def _available_fonts() -> dict[str, Path]:
    """Read only known installed font locations; do not invoke font/TeX tools."""
    roots = [Path(__file__).resolve().parent.parent / "fonts"]
    executable = shutil.which("xelatex")
    if executable:
        executable_path = Path(executable).resolve()
        roots.extend(parent / "texmf-dist" / "fonts" / "opentype" / "public" / "fandol"
                     for parent in list(executable_path.parents)[:3])
    names = {"songti": "FandolSong-Regular.otf", "heiti": "FandolHei-Regular.otf", "kaiti": "FandolKai-Regular.otf"}
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    fallback = {"songti": "simsun.ttc", "heiti": "simhei.ttf", "kaiti": "simkai.ttf"}
    result = {}
    for family, filename in names.items():
        candidates = [root / filename for root in roots] + [windows / fallback[family]]
        candidate = next((path for path in candidates if path.is_file()), None)
        if candidate is not None:
            result[family] = candidate
    return result


def _signature(image: Image.Image) -> Image.Image | None:
    box = image.getbbox()
    if box is None:
        return None
    glyph = ImageOps.contain(image.crop(box), (28, 28), Image.Resampling.NEAREST)
    signature = Image.new("L", (32, 32), 0)
    signature.paste(glyph, ((32 - glyph.width) // 2, (32 - glyph.height) // 2))
    return signature


def _glyph_samples(lines: list[LayoutLine], analysis: SourceAnalysis) -> list[tuple[str, Image.Image, float]]:
    samples = []
    mask = Image.frombytes("L", (analysis.width, analysis.height), bytes(value * 255 for value in analysis.mask))
    for line in lines[:3]:
        text = _plain_text(line.latex)
        if line.bbox is None or not re.fullmatch(r"[\u3400-\u9fff，。；：！？、（）《》“”‘’]+", text) or len(text) < 6:
            continue
        band = analysis.measure_bbox(line.bbox)
        if band is None:
            continue
        x0, y0, x1, y1 = band.bbox
        pitch = (x1 - x0) * analysis.width / max(1, len(text) - .12)
        for index in range(1, min(len(text) - 1, 5)):
            character = text[index]
            if not "\u3400" <= character <= "\u9fff":
                continue
            left = math.floor(x0 * analysis.width + (index - .04) * pitch)
            right = math.floor(x0 * analysis.width + (index + .96) * pitch)
            crop = mask.crop((left, math.floor(y0 * analysis.height), right, round(y1 * analysis.height)))
            signature = _signature(crop)
            bounds = crop.getbbox()
            if signature is not None and bounds is not None:
                samples.append((character, signature, (bounds[3] - bounds[1]) / analysis.height))
            if len(samples) == 8:
                return samples
    return samples


def _fit_font(
    lines: list[LayoutLine], analysis: SourceAnalysis, height_bp: float, project_family: str,
) -> tuple[str, float | None, str]:
    samples = _glyph_samples(lines, analysis)
    fitted = []
    for family, path in _available_fonts().items():
        if lines and lines[0].style.bold and "-Regular" in path.name:
            bold_path = path.with_name(path.name.replace("-Regular", "-Bold"))
            if bold_path.is_file():
                path = bold_path
        try:
            font = ImageFont.truetype(str(path), 64)
        except OSError:
            continue
        scores, sizes = [], []
        for character, source, source_height in samples:
            box = font.getbbox(character)
            if box is None or box[3] <= box[1]:
                continue
            glyph = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
            ImageDraw.Draw(glyph).text((-box[0], -box[1]), character, font=font, fill=255)
            candidate = _signature(glyph)
            if candidate is None:
                continue
            difference = sum(ImageChops.difference(source, candidate).getdata())
            ink = sum(ImageChops.lighter(source, candidate).getdata())
            scores.append(difference / max(1, ink))
            sizes.append(source_height * height_bp * 64 / (box[3] - box[1]))
        if scores:
            fitted.append((statistics.mean(scores), family, statistics.median(sizes)))
    fitted.sort()
    if fitted and fitted[0][0] < .72 and (len(fitted) == 1 or fitted[1][0] - fitted[0][0] >= .025):
        return fitted[0][1], fitted[0][2], "local_measurement"
    # Even when family evidence is inconclusive, its natural glyph height can
    # recover a size. No character width is scaled to make a row fit.
    project = next((item for item in fitted if item[1] == project_family), None)
    return project_family, project[2] if project else None, "project"


def _font_family_from_pdf(name: str) -> str | None:
    name = name.lower()
    if any(token in name for token in ("song", "simsun", "ming", "宋")):
        return "songti"
    if any(token in name for token in ("hei", "simhei", "黑")):
        return "heiti"
    if any(token in name for token in ("kai", "楷")):
        return "kaiti"
    return None


def _shared_styles(
    layout: SourceFidelityLayout, analysis: SourceAnalysis, measurements: dict[str, InkBand],
    pdf_matches: dict[str, PdfTextLine], book: Book,
) -> None:
    height_bp = layout.canvas_height_bp
    default_size = tex_pt_to_bp(book.layout.font_size_pt or PAPER_SIZES[book.paper_size][3])
    line_map = {line.line_id: line for line in layout.lines}
    text_heights = [band.bbox[3] - band.bbox[1] for line_id, band in measurements.items()
                    if line_map[line_id].kind == "text"]
    body_height = statistics.median(text_heights) if text_heights else default_size / height_bp
    regions = {region.region_id: region for region in layout.regions}
    groups: dict[tuple, list[LayoutLine]] = defaultdict(list)
    for line in layout.lines:
        evidence = pdf_matches.get(line.line_id)
        if evidence is not None:
            line.style.bold, line.style.italic = evidence.bold, evidence.italic
        band = measurements.get(line.line_id)
        ratio = (band.bbox[3] - band.bbox[1]) / body_height if band is not None else 1
        size_class = 1 if .8 <= ratio <= 1.25 or line.kind == "equation" else round(ratio * 4) / 4
        region_kind = regions[line.block_id].kind
        role = region_kind if region_kind in {"header", "footer", "footnote"} else "body" if line.kind in {"text", "equation"} else line.kind
        groups[(role, size_class, bool(line.style.bold), bool(line.style.italic))].append(line)
    body_candidates = []
    for key, lines in groups.items():
        family, glyph_size, family_basis = _fit_font(lines, analysis, height_bp, book.layout.font_family)
        pdf = [pdf_matches[line.line_id] for line in lines if line.line_id in pdf_matches]
        pdf_sizes = [item.font_size_bp for item in pdf]
        if pdf_sizes and max(pdf_sizes) / min(pdf_sizes) <= 1.12:
            size = statistics.median(pdf_sizes)
            size_basis = "file_metadata"
            pdf_families = {_font_family_from_pdf(item.font_name) for item in pdf}
            if len(pdf_families) == 1 and None not in pdf_families:
                family = next(iter(pdf_families))
                family_basis = "file_metadata"
        else:
            heights = [measurements[line.line_id].bbox[3] - measurements[line.line_id].bbox[1]
                       for line in lines if line.line_id in measurements and line.kind != "equation"]
            size = glyph_size or (statistics.median(heights) * height_bp / .88 if heights else default_size)
            size_basis = "local_measurement" if glyph_size is not None or heights else "project"
        if not 4 <= size <= 72:
            size, size_basis = default_size, "project"
        # The single style basis covers both family and size. Measured height
        # cannot turn an assumed project family into a confirmed source font.
        if "project" in {family_basis, size_basis}:
            basis = "project"
        elif family_basis == size_basis == "file_metadata":
            basis = "file_metadata"
        else:
            basis = "local_measurement"
        for line in lines:
            line.style.font_family = family
            line.style.font_size_bp = size
            line.style.font_size_ratio = None
            line.style.basis = basis
        if key[0] == "body" and key[1] == 1:
            body_candidates.append((len(lines), size, family, basis))
    if body_candidates:
        _, size, family, basis = max(body_candidates)
    else:
        size, family, basis = default_size, book.layout.font_family, "project"
    layout.body_font_size_bp, layout.body_font_family, layout.body_font_basis = size, family, basis


def solve_layout(
    result: StructuredPageResult, metadata: PageSourceMetadata, book: Book,
    content_revision: int, layout_revision: int, *, image_path: Path, book_dir: Path,
    analysis: SourceAnalysis | None = None,
) -> SourceFidelityLayout:
    """Explicit V1 compatibility; V2 uses derive_layout with existing analysis."""
    if result.layout is None:
        raise ValueError("页面响应没有可用于自动布局的原行观察")
    observation = result.layout.model_copy(deep=True)
    if metadata.pdf_geometry is not None:
        width, height = metadata.pdf_geometry.width_bp, metadata.pdf_geometry.height_bp
        if metadata.pdf_geometry.rotation in (90, 270):
            width, height = height, width
        canvas_basis = "file_metadata"
    else:
        width = PAPER_SIZES[book.paper_size][0] * 72 / 25.4
        height = width * metadata.canonical_height_px / metadata.canonical_width_px
        canvas_basis = "project"
    layout = SourceFidelityLayout(
        **observation.model_dump(), source=metadata, content_revision=content_revision, layout_revision=layout_revision,
        generated_content_revision=None if is_latex_document(result.body_latex) else content_revision,
        generator_version=GENERATOR_VERSION, canvas_width_bp=width, canvas_height_bp=height, canvas_basis=canvas_basis,
    )
    analysis = analysis if analysis is not None else analyze_source(image_path, metadata, book_dir=book_dir, max_crops=0)
    analysis.require_source(metadata)
    pdf_matches = {line.line_id: evidence for line in layout.lines
                   if (evidence := _pdf_match(line, analysis.pdf_text_lines)) is not None}
    lines = {line.line_id: line for line in layout.lines}
    # A group box can locate a single complex formula without treating its
    # fraction bars or scripts as additional OCR lines.
    for group in layout.equation_groups:
        missing = [lines[line_id] for line_id in group.line_ids if lines[line_id].bbox is None]
        if group.bbox is not None and len(group.line_ids) == 1 and missing:
            missing[0].bbox = group.bbox
        elif group.bbox is not None and len(missing) == len(group.line_ids):
            bands = analysis.bands_in_bbox(group.bbox)
            if len(bands) == len(missing):
                for line, band in zip(missing, bands):
                    line.bbox = band.bbox
    grouped: dict[str, list[LayoutLine]] = defaultdict(list)
    for line in layout.lines:
        grouped[line.block_id].append(line)
    measurements: dict[str, InkBand] = {}
    unresolved = []
    for region in layout.regions:
        if region.kind == "figure" and region.bbox is not None:
            layout.source_assets.append(persist_source_region(
                book_dir, image_path, metadata, region.bbox, region.region_id,
                "原书图形以源区域资源保留", purpose="figure",
            ))
        region_lines = grouped.get(region.region_id, [])
        if region_lines:
            unresolved.extend(_match_region(region_lines, region.bbox or (0., 0., 1., 1.), analysis, pdf_matches, measurements))
    for group in layout.equation_groups:
        positioned = [lines[line_id].bbox for line_id in group.line_ids if lines[line_id].bbox is not None]
        if len(positioned) == len(group.line_ids) and all(line_id in measurements for line_id in group.line_ids):
            group.bbox = (min(box[0] for box in positioned), min(box[1] for box in positioned),
                          max(box[2] for box in positioned), max(box[3] for box in positioned))
            requires_anchor = len(group.line_ids) > 1 or any(
                equation_alignment_index(lines[line_id].latex) is not None for line_id in group.line_ids
            )
            if not requires_anchor and group.align_x is None and group.number is None:
                group.basis = "local_measurement"
            # Otherwise preserve the observation's anchor/number basis: the
            # measured union above establishes only the group's outer box.
        # A missing alignment anchor is left unresolved rather than guessed from
        # LaTeX source length. Render diagnostics can retain that exact group.
    pdf_matches = {line_id: evidence for line_id, evidence in pdf_matches.items()
                   if line_id in measurements and _band_score(evidence.bbox, measurements[line_id]) > .45}
    _shared_styles(layout, analysis, measurements, pdf_matches, book)
    program_reasons = []
    if canvas_basis == "project":
        program_reasons.append("源图片无可信物理尺寸；画布采用项目纸宽并保持原图比例，尺寸依据为project。")
    if unresolved:
        program_reasons.append("以下原行无法唯一匹配源图行带，将自动复核或保留其源区域：" + ", ".join(unresolved)[:1_500])
    uncovered = analysis.uncovered_regions(layout)
    if uncovered:
        program_reasons.append(f"本地来源分析发现 {len(uncovered)} 个未定位或未覆盖区域，自动复核使用其归一化坐标定位并处理。")
    # The wire contract reserves three program reasons beyond its model limit.
    layout.review_reasons = layout.review_reasons[:100] + program_reasons[:3]
    return layout


def _text_line_latex(line) -> str:
    pieces = []
    for span in line.spans:
        if isinstance(span, MathSpan):
            pieces.append(r"\(" + span.text + r"\)")
            continue
        value = escape_latex(span.text)
        if span.bold:
            value = r"\textbf{" + value + "}"
        if span.italic:
            value = r"\textit{" + value + "}"
        if span.font_family:
            command = {"songti": r"\songti", "heiti": r"\heiti", "kaiti": r"\kaishu"}[span.font_family]
            value = "{" + command + " " + value + "}"
        pieces.append(value)
    return "".join(pieces)


def _content_lines(content: PageContent) -> list[LayoutLine]:
    """Render projection only; every string is derived from saved content."""
    lines = []
    for block in content.blocks:
        if isinstance(block, FigureBlock):
            continue
        source_lines = block.lines if isinstance(block, (TextBlock, EquationBlock)) else [
            line for cell in block.cells for line in cell.lines
        ]
        for source_line in source_lines:
            if not source_line.line_id:
                raise ValueError("已保存内容原行缺少程序 ID，布局不能自行更改内容身份")
            if isinstance(block, EquationBlock):
                try:
                    equation_alignment_index(source_line.latex)
                    kind, latex = "equation", source_line.latex
                except ValueError:
                    # The readable fragment remains content. An unsupported
                    # outer environment is preserved as a bounded source region
                    # rather than silently rewritten into a different formula.
                    kind, latex = "text", r"\(\displaystyle " + source_line.latex + r"\)"
            else:
                kind = "table" if isinstance(block, TableBlock) else block.role if block.role in {
                    "header", "footer", "page_number", "footnote", "caption"
                } else "text"
                latex = _text_line_latex(source_line)
            lines.append(LayoutLine(line_id=source_line.line_id, block_id=block.block_id, order=len(lines),
                                    kind=kind, latex=latex, bbox=source_line.bbox,
                                    basis="model_estimate" if source_line.bbox else None))
    return lines


def _block_box(block) -> BBox | None:
    boxes = [block.bbox] if block.bbox is not None else []
    boxes.extend(line.bbox for line in getattr(block, "lines", []) if line.bbox is not None)
    boxes.extend(cell.bbox for cell in getattr(block, "cells", []) if cell.bbox is not None)
    if not boxes:
        return None
    return min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)


def _canvas(metadata: PageSourceMetadata, book: Book) -> tuple[float, float, str]:
    if metadata.pdf_geometry is not None:
        a, b, c, d, _, _ = metadata.canonical_to_source_affine
        return (math.hypot(a, b) * metadata.canonical_width_px,
                math.hypot(c, d) * metadata.canonical_height_px, "file_metadata")
    width = PAPER_SIZES[book.paper_size][0] * 72 / 25.4
    return width, width * metadata.canonical_height_px / metadata.canonical_width_px, "project"


def derive_layout(
    content: PageContent, metadata: PageSourceMetadata, analysis: SourceAnalysis, book: Book, *,
    image_path: Path, book_dir: Path, layout_revision_id: str | None = None,
) -> PageLayout:
    """Derive geometry after content is saved, reusing exactly its source analysis."""
    analysis.require_source(metadata)
    if (content.book_id, content.page_id, content.source_version) != (metadata.book_id, metadata.page_id, metadata.source_version):
        raise ValueError("内容、来源与布局输入版本不一致")
    width, height, canvas_basis = _canvas(metadata, book)
    lines = _content_lines(content)
    by_block: dict[str, list[LayoutLine]] = defaultdict(list)
    for line in lines:
        by_block[line.block_id].append(line)
    regions = []
    for order, block in enumerate(content.blocks):
        box = _block_box(block)
        basis = "model_estimate" if box else None
        if box is None:
            candidates = [region for region in analysis.regions if region.region_id == block.source_region_id]
            if not candidates and block.role in {"header", "footer", "title", "footnote"}:
                candidates = [region for region in analysis.regions if region.kind == block.role]
            if len(candidates) == 1:
                box, basis = candidates[0].bbox, "local_measurement"
        kind = block.type if block.type != "text" else block.role if block.role in {
            "header", "footer", "footnote"
        } else "paragraph"
        regions.append(LayoutRegion(region_id=block.block_id, kind=kind, order=order, bbox=box, basis=basis))
    groups = []
    line_map = {line.line_id: line for line in lines}
    for block in content.blocks:
        if not isinstance(block, EquationBlock):
            continue
        for source_line in block.lines:
            line = line_map[source_line.line_id]
            if line.kind != "equation":
                continue
            groups.append(EquationGroup(group_id=f"equation-{source_line.line_id}", line_ids=[source_line.line_id],
                                        bbox=source_line.bbox or _block_box(block),
                                        number=EquationNumber(latex=escape_latex(source_line.number), line_id=source_line.line_id)
                                        if source_line.number else None, basis="model_estimate"))
    candidate = SourceFidelityLayout(recognition_scope=content.recognition_scope, source=metadata, content_revision=0, layout_revision=0,
                                    generated_content_revision=0, generator_version=GENERATOR_VERSION,
                                    canvas_width_bp=width, canvas_height_bp=height, canvas_basis=canvas_basis,
                                    regions=regions, lines=lines, equation_groups=groups)
    pdf_matches = {line.line_id: evidence for line in lines
                   if (evidence := _pdf_match(line, analysis.pdf_text_lines)) is not None}
    for region in regions:
        block_lines = by_block[region.region_id]
        evidence = [pdf_matches[line.line_id] for line in block_lines if line.line_id in pdf_matches]
        if region.bbox is None and block_lines and len(evidence) == len(block_lines):
            region.bbox = (min(item.bbox[0] for item in evidence), min(item.bbox[1] for item in evidence),
                           max(item.bbox[2] for item in evidence), max(item.bbox[3] for item in evidence))
            region.basis = "file_metadata"
    measurements: dict[str, InkBand] = {}
    reasons = []
    # Plain pages without model boxes still get a source-based attempt. Equal
    # visual-row counts establish only geometry, never content correctness.
    if content.blocks and all(isinstance(block, TextBlock) for block in content.blocks) and all(line.bbox is None for line in lines):
        bands = analysis.region_bands((0., 0., 1., 1.))
        if len(bands) == len(lines):
            for line, band in zip(lines, bands):
                measurements[line.line_id] = band
                _set_geometry(line, band, analysis)
            for region in regions:
                boxes = [line.bbox for line in by_block[region.region_id]]
                region.bbox = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                               max(box[2] for box in boxes), max(box[3] for box in boxes))
                region.basis = "local_measurement"
            reasons.append("原行与完整源图行带按顺序等数对应；这仅恢复位置，文字结论仍取决于独立内容复核")
    unresolved = []
    preservation: dict[str, tuple[BBox, str, str]] = {}
    for block, region in zip(content.blocks, regions):
        block_lines = by_block[block.block_id]
        if isinstance(block, FigureBlock):
            if region.bbox is not None:
                preservation[block.block_id] = (region.bbox, "figure", "可见图形以对应源区域保留")
            else:
                reasons.append(f"图形 {block.block_id} 的源位置未知")
            continue
        if isinstance(block, TableBlock):
            for cell in block.cells:
                cell_lines = [line_map[line.line_id] for line in cell.lines]
                if cell_lines and cell.bbox is not None:
                    unresolved.extend(_match_region(cell_lines, cell.bbox, analysis, pdf_matches, measurements, printed_only=content.recognition_scope == "printed_original_only"))
                elif cell_lines:
                    unresolved.extend(line.line_id for line in cell_lines)
            if region.bbox is not None:
                preservation[block.block_id] = (region.bbox, "uncertain_content", "缺少表格网格与合并边界的来源测量，保留完整可定位表格及表头；单元格内容仍保存在 JSON")
                reasons.append(f"表格 {block.block_id} 网格及合并几何未知；单元格正文已保存，忠实 PDF 未生成"
                               if content.recognition_scope == "printed_original_only" else
                               f"表格 {block.block_id} 网格及合并几何未知，按完整源区域保留，布局为 approximate")
            else:
                reasons.append(f"表格 {block.block_id} 完整源位置与网格几何未知；单元格正文已保存，忠实 PDF 未生成"
                               if content.recognition_scope == "printed_original_only" else
                               f"表格 {block.block_id} 完整源位置未知；不伪造整页表格框，布局不可用")
        elif block_lines and not all(line.line_id in measurements for line in block_lines):
            if isinstance(block, EquationBlock) and len(block_lines) == 1 and block_lines[0].bbox is None:
                block_lines[0].bbox = region.bbox
            if region.bbox is not None:
                unresolved.extend(_match_region(block_lines, region.bbox, analysis, pdf_matches, measurements, printed_only=content.recognition_scope == "printed_original_only"))
            else:
                unresolved.extend(line.line_id for line in block_lines)
        if isinstance(block, EquationBlock):
            unsupported = any(line.kind != "equation" for line in block_lines)
            need_anchor = any(line.kind == "equation" and equation_alignment_index(line.latex) is not None for line in block_lines)
            if unsupported or need_anchor:
                reason = "公式外层环境不能直接派生原行布局" if unsupported else "公式对齐锚点尚无源图测量依据"
                reasons.append(f"{block.block_id}：{reason}；原数学内容保持不变")
                if region.bbox is not None:
                    preservation[block.block_id] = (region.bbox, "uncertain_content", reason + "，保留完整公式组与编号")
            for group in (item for item in groups if item.line_ids[0] in {line.line_id for line in block_lines}):
                line = line_map[group.line_ids[0]]
                group.bbox = line.bbox or region.bbox
                if group.number is not None:
                    # Native PDF numbering can corroborate geometry only when
                    # its visible position belongs to this exact formula region.
                    matches = [item for item in analysis.pdf_text_lines if item.visible_ink and group.bbox is not None
                               and _plain_text(item.text) == _plain_text(group.number.latex)
                               and item.bbox[1] < group.bbox[3] and item.bbox[3] > group.bbox[1]
                               and region.bbox is not None and item.bbox[0] >= region.bbox[0]
                               and item.bbox[2] <= region.bbox[2]]
                    if len(matches) == 1:
                        group.number.bbox = matches[0].bbox
                        group.number.anchor_x = matches[0].bbox[2]
                    elif region.bbox is not None:
                        preservation[block.block_id] = (region.bbox, "uncertain_content", "公式编号无法唯一定位，保留完整公式组及编号")
                        reasons.append(f"公式编号 {group.number.line_id} 位置未知")
        if block.review_status in {"uncertain", "unavailable"} or block.recognition_status == "unavailable":
            if region.bbox is not None and block.block_id not in preservation:
                preservation[block.block_id] = (region.bbox, "uncertain_content", "内容复核未能可靠文字化此区域，保留源内容及已识别 JSON")
        if any(line.baseline is None for line in block_lines):
            if region.bbox is not None and block.block_id not in preservation:
                preservation[block.block_id] = (region.bbox, "uncertain_content", "此块原行未能唯一恢复源位置，保留必要源区域")
            reasons.append(f"块 {block.block_id} 部分原行位置或基线未知")
    if content.recognition_scope == "printed_original_only":
        # No region expansion can turn unverified pixels into a clean source asset.
        # Keep recognized text even when geometry or a graphic remains unresolved.
        clean_figures = {region.region_id: region for region in content.regions if region.layer == "printed" and region.kind == "figure" and region.cleanliness_verified}
        for block in content.blocks:
            if isinstance(block, FigureBlock) and block.source_region_id in clean_figures:
                region = clean_figures[block.source_region_id]
                try:
                    asset = persist_source_region(book_dir, image_path, metadata, region.bbox, block.block_id, "已核验干净印刷图形", purpose="figure")
                    candidate.source_assets.append(asset.model_copy(update={"source_layer": "printed", "cleanliness_verified": True}))
                except (OSError, RuntimeError, ValueError) as error:
                    reasons.append(f"干净图形 {block.block_id} 保存失败：{str(error)[:250]}")
        if preservation:
            reasons.append("已辨印刷正文保存；几何、图形或内容仍有未决项，忠实 PDF 未生成")
        preservation = {}
    # Expand a preserved region to cover any other item it intersects. This
    # avoids clipping a line in half or drawing the source and text twice.
    for block_id, (box, purpose, reason) in list(preservation.items()):
        owned = [line.bbox for line in by_block[block_id] if line.bbox is not None]
        owned.extend(group.number.bbox for group in groups if group.number is not None and group.number.bbox is not None
                     and line_map[group.number.line_id].block_id == block_id)
        if owned:
            box = (min([box[0], *(other[0] for other in owned)]), min([box[1], *(other[1] for other in owned)]),
                   max([box[2], *(other[2] for other in owned)]), max([box[3], *(other[3] for other in owned)]))
        if purpose == "uncertain_content":
            # Complete visible rows retain peripheral numbering and strokes;
            # this is independent of the much larger context used by OCR crops.
            bands = analysis.region_bands((0., 0., 1., 1.))
            for _ in range(3):
                crossing = [band.bbox for band in bands if max(box[0], band.bbox[0]) < min(box[2], band.bbox[2])
                            and max(box[1], band.bbox[1]) < min(box[3], band.bbox[3])]
                new_box = (min([box[0], *(other[0] for other in crossing)]), min([box[1], *(other[1] for other in crossing)]),
                           max([box[2], *(other[2] for other in crossing)]), max([box[3], *(other[3] for other in crossing)]))
                if new_box == box:
                    break
                box = new_box
        for _ in range(len(regions) + 1):
            crossing = [region.bbox for region in regions if region.bbox is not None
                        and max(box[0], region.bbox[0]) < min(box[2], region.bbox[2])
                        and max(box[1], region.bbox[1]) < min(box[3], region.bbox[3])]
            crossing.extend(line.bbox for line in lines if line.bbox is not None
                            and max(box[0], line.bbox[0]) < min(box[2], line.bbox[2])
                            and max(box[1], line.bbox[1]) < min(box[3], line.bbox[3]))
            new_box = (min([box[0], *(other[0] for other in crossing)]), min([box[1], *(other[1] for other in crossing)]),
                       max([box[2], *(other[2] for other in crossing)]), max([box[3], *(other[3] for other in crossing)]))
            if new_box == box:
                break
            box = new_box
        if any(asset.region_id == block_id and asset.purpose == purpose and asset.bbox[0] <= box[0] and asset.bbox[1] <= box[1] and asset.bbox[2] >= box[2] and asset.bbox[3] >= box[3]
               for asset in candidate.source_assets):
            continue
        try:
            candidate.source_assets.append(persist_source_region(book_dir, image_path, metadata, box, block_id, reason, purpose=purpose))
        except (OSError, RuntimeError, ValueError) as error:
            reasons.append(f"源区域 {block_id} 保存失败：{str(error)[:250]}")
    pdf_matches = {line_id: evidence for line_id, evidence in pdf_matches.items()
                   if line_id in measurements and _band_score(evidence.bbox, measurements[line_id]) > .45}
    _shared_styles(candidate, analysis, measurements, pdf_matches, book)
    if canvas_basis == "project":
        reasons.append("图片缺少可信物理尺寸；采用项目纸宽及原图比例，字号与画布尺寸仍为估计")
    if any(line.style.basis == "project" for line in lines):
        reasons.append("部分字体族或字号采用项目设置，未把估计标为测量真值")
    uncovered = analysis.uncovered_content(content) if content.recognition_scope == "legacy_all_visible" else []
    if uncovered:
        reasons.append(f"完整源页另有 {len(uncovered)} 个可能遗漏区域；墨迹只作复核线索，不宣称文字已覆盖")
    unresolved_unknown = any(line.baseline is None and not any(asset.region_id == line.block_id for asset in candidate.source_assets)
                             for line in lines)
    figures_unknown = any(isinstance(block, FigureBlock) and (content.recognition_scope == "printed_original_only" or _block_box(block) is None)
                          and not any(asset.region_id == block.block_id for asset in candidate.source_assets) for block in content.blocks)
    tables_unknown = any(isinstance(block, TableBlock) and not any(asset.region_id == block.block_id and asset.purpose == "uncertain_content"
                                                                 for asset in candidate.source_assets) for block in content.blocks)
    preservation_failed = any(not any(asset.region_id == block_id for asset in candidate.source_assets) for block_id in preservation)
    conclusion = "unavailable" if unresolved_unknown or figures_unknown or tables_unknown or preservation_failed or (not content.blank and not content.blocks) else (
        "approximate" if reasons or candidate.source_assets else "faithful")
    body_boxes = [region.bbox for region in regions if region.bbox is not None and region.kind not in {"header", "footer"}]
    body_frame = ((min(box[0] for box in body_boxes), min(box[1] for box in body_boxes),
                   max(box[2] for box in body_boxes), max(box[3] for box in body_boxes)) if body_boxes else None)
    return PageLayout(recognition_scope=content.recognition_scope, layout_revision_id=layout_revision_id or str(uuid4()), content_revision_id=content.content_revision_id,
                      page_id=content.page_id, source_version=content.source_version, source=metadata,
                      canvas_width_bp=width, canvas_height_bp=height, canvas_basis=canvas_basis, body_frame=body_frame,
                      regions=regions, lines=[PageLinePlacement(**line.model_dump(exclude={"kind", "latex"})) for line in lines],
                      equation_groups=groups, source_assets=candidate.source_assets, conclusion=conclusion,
                      body_font_size_bp=candidate.body_font_size_bp, body_font_family=candidate.body_font_family,
                      body_font_basis=candidate.body_font_basis, review_reasons=list(dict.fromkeys(reasons))[:500])


def to_source_fidelity_layout(
    content: PageContent, layout: PageLayout, *, content_revision: int = 0, layout_revision: int = 0,
) -> SourceFidelityLayout:
    """Explicit renderer adapter; layout can never provide authoritative text."""
    if (layout.content_revision_id, layout.source.book_id, layout.page_id, layout.source_version) != (
        content.content_revision_id, content.book_id, content.page_id, content.source_version
    ):
        raise ValueError("输出布局必须引用当前内容修订及来源版本")
    lines = _content_lines(content)
    line_map = {line.line_id: line for line in lines}
    placements = {line.line_id: line for line in layout.lines}
    if set(placements) != set(line_map):
        raise ValueError("布局原行引用与内容原行不一致")
    for line in lines:
        placement = placements[line.line_id]
        if placement.block_id != line.block_id:
            raise ValueError("布局原行不能改换内容块归属")
        line.order, line.bbox, line.baseline = placement.order, placement.bbox, placement.baseline
        line.style, line.basis = placement.style.model_copy(deep=True), placement.basis
        line.match_status = placement.match_status
    if content.recognition_scope == "printed_original_only":
        # Unsupported math can use a text rendering kind, but remains a formula.
        for block in content.blocks:
            if isinstance(block, EquationBlock):
                for source_line in block.lines:
                    line_map[source_line.line_id].baseline = None
                    line_map[source_line.line_id].match_status = "unknown"
    content_numbers = {line.line_id: line.number for block in content.blocks if isinstance(block, EquationBlock) for line in block.lines}
    groups = [group.model_copy(deep=True) for group in layout.equation_groups]
    for group in groups:
        if any(line_id not in line_map or line_map[line_id].kind != "equation" for line_id in group.line_ids):
            raise ValueError("布局公式组必须引用真实内容公式行")
        if len({line_map[line_id].block_id for line_id in group.line_ids}) != 1:
            raise ValueError("布局公式组不能跨越内容公式块")
        if group.number is not None:
            if group.number.line_id not in group.line_ids or not content_numbers.get(group.number.line_id):
                raise ValueError("布局公式编号必须归属有编号的内容公式行")
            group.number.latex = escape_latex(content_numbers[group.number.line_id])
    grouped_numbers = {group.number.line_id for group in groups if group.number is not None}
    if any(number and line_id not in grouped_numbers for line_id, number in content_numbers.items() if line_map[line_id].kind == "equation"):
        raise ValueError("布局遗漏已保存的公式编号")
    assets = output_source_assets(layout.model_copy(update={"recognition_scope": content.recognition_scope}))
    whole_page = any(asset.purpose == "source_page" or (asset.purpose == "uncertain_content" and asset.bbox == (0., 0., 1., 1.))
                     for asset in assets)
    disposition = "source_page_preserved" if whole_page else "regions_preserved" if any(
        asset.purpose == "uncertain_content" for asset in assets
    ) else "transcribed"
    return SourceFidelityLayout(recognition_scope=content.recognition_scope, unresolved_notices=printed_content_notices(content) if content.recognition_scope == "printed_original_only" else [], source=layout.source, content_revision=content_revision, layout_revision=layout_revision,
                                generated_content_revision=content_revision, generator_version=GENERATOR_VERSION,
                                canvas_width_bp=layout.canvas_width_bp, canvas_height_bp=layout.canvas_height_bp,
                                canvas_basis=layout.canvas_basis, body_frame=layout.body_frame, regions=layout.regions,
                                lines=lines, equation_groups=groups, source_assets=assets,
                                body_font_size_bp=layout.body_font_size_bp, body_font_family=layout.body_font_family,
                                body_font_basis=layout.body_font_basis, review_reasons=layout.review_reasons[:103],
                                source_disposition=disposition,
                                disposition_reason="整页源内容保留；已识别内容仍在 JSON" if whole_page else
                                "部分区域缺少可靠内容或布局依据，按对应源框保留" if disposition == "regions_preserved" else None)


def summarize_book_styles(layouts: list[SourceFidelityLayout]) -> dict:
    """Summarize consistent automatic pages without rewriting older revisions."""
    candidates = [layout for layout in layouts if layout.source_disposition == "transcribed"
                  and not layout.review_reasons and layout.body_font_basis in {"local_measurement", "file_metadata"}
                  and layout.body_font_size_bp is not None]
    if len(candidates) < 2:
        return {}
    family, count = Counter(layout.body_font_family for layout in candidates).most_common(1)[0]
    coherent = [layout for layout in candidates if layout.body_font_family == family]
    sizes = [layout.body_font_size_bp for layout in coherent]
    if count < len(candidates) * .8 or max(sizes) / min(sizes) > 1.12:
        return {}
    headers = Counter(_plain_text(line.latex) for layout in coherent for line in layout.lines if line.kind == "header")
    return {"font_family": family, "font_size_bp": statistics.median(sizes), "page_count": len(coherent),
            "repeated_headers": [text for text, repetitions in headers.items() if text and repetitions >= 2]}
