from __future__ import annotations

import re
import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .fidelity_rendering import (
    MEASUREMENT_VERSION, FidelityItem, canvas_dimensions, fidelity_items, line_font_size,
)
from .latex_export import LatexDocument
from .layout_contract import BoxBp, SourceFidelityLayout, normalized_bbox_to_bp, tex_pt_to_bp
from .models import Page, QualityStatus, RenderDiagnostic


DIAGNOSTICS_VERSION = "layout-diagnostics-v2"

_OVERFULL = re.compile(
    r"Overfull \\([hv])box \((\d+(?:\.\d+)?)pt too (?:wide|high)\)([^\n]*)"
)
_SOURCE_LINE = re.compile(r"at lines?\s+(\d+)")


def layout_warnings(log_path: Path, source: str, page_order: list[int]) -> list[str]:
    """Map final XeLaTeX overfull warnings to the source pages' arranged positions."""
    if not log_path.is_file():
        return []

    page_starts = [
        line_number
        for line_number, line in enumerate(source.splitlines(), 1)
        if line.startswith(r"\EbookPage{")
    ]
    pages = list(zip(page_starts, page_order))
    warnings: list[str] = []
    log = log_path.read_text(encoding="utf-8", errors="replace")
    for match in _OVERFULL.finditer(log):
        line_match = _SOURCE_LINE.search(match.group(3))
        page_number = page_order[0] if len(page_order) == 1 else None
        if line_match:
            source_line = int(line_match.group(1))
            page_number = next(
                (number for start, number in reversed(pages) if start <= source_line),
                page_number,
            )
        location = f"编排第 {page_number} 页：" if page_number is not None else ""
        dimension = "宽度" if match.group(1) == "h" else "高度"
        warning = f"{location}内容超出可用{dimension} {float(match.group(2)):.2f} pt。"
        if warning not in warnings:
            warnings.append(warning)
    return warnings


@dataclass(frozen=True)
class NaturalMeasurement:
    natural_width_bp: float
    height_bp: float
    depth_bp: float
    anchor_prefix_bp: float


def read_measurements(path: Path) -> dict[str, NaturalMeasurement]:
    """Read the measurement-v2 CSV sidecar in unscaled canvas bp."""
    with path.open(encoding="utf-8", newline="") as stream:
        if stream.readline().strip() != MEASUREMENT_VERSION:
            raise ValueError("自然尺寸记录版本不匹配")
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["token", "natural_width_bp", "height_bp", "depth_bp", "anchor_prefix_bp"]:
            raise ValueError("自然尺寸记录字段不完整")
        measurements = {}
        for row in reader:
            token = row["token"]
            if token in measurements:
                raise ValueError("自然尺寸记录包含重复原行")
            try:
                dimensions = [float(row[name]) for name in reader.fieldnames[1:]]
            except (TypeError, ValueError) as error:
                raise ValueError("自然尺寸记录含无效数值") from error
            if any(not math.isfinite(value) or value < 0 for value in dimensions):
                raise ValueError("自然尺寸记录含非法尺寸")
            measurements[token] = NaturalMeasurement(*dimensions)
    return measurements


def measured_item_bbox(item: FidelityItem, measurement: NaturalMeasurement) -> BoxBp:
    x = item.anchor_x_bp
    if item.alignment == "anchor":
        x -= measurement.anchor_prefix_bp
    elif item.alignment == "right":
        x -= measurement.natural_width_bp
    return (
        x, item.baseline_bp - measurement.height_bp,
        x + measurement.natural_width_bp, item.baseline_bp + measurement.depth_bp,
    )


def _outside(box: BoxBp, boundary: BoxBp) -> float:
    return max(boundary[0] - box[0], boundary[1] - box[1], box[2] - boundary[2], box[3] - boundary[3], 0)


