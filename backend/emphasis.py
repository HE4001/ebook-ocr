"""Apply exact source spans without rewriting the transcribed LaTeX."""

from __future__ import annotations

import re

from .latex_content import validate_latex_fragment
from .models import EmphasisResult


_PREFIXES = {
    "bold": r"{\bfseries\boldmath ",
    "boldsymbol": r"\boldsymbol{",
    "pmb": r"\pmb{",
}
_TOKENS = re.compile(r"\\(?:[A-Za-z]+|[\s\S])|%[^\r\n]*|[{}]")
_VERBATIM_ARGUMENT = re.compile(r"\s*\{verbatim\}")


def _validate_span(original: str, start: int, end: int) -> None:
    fragment = original[start:end]
    validate_latex_fragment(fragment)
    braces = 0
    math: list[str] = []
    skip_until = 0
    ends_with_command = False
    for token_match in _TOKENS.finditer(original):
        token_start, token_end = token_match.span()
        if token_start < skip_until:
            continue
        if token_start >= end:
            break
        token = token_match.group()
        raw_end = None
        if token.startswith("%"):
            raw_end = token_end
        elif token == r"\verb":
            delimiter_at = token_end + (original[token_end:token_end + 1] == "*")
            delimiter = original[delimiter_at:delimiter_at + 1]
            if not delimiter or delimiter.isspace() or delimiter.isalpha():
                raise ValueError("字重复核正文中的 \\verb 分隔符无效")
            closing = original.find(delimiter, delimiter_at + 1)
            if closing < 0:
                raise ValueError("字重复核正文中的 \\verb 未闭合")
            raw_end = closing + 1
        elif token == r"\begin":
            argument = _VERBATIM_ARGUMENT.match(original, token_end)
            if argument:
                closing = original.find(r"\end{verbatim}", argument.end())
                if closing < 0:
                    raise ValueError("字重复核正文中的 verbatim 未闭合")
                raw_end = closing + len(r"\end{verbatim}")
        if raw_end is not None:
            if token_start < end and raw_end > start:
                raise ValueError("字重复核范围不能接触注释、verb 或 verbatim 字面区域")
            skip_until = raw_end
            continue
        if token_start < start < token_end or token_start < end < token_end:
            raise ValueError("字重复核范围不能截断 LaTeX 命令或转义字符")
        if token_start < start:
            continue
        if token.startswith("\\") and token[1:].isascii() and token[1:].isalpha():
            ends_with_command = original[token_end:end].strip() in {"", "*"}
        if token == "{":
            braces += 1
        elif token == "}":
            braces -= 1
            if braces < 0:
                raise ValueError("字重复核范围跨越了已有 LaTeX 分组边界")
        elif token in {r"\(", r"\["}:
            math.append(token)
        elif token in {r"\)", r"\]"}:
            opening = r"\(" if token == r"\)" else r"\["
            if not math or math.pop() != opening:
                raise ValueError("字重复核范围必须保留完整的数学定界符")
    if braces or math:
        raise ValueError("字重复核范围包含未闭合的 LaTeX 分组或数学定界符")
    following = original[end:].lstrip()
    if following.startswith(("{", "[")) and (
        ends_with_command or fragment.rstrip().endswith(("}", "]"))
    ):
        raise ValueError("字重复核范围不能在命令与参数之间或连续参数组之间结束，请选择完整片段")


def apply_emphasis(original: str, result: EmphasisResult) -> str:
    ranges: list[tuple[int, int, str]] = []
    for index, span in enumerate(result.spans, start=1):
        search_from = 0
        for _ in range(span.occurrence):
            start = original.find(span.fragment, search_from)
            if start < 0:
                raise ValueError(
                    f"字重复核片段 {index} 在正文中找不到第 {span.occurrence} 次精确匹配"
                )
            search_from = start + len(span.fragment)
        _validate_span(original, start, search_from)
        ranges.append((start, search_from, span.style))

    # Outer spans open first. A stack permits containment and adjacent spans,
    # while rejecting identical or crossing ranges before any insertion.
    ranges.sort(key=lambda span: (span[0], -span[1]))
    ends: list[int] = []
    previous: tuple[int, int] | None = None
    for start, end, _ in ranges:
        if previous == (start, end):
            raise ValueError("字重复核重复指定了同一正文范围")
        previous = (start, end)
        while ends and start >= ends[-1]:
            ends.pop()
        if ends and end > ends[-1]:
            raise ValueError("字重复核片段范围交叉，只允许分离、相邻或完整包含的范围")
        ends.append(end)

    events: list[tuple[int, int, int, str]] = []
    for start, end, style in ranges:
        events.append((start, 1, -end, _PREFIXES[style]))
        events.append((end, 0, -start, "}"))
    # At a shared offset close existing groups before opening adjacent groups;
    # nested groups close inside-out and open outside-in.
    events.sort()
    parts: list[str] = []
    cursor = 0
    for position, _, _, marker in events:
        parts.append(original[cursor:position])
        parts.append(marker)
        cursor = position
    parts.append(original[cursor:])
    return "".join(parts)
