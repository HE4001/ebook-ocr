"""Bounded local source evidence; observations never replace transcribed content."""

from __future__ import annotations

import math
import statistics
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pymupdf as fitz
from PIL import Image, ImageOps

from .importers import IMAGE_LOCK, original_source_image
from .content_contract import ContentIssue, EquationBlock, FigureBlock, PageContent, TableBlock, TextBlock
from .layout_contract import (
    BBox, CoarseRegion, CropMapping, PageSourceMetadata, RecognitionInput, SourceFidelityLayout, SourceRegionAsset,
    equation_alignment_index, inverse_transform, transform_point,
)


@dataclass(frozen=True)
class InkBand:
    bbox: tuple[float, float, float, float]
    baseline: float
    body_height: float
    ink_pixels: int


@dataclass(frozen=True)
class PdfTextLine:
    text: str
    bbox: tuple[float, float, float, float]
    baseline: float
    font_size_bp: float
    font_name: str
    bold: bool
    italic: bool
    visible_ink: bool = False
    basis: str = "file_metadata"
    reason: str = "PDF 文字层仅为辅助证据，必须与可见源图及对应内容核对"


@dataclass(frozen=True)
class SourceGeometryIssue:
    bbox: tuple[float, float, float, float]
    region_id: str | None
    line_ids: tuple[str, ...]
    reason: str


def _pixel_box(bbox: BBox, width: int, height: int) -> tuple[int, int, int, int]:
    return (
        max(0, math.floor(bbox[0] * width)), max(0, math.floor(bbox[1] * height)),
        min(width, math.ceil(bbox[2] * width)), min(height, math.ceil(bbox[3] * height)),
    )


def _intersection(a: BBox, b: BBox) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _contains(outer: BBox, inner: BBox) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _union(boxes: list[BBox]) -> BBox:
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _padded(box: BBox, x: float = .012, y: float = .008) -> BBox:
    return max(0., box[0] - x), max(0., box[1] - y), min(1., box[2] + x), min(1., box[3] + y)


