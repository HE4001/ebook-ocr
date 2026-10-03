"""One-time migration helpers for databases saved before the LaTeX format.

There is intentionally no Markdown editing, preview or export API. Storage uses
these helpers only while upgrading legacy records and retains their source text.
"""

from __future__ import annotations

from typing import Any

import mistune
from mistune.plugins.math import math_in_list, math_in_quote
from mistune.plugins.table import table_in_list, table_in_quote

from .latex_content import escape_latex


_MARKDOWN = mistune.create_markdown(
    renderer="ast",
    plugins=["table", "math", "strikethrough", "task_lists", math_in_list, math_in_quote, table_in_list, table_in_quote],
)
_HEADINGS = ("section", "subsection", "subsubsection", "paragraph", "subparagraph", "subparagraph")
_LABELS = {
    "example": "实例", "caption": "图注", "footnote": "脚注",
    "header": "页眉", "footer": "页脚", "page_number": "页码",
}


def _code_text(text: str) -> str:
    return escape_latex(text.expandtabs(4)).replace(" ", "\\ ")


def _table(token: dict[str, Any]) -> str:
    head = next(child for child in token["children"] if child["type"] == "table_head")
    body = next(child for child in token["children"] if child["type"] == "table_body")
    columns = head["children"]
    count = len(columns)
    specs: list[str] = []
    for cell in columns:
        alignment = cell.get("attrs", {}).get("align")
        command = {"left": "raggedright", "center": "centering", "right": "raggedleft"}.get(alignment, "raggedright")
        width = rf"\dimexpr\linewidth/{count}-2\tabcolsep-2\arrayrulewidth\relax"
        specs.append(rf">{{\{command}\arraybackslash}}p{{{width}}}")

    def row(cells: list[dict[str, Any]]) -> str:
        return " & ".join(_render(cell.get("children", [])) for cell in cells) + r" \\ \hline"

    header = row(columns)
    parts = [r"\begin{longtable}{|" + "|".join(specs) + "|}", r"\hline", header,
             r"\endfirsthead", r"\hline", header, r"\endhead"]
    parts.extend(row(child["children"]) for child in body["children"])
    parts.append(r"\end{longtable}")
    return "\n".join(parts) + "\n\n"


def _list(token: dict[str, Any]) -> str:
    attrs = token.get("attrs", {})
    ordered = attrs.get("ordered", False)
    environment = "enumerate" if ordered else "itemize"
    start = attrs.get("start", 1)
    parts = [rf"\begin{{{environment}}}"]
    for index, child in enumerate(token["children"]):
        if child["type"] == "task_list_item":
            label = r"[$\boxtimes$]" if child["attrs"]["checked"] else r"[$\square$]"
        elif ordered:
            label = "[{" + str(start + index) + ".}]"
        else:
            label = ""
        parts.append(r"\item" + label + " " + _render(child.get("children", [])).strip())
    parts.append(rf"\end{{{environment}}}")
    return "\n".join(parts) + "\n\n"


def _render(tokens: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for token in tokens:
        kind = token["type"]
        children = token.get("children", [])
        raw = token.get("raw", "")
        if kind == "blank_line":
            continue
        if kind == "text":
            value = escape_latex(raw)
        elif kind == "softbreak":
            value = " "
        elif kind == "linebreak":
            value = r"\newline{}"
        elif kind in {"paragraph", "block_text"}:
            value = _render(children) + ("\n\n" if kind == "paragraph" else "\n")
        elif kind == "heading":
            command = _HEADINGS[token["attrs"]["level"] - 1]
            value = rf"\{command}*{{{_render(children)}}}" + "\n\n"
        elif kind in {"strong", "emphasis", "strikethrough"}:
            command = {"strong": "textbf", "emphasis": "emph", "strikethrough": "sout"}[kind]
            value = rf"\{command}{{{_render(children)}}}"
        elif kind == "codespan":
            value = rf"\texttt{{{_code_text(raw)}}}"
        elif kind == "block_code":
            # Escaped boxes also handle literal \end{verbatim} and ^^ safely.
            lines = [rf"\noindent\mbox{{\strut {_code_text(line)}}}\par" for line in raw.splitlines()]
            value = "\n".join([r"\begin{flushleft}\ttfamily\small", *lines, r"\end{flushleft}"]) + "\n\n"
        elif kind == "inline_math":
            value = r"\(" + raw + r"\)"
        elif kind == "block_math":
            value = "\\[\n" + raw + "\n\\]\n\n"
        elif kind == "block_quote":
            value = "\\begin{quote}\n" + _render(children) + "\\end{quote}\n\n"
        elif kind == "list":
            value = _list(token)
        elif kind == "table":
            value = _table(token)
        elif kind == "thematic_break":
            value = r"\noindent\rule{\linewidth}{0.4bp}" + "\n\n"
        elif kind in {"link", "image"}:
            value = _render(children)
        elif children:
            value = _render(children)
        else:
            value = escape_latex(raw)
        parts.append(value)
    return "".join(parts)


def markdown_to_latex(text: str) -> str:
    """Convert stored legacy Markdown once, preserving existing math source."""
    return _render(_MARKDOWN(text)).strip()


def _sections_to_markdown(sections: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for section in sections:
        value = section["text"].strip()
        if not value:
            continue
        kind = section["type"]
        if kind == "equation" and value.startswith("$$") and value.endswith("$$"):
            value = "$$\n" + value[2:-2].strip("\r\n") + "\n$$"
        if kind == "quote" and not value.lstrip().startswith(">"):
            value = "\n".join("> " + line if line else ">" for line in value.splitlines())
        label = _LABELS.get(kind)
        if label and not value.startswith((f"【{label}】", f"**{label}：**", f"{label}：")):
            value = f"**{label}：** {value}"
        parts.append(value)
    return "\n\n".join(parts)


def legacy_blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Preserve the oldest blocks before the one-time Markdown migration."""
    sections: list[dict[str, str]] = []
    for block in blocks:
        kind = str(block.get("type", "unknown"))
        value = str(block.get("text") or "").strip()
        if kind == "heading" and value and not value.startswith("#"):
            level = block.get("level")
            level = level if isinstance(level, int) and 1 <= level <= 6 else 2
            value = "#" * level + " " + value
        latex = block.get("latex")
        if kind == "equation" and isinstance(latex, str) and latex and latex not in value:
            value = (value + "\n\n" if value else "") + f"$$\n{latex}\n$$"
        rows = block.get("rows")
        if kind == "table" and isinstance(rows, list) and rows:
            table_rows = ["| " + " | ".join(str(cell).replace("|", r"\|") for cell in row) + " |"
                          for row in rows]
            separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
            table_text = "\n".join([table_rows[0], separator, *table_rows[1:]])
            if table_text not in value:
                value = (value + "\n\n" if value else "") + table_text
        if value:
            sections.append({"type": kind, "text": value})
    return _sections_to_markdown(sections)