def diagnose_document(
    document: LatexDocument, page: Page, output_dir: Path, *, book_id: str,
    arrangement_position: int, output_page_start: int | None = None,
    output_page_end: int | None = None,
) -> list[RenderDiagnostic]:
    """Combine compiler evidence and measured line boxes; PDF checks belong to M5."""
    diagnostics = []
    identity = dict(
        book_id=book_id, page_number=page.number, source_id=page.source_id,
        source_page=page.source_page, arrangement_position=arrangement_position,
        output_page_start=output_page_start, output_page_end=output_page_end,
        content_revision=page.content_revision, layout_revision=page.layout_revision,
    )

    def add(code: str, message: str, suggestion: str, *, severity: str = "info",
            basis: str = "generator", coverage: str = "complete", item: FidelityItem | None = None,
            box: BoxBp | None = None, overflow: float | None = None) -> None:
        diagnostics.append(RenderDiagnostic(
            **identity, code=code, severity=severity, message=message, suggestion=suggestion,
            basis=basis, coverage=coverage,
            line_id=item.line.line_id if item else None,
            block_id=item.line.block_id if item else None,
            source_bbox=item.source_bbox if item else None,
            output_bbox_bp=box, overflow_bp=overflow,
        ))

    log_path = output_dir / "compiled.log"
    if log_path.is_file():
        log = log_path.read_text(encoding="utf-8", errors="replace")
        for message in dict.fromkeys(re.findall(r"Missing character:[^\r\n]+", log)):
            add("MISSING_GLYPH", f"编译器报告缺字：{message}", "检查该字及字体，选择包含所需字符的字体后重新编译。",
                severity="error", basis="compiler_log", coverage="partial")
        font_warnings = re.findall(r"(?:LaTeX Font Warning:|Package fontspec Warning:)(.*?)(?=\n\s*\n|\Z)", log, re.S)
        for message in dict.fromkeys(" ".join(warning.split()) for warning in font_warnings):
            if re.search(r"substitut|undefined|not available|instead|replac", message, re.I):
                add("FONT_SUBSTITUTION", f"字体或字形被替换：{message}", "核对字体、字重及数学字形，修正字体设置后重新编译。",
                    severity="warning", basis="compiler_log", coverage="partial")
        for match in _OVERFULL.finditer(log):
            dimension = "宽度" if match.group(1) == "h" else "高度"
            overflow = tex_pt_to_bp(float(match.group(2)))
            add("CONTENT_OUTSIDE_FRAME", f"编译器报告内容超出可用{dimension} {overflow:.2f} bp。",
                "核对原行断点、公式结构与字体尺寸；不要删字或压缩单行。",
                severity="warning", basis="compiler_log", coverage="partial", overflow=overflow)
    else:
        add("LAYOUT_UNVERIFIED", "缺少当前编译日志，无法检查缺字与字体替换。", "重新编译当前修订以生成诊断记录。", coverage="none")

    if document.render_strategy != "source_fidelity":
        add("LAYOUT_UNVERIFIED", "当前源码由模板或自定义文档控制，生成器未覆盖其全部自然尺寸。",
            "系统保留现有源码，并将其自然尺寸覆盖记录为未完成。", coverage="partial")
        return diagnostics
    layout = page.layout_source
    if layout is None:
        add("LAYOUT_UNVERIFIED", "此源页缺少原书布局。", "自动布局未形成可用原行位置，按源内容保留策略处理。", coverage="none")
        return diagnostics
    if (layout.content_revision != page.content_revision or layout.layout_revision != page.layout_revision
            or layout.source.page_number != page.number or layout.source.source_id != page.source_id
            or layout.source.source_page != page.source_page
            or (page.generated_content_revision is not None and page.generated_content_revision != page.content_revision)):
        add("LAYOUT_SOURCE_MISMATCH", "当前布局、来源或生成源码与页面修订不一致。", "保存当前布局并重新生成本修订源码。", severity="error")
    if layout.canvas_basis in {"project", None} and (layout.canvas_width_bp is not None or layout.source.pdf_geometry is None):
        add("SOURCE_PHYSICAL_SIZE_ASSUMED", "原页没有可靠物理尺寸，画布保持原图比例并采用项目纸宽。",
            "尺寸依据已记录，系统按此画布检查自然尺寸与输出范围。", basis="project")
    for reason in layout.review_reasons:
        add("SOURCE_CONTENT_REVIEW", reason, "系统执行有限复核与局部修复，仍不确定的区域保留源图。", severity="warning", basis="source_observation")
    for asset in layout.source_assets:
        if asset.purpose != "figure":
            diagnostics.append(RenderDiagnostic(
                **identity, code="SOURCE_PAGE_PRESERVED" if asset.purpose == "source_page" else "SOURCE_REGION_PRESERVED",
                severity="warning", message=asset.reason or "未完成可靠转录的内容已保留源图。",
                suggestion="源图保留结果自动提供；图像替代不计为成功文字化。",
                basis="source_asset", coverage="partial", block_id=asset.region_id, source_bbox=asset.bbox,
            ))
    if layout.source_disposition == "source_page_preserved":
        return diagnostics
    figures = {asset.region_id for asset in layout.source_assets if asset.purpose == "figure"}
    for region in layout.regions:
        if region.kind == "figure" and region.region_id not in figures:
            diagnostics.append(RenderDiagnostic(
                **identity, code="SOURCE_FIGURE_MISSING", severity="error", message="源图形区域尚未保留。",
                suggestion="自动保存该图形区域并随输出打包。", basis="source_observation", coverage="partial",
                block_id=region.region_id, source_bbox=region.bbox,
            ))
    if layout.lines:
        if layout.body_frame is None and any(line.kind in {"text", "equation", "table", "caption"} for line in layout.lines):
            add("LAYOUT_UNVERIFIED", "正文版心尚未定位，无法完整判断版心越界。", "系统结合源图行带自动恢复正文版心。", coverage="partial")
        confirmed = {"file_metadata", "local_measurement", "manual"}
        unconfirmed_font = any(
            ((line.style.font_size_bp is None or line.style.font_family is None)
             and (layout.body_font_size_bp is None or layout.body_font_family is None or layout.body_font_basis not in confirmed))
            or ((line.style.font_size_bp is not None or line.style.font_family is not None
                 or line.style.font_size_ratio is not None or line.style.bold is not None or line.style.italic is not None)
                and line.style.basis not in confirmed)
            for line in layout.lines
        )
        if unconfirmed_font:
            add("LAYOUT_UNVERIFIED", "部分字体或字号来自临时配置、模型估计或缺少依据。", "系统按样式组进行本地字形拟合；不能确定时标记输出。", coverage="partial")
        if any(line.basis not in confirmed for line in layout.lines):
            add("LAYOUT_UNVERIFIED", "部分原行位置或基线尚未取得本地测量依据。", "系统从源图行带恢复位置；不能确定时保留源区域。", coverage="partial")
        if any(group.align_x is None or group.basis not in confirmed for group in layout.equation_groups if len(group.line_ids) > 1):
            add("LAYOUT_UNVERIFIED", "部分多行公式组的锚点尚未确定。", "系统恢复公式组共同锚点；不能确定时保留公式源区域。", coverage="partial")
    if document.source_to_output_affine is None or document.canvas_scale is None:
        add("LAYOUT_UNVERIFIED", "缺少本次源画布到输出页的坐标映射。", "重新生成当前页的编译文档与页映射。", coverage="none")
        return diagnostics
    a, _, _, d, x, y = document.source_to_output_affine
    scale = document.canvas_scale
    width, height = a / scale, d / scale
    items = fidelity_items(layout, width, height)
    metrics_path = output_dir / "compiled.ebook-metrics.csv"
    if not metrics_path.is_file():
        add("LAYOUT_UNVERIFIED", "缺少当前自然尺寸测量记录。", "重新编译当前原书布局，生成 measurement-v2 CSV 记录。", coverage="none")
        return diagnostics
    try:
        measurements = read_measurements(metrics_path)
    except ValueError as error:
        add("LAYOUT_UNVERIFIED", f"自然尺寸记录不可用：{error}。", "重新编译当前修订并保留完整测量文件。", coverage="none")
        return diagnostics
    if set(measurements) != {item.token for item in items}:
        add("LAYOUT_UNVERIFIED", "自然尺寸记录与当前原行/编号不一致。", "重新编译当前修订，避免使用旧测量记录。", coverage="none")
        return diagnostics
    regions = {region.region_id: region for region in layout.regions}
    placed = []
    page_box = (0.0, 0.0, document.output_width_bp, document.output_height_bp)
    for item in items:
        if any(asset.purpose == "uncertain_content" and item.source_bbox is not None
               and asset.bbox[0] <= item.source_bbox[0] and asset.bbox[1] <= item.source_bbox[1]
               and asset.bbox[2] >= item.source_bbox[2] and asset.bbox[3] >= item.source_bbox[3]
               for asset in layout.source_assets):
            continue  # Opaque source replacement is checked as an asset, not as successful text.
        measured = measurements[item.token]
        source_box = measured_item_bbox(item, measured)
        if source_box[0] == source_box[2] or source_box[1] == source_box[3]:
            continue  # Empty editable lines have no painted extent.
        output_box = tuple(value * scale + (x if index % 2 == 0 else y) for index, value in enumerate(source_box))
        placed.append((item, source_box, output_box))
        overflow = _outside(output_box, page_box)
        if overflow > 0.001:
            add("CONTENT_OUTSIDE_PAGE", f"原行或编号超出最终纸张 {overflow:.2f} bp。", "核对原行断点、字体及落点；不要裁切或压缩单行。",
                severity="error", basis=MEASUREMENT_VERSION, item=item, box=output_box, overflow=overflow)
        frame = None
        region = regions[item.line.block_id]
        while region is not None:
            if region.bbox is not None and region.kind in {"body", "column", "paragraph", "equation", "table", "footnote"}:
                frame = region.bbox
                break
            region = regions.get(region.parent_id)
        if frame is None and item.line.kind in {"text", "equation", "table", "caption"}:
            frame = layout.body_frame
        if frame is not None and not item.is_number:
            overflow = _outside(source_box, normalized_bbox_to_bp(frame, width, height)) * scale
            if overflow > 0.001:
                add("CONTENT_OUTSIDE_FRAME", f"原行自然尺寸超出所属版心或区域 {overflow:.2f} bp。", "校准所属区域与原行字体，核对是否合并了源图中的断行。",
                    severity="warning", basis=MEASUREMENT_VERSION, item=item, box=output_box, overflow=overflow)
        if item.line.kind in {"text", "equation", "table", "footnote", "caption"} and line_font_size(layout, item.line) * scale < 6:
            add("READABILITY_TOO_SMALL", "整页映射后的正文字号低于 6 bp 警戒值。", "选择更大输出纸型或原尺寸输出，并核对正常阅读字号。",
                severity="warning", basis="canvas_transform", item=item, box=output_box)
    for index, (item, box, _) in enumerate(placed):
        for other, other_box, other_output in placed[index + 1:]:
            overlap_width = min(box[2], other_box[2]) - max(box[0], other_box[0])
            overlap_height = min(box[3], other_box[3]) - max(box[1], other_box[1])
            if overlap_width > 0.1 and overlap_height > 0.1:
                add("BLOCK_OVERLAP", f"原行 {item.line.line_id} 与 {other.line.line_id} 的自然内容区域发生碰撞。",
                    "核对两行基线、公式高度与编号位置；数学结构内部叠放不参与此检查。",
                    severity="warning", basis=MEASUREMENT_VERSION, item=other, box=other_output)
    return diagnostics