@dataclass
class SourceAnalysis:
    width: int
    height: int
    mask: bytes = field(repr=False)
    columns: list[tuple[float, float, float, float]] = field(default_factory=list)
    line_bands: list[InkBand] = field(default_factory=list)
    header_candidates: list[InkBand] = field(default_factory=list)
    footer_candidates: list[InkBand] = field(default_factory=list)
    pdf_text_lines: list[PdfTextLine] = field(default_factory=list)
    page_id: str = ""
    source_version: int = 1
    regions: list[CoarseRegion] = field(default_factory=list)
    reading_order: list[str] = field(default_factory=list)
    crops: list[RecognitionInput] = field(default_factory=list)
    crop_candidates: list[SourceGeometryIssue] = field(default_factory=list)
    source_reasons: list[str] = field(default_factory=list)

    def require_source(self, metadata: PageSourceMetadata) -> None:
        if (self.page_id, self.source_version) != (metadata.page_id, metadata.source_version):
            raise ValueError("来源分析与当前页或来源版本不一致")

    def measure_bbox(self, bbox: BBox, *, equation: bool = False) -> InkBand | None:
        """Measure ink without splitting a fraction, script or equation group."""
        x0, y0, x1, y1 = _pixel_box(bbox, self.width, self.height)
        rows = [sum(self.mask[y * self.width + x0:y * self.width + x1]) for y in range(y0, y1)]
        active = [index for index, count in enumerate(rows) if count]
        if not active or sum(rows) < 4:
            return None
        top, bottom = y0 + active[0], y0 + active[-1] + 1
        occupied: list[tuple[int, int, int]] = []
        for x in range(x0, x1):
            ys = [y for y in range(top, bottom) if self.mask[y * self.width + x]]
            if ys:
                occupied.append((x, ys[0], ys[-1] + 1))
        left, right = occupied[0][0], occupied[-1][0] + 1
        # Most glyph bottoms identify the writing baseline; descenders and
        # sparse superscripts do not determine the whole line's body height.
        bottoms = sorted(item[2] for item in occupied)
        baseline = bottoms[min(len(bottoms) - 1, int(len(bottoms) * .7))]
        heights = [end - start for _, start, end in occupied]
        body_height = statistics.median(heights)
        if equation:
            dense = [index for index, count in enumerate(rows) if count >= max(rows) * .35]
            core_top, core_bottom = y0 + dense[0], y0 + dense[-1] + 1
            baseline = core_top + (core_bottom - core_top) * .78
            body_height = min(body_height, core_bottom - core_top)
        return InkBand(
            (left / self.width, top / self.height, right / self.width, bottom / self.height),
            min(bottom, max(top, baseline)) / self.height,
            max(1, body_height) / self.height, sum(rows),
        )

    def bands_in_bbox(self, bbox: BBox) -> list[InkBand]:
        x0, y0, x1, y1 = _pixel_box(bbox, self.width, self.height)
        minimum = max(2, (x1 - x0) * .002)
        rows = [sum(self.mask[y * self.width + x0:y * self.width + x1]) for y in range(y0, y1)]
        active = [y0 + index for index, count in enumerate(rows) if count >= minimum]
        if not active:
            return []
        max_gap = max(1, round(self.height * .001))
        spans: list[tuple[int, int]] = []
        start = previous = active[0]
        for row in active[1:]:
            if row - previous > max_gap + 1:
                spans.append((start, previous + 1))
                start = row
            previous = row
        spans.append((start, previous + 1))
        bands = []
        for start, end in spans:
            band = self.measure_bbox((x0 / self.width, start / self.height, x1 / self.width, end / self.height))
            if band is not None and band.ink_pixels >= 8:
                bands.append(band)
        return bands

    def region_bands(self, bbox: BBox) -> list[InkBand]:
        """Read full-width headers separately and body columns left to right."""
        if len(self.columns) < 2:
            return self.bands_in_bbox(bbox)
        left, right = self.columns
        gutter = (left[2] + right[0]) / 2
        if not bbox[0] < gutter < bbox[2] or bbox[2] - bbox[0] < .55:
            return self.bands_in_bbox(bbox)
        spanning = [region.bbox for region in self.regions if region.column_id is None
                    and region.kind in {"title", "header", "footer"} and _intersection(region.bbox, bbox)]
        cuts = sorted({bbox[1], bbox[3], *(max(bbox[1], box[1]) for box in spanning),
                       *(min(bbox[3], box[3]) for box in spanning)})
        bands = []
        for top, bottom in zip(cuts, cuts[1:]):
            if top >= bottom:
                continue
            strip = (bbox[0], top, bbox[2], bottom)
            if any(box[1] <= top and box[3] >= bottom for box in spanning):
                bands.extend(self.bands_in_bbox(strip))
            else:
                bands.extend(self.bands_in_bbox((bbox[0], top, gutter, bottom)))
                bands.extend(self.bands_in_bbox((gutter, top, bbox[2], bottom)))
        return bands

    def uncovered_content(self, content: PageContent) -> list[SourceGeometryIssue]:
        """Scan every source row, including outside all coarse/candidate regions.

        These are possible omissions for independent content review. Ink never
        establishes what the text says or promotes a block to a passed status.
        """
        if (content.page_id, content.source_version) != (self.page_id, self.source_version):
            raise ValueError("内容与来源分析版本不一致")
        boxes = []
        for block in content.blocks:
            if isinstance(block, (TextBlock, EquationBlock)):
                boxes.extend(line.bbox for line in block.lines if line.bbox is not None)
            elif isinstance(block, TableBlock):
                boxes.extend(cell.bbox for cell in block.cells if cell.bbox is not None)
            elif isinstance(block, FigureBlock) and block.bbox is not None:
                boxes.append(block.bbox)
        return self._uncovered_ink(boxes)

    def _uncovered_ink(self, boxes: list[BBox]) -> list[SourceGeometryIssue]:
        issues = []
        for band in self.line_bands:
            nearby = [_pixel_box(box, self.width, self.height) for box in boxes if _intersection(box, band.bbox)]
            x0, y0, x1, y1 = _pixel_box(band.bbox, self.width, self.height)
            remaining = 0
            for y in range(y0, y1):
                intervals = sorted((max(x0, left), min(x1, right)) for left, top, right, bottom in nearby
                                   if top <= y < bottom)
                cursor = x0
                for left, right in intervals:
                    if left > cursor:
                        remaining += sum(self.mask[y * self.width + cursor:y * self.width + left])
                    cursor = max(cursor, right)
                remaining += sum(self.mask[y * self.width + cursor:y * self.width + x1])
            if remaining >= max(8, band.ink_pixels * .18):
                region = next((region for region in self.regions if _contains(region.bbox, band.bbox)), None)
                issues.append(SourceGeometryIssue(band.bbox, region.region_id if region else None, (),
                                                 "完整源页存在未对应到内容原行的可见墨迹，需独立核对是否遗漏"))
        return issues

    def uncovered_regions(self, layout: SourceFidelityLayout) -> list[SourceGeometryIssue]:
        """Locate unresolved geometry and source ink beyond positioned content."""
        regions = {region.region_id: region for region in layout.regions}
        issues: list[SourceGeometryIssue] = []
        covered = [line.bbox for line in layout.lines if line.bbox is not None and line.baseline is not None]
        covered.extend(asset.bbox for asset in layout.source_assets)
        lines = {line.line_id: line for line in layout.lines}
        for group in layout.equation_groups:
            requires_anchor = len(group.line_ids) > 1 or any(
                equation_alignment_index(lines[line_id].latex) is not None for line_id in group.line_ids
            )
            if requires_anchor and (group.align_x is None or group.basis not in {"file_metadata", "local_measurement", "manual"}):
                box = group.bbox or regions[lines[group.line_ids[0]].block_id].bbox or (0., 0., 1., 1.)
                reason = "公式组缺少可定位的源图对齐锚点" if group.align_x is None else "公式组对齐锚点尚无源图测量依据"
                issues.append(SourceGeometryIssue(box, lines[group.line_ids[0]].block_id, tuple(group.line_ids), reason))
            if group.number is not None and group.number.bbox is None and group.number.anchor_x is None:
                box = group.bbox or regions[lines[group.line_ids[0]].block_id].bbox or (0., 0., 1., 1.)
                issues.append(SourceGeometryIssue(box, lines[group.line_ids[0]].block_id, tuple(group.line_ids), "公式编号缺少可定位的源图位置"))
            if group.number is not None and group.number.bbox is not None and lines[group.number.line_id].baseline is not None:
                number_band = self.measure_bbox(group.number.bbox)
                if number_band is not None:
                    covered.append(number_band.bbox)
            if group.bbox is not None and all(
                lines[line_id].bbox is not None and lines[line_id].baseline is not None
                for line_id in group.line_ids
            ):
                covered.append(group.bbox)
        for line in layout.lines:
            if line.bbox is None or line.baseline is None:
                box = line.bbox or regions[line.block_id].bbox or (0., 0., 1., 1.)
                issues.append(SourceGeometryIssue(box, line.block_id, (line.line_id,), "原行未取得唯一的源图位置或基线"))
        for band in self.line_bands:
            nearby = [box for box in covered if _intersection(box, band.bbox)]
            pixels = [_pixel_box(box, self.width, self.height) for box in nearby]
            x0, y0, x1, y1 = _pixel_box(band.bbox, self.width, self.height)
            remaining = 0
            for y in range(y0, y1):
                intervals = sorted((max(x0, left), min(x1, right)) for left, top, right, bottom in pixels if top <= y < bottom)
                cursor = x0
                for left, right in intervals:
                    if left > cursor:
                        remaining += sum(self.mask[y * self.width + cursor:y * self.width + left])
                    cursor = max(cursor, right)
                remaining += sum(self.mask[y * self.width + cursor:y * self.width + x1])
            if remaining < max(8, band.ink_pixels * .18):
                continue
            center = ((band.bbox[0] + band.bbox[2]) / 2, (band.bbox[1] + band.bbox[3]) / 2)
            containing = [region for region in layout.regions if region.bbox is not None
                          and region.bbox[0] <= center[0] <= region.bbox[2]
                          and region.bbox[1] <= center[1] <= region.bbox[3]]
            region = min(containing, key=lambda item: (item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1]), default=None)
            issues.append(SourceGeometryIssue(
                band.bbox, region.region_id if region else None, (), "源图行带存在未被已定位原行或保留资源覆盖的墨迹",
            ))
        return issues


