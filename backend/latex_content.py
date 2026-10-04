"""Escape plain text and distinguish explicit LaTeX documents from fragments."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath


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


def source_resource_name(name: str) -> str:
    """Keep source resources relative to the book and its exported source tree."""
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (path.is_absolute() or PureWindowsPath(name).drive or ".." in path.parts
            or not path.parts or any(character in normalized for character in "{}\r\n")):
        raise ValueError("源区域资源必须是书目录内的相对路径")
    return path.as_posix()


def latex_image_resources(source: str) -> tuple[str, ...]:
    """Collect explicit relative image references without rewriting custom source."""
    names = re.findall(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}", source)
    return tuple(dict.fromkeys(source_resource_name(name.strip()) for name in names))
