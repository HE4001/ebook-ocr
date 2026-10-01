"""Build the application's Chinese LaTeX document and compile it with XeLaTeX."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

from .latex_content import escape_latex, validate_latex_fragment
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
\usepackage{geometry}
\usepackage{amsmath,amssymb}
\usepackage{longtable,array}
\usepackage{fancyhdr}
\usepackage[normalem]{ulem}
\usepackage[hidelinks]{hyperref}
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}
\ctexset{section={numbering=false},subsection={numbering=false},subsubsection={numbering=false},paragraph={numbering=false,afterskip=0.5em},subparagraph={numbering=false,afterskip=0.5em}}
\newcommand{\EbookLayout}[4]{%
  \renewcommand{\normalsize}{\fontsize{#1bp}{#2bp}\selectfont}%
  \normalsize
  \setlength{\parindent}{#3\ccwd}%
  \setlength{\parskip}{#4bp}%
  \setlength{\emergencystretch}{2em}%
}
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
    groups = [
        [field for field in page.cover_fields if field.kind in title],
        [field for field in page.cover_fields if field.kind in authors],
        [field for field in page.cover_fields if field.kind not in title | authors],
    ]
    parts = [r"\begin{center}", r"\vspace*{0pt}"]
    if page.page_kind == "back_cover":
        parts.append(r"\vfill")
    else:
        parts.append(r"\vspace*{0.08\textwidth}")
    for index, fields in enumerate(groups):
        if not fields:
            continue
        if index:
            parts.append(r"\vspace{1.5em}")
        for field in fields:
            scale = 2.0 if field.kind == "title" else 1.35 if field.kind == "subtitle" else 1
            content = _plain_lines(field.text)
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
        if page.page_kind == "content":
            validate_latex_fragment(page.text)
            content = page.text if page.text.strip() else r"\null"
            parts.extend((r"\begingroup", content, r"\par\endgroup"))
        else:
            parts.append(_cover(page, font_size, layout.line_height))
    parts.append(r"\end{document}")
    return "\n".join(parts) + "\n"


def _compile_reason(output: str) -> str:
    lines = output.splitlines()
    for line in lines:
        if "not found" in line and "File" in line:
            match = re.search(r"File [`']([^`']+)[`'] not found", line)
            name = Path(match.group(1)).name if match else "所需宏包或字体"
            return f"缺少 LaTeX 文件 {name}；请预先安装完整的 TeX 发行版及所需宏包"
        if "Undefined control sequence" in line:
            return "正文含有编译器不认识的命令，请检查 LaTeX 片段"
        if "font" in line.lower() and ("cannot be found" in line or "not loadable" in line):
            return "缺少所需字体；请检查 TeX 发行版中的 Fandol 字体"
        if line.startswith("!") or re.match(r"(?:\./)?document\.tex:\d+:", line):
            reason = re.sub(r"^[! ]+", "", line)
            reason = re.sub(r"[A-Za-z]:[\\/][^\s\"'`]+", "[路径]", reason)
            reason = re.sub(r"(?<!\S)/(?:[^\s/]+/)+[^\s\"'`]*", "[路径]", reason)
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
    """Compile the trusted document returned by build_latex, never an upload."""
    executable = _xelatex_executable()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "document.pdf"
    compiled_path = output_dir / "compiled.pdf"
    pdf_path.unlink(missing_ok=True)
    compiled_path.unlink(missing_ok=True)
    (output_dir / "document.tex").write_text(source, encoding="utf-8")
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