def _ink_threshold(image: Image.Image) -> int:
    histogram = image.histogram()
    total, weighted = sum(histogram), sum(index * count for index, count in enumerate(histogram))
    count = running = 0
    best, threshold = -1., 160
    for index, frequency in enumerate(histogram):
        count += frequency
        running += index * frequency
        if count == 0 or count == total:
            continue
        variance = count * (total - count) * (running / count - (weighted - running) / (total - count)) ** 2
        if variance > best:
            best, threshold = variance, index
    return min(220, max(90, threshold + 20))


def _columns(analysis: SourceAnalysis) -> list[tuple[float, float, float, float]]:
    width, height = analysis.width, analysis.height
    top, bottom = round(height * .12), round(height * .88)
    counts = [sum(analysis.mask[y * width + x] for y in range(top, bottom)) for x in range(width)]
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(round(width * .28), round(width * .72)):
        if counts[x] <= max(1, (bottom - top) * .012):
            if start is None:
                start = x
        elif start is not None:
            spans.append((start, x))
            start = None
    if start is not None:
        spans.append((start, round(width * .72)))
    spans = [(left, right) for left, right in spans if right - left >= width * .018
             and sum(counts[:left]) > width * 2 and sum(counts[right:]) > width * 2]
    if not spans:
        return [(0., 0., 1., 1.)]
    left, right = max(spans, key=lambda span: span[1] - span[0])
    # A wide page border or a sparse illustration is not a column gutter.
    before = analysis.bands_in_bbox((0., .12, left / width, .88))
    after = analysis.bands_in_bbox((right / width, .12, 1., .88))
    if min(len(before), len(after)) < 5:
        return [(0., 0., 1., 1.)]
    return [(0., 0., left / width, 1.), (right / width, 0., 1., 1.)]


def _band_groups(bands: list[InkBand]) -> list[list[InkBand]]:
    if not bands:
        return []
    heights = [band.bbox[3] - band.bbox[1] for band in bands]
    typical = statistics.median(heights)
    gaps = [b.bbox[1] - a.bbox[3] for a, b in zip(bands, bands[1:]) if b.bbox[1] > a.bbox[3]]
    gap = max(typical * 1.2, statistics.median(gaps) * 2 if gaps else typical * 1.2)
    groups = [[bands[0]]]
    for band in bands[1:]:
        previous = groups[-1][-1]
        if band.bbox[1] - previous.bbox[3] > gap:
            groups.append([])
        groups[-1].append(band)
    return groups


