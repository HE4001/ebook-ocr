"""Bounded local source evidence; observations never replace transcribed content."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pymupdf as fitz
from PIL import Image, ImageOps

from .importers import IMAGE_LOCK
from .layout_contract import (
    BBox, PageSourceMetadata, SourceFidelityLayout, SourceRegionAsset,
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
        if len(self.columns) < 2 or bbox[1] >= .9 or bbox[3] <= .1:
            return self.bands_in_bbox(bbox)
        left, right = self.columns
        gutter = (left[2] + right[0]) / 2
        if not bbox[0] < gutter < bbox[2] or bbox[2] - bbox[0] < .55:
            return self.bands_in_bbox(bbox)
        bands: list[InkBand] = []
        if bbox[1] < .1:
            bands.extend(self.bands_in_bbox((bbox[0], bbox[1], bbox[2], min(.1, bbox[3]))))
        body_top, body_bottom = max(.1, bbox[1]), min(.9, bbox[3])
        if body_top < body_bottom:
            bands.extend(self.bands_in_bbox((bbox[0], body_top, gutter, body_bottom)))
            bands.extend(self.bands_in_bbox((gutter, body_top, bbox[2], body_bottom)))
        if bbox[3] > .9:
            bands.extend(self.bands_in_bbox((bbox[0], max(.9, bbox[1]), bbox[2], bbox[3])))
        return bands

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
        return [(0., .1, 1., .9)]
    left, right = max(spans, key=lambda span: span[1] - span[0])
    return [(0., .1, left / width, .9), (right / width, .1, 1., .9)]


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


def analyze_source(image_path: Path, metadata: PageSourceMetadata) -> SourceAnalysis:
    """Analyze at most a 1400-pixel edge; PDF text remains corroborating evidence."""
    with Image.open(image_path) as source:
        gray = ImageOps.grayscale(source)
        gray.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        threshold = _ink_threshold(gray)
        mask = bytes(1 if value <= threshold else 0 for value in gray.tobytes())
        width, height = gray.size
    analysis = SourceAnalysis(width, height, mask)
    analysis.columns = _columns(analysis)
    analysis.header_candidates = analysis.bands_in_bbox((0., 0., 1., .1))
    analysis.footer_candidates = analysis.bands_in_bbox((0., .9, 1., 1.))
    analysis.line_bands = analysis.region_bands((0., 0., 1., 1.))
    analysis.pdf_text_lines = _pdf_text_lines(image_path, metadata)
    return analysis


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
    with Image.open(image_path) as image:
        box = _pixel_box(bbox, *image.size)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("保留的源区域没有有效像素")
        image.crop(box).save(target, format="PNG")
        stored_bbox = (box[0] / image.width, box[1] / image.height, box[2] / image.width, box[3] / image.height)
    return SourceRegionAsset(
        asset_id=asset_id, region_id=region_id, bbox=stored_bbox, image_name=relative.as_posix(),
        purpose=purpose or ("source_page" if bbox == (0., 0., 1., 1.) else "uncertain_content"), reason=reason[:2_000],
    )
