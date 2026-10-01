from __future__ import annotations

import re
from pathlib import Path


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
        page_number = None
        if line_match:
            source_line = int(line_match.group(1))
            page_number = next(
                (number for start, number in reversed(pages) if start <= source_line),
                None,
            )
        location = f"编排第 {page_number} 页：" if page_number is not None else ""
        dimension = "宽度" if match.group(1) == "h" else "高度"
        warning = f"{location}内容超出可用{dimension} {float(match.group(2)):.2f} pt。"
        if warning not in warnings:
            warnings.append(warning)
    return warnings