def _coarse_regions(analysis: SourceAnalysis) -> list[CoarseRegion]:
    """Observe columns and complete vertical groups without assigning truth."""
    regions = []
    full = analysis.line_bands
    typical = statistics.median([band.bbox[3] - band.bbox[1] for band in full]) if full else .015
    spanning = []
    if len(analysis.columns) > 1:
        gutter_left, gutter_right = analysis.columns[0][2], analysis.columns[1][0]
        for band in full:
            if not (band.bbox[0] < gutter_left and band.bbox[2] > gutter_right):
                continue
            x0, y0, x1, y1 = _pixel_box((gutter_left, band.bbox[1], gutter_right, band.bbox[3]), analysis.width, analysis.height)
            central_ink = sum(sum(analysis.mask[y * analysis.width + x0:y * analysis.width + x1]) for y in range(y0, y1))
            if central_ink >= max(8, (x1 - x0) * .8):
                spanning.append(band)
    for group in _band_groups(spanning):
        box = _union([band.bbox for band in group])
        kind = "header" if box[3] < .08 else "footer" if box[1] > .92 else "title"
        regions.append(CoarseRegion(region_id=f"region-{len(regions) + 1}", kind=kind, bbox=box, order=0,
                                    reasons=["墨迹跨越候选栏间隙；跨栏关系是来源观察，标题身份仍需内容确认"]))
    spanning_boxes = [region.bbox for region in regions]
    dividers = sorted({coordinate for box in spanning_boxes for coordinate in (box[1], box[3])})
    for column_index, column in enumerate(analysis.columns):
        bands = analysis.bands_in_bbox(column)
        bands = [band for band in bands if not any(_intersection(band.bbox, box) for box in spanning_boxes)]
        sections: dict[int, list[InkBand]] = {}
        for band in bands:
            sections.setdefault(sum(band.bbox[1] >= boundary for boundary in dividers), []).append(band)
        groups = [group for section in sections.values() for group in _band_groups(section)]
        for group in groups:
            box = _union([band.bbox for band in group])
            kind = "header" if box[3] < .08 else "footer" if box[1] > .92 else "text"
            tall = any(band.bbox[3] - band.bbox[1] > typical * 2.3 for band in group)
            if tall:
                kind = "unknown"
            reasons = ["按完整源页墨迹及可见行间空隙观察区域；不是已识别或已覆盖声明"]
            if tall:
                reasons.append("高行带可能含公式、表格或图形，类型和内部边界未知，不逐行机械裁切")
            regions.append(CoarseRegion(region_id=f"region-{len(regions) + 1}", kind=kind, bbox=box, order=0,
                                        column_id=f"column-{column_index + 1}" if len(analysis.columns) > 1 else None,
                                        reasons=reasons))
    # Spanning rows divide the page into reading sections; within each section
    # left column precedes right column, regardless of recognition completion.
    def key(region: CoarseRegion) -> tuple:
        section = sum(region.bbox[1] >= boundary for boundary in dividers)
        column = int(region.column_id.rsplit("-", 1)[1]) if region.column_id else 0
        return section, column, region.bbox[1], region.bbox[0]
    regions.sort(key=key)
    for order, region in enumerate(regions):
        region.order = order
    return regions


