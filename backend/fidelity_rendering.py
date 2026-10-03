"""Place observed lines on one source canvas and measure their natural boxes."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .layout_contract import LayoutLine, SourceFidelityLayout, equation_alignment_index


GENERATOR_VERSION = "source-fidelity-v1"
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


def fidelity_items(layout: SourceFidelityLayout, width_bp: float, height_bp: float) -> list[FidelityItem]:
    groups = {line_id: group for group in layout.equation_groups for line_id in group.line_ids}
    items = []
    for index, line in enumerate(sorted(layout.lines, key=lambda line: line.order), 1):
        if line.bbox is None or line.baseline is None:
            raise FidelityLayoutError(f"原行 {line.line_id} 缺少区域或基线，请先校准原行位置", line.line_id)
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
\usepackage{geometry,graphicx,amsmath,amssymb,mathrsfs,array}
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
    items = fidelity_items(layout, canvas_width_bp, canvas_height_bp)
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
    parts.extend((
        r"\end{picture}}}", r"\end{picture}}}",
        r"\immediate\closeout\EbookMetrics", r"\end{document}",
    ))
    return "\n".join(parts) + "\n"
