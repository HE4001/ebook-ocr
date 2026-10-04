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

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from .fidelity_rendering import GENERATOR_VERSION
from .latex_content import is_latex_document
from .latex_export import PAPER_SIZES
from .layout_contract import BBox, LayoutLine, PageSourceMetadata, SourceFidelityLayout, equation_alignment_index, tex_pt_to_bp
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


def _match_region(
    lines: list[LayoutLine], box: BBox, analysis: SourceAnalysis,
    pdf_matches: dict[str, PdfTextLine], measurements: dict[str, InkBand],
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
        band = analysis.measure_bbox(line.bbox, equation=True) if line.bbox is not None else None
        if band is None:
            line.baseline = None
            unresolved.append(line.line_id)
        else:
            measurements[line.line_id] = band
            _set_geometry(line, band, analysis)
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
) -> SourceFidelityLayout:
    """Recover a candidate; unresolved evidence is handled by automatic review."""
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
    analysis = analyze_source(image_path, metadata)
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