def local_geometry_adjustment(
    layout: SourceFidelityLayout, diagnostics: list[RenderDiagnostic],
    measurements: dict[str, NaturalMeasurement],
) -> SourceFidelityLayout | None:
    """Move measured boxes within observed regions; never alter text or font sizes."""
    if layout.source_disposition == "source_page_preserved" or not measurements:
        return None
    affected = {item.line_id for item in diagnostics if item.line_id and item.code in {
        "CONTENT_OUTSIDE_FRAME", "CONTENT_OUTSIDE_PAGE", "BLOCK_OVERLAP",
    }}
    if not affected:
        return None
    width, height = canvas_dimensions(layout)
    regions = {region.region_id: region for region in layout.regions}
    updates = {}
    for item in fidelity_items(layout, width, height):
        line = item.line
        if line.line_id not in affected or item.is_number or item.token not in measurements:
            continue
        measured = measurements[item.token]
        box = measured_item_bbox(item, measured)
        limit = max(1.0, line_font_size(layout, line) * .35)
        region = regions[line.block_id]
        frame = region.bbox or layout.body_frame or (0., 0., 1., 1.)
        left, _, right, _ = normalized_bbox_to_bp(frame, width, height)
        dx = 0.0
        if item.alignment == "left" and measured.natural_width_bp <= right - left:
            dx = min(max(box[0], left), right - measured.natural_width_bp) - box[0]
        lower = line.bbox[1] * height + measured.height_bp
        upper = line.bbox[3] * height - measured.depth_bp
        dy = min(max(item.baseline_bp, lower), upper) - item.baseline_bp if lower <= upper else 0.0
        if abs(dx) > limit or abs(dy) > limit:
            continue
        x0, y0, x1, y1 = line.bbox
        if not 0 <= x0 + dx / width < x1 + dx / width <= 1:
            continue
        if abs(dx) > .05 or abs(dy) > .05:
            updates[line.line_id] = line.model_copy(update={
                "bbox": (x0 + dx / width, y0, x1 + dx / width, y1),
                "baseline": line.baseline + dy / height, "basis": "local_measurement",
            })
    if not updates:
        return None
    return layout.model_copy(update={"lines": [updates.get(line.line_id, line) for line in layout.lines]})


def diagnostics_quality_status(diagnostics: list[RenderDiagnostic], *, output_checks_complete: bool) -> QualityStatus:
    if any(diagnostic.code == "COMPILE_FAILED" for diagnostic in diagnostics):
        return "compile_failed"
    if any(diagnostic.severity in {"error", "warning"} for diagnostic in diagnostics):
        return "needs_review"
    if not output_checks_complete or any(diagnostic.coverage != "complete" or diagnostic.code == "LAYOUT_UNVERIFIED" for diagnostic in diagnostics):
        return "unverified"
    return "passed"