def _pdf_text_lines(image_path: Path, metadata: PageSourceMetadata) -> list[PdfTextLine]:
    if metadata.source_kind != "pdf":
        return []
    transform = inverse_transform(metadata.canonical_to_source_affine)
    width, height = metadata.canonical_width_px, metadata.canonical_height_px
    result = []
    with IMAGE_LOCK, fitz.open(image_path.parent / "source.pdf") as document:
        page = document[metadata.source_page - 1]
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [span for span in line["spans"] if span["text"].strip()]
                if not spans:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                points = [transform_point(transform, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                box = (max(0., min(x for x, _ in points) / width), max(0., min(y for _, y in points) / height),
                       min(1., max(x for x, _ in points) / width), min(1., max(y for _, y in points) / height))
                if box[0] >= box[2] or box[1] >= box[3]:
                    continue
                span = max(spans, key=lambda item: len(item["text"]))
                _, baseline = transform_point(transform, *span["origin"])
                result.append(PdfTextLine(
                    "".join(item["text"] for item in spans), box, min(box[3], max(box[1], baseline / height)),
                    span["size"], span["font"], bool(span["flags"] & 16), bool(span["flags"] & 2),
                ))
    return result


def _analysis_paths(book_dir: Path, metadata: PageSourceMetadata) -> tuple[Path, Path]:
    key = "".join(char if char.isalnum() or char in "_-" else "_" for char in metadata.page_id)
    directory = book_dir / "source-analysis" / f"{key or metadata.page_number}-v{metadata.source_version}"
    return directory / "analysis.json", directory / "ink.mask"


def _save_analysis(book_dir: Path, metadata: PageSourceMetadata, analysis: SourceAnalysis) -> None:
    path, mask_path = _analysis_paths(book_dir, metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 2, "source": metadata.model_dump(mode="json"),
        "width": analysis.width, "height": analysis.height, "columns": analysis.columns,
        "line_bands": [asdict(item) for item in analysis.line_bands],
        "header_candidates": [asdict(item) for item in analysis.header_candidates],
        "footer_candidates": [asdict(item) for item in analysis.footer_candidates],
        "pdf_text_lines": [asdict(item) for item in analysis.pdf_text_lines],
        "regions": [item.model_dump(mode="json") for item in analysis.regions], "reading_order": analysis.reading_order,
        "crops": [item.model_dump(mode="json") for item in analysis.crops],
        "crop_candidates": [asdict(item) for item in analysis.crop_candidates], "source_reasons": analysis.source_reasons,
    }
    temporary_mask = mask_path.with_name(f"{uuid4().hex}.mask.tmp")
    temporary_json = path.with_name(f"{uuid4().hex}.json.tmp")
    temporary_mask.write_bytes(analysis.mask)
    temporary_json.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary_mask.replace(mask_path)
    temporary_json.replace(path)


def _load_analysis(book_dir: Path, metadata: PageSourceMetadata) -> tuple[SourceAnalysis | None, str | None]:
    path, mask_path = _analysis_paths(book_dir, metadata)
    if not path.is_file():
        return None, "同来源版本尚无已保存的分析资产；按当前完整源页分析一次"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 2 or value.get("source") != metadata.model_dump(mode="json"):
            return None, "已保存分析与当前来源版本或几何不一致，重新取得当前源页证据"
        mask = mask_path.read_bytes()
        if len(mask) != value["width"] * value["height"]:
            return None, "来源分析墨迹资产不完整，重新取得当前源页证据"
        analysis = SourceAnalysis(value["width"], value["height"], mask, page_id=metadata.page_id,
                                  source_version=metadata.source_version,
                                  columns=[tuple(box) for box in value["columns"]],
                                  line_bands=[InkBand(**item) for item in value["line_bands"]],
                                  header_candidates=[InkBand(**item) for item in value["header_candidates"]],
                                  footer_candidates=[InkBand(**item) for item in value["footer_candidates"]],
                                  pdf_text_lines=[PdfTextLine(**item) for item in value["pdf_text_lines"]],
                                  regions=[CoarseRegion.model_validate(item) for item in value["regions"]],
                                  reading_order=value["reading_order"],
                                  crops=[RecognitionInput.model_validate(item) for item in value["crops"]],
                                  crop_candidates=[SourceGeometryIssue(**item) for item in value["crop_candidates"]],
                                  source_reasons=value["source_reasons"])
        return analysis, None
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, "来源分析资产不可读取，重新取得当前源页证据：" + str(error)[:250]


def analyze_source(
    image_path: Path, metadata: PageSourceMetadata, *, book_dir: Path | None = None, max_crops: int = 3,
) -> SourceAnalysis:
    """Analyze at most a 1400-pixel edge; PDF text remains corroborating evidence."""
    cache_reason = None
    if book_dir is not None:
        cached, cache_reason = _load_analysis(book_dir, metadata)
        if cached is not None:
            prepare_recognition_inputs(image_path, metadata, cached, book_dir=book_dir, max_crops=max_crops)
            return cached
    with Image.open(image_path) as source:
        gray = ImageOps.grayscale(source)
        gray.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        threshold = _ink_threshold(gray)
        mask = bytes(1 if value <= threshold else 0 for value in gray.tobytes())
        width, height = gray.size
    analysis = SourceAnalysis(width, height, mask, page_id=metadata.page_id, source_version=metadata.source_version)
    if cache_reason:
        analysis.source_reasons.append(cache_reason)
    analysis.columns = _columns(analysis)
    analysis.header_candidates = analysis.bands_in_bbox((0., 0., 1., .1))
    analysis.footer_candidates = analysis.bands_in_bbox((0., .9, 1., 1.))
    # Full-width scan is deliberately independent of the coarse region list.
    analysis.line_bands = analysis.bands_in_bbox((0., 0., 1., 1.))
    analysis.regions = _coarse_regions(analysis)
    analysis.reading_order = [region.region_id for region in analysis.regions]
    try:
        evidence = _pdf_text_lines(image_path, metadata)
    except (OSError, RuntimeError, ValueError) as error:
        evidence = []
        analysis.source_reasons.append("PDF 辅助文字证据不可用，仍使用完整源图：" + str(error)[:250])
    for item in evidence:
        visible = analysis.measure_bbox(item.bbox) is not None
        analysis.pdf_text_lines.append(PdfTextLine(item.text, item.bbox, item.baseline, item.font_size_bp,
                                                  item.font_name, item.bold, item.italic, visible))
    for region in analysis.regions:
        bands = analysis.bands_in_bbox(region.bbox)
        heights = [band.bbox[3] - band.bbox[1] for band in bands]
        small = bool(heights) and statistics.median(heights) * min(metadata.canonical_height_px, 1800) < 13
        dense = len(bands) >= 35 and region.bbox[3] - region.bbox[1] > .4
        complex_region = region.kind == "unknown"
        if small or dense or complex_region or len(analysis.columns) > 1:
            reasons = [reason for active, reason in ((small, "概览中候选文字较小"), (dense, "来源行带密集"),
                       (complex_region, "可能含复杂公式、表格或图形，保留完整区域"),
                       (len(analysis.columns) > 1, "多栏来源需要局部细节")) if active]
            analysis.crop_candidates.append(SourceGeometryIssue(region.bbox, region.region_id, (), "；".join(reasons)))
    if not analysis.regions and any(mask):
        analysis.source_reasons.append("粗区域关系未知；保留整页输入，不阻断内容识别")
    if book_dir is not None:
        prepare_recognition_inputs(image_path, metadata, analysis, book_dir=book_dir, max_crops=max_crops)
        try:
            _save_analysis(book_dir, metadata, analysis)
        except OSError as error:
            analysis.source_reasons.append("来源分析已取得但缓存保存失败：" + str(error)[:250])
    return analysis


def map_crop_bbox(bbox: BBox, mapping: CropMapping) -> BBox:
    """Map normalized crop edges back to normalized canonical page edges."""
    points = [transform_point(mapping.crop_to_canonical_affine, x, y)
              for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3]))]
    box = (max(0., min(x for x, _ in points)), max(0., min(y for _, y in points)),
           min(1., max(x for x, _ in points)), min(1., max(y for _, y in points)))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError("裁切返回的位置没有有效源区域")
    return box


