"""Conservative layout changes for exported bodies; stored OCR stays untouched."""

from __future__ import annotations

import re
from dataclasses import dataclass


_BREAK = re.compile(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*")
_TOKEN = re.compile(
    r"(?P<verbatim>\\begin\s*\{verbatim\}.*?\\end\s*\{verbatim\})"
    r"|(?P<verb>\\verb\*?(?P<delimiter>[^a-zA-Z\s])[^\r\n]*?(?P=delimiter))"
    r"|(?P<inline>\\\(.*?\\\))"
    r"|(?P<display>\\\[.*?\\\])"
    r"|\\(?P<action>begin|end)\s*\{[^{}]+\}"
    r"|(?P<escape>\\.)"
    r"|(?P<comment>%[^\r\n]*(?:\r?\n|$))",
    re.DOTALL,
)
_NUMBER = re.compile(r"([0-9]+)([.．、)）])(?=[ \t]*\S)")
_BOUNDARY = re.compile(r"\\(?:section|subsection|subsubsection|paragraph|subparagraph|item|par)\b")
_DISPLAY_STRUCTURE = re.compile(r"\\(?:begin|end|tag|label)\b|\\\\|&|%")
_DISPLAY_NUMBER = re.compile(r"(?:\\text\{)?[（(]\s*[0-9]+(?:[.-][0-9]+)*\s*[)）]\}?\s*[.,，。;；]?$")
_CONTINUATION = re.compile(r"^(?:称为|其中|式中|这里|即为|亦即)")


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    kind: str


def _paragraphs(text: str, start: int, end: int) -> list[_Block]:
    blocks: list[_Block] = []
    cursor = start
    for boundary in [*_BREAK.finditer(text, start, end), None]:
        stop = boundary.start() if boundary else end
        paragraph = text[cursor:stop]
        left = cursor + len(paragraph) - len(paragraph.lstrip())
        right = stop - len(paragraph) + len(paragraph.rstrip())
        if left < right:
            kind = "protected" if _BOUNDARY.match(text[left:right]) else "text"
            blocks.append(_Block(left, right, kind))
        cursor = boundary.end() if boundary else end
    return blocks


def _blocks(text: str) -> list[_Block]:
    """Find outer prose/displays, treating every explicit environment as opaque."""
    blocks: list[_Block] = []
    cursor = 0
    environment_start = 0
    depth = 0
    for token in _TOKEN.finditer(text):
        action = token.group("action")
        if action == "begin":
            if not depth:
                blocks.extend(_paragraphs(text, cursor, token.start()))
                environment_start = token.start()
            depth += 1
        elif action == "end" and depth:
            depth -= 1
            if not depth:
                blocks.append(_Block(environment_start, token.end(), "protected"))
                cursor = token.end()
        elif not depth and token.lastgroup in {"display", "verbatim", "verb", "comment"}:
            blocks.extend(_paragraphs(text, cursor, token.start()))
            kind = "display" if token.lastgroup == "display" else "protected"
            blocks.append(_Block(token.start(), token.end(), kind))
            cursor = token.end()
    if depth:
        blocks.append(_Block(environment_start, len(text), "protected"))
    else:
        blocks.extend(_paragraphs(text, cursor, len(text)))
    return blocks


def _number(text: str, block: _Block) -> re.Match[str] | None:
    if block.kind != "text":
        return None
    match = _NUMBER.match(text, block.start, block.end)
    # A decimal at paragraph start is not an exercise number.
    if match and match[2] in ".．" and text[match.end():match.end() + 1].isdigit():
        return None
    return match


def _runs(text: str, blocks: list[_Block], marks: str) -> list[list[tuple[int, re.Match[str]]]]:
    runs: list[list[tuple[int, re.Match[str]]]] = []
    current: list[tuple[int, re.Match[str]]] = []
    previous: re.Match[str] | None = None
    for index, block in enumerate(blocks):
        number = _number(text, block)
        if number is None:
            continue
        if number[2] not in marks:
            if marks == ")）":
                if len(current) >= 2:
                    runs.append(current)
                current = []
                previous = None
            continue
        if previous is None or number[2] != previous[2] or int(number[1]) != int(previous[1]) + 1:
            if len(current) >= 2:
                runs.append(current)
            current = []
        current.append((index, number))
        previous = number
    if len(current) >= 2:
        runs.append(current)
    return runs


def _continued(text: str, before: _Block, after: _Block | None) -> bool:
    prose = text[before.start:before.end]
    return prose.endswith((":", "：", ",", "，", "则")) or (
        after is not None
        and after.kind == "text"
        and not prose.endswith(("。", ".", "！", "!", "？", "?", "；", ";"))
        and bool(_CONTINUATION.match(text[after.start:after.end]))
    )


def _tail(text: str, blocks: list[_Block], index: int) -> int:
    """Only extend a final item across visibly continuous display/prose edges."""
    while index + 1 < len(blocks):
        before, after = blocks[index:index + 2]
        following = blocks[index + 2] if index + 2 < len(blocks) else None
        if before.kind == "text" and after.kind == "display" and _continued(text, before, following):
            index += 1
        elif before.kind == "display" and after.kind == "display":
            index += 1
        elif before.kind == "display" and after.kind == "text" and _number(text, after) is None and (
            text[before.start + 2:before.end - 2].rstrip().endswith((",", "，"))
            or _CONTINUATION.match(text[after.start:after.end])
        ):
            index += 1
        else:
            break
    return index


def _list_edits(text: str, blocks: list[_Block]) -> list[tuple[int, int, str]]:
    edits: list[tuple[int, int, str]] = []

    def add_list(items: list[tuple[int, re.Match[str]]], end: int) -> None:
        for position, (index, number) in enumerate(items):
            block = blocks[index]
            opening = "\\begin{enumerate}\n" if position == 0 else ""
            spacing = "" if text[number.end():number.end() + 1].isspace() else " "
            edits.append((block.start, number.end(), opening + r"\item[" + number[0] + "]" + spacing))
        edits.append((blocks[end].end, blocks[end].end, "\n\\end{enumerate}"))

    majors = _runs(text, blocks, ".．、")
    if not majors:
        for items in _runs(text, blocks, ")）"):
            add_list(items, _tail(text, blocks, items[-1][0]))
        return edits
    for items in majors:
        last = _tail(text, blocks, items[-1][0])
        for position, (index, _) in enumerate(items):
            limit = next(
                (candidate for candidate in range(index + 1, len(blocks))
                 if (number := _number(text, blocks[candidate])) is not None and number[2] in ".．、"),
                len(blocks),
            )
            children = blocks[index + 1:limit]
            for child_run in _runs(text, children, ")）"):
                # Only a reset to 1 clearly denotes a subordinate series.
                if int(child_run[0][1][1]) != 1:
                    continue
                child_items = [(index + 1 + child, number) for child, number in child_run]
                child_end = index + 1 + _tail(text, children, child_run[-1][0])
                add_list(child_items, child_end)
                if position + 1 == len(items):
                    last = max(last, child_end)
        add_list(items, last)
    return edits


def _display_edits(text: str, blocks: list[_Block]) -> list[tuple[int, int, str]]:
    edits: list[tuple[int, int, str]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.kind != "display":
            index += 1
            continue
        end = index
        while end + 1 < len(blocks) and blocks[end + 1].kind == "display":
            contents = [text[item.start + 2:item.end - 2].strip() for item in blocks[index:end + 2]]
            if any(not content or _DISPLAY_STRUCTURE.search(content) or _DISPLAY_NUMBER.search(content) for content in contents):
                break
            end += 1
        if end > index:
            rows = (r" \\" + "\n").join(text[item.start + 2:item.end - 2].strip() for item in blocks[index:end + 1])
            edits.append((block.start, blocks[end].end, "\\[\n\\begin{gathered}\n" + rows + "\n\\end{gathered}\n\\]"))
        previous = blocks[index - 1] if index else None
        following = blocks[end + 1] if end + 1 < len(blocks) else None
        if previous and previous.kind == "text" and _continued(text, previous, following):
            if _BREAK.search(text[previous.end:block.start]):
                edits.append((previous.end, block.start, "\n"))
        if following and following.kind == "text" and _number(text, following) is None and (
            text[blocks[end].start + 2:blocks[end].end - 2].rstrip().endswith((",", "，"))
            or _CONTINUATION.match(text[following.start:following.end])
        ):
            if _BREAK.search(text[blocks[end].end:following.start]):
                edits.append((blocks[end].end, following.start, "\n"))
        index = end + 1
    return edits


def normalize_latex_layout(text: str) -> str:
    """Organize clear numeric lists and display boundaries without rewriting math.

    The caller validates the original fragment first. Explicit environments,
    literal code, comments, lone numbers and ambiguous formula structures stay
    intact. This function is for preview/export, never a storage migration.
    """
    blocks = _blocks(text)
    edits = _display_edits(text, blocks)
    region: list[_Block] = []
    for block in [*blocks, _Block(len(text), len(text), "protected")]:
        if block.kind == "protected":
            edits.extend(_list_edits(text, region))
            region = []
        else:
            region.append(block)
    # Apply source positions backwards; equal-position inserts retain their
    # intended order, so subordinate lists close before their enclosing list.
    for _, (start, end, replacement) in sorted(enumerate(edits), key=lambda edit: (edit[1][0], edit[1][1], edit[0]), reverse=True):
        text = text[:start] + replacement + text[end:]
    return text
