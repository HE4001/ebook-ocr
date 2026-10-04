"""Build template or standalone LaTeX documents and compile them with XeLaTeX."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .fidelity_rendering import (
    GENERATOR_VERSION, MATHRSFS_FONT_SHAPES, FidelityLayoutError, canvas_dimensions, render_fidelity_source,
)
from .latex_content import escape_latex, is_latex_document, latex_image_resources, source_resource_name
from .layout_contract import AffineTransform, RenderStrategy, SourceFidelityLayout
from .models import BookDetail, MarginSegment, Page


# Keep these physical dimensions identical to frontend/src/paper.ts.
PAPER_SIZES = {
    "a4": (210, 297, 18, 12),
    "a5": (148, 210, 14, 11),
    "a6": (105, 148, 10, 10.5),
    "b5": (176, 250, 16, 11.5),
    "b6": (125, 176, 12, 11),
    "trade_6x9": (152.4, 228.6, 14, 11),
}

_PREAMBLE = r"""\documentclass[UTF8,fontset=fandol,oneside,openany,linespread=1,autoindent=false]{ctexbook}
\usepackage{geometry,graphicx}
\usepackage{amsmath,amssymb}
\usepackage{mathrsfs}
""" + MATHRSFS_FONT_SHAPES + r"""
\usepackage{longtable,array}
\usepackage{fancyhdr}
\usepackage[normalem]{ulem}
\usepackage[hidelinks]{hyperref}
% Optional features apply to both bold and bold italic shapes.
\newcommand{\EbookCJKFont}[4][]{%
  #2{#3}[%
    Extension=.otf,
    BoldFont=#4,
    BoldFeatures={#1},
    ItalicFont=#3,
    ItalicFeatures={FakeSlant=0.2},
    BoldItalicFont=#4,
    BoldItalicFeatures={FakeSlant=0.2,#1}%
  ]%
}
\EbookCJKFont{\setCJKmainfont}{FandolSong-Regular}{FandolSong-Bold}
\EbookCJKFont{\setCJKsansfont}{FandolHei-Regular}{FandolHei-Bold}
\EbookCJKFont{\setCJKfamilyfont{zhsong}}{FandolSong-Regular}{FandolSong-Bold}
\EbookCJKFont{\setCJKfamilyfont{zhhei}}{FandolHei-Regular}{FandolHei-Bold}
\EbookCJKFont[FakeBold=1.5]{\setCJKfamilyfont{zhkai}}{FandolKai-Regular}{FandolKai-Regular}
\raggedbottom
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}
\ctexset{section={numbering=false},subsection={numbering=false},subsubsection={numbering=false},paragraph={numbering=false,afterskip=0.5em},subparagraph={numbering=false,afterskip=0.5em}}
\makeatletter
\newcommand{\EbookListLayout}[3]{%
  \setlength{\leftmargin}{#1}%
  \setlength{\labelsep}{0.5\ccwd}%
  \setlength{\labelwidth}{\dimexpr\leftmargin-\labelsep\relax}%
  \setlength{\itemindent}{0pt}%
  \setlength{\listparindent}{0pt}%
  \setlength{\topsep}{#2}%
  \setlength{\partopsep}{0.1\ccwd}%
  \setlength{\itemsep}{#3}%
  \setlength{\parsep}{0.1\ccwd}%
}
\newcommand{\EbookLayout}[4]{%
  \renewcommand{\normalsize}{%
    \fontsize{#1bp}{#2bp}\selectfont
    \setlength{\abovedisplayskip}{0.5\ccwd plus 0.1\ccwd minus 0.05\ccwd}%
    \setlength{\belowdisplayskip}{0.5\ccwd plus 0.1\ccwd minus 0.05\ccwd}%
    \setlength{\abovedisplayshortskip}{0.25\ccwd plus 0.1\ccwd minus 0.05\ccwd}%
    \setlength{\belowdisplayshortskip}{0.4\ccwd plus 0.1\ccwd minus 0.05\ccwd}%
    \setlength{\jot}{0.25\ccwd}%
    \let\@listi\@listI
  }%
  \normalsize
  \setlength{\parindent}{#3\ccwd}%
  \setlength{\parskip}{#4bp}%
  \setlength{\emergencystretch}{2em}%
  \setlength{\leftmargini}{2.5\ccwd}%
  \setlength{\leftmarginii}{2\ccwd}%
  \setlength{\leftmarginiii}{1.8\ccwd}%
  \setlength{\leftmarginiv}{1.8\ccwd}%
  \setlength{\leftmarginv}{1.5\ccwd}%
  \setlength{\leftmarginvi}{1.5\ccwd}%
  \def\@listI{\EbookListLayout{\leftmargini}{0.5\ccwd}{0.4\ccwd}}%
  \let\@listi\@listI
  \def\@listii{\EbookListLayout{\leftmarginii}{0.25\ccwd}{0.15\ccwd}}%
  \def\@listiii{\EbookListLayout{\leftmarginiii}{0.2\ccwd}{0.1\ccwd}}%
  \def\@listiv{\EbookListLayout{\leftmarginiv}{0.2\ccwd}{0.1\ccwd}}%
  \def\@listv{\EbookListLayout{\leftmarginv}{0.2\ccwd}{0.1\ccwd}}%
  \def\@listvi{\EbookListLayout{\leftmarginvi}{0.2\ccwd}{0.1\ccwd}}%
}
\makeatother
\newcommand{\EbookMarginRow}[3]{%
  \noindent\makebox[\textwidth][l]{%
    \makebox[0pt][l]{\strut#1}%
    \makebox[\textwidth][c]{\strut#2}%
    \makebox[0pt][r]{\strut#3}}\par
}
\newcommand{\EbookPage}[7]{%
  \newgeometry{left=#1mm,right=#2mm,top=#3mm,bottom=#3mm,headheight=#4mm,headsep=3mm,footskip=#5mm,includehead,includefoot}%
  \fancyhf{}%
  \fancyhead[C]{#6}%
  \fancyfoot[C]{#7}%
}
"""
_COMPILE_TIMEOUT_SECONDS = 120


class LatexCompileError(Exception):
    """A readable dependency, source or typesetting error for the HTTP layer."""

    def __init__(self, message: str, *, code: str | None = None,
                 page_number: int | None = None, line_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.page_number = page_number
        self.line_id = line_id


@dataclass(frozen=True)
class LatexDocument:
    source: str
    page_order: list[int]
    render_strategy: RenderStrategy = "legacy_template"
    source_to_output_affine: AffineTransform | None = None
    canvas_scale: float | None = None
    output_width_bp: float | None = None
    output_height_bp: float | None = None
    resource_names: tuple[str, ...] = ()


def _number(value: float) -> str:
    result = format(Decimal(str(value)), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _plain_lines(text: str) -> str:
    return r"\\".join(escape_latex(line) for line in text.splitlines())


def _margin_content(segments: list[MarginSegment], font_size: float, line_height: float) -> str:
    if not segments:
        return ""
    rows: list[str] = []
    for row in range(1, max(segment.row for segment in segments) + 1):
        cells: list[str] = []
        for alignment in ("left", "center", "right"):
            parts: list[str] = []
            for segment in segments:
                if segment.row != row or segment.alignment != alignment:
                    continue
                content = _plain_lines(segment.text)
                if "\n" in segment.text or "\r" in segment.text:
                    content = rf"\shortstack[{alignment[0]}]{{{content}}}"
                if segment.italic:
                    content = rf"\textit{{{content}}}"
                if segment.bold:
                    content = rf"\textbf{{{content}}}"
                size = font_size * (0.85 if segment.font_size == "small" else 1)
                parts.append(rf"{{\fontsize{{{_number(size)}bp}}{{{_number(size * line_height)}bp}}\selectfont {content}}}")
            cells.append(r"\quad ".join(parts))
        rows.append(r"\EbookMarginRow" + "".join("{" + cell + "}" for cell in cells))
    return r"\parbox[b]{\textwidth}{" + "\n".join(rows) + "}"


def _margin_height(segments: list[MarginSegment], font_size: float, line_height: float) -> float:
    if not segments:
        return 0
    extra_lines = sum(
        max((len(segment.text.splitlines()) - 1 for segment in segments if segment.row == row), default=0)
        for row in range(1, max(segment.row for segment in segments) + 1)
    )
    rows = max(segment.row for segment in segments) + extra_lines
    return rows * font_size * line_height * 25.4 / 72


def _page_setup(page: Page, margin: float, font_size: float, line_height: float, print_version: bool) -> str:
    left = right = margin
    if print_version and page.page_kind == "content" and any(segment.text.strip() for segment in page.footer_segments):
        if page.page_side == "left":
            left, right = margin * 0.8, margin * 1.2
        elif page.page_side == "right":
            left, right = margin * 1.2, margin * 0.8
    head_height = _margin_height(page.header_segments, font_size, line_height)
    foot_height = _margin_height(page.footer_segments, font_size, line_height)
    header = _margin_content(page.header_segments, font_size, line_height)
    footer = _margin_content(page.footer_segments, font_size, line_height)
    values = (left, right, margin, head_height, foot_height + 3)
    return r"\EbookPage" + "".join("{" + _number(value) + "}" for value in values) + "{" + header + "}{" + footer + "}"


def _cover(page: Page, font_size: float, line_height: float) -> str:
    if not page.cover_fields:
        return r"\null"
    title = {"title", "subtitle"}
    authors = {"author", "translator", "editor"}
    parts = [r"\begin{center}", r"\vspace*{0pt}"]
    if page.page_kind == "back_cover":
        parts.append(r"\vfill")
    else:
        parts.append(r"\vspace*{0.08\textwidth}")
    previous_group = None
    for field in page.cover_fields:
        group = 0 if field.kind in title else 1 if field.kind in authors else 2
        if previous_group is not None and group != previous_group:
            parts.append(r"\vspace{1.5em}")
        previous_group = group
        scale = 2.0 if field.kind == "title" else 1.35 if field.kind == "subtitle" else 1
        content = rf"\shortstack[c]{{{_plain_lines(field.text)}}}"
        if field.kind == "title":
            content = rf"\textbf{{{content}}}"
        size = font_size * scale
        parts.append(rf"{{\fontsize{{{_number(size)}bp}}{{{_number(size * line_height)}bp}}\selectfont {content}\par}}")
    if page.page_kind == "front_cover":
        parts.append(r"\vfill")
    parts.append(r"\end{center}")
    return "\n".join(parts)


def build_latex(detail: BookDetail, print_version: bool = False) -> str:
    book = detail.book
    width, height, default_margin, default_font = PAPER_SIZES[book.paper_size]
    layout = book.layout
    margin = layout.margin_mm if layout.margin_mm is not None else default_margin
    font_size = layout.font_size_pt if layout.font_size_pt is not None else default_font
    font = {"songti": r"\songti", "heiti": r"\heiti", "kaiti": r"\kaishu"}[layout.font_family]
    config = rf"\geometry{{paperwidth={_number(width)}mm,paperheight={_number(height)}mm,margin={_number(margin)}mm}}"
    parts = [
        _PREAMBLE + config,
        r"\begin{document}",
        font,
        rf"\EbookLayout{{{_number(font_size)}}}{{{_number(font_size * layout.line_height)}}}{{{_number(layout.paragraph_indent)}}}{{{_number(layout.paragraph_spacing_pt)}}}",
    ]
    for index, page in enumerate(detail.pages):
        if index:
            parts.append(r"\clearpage")
        parts.append(_page_setup(page, margin, font_size, layout.line_height, print_version))
        # newgeometry restores the class size; reapply the configured size/baseline.
        parts.append(r"\normalsize")
        if page.page_kind == "content":
            content = page.text if page.text.strip() else r"\null"
            parts.extend((r"\begingroup", content, r"\par\endgroup"))
        else:
            parts.append(_cover(page, font_size, layout.line_height))
    parts.append(r"\end{document}")
    return "\n".join(parts) + "\n"


def generate_source_fidelity_latex(layout: SourceFidelityLayout) -> str:
    """Save a standalone document at the native source-canvas dimensions."""
    width, height = canvas_dimensions(layout)
    return render_fidelity_source(
        layout, canvas_width_bp=width, canvas_height_bp=height,
        output_width_bp=width, output_height_bp=height,
    )


def _fidelity_document(detail: BookDetail, page: Page, position: int, print_version: bool) -> LatexDocument:
    source_layout = page.layout_source
    if source_layout is None:
        raise LatexCompileError(
            f"源页 {page.number} 尚未取得可生成的原书布局",
            code="LAYOUT_UNVERIFIED", page_number=page.number,
        )
    width_mm, height_mm, default_margin, default_font = PAPER_SIZES[detail.book.paper_size]
    settings = detail.book.layout
    width, height = canvas_dimensions(source_layout, width_mm * 72 / 25.4)
    # These are preview defaults, never persisted as measured source facts.
    layout = source_layout.model_copy(update={
        "body_font_size_bp": source_layout.body_font_size_bp or settings.font_size_pt or default_font,
        "body_font_family": source_layout.body_font_family or settings.font_family,
    })
    if settings.source_fidelity_paper == "source":
        output_width, output_height = width, height
        scale, x, y = 1.0, 0.0, 0.0
    else:
        output_width, output_height = width_mm * 72 / 25.4, height_mm * 72 / 25.4
        margin = (settings.margin_mm if settings.margin_mm is not None else default_margin) * 72 / 25.4
        left = right = margin
        if print_version and page.page_side == "left":
            left, right = margin * 0.8, margin * 1.2
        elif print_version and page.page_side == "right":
            left, right = margin * 1.2, margin * 0.8
        scale = min((output_width - left - right) / width, (output_height - 2 * margin) / height)
        if scale <= 0:
            raise LatexCompileError("目标纸型的边距未留下可用画布空间，请减小边距", page_number=page.number)
        x = left + (output_width - left - right - width * scale) / 2
        y = margin + (output_height - 2 * margin - height * scale) / 2
    try:
        source = render_fidelity_source(
            layout, canvas_width_bp=width, canvas_height_bp=height,
            output_width_bp=output_width, output_height_bp=output_height,
            scale=scale, offset_x_bp=x, offset_y_bp=y,
        )
    except FidelityLayoutError as error:
        raise LatexCompileError(
            f"源页 {page.number}：{error}", code="LAYOUT_UNVERIFIED",
            page_number=page.number, line_id=error.line_id,
        ) from error
    return LatexDocument(
        source, [position], "source_fidelity", (width * scale, 0, 0, height * scale, x, y),
        scale, output_width, output_height,
        tuple(source_resource_name(asset.image_name) for asset in layout.source_assets),
    )


def build_latex_documents(detail: BookDetail, print_version: bool = False) -> list[LatexDocument]:
    """Compile every source page independently, keeping complete custom sources exact."""
    documents = []
    for position, page in enumerate(detail.pages, 1):
        if page.render_strategy == "source_fidelity":
            documents.append(_fidelity_document(detail, page, position, print_version))
        elif is_latex_document(page.text):
            documents.append(LatexDocument(page.text, [position], "custom_latex",
                                           resource_names=latex_image_resources(page.text)))
        else:
            page_detail = detail.model_copy(update={"pages": [page]})
            width, height, _, _ = PAPER_SIZES[detail.book.paper_size]
            documents.append(LatexDocument(
                build_latex(page_detail, print_version), [position], page.render_strategy,
                output_width_bp=width * 72 / 25.4, output_height_bp=height * 72 / 25.4,
                resource_names=latex_image_resources(page.text),
            ))
    return documents


def copy_document_resources(document: LatexDocument, book_dir: Path, output_dir: Path) -> None:
    """Copy exactly the referenced assets while retaining book-relative names."""
    root = book_dir.resolve()
    for name in document.resource_names:
        relative = source_resource_name(name)
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise LatexCompileError(f"缺少源区域资源：{relative}", code="SOURCE_RESOURCE_MISSING")
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _compile_reason(output: str) -> str:
    lines = output.splitlines()
    for line in lines:
        if "not found" in line and "File" in line:
            match = re.search(r"File [`']([^`']+)[`'] not found", line)
            name = Path(match.group(1)).name if match else "所需宏包或字体"
            return f"缺少 LaTeX 文件 {name}；请预先安装完整的 TeX 发行版及所需宏包"
        if "Undefined control sequence" in line:
            return "源码含有编译器不认识的命令，请检查 LaTeX 源码"
        if "font" in line.lower() and ("cannot be found" in line or "not loadable" in line):
            return "缺少所需字体；请检查 LaTeX 源码指定的字体"
        if line.startswith("!") or re.match(r"(?:\./)?document\.tex:\d+:", line):
            reason = re.sub(r"^[! ]+", "", line)
            reason = re.sub(r"[A-Za-z]:[\\/][^\s\"'`]+", "[路径]", reason)
            reason = re.sub(r"(?<!\S)/(?:[^\s/]+/)+[^\s\"'`]*", "[路径]", reason)
            if r"\bfseries" in reason and "invalid in math mode" in reason:
                return (
                    reason[:240]
                    + r"；\bfseries 用于文字模式；公式内部请按实际字形使用 \boldsymbol 等数学命令，并检查下标范围及外层括号。"
                )
            return reason[:240]
    return "XeLaTeX 未能完成排版，请检查公式括号、环境、表格列数与文字转义"


def _xelatex_executable() -> str:
    configured = os.environ.get("EBOOK_OCR_XELATEX")
    executable = shutil.which(configured if configured else "xelatex")
    if configured and executable is None:
        raise LatexCompileError(
            "EBOOK_OCR_XELATEX 配置无效：未找到可执行的 XeLaTeX。"
            "请设置为可执行文件路径，或清空该变量以自动查找"
        )
    if executable is None and os.name == "nt":
        for root_name, bin_name in (
            ("APPDATA", "windows"),
            ("PROGRAMDATA", "windows"),
            ("APPDATA", "win32"),
            ("PROGRAMDATA", "win32"),
        ):
            root = os.environ.get(root_name)
            if not root:
                continue
            executable = shutil.which(str(Path(root) / "TinyTeX" / "bin" / bin_name / "xelatex.exe"))
            if executable is not None:
                break
    if executable is None:
        searched = "PATH"
        if os.name == "nt":
            searched += " 和 APPDATA、PROGRAMDATA 下 TinyTeX 的 bin\\windows、bin\\win32 默认目录"
        raise LatexCompileError(
            f"未找到 XeLaTeX，已查找 {searched}。请预先安装 TinyTeX、TeX Live 或 MiKTeX，"
            "将 xelatex 加入 PATH，或设置 EBOOK_OCR_XELATEX 为可执行文件路径"
        )
    return os.path.abspath(executable)


async def compile_pdf(source: str, output_dir: Path) -> Path:
    """Compile a LaTeX source with the existing XeLaTeX execution restrictions."""
    executable = _xelatex_executable()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "document.pdf"
    compiled_path = output_dir / "compiled.pdf"
    pdf_path.unlink(missing_ok=True)
    compiled_path.unlink(missing_ok=True)
    (output_dir / "compiled.ebook-metrics.csv").unlink(missing_ok=True)
    (output_dir / "document.tex").write_text(source, encoding="utf-8", newline="")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(executable).parent) + os.pathsep + environment.get("PATH", "")
    environment.update({"openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": str(output_dir), "shell_escape": "f"})
    options: dict = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
    arguments = (
        executable, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
        "-file-line-error", "-jobname=compiled", "-output-directory=.", "document.tex",
    )
    # A second pass settles longtable widths and hyperref's auxiliary output.
    for _ in range(2):
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments, cwd=output_dir, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options,
            )
        except OSError as error:
            raise LatexCompileError("XeLaTeX 无法启动，请检查可执行文件与系统权限") from error
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=_COMPILE_TIMEOUT_SECONDS)
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise LatexCompileError("LaTeX 编译超过 120 秒，请简化当前页的复杂公式或表格") from error
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        if process.returncode != 0:
            raise LatexCompileError("LaTeX 编译失败：" + _compile_reason(output.decode("utf-8", errors="replace")))
    if not compiled_path.is_file():
        raise LatexCompileError("XeLaTeX 已结束，但没有生成 PDF")
    compiled_path.replace(pdf_path)
    return pdf_path