def _complete_crop_box(target: BBox, analysis: SourceAnalysis) -> BBox | None:
    """Include whole observed groups, source rows, numbering and nearby context."""
    regions = [region for region in analysis.regions if _intersection(region.bbox, target)]
    if not regions:
        analysis.source_reasons.append("目标区域没有可确认的完整内容边界，继续提供整页；公式或表格关联边界未知")
        return None
    box = _union([target, *(region.bbox for region in regions)])
    complete_bands = analysis.region_bands((0., 0., 1., 1.))
    # Fixed-point expansion includes crossing source lines rather than cutting
    # a numerator, equation number, table heading or a cross-column title.
    for _ in range(3):
        crossing = [band.bbox for band in complete_bands if _intersection(band.bbox, box)
                    and not _contains(box, band.bbox)]
        crossing.extend(item.bbox for item in analysis.pdf_text_lines if item.visible_ink
                        and _intersection(item.bbox, box) and not _contains(box, item.bbox))
        if not crossing:
            break
        box = _union([box, *crossing])
    if any(_intersection(band.bbox, box) and not _contains(box, band.bbox) for band in complete_bands):
        analysis.source_reasons.append("来源关联区域不能有界地完整裁切，使用整页输入；边界未知")
        return None
    padding_y = max(.008, statistics.median([band.bbox[3] - band.bbox[1] for band in analysis.line_bands])
                    if analysis.line_bands else .008)
    return _padded(box, .015, padding_y)


def prepare_recognition_inputs(
    image_path: Path, metadata: PageSourceMetadata, analysis: SourceAnalysis, *,
    book_dir: Path, max_crops: int = 3, target_bbox: BBox | None = None,
) -> list[RecognitionInput]:
    """Overview plus at most three boundary-aware crops from original assets.

    Crop affines map normalized crop coordinates to normalized canonical
    coordinates. No overview is enlarged, no mechanical grid is used, and this
    function reuses source analysis rather than performing another analysis.
    """
    analysis.require_source(metadata)
    overview = RecognitionInput(image_path=str(image_path), kind="overview")
    limit = max(0, min(3, max_crops))
    if not limit:
        return [overview]
    candidates = ([SourceGeometryIssue(target_bbox, None, (), "独立局部重读的完整目标及周边")]
                  if target_bbox is not None else analysis.crop_candidates)
    boxes: list[tuple[BBox, str]] = []
    for candidate in candidates:
        box = _complete_crop_box(candidate.bbox, analysis)
        if box is None or any(_contains(existing, box) for existing, _ in boxes):
            continue
        boxes = [(existing, reason) for existing, reason in boxes if not _contains(box, existing)]
        boxes.append((box, candidate.reason))
        if len(boxes) >= limit:
            break
    if not boxes:
        return [overview]
    inputs = []
    pending = []
    for box, reason in boxes:
        existing = next((item for item in analysis.crops if item.mapping is not None
                         and all(abs(a - b) <= .002 for a, b in zip(item.mapping.bbox, box))
                         and _contains(item.mapping.bbox, box)
                         and Path(item.image_path).is_file()), None)
        if existing is not None:
            inputs.append(existing)
        else:
            pending.append((box, reason))
    try:
        source = original_source_image(image_path, metadata) if pending else None
    except (OSError, RuntimeError, ValueError) as error:
        analysis.source_reasons.append("原始高清资产不可读取，保留整页输入：" + str(error)[:250])
        return [overview, *inputs]
    total_pixels = sum(item.mapping.width_px * item.mapping.height_px for item in inputs if item.mapping is not None)
    try:
        for box, reason in pending:
            pixels = _pixel_box(box, *source.size)
            width, height = pixels[2] - pixels[0], pixels[3] - pixels[1]
            if width < 2 or height < 2 or total_pixels + width * height > 24_000_000:
                analysis.source_reasons.append("高清区域超过本次输入像素界限，保留完整概览；不放大或切断内容组")
                continue
            stored_box = (pixels[0] / source.width, pixels[1] / source.height,
                          pixels[2] / source.width, pixels[3] / source.height)
            crop_id = uuid4().hex
            page_key = "".join(char if char.isalnum() or char in "_-" else "_" for char in metadata.page_id)
            relative = Path("recognition-inputs") / f"{page_key or metadata.page_number}-v{metadata.source_version}" / f"{crop_id}.png"
            target = book_dir / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.crop(pixels) as crop:
                    crop.save(target, format="PNG")
            except OSError as error:
                analysis.source_reasons.append("局部输入保存失败，仍保留整页：" + str(error)[:250])
                continue
            x0, y0, x1, y1 = stored_box
            mapping = CropMapping(crop_id=crop_id, page_id=metadata.page_id, source_version=metadata.source_version,
                                  bbox=stored_box, width_px=width, height_px=height,
                                  crop_to_canonical_affine=(x1 - x0, 0., 0., y1 - y0, x0, y0),
                                  source_region_ids=[region.region_id for region in analysis.regions
                                                     if _intersection(region.bbox, stored_box)][:100], reason=reason[:2_000])
            item = RecognitionInput(image_path=str(target), kind="crop", mapping=mapping)
            analysis.crops.append(item)
            inputs.append(item)
            total_pixels += width * height
    finally:
        if source is not None:
            source.close()
    if pending and _analysis_paths(book_dir, metadata)[0].is_file():
        try:
            _save_analysis(book_dir, metadata, analysis)
        except OSError as error:
            analysis.source_reasons.append("新增高清映射已取得但缓存保存失败：" + str(error)[:250])
    return [overview, *inputs[:limit]]


