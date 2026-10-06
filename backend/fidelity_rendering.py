"""Place observed lines on one source canvas and measure their natural boxes."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .layout_contract import LayoutLine, SourceFidelityLayout, equation_alignment_index
from .latex_content import source_resource_name


GENERATOR_VERSION = "source-fidelity-v2"
MEASUREMENT_VERSION = "measurement-v2"
_FONT_COMMANDS = {"songti": r"\songti", "heiti": r"\heiti", "kaiti": r"\kaishu"}
MATHRSFS_FONT_SHAPES = r"""\input{ursfs.fd}
% Keep the original optical designs, scalable to the requested physical size.
\DeclareFontShape{U}{rsfs}{m}{n}{<-6.5>rsfs5<6.5-7.5>rsfs7<7.5->rsfs10}{}
"""


class FidelityLayoutError(ValueError):
    def __init__(self, message: str, line_id: str | None = None):
        super().__init__(message)
        self.line_id = line_id


@dataclass(frozen=True)
class FidelityItem:
    token: str
    line: LayoutLine
    latex: str
    suffix: str
    anchor_x_bp: float
    baseline_bp: float
    alignment: str
    source_bbox: tuple[float, float, float, float] | None
    is_number: bool = False


def number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def canvas_dimensions(layout: SourceFidelityLayout, draft_width_bp: float = 210 * 72 / 25.4) -> tuple[float, float]:
    """Unknown image DPI uses an explicitly provisional project-width canvas."""
    if layout.canvas_width_bp is not None:
        return layout.canvas_width_bp, layout.canvas_height_bp
    geometry = layout.source.pdf_geometry
    if geometry is not None:
        # The canonical image may be a crop or a rotated PDF render.
        a, b, c, d, _, _ = layout.source.canonical_to_source_affine
        return (
            math.hypot(a, b) * layout.source.canonical_width_px,
            math.hypot(c, d) * layout.source.canonical_height_px,
        )
    return draft_width_bp, draft_width_bp * layout.source.canonical_height_px / layout.source.canonical_width_px


def split_math_anchor(latex: str) -> tuple[str, str] | None:
    """Use the same equation-body parser as the layout contract."""
    try:
        anchor = equation_alignment_index(latex)
    except ValueError as error:
        raise FidelityLayoutError(str(error)) from error
    return None if anchor is None else (latex[:anchor], latex[anchor + 1:])


def line_font_size(layout: SourceFidelityLayout, line: LayoutLine) -> float:
    return line.style.font_size_bp or (layout.body_font_size_bp or 11) * (line.style.font_size_ratio or 1)


def _styled(layout: SourceFidelityLayout, line: LayoutLine, latex: str, *, math_mode: bool) -> str:
    size = number(line_font_size(layout, line))
    font = _FONT_COMMANDS[line.style.font_family or layout.body_font_family or "songti"]
    weight = r"\bfseries\boldmath " if line.style.bold else ""
    italic = r"\itshape " if line.style.italic else ""
    content = r"\(\displaystyle " + latex + r"\)" if math_mode else latex
    return rf"{{{font} {weight}{italic}\fontsize{{{size}bp}}{{{size}bp}}\selectfont {content}}}"


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def source_preserved_line_ids(layout: SourceFidelityLayout) -> set[str]:
    """Hide uncertain candidate text only when the source image covers the whole item."""
    if layout.source_disposition == "source_page_preserved":
        return {line.line_id for line in layout.lines}
    assets = [asset for asset in layout.source_assets if asset.purpose == "uncertain_content"]
    regions = {region.region_id: region for region in layout.regions}
    lines = {line.line_id: line for line in layout.lines}
    preserved = set()
    for line in layout.lines:
        if line.bbox is None:
            region = regions[line.block_id]
            if any(asset.region_id == line.block_id and _contains(asset.bbox, region.bbox or (0., 0., 1., 1.))
                   for asset in assets):
                preserved.add(line.line_id)
            continue
        overlaps = [asset for asset in assets if min(asset.bbox[2], line.bbox[2]) > max(asset.bbox[0], line.bbox[0])
                    and min(asset.bbox[3], line.bbox[3]) > max(asset.bbox[1], line.bbox[1])]
        if overlaps and not any(_contains(asset.bbox, line.bbox) for asset in overlaps):
            raise FidelityLayoutError(f"保留区域只覆盖原行 {line.line_id} 的一部分，无法安全替换整行", line.line_id)
        if overlaps:
            preserved.add(line.line_id)
    for group in layout.equation_groups:
        if group.number is not None and group.number.line_id in preserved:
            bbox = group.number.bbox
            region = regions[lines[group.line_ids[0]].block_id]
            complete_group = group.bbox or region.bbox
            if bbox is None and complete_group is not None and any(
                _contains(asset.bbox, complete_group) for asset in assets
            ):
                preserved.update(group.line_ids)
                continue
            if bbox is None or not any(_contains(asset.bbox, bbox) for asset in assets):
                raise FidelityLayoutError(f"保留公式区域未完整覆盖编号 {group.number.line_id}", group.number.line_id)
    return preserved


def fidelity_items(layout: SourceFidelityLayout, width_bp: float, height_bp: float) -> list[FidelityItem]:
    groups = {line_id: group for group in layout.equation_groups for line_id in group.line_ids}
    items = []
    preserved = source_preserved_line_ids(layout)
    for index, line in enumerate(sorted(layout.lines, key=lambda line: line.order), 1):
        if line.line_id in preserved:
            continue
        if line.bbox is None or line.baseline is None:
            raise FidelityLayoutError(f"原行 {line.line_id} 未取得可用区域或基线", line.line_id)
        latex = line.latex
        suffix = ""
        x = line.bbox[0] * width_bp
        alignment = "left"
        group = groups.get(line.line_id)
        if line.kind == "equation":
            split = split_math_anchor(latex)
            if split is not None:
                if group is None or group.align_x is None:
                    raise FidelityLayoutError(f"公式行 {line.line_id} 含对齐符但缺少公式组锚点", line.line_id)
                prefix, suffix = split
                latex = prefix + suffix
                x = group.align_x * width_bp
                alignment = "anchor"
        items.append(FidelityItem(
            f"line{index:04d}", line,
            _styled(layout, line, latex, math_mode=line.kind == "equation"),
            _styled(layout, line, suffix, math_mode=line.kind == "equation"),
            x, line.baseline * height_bp, alignment, line.bbox,
        ))
        if group is not None and group.number is not None and group.number.line_id == line.line_id:
            label = group.number
            right = label.anchor_x if label.anchor_x is not None else label.bbox[2] if label.bbox is not None else None
            if right is None:
                raise FidelityLayoutError(f"公式行 {line.line_id} 的编号缺少右侧锚点", line.line_id)
            items.append(FidelityItem(
                f"number{index:04d}", line,
                _styled(layout, line, label.latex, math_mode=False), "",
                right * width_bp, line.baseline * height_bp, "right", label.bbox, True,
            ))
    return items


_PREAMBLE = r"""\documentclass[UTF8,fontset=fandol,oneside,linespread=1,autoindent=false]{ctexart}
\usepackage{geometry,graphicx,xcolor,amsmath,amssymb,mathrsfs,array}
""" + MATHRSFS_FONT_SHAPES + r"""
\usepackage[normalem]{ulem}
\newcommand{\EbookCJKFont}[4][]{%
  #2{#3}[Extension=.otf,BoldFont=#4,BoldFeatures={#1},%
    ItalicFont=#3,ItalicFeatures={FakeSlant=0.2},%
    BoldItalicFont=#4,BoldItalicFeatures={FakeSlant=0.2,#1}]%
}
\EbookCJKFont{\setCJKmainfont}{FandolSong-Regular}{FandolSong-Bold}
\EbookCJKFont{\setCJKsansfont}{FandolHei-Regular}{FandolHei-Bold}
\EbookCJKFont{\setCJKfamilyfont{zhsong}}{FandolSong-Regular}{FandolSong-Bold}
\EbookCJKFont{\setCJKfamilyfont{zhhei}}{FandolHei-Regular}{FandolHei-Bold}
\EbookCJKFont[FakeBold=1.5]{\setCJKfamilyfont{zhkai}}{FandolKai-Regular}{FandolKai-Regular}
\pagestyle{empty}
\setlength{\unitlength}{1bp}
\newsavebox{\EbookNaturalBox}
\newsavebox{\EbookSuffixBox}
\newdimen\EbookAnchorPrefix
\newwrite\EbookMetrics
\ExplSyntaxOn
\cs_new:Npn \EbookBp #1 {\fp_eval:n {round(\dim_to_decimal:n {#1} * 72 / 72.27, 6)}}
\ExplSyntaxOff
% Boxes are measured before the single whole-canvas transform, with no clipping.
\newcommand{\EbookMeasuredLine}[6]{%
  \sbox{\EbookNaturalBox}{#5}%
  \sbox{\EbookSuffixBox}{#6}%
  \EbookAnchorPrefix=0pt%
  \ifnum#4=1\EbookAnchorPrefix=\dimexpr\wd\EbookNaturalBox-\wd\EbookSuffixBox\relax\fi%
  \immediate\write\EbookMetrics{#1,\EbookBp{\wd\EbookNaturalBox},\EbookBp{\ht\EbookNaturalBox},\EbookBp{\dp\EbookNaturalBox},\EbookBp{\EbookAnchorPrefix}}%
  \put(#2,#3){\ifnum#4=1\kern-\EbookAnchorPrefix\else\ifnum#4=2\kern-\wd\EbookNaturalBox\fi\fi\usebox{\EbookNaturalBox}}%
}
"""


def render_fidelity_source(
    layout: SourceFidelityLayout, *, canvas_width_bp: float, canvas_height_bp: float,
    output_width_bp: float, output_height_bp: float, scale: float = 1,
    offset_x_bp: float = 0, offset_y_bp: float = 0,
) -> str:
    preserved_page = layout.source_disposition == "source_page_preserved"
    items = [] if preserved_page else fidelity_items(layout, canvas_width_bp, canvas_height_bp)
    width, height = number(output_width_bp), number(output_height_bp)
    bottom = output_height_bp - offset_y_bp - scale * canvas_height_bp
    parts = [
        f"% generator={GENERATOR_VERSION}; coordinates=normalized-top-left; sizes=bp",
        "% Unknown physical dimensions or fonts remain provisional; see render diagnostics.",
        _PREAMBLE,
        rf"\geometry{{paperwidth={width}bp,paperheight={height}bp,margin=0bp}}",
        r"\begin{document}",
        r"\immediate\openout\EbookMetrics=\jobname.ebook-metrics.csv",
        rf"\immediate\write\EbookMetrics{{{MEASUREMENT_VERSION}}}",
        r"\immediate\write\EbookMetrics{token,natural_width_bp,height_bp,depth_bp,anchor_prefix_bp}",
        r"\hoffset=-1in\voffset=-1in",
        r"\shipout\vbox{\offinterlineskip\hbox{%",
        rf"\special{{papersize={width}bp,{height}bp}}%",
        rf"\begin{{picture}}({width},{height})",
        rf"\put({number(offset_x_bp)},{number(bottom)}){{\scalebox{{{number(scale)}}}{{%",
        rf"\begin{{picture}}({number(canvas_width_bp)},{number(canvas_height_bp)})",
    ]
    for item in items:
        mode = {"left": 0, "anchor": 1, "right": 2}[item.alignment]
        parts.append(
            rf"\EbookMeasuredLine{{{item.token}}}{{{number(item.anchor_x_bp)}}}"
            rf"{{{number(canvas_height_bp - item.baseline_bp)}}}{{{mode}}}"
            "{" + item.latex + "}{" + item.suffix + "}%"
        )
    for asset in layout.source_assets:
        image_name = source_resource_name(asset.image_name)
        x0, y0, x1, y1 = asset.bbox
        asset_width = (x1 - x0) * canvas_width_bp
        asset_height = (y1 - y0) * canvas_height_bp
        asset_x, asset_y = x0 * canvas_width_bp, (1 - y1) * canvas_height_bp
        # Source images replace visible content only in their recorded regions.
        # The best transcription remains in the layout and structured export.
        parts.append(
            rf"\put({number(asset_x)},{number(asset_y)}){{{{\color{{white}}"
            rf"\rule{{{number(asset_width)}bp}}{{{number(asset_height)}bp}}}}}}%"
        )
        parts.append(
            rf"\put({number(asset_x)},{number(asset_y)}){{\includegraphics"
            rf"[width={number(asset_width)}bp,height={number(asset_height)}bp]"
            rf"{{\detokenize{{{image_name}}}}}}}%"
        )
    parts.extend((
        r"\end{picture}}}", r"\end{picture}}}",
        r"\immediate\closeout\EbookMetrics", r"\end{document}",
    ))
    return "\n".join(parts) + "\n"
