"""Escape plain text and distinguish explicit LaTeX documents from fragments."""

from __future__ import annotations

import re


_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
    "%": r"\%", "&": r"\&", "_": r"\_", "#": r"\#", "$": r"\$",
    "^": r"\textasciicircum{}", "~": r"\textasciitilde{}",
}

_SPACE_OR_COMMENT = r"(?:\s|%[^\r\n]*(?:\r\n?|\n|\Z))*"
_OPTIONS = rf"(?:\[[^\[\]]*\]{_SPACE_OR_COMMENT})?"
_ARGUMENT = rf"\{{[^{{}}]+\}}{_SPACE_OR_COMMENT}"
_PRECLASS = (
    rf"(?:\\RequirePackage{_SPACE_OR_COMMENT}{_OPTIONS}{_ARGUMENT}{_OPTIONS}"
    rf"|\\PassOptionsTo(?:Package|Class){_SPACE_OR_COMMENT}{_ARGUMENT}{_ARGUMENT})"
)
_DOCUMENT_START = re.compile(
    rf"\A{_SPACE_OR_COMMENT}(?:{_PRECLASS})*"
    rf"\\documentclass{_SPACE_OR_COMMENT}{_OPTIONS}\{{[^{{}}]+\}}"
)


def escape_latex(text: str) -> str:
    return "".join(_ESCAPES.get(character, character) for character in text)


def is_latex_document(source: str) -> bool:
    """Recognize a leading documentclass, not TeX source printed inside a fragment.

    Whitespace, comments, RequirePackage and PassOptionsToPackage/Class may
    precede it. This format check neither validates TeX nor expands macros.
    """
    return _DOCUMENT_START.match(source) is not None