def _content_key(block) -> str:
    """Exact visible content and structure, with program IDs/geometry excluded."""
    def line_key(line) -> dict:
        return line.model_dump(exclude={"line_id", "bbox"})
    if isinstance(block, (TextBlock, EquationBlock)):
        value = {"type": block.type, "role": block.role, "lines": [line_key(line) for line in block.lines]}
    elif isinstance(block, TableBlock):
        value = {"type": block.type, "role": block.role, "rows": block.rows, "columns": block.columns,
                 "cells": [{**cell.model_dump(exclude={"cell_id", "bbox", "lines"}),
                            "lines": [line_key(line) for line in cell.lines]} for cell in block.cells]}
    else:
        value = {"type": block.type, "role": block.role, "description": block.description}
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _same_position(a: BBox | None, b: BBox | None) -> bool:
    if a is None or b is None:
        return False
    intersection = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union >= .82 if union else False


def _content_position(block) -> BBox | None:
    if block.bbox is not None:
        return block.bbox
    boxes = [line.bbox for line in getattr(block, "lines", []) if line.bbox is not None]
    boxes.extend(cell.bbox for cell in getattr(block, "cells", []) if cell.bbox is not None)
    return _union(boxes) if boxes else None


def _same_source_item(a, b) -> bool:
    if not _same_position(_content_position(a), _content_position(b)):
        return False
    a_lines = [*getattr(a, "lines", []), *(line for cell in getattr(a, "cells", []) for line in cell.lines)]
    b_lines = [*getattr(b, "lines", []), *(line for cell in getattr(b, "cells", []) for line in cell.lines)]
    if len(a_lines) != len(b_lines):
        return False
    for first, second in zip(a_lines, b_lines):
        if first.bbox is not None or second.bbox is not None:
            if not _same_position(first.bbox, second.bbox):
                return False
    return True


def source_reading_order(content: PageContent, analysis: SourceAnalysis) -> list[str]:
    """Return source-supported block order; unlocated blocks retain their slots."""
    if (content.page_id, content.source_version) != (analysis.page_id, analysis.source_version):
        raise ValueError("阅读顺序与来源版本不一致")
    known = []
    unknown = {}
    for index, block in enumerate(content.blocks):
        box = _content_position(block)
        regions = [region for region in analysis.regions if region.region_id == block.source_region_id]
        if not regions and box is not None:
            area = (box[2] - box[0]) * (box[3] - box[1])
            regions = [region for region in analysis.regions
                       if max(0., min(region.bbox[2], box[2]) - max(region.bbox[0], box[0]))
                       * max(0., min(region.bbox[3], box[3]) - max(region.bbox[1], box[1])) / area >= .6]
        if box is None or len(regions) != 1:
            unknown[index] = block.block_id
            continue
        region = regions[0]
        known.append((region.order, box[1], box[0], index, block.block_id))
    ordered = iter(item[-1] for item in sorted(known))
    return [unknown[index] if index in unknown else next(ordered) for index in range(len(content.blocks))]


def merge_region_content(
    base: PageContent, local: PageContent, *, mapping: CropMapping | None = None, target_bbox: BBox | None = None,
) -> PageContent:
    """Keep unrelated blocks and conflicts; deduplicate only the same source item.

    Both inputs use canonical coordinates when mapping is None (including a
    local result already parsed with crop_mapping). Pass mapping only for a
    result whose coordinates are still normalized to the crop.
    """
    if (base.book_id, base.page_id, base.source_version) != (local.book_id, local.page_id, local.source_version):
        raise ValueError("局部内容必须来自同一书籍、页面和来源版本")
    if mapping is not None and (mapping.page_id, mapping.source_version) != (base.page_id, base.source_version):
        raise ValueError("局部映射与内容来源不一致")
    merged = base.model_copy(deep=True)
    merged.content_revision_id = str(uuid4())
    local_copy = local.model_copy(deep=True)
    remapped_ids = {}
    added_ids = set()
    used_line_ids = {line.line_id for old in merged.blocks for line in getattr(old, "lines", [])}
    used_line_ids.update(line.line_id for old in merged.blocks for cell in getattr(old, "cells", []) for line in cell.lines)
    for block in local_copy.blocks:
        if mapping is not None:
            if block.crop_id == mapping.crop_id:
                raise ValueError("局部内容已经映射到规范页，禁止重复坐标转换")
            block.bbox = map_crop_bbox(block.bbox, mapping) if block.bbox is not None else None
            block.crop_id = mapping.crop_id
            for line in getattr(block, "lines", []):
                line.bbox = map_crop_bbox(line.bbox, mapping) if line.bbox is not None else None
            for cell in getattr(block, "cells", []):
                cell.bbox = map_crop_bbox(cell.bbox, mapping) if cell.bbox is not None else None
                for line in cell.lines:
                    line.bbox = map_crop_bbox(line.bbox, mapping) if line.bbox is not None else None
        position = _content_position(block)
        duplicates = [old for old in merged.blocks if _same_source_item(old, block)
                      and _content_key(old) == _content_key(block)]
        if duplicates:
            remapped_ids[block.block_id] = duplicates[0].block_id
            continue
        if any(old.block_id == block.block_id for old in merged.blocks):
            old_id = block.block_id
            block.block_id = str(uuid4())
            remapped_ids[old_id] = block.block_id
        for line in [*getattr(block, "lines", []), *(line for cell in getattr(block, "cells", []) for line in cell.lines)]:
            if line.line_id in used_line_ids:
                line.line_id = str(uuid4())
            used_line_ids.add(line.line_id)
        overlaps = [old for old in merged.blocks if _same_position(_content_position(old), position)]
        if overlaps:
            reason = "同一源位置的独立候选内容冲突；已保留双方内容，需源图复核后才能采用替换"
            block.review_status = "uncertain"
            block.unresolved_reasons = [*block.unresolved_reasons, reason][:100]
            for old in overlaps:
                old.review_status = "uncertain"
                old.unresolved_reasons = [*old.unresolved_reasons, reason][:100]
            merged.issues.append(ContentIssue(category="invalid_structure", reason=reason, block_id=block.block_id,
                                               source_bbox=position, response_id=block.response_id))
        if target_bbox is not None and (position is None or not _intersection(position, target_bbox)):
            merged.issues.append(ContentIssue(category="layout", reason="局部返回内容未能唯一对应目标区域，保留候选并标记未知",
                                               block_id=block.block_id, source_bbox=position))
        merged.blocks.append(block)
        added_ids.add(block.block_id)
    for issue in local_copy.issues:
        issue.block_id = remapped_ids.get(issue.block_id, issue.block_id)
        if mapping is not None and issue.source_bbox is not None:
            issue.source_bbox = map_crop_bbox(issue.source_bbox, mapping)
        if not any(old.issue_id == issue.issue_id for old in merged.issues):
            merged.issues.append(issue)
    merged.response_ids = list(dict.fromkeys([*base.response_ids, *local.response_ids]))
    merged.blank = base.blank and local.blank and not merged.blocks
    for block in merged.blocks:
        block.content_revision_id = merged.content_revision_id
        if isinstance(block, FigureBlock) and block.block_id in added_ids:
            block.caption_block_ids = [remapped_ids.get(block_id, block_id) for block_id in block.caption_block_ids]
    return PageContent.model_validate(merged.model_dump())


def persist_source_region(
    book_dir: Path, image_path: Path, metadata: PageSourceMetadata,
    bbox: BBox, region_id: str | None, reason: str, *,
    purpose: Literal["figure", "uncertain_content", "source_page"] | None = None,
) -> SourceRegionAsset:
    """Save a version-associated source crop without modifying any original asset."""
    asset_id = uuid4().hex
    page_key = metadata.page_id or f"page-{metadata.page_number:04d}"
    page_key = "".join(character if character.isalnum() or character in "_-" else "_" for character in page_key)
    relative = Path("source-assets") / f"{page_key}-v{metadata.source_version}" / f"{asset_id}.png"
    target = book_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with original_source_image(image_path, metadata) as image:
        box = _pixel_box(bbox, *image.size)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("保留的源区域没有有效像素")
        image.crop(box).save(target, format="PNG")
        stored_bbox = (box[0] / image.width, box[1] / image.height, box[2] / image.width, box[3] / image.height)
    return SourceRegionAsset(
        asset_id=asset_id, region_id=region_id, bbox=stored_bbox, image_name=relative.as_posix(),
        purpose=purpose or ("source_page" if bbox == (0., 0., 1., 1.) else "uncertain_content"), reason=reason[:2_000],
    )
