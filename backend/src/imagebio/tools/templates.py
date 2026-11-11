"""The placeholders of a declared command: ``{movie}``, ``{out}``, ``{apix}``...

A command is a list of arguments written by the administrator. Each argument is a template;
filling it replaces the placeholders inside that one argument, which stays one argument
whatever the value holds (spaces, quotes, dashes): nothing is ever split or handed to a shell.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Final

__all__ = ["PLACEHOLDERS", "MissingValueError", "UnsafeValueError", "check_template", "fill", "number"]

#: What a template may name, and what it stands for.
PLACEHOLDERS: Final[Mapping[str, str]] = {
    "movie": "the movie of the exposure (a path on the server)",
    "image": "the micrograph: the sum the motion step wrote, else the image the microscope integrated",
    "out": "a directory made for this run, removed afterwards",
    "apix": "pixel size of the micrograph, Å",
    "kv": "accelerating voltage, kV",
    "cs": "spherical aberration, mm",
    "amp": "amplitude contrast",
    "dose": "dose per frame, e/Å²",
    "frames": "number of frames of the movie",
}
_PLACEHOLDER: Final = re.compile(r"\{([a-z_]+)\}")
_UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f]")


class MissingValueError(Exception):
    """A template names something that is not known for this exposure."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class UnsafeValueError(Exception):
    """A value holds a control character: in the answers typed to a program, a line break in a
    file name would be one more answer."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def check_template(template: str, allowed: Collection[str] = tuple(PLACEHOLDERS)) -> None:
    """Raise :class:`ValueError` when ``template`` names an unknown placeholder, holds a brace
    that is not one, or a control character."""
    for name in _PLACEHOLDER.findall(template):
        if name not in allowed:
            raise ValueError(f"unknown placeholder {{{name}}} (known: {', '.join(sorted(allowed))})")
    rest = _PLACEHOLDER.sub("", template)
    if "{" in rest or "}" in rest:
        raise ValueError("a brace that is not a placeholder: braces cannot be written literally")
    if _UNSAFE.search(template):
        raise ValueError("control characters are not allowed")


def fill(template: str, values: Mapping[str, str | None]) -> str:
    """``template`` with its placeholders replaced. Raises :class:`MissingValueError` for one
    whose value is None or absent, :class:`UnsafeValueError` for a value with a control character."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values.get(name)
        if value is None:
            raise MissingValueError(name)
        if _UNSAFE.search(value):
            raise UnsafeValueError(name)
        return value

    return _PLACEHOLDER.sub(replace, template)


def number(value: float | int | None) -> str | None:
    """A number as a program reads it (``0.834``, ``300``); None stays None."""
    if value is None or isinstance(value, bool):
        return None
    text = format(float(value), ".6g")
    return text if "e" not in text else format(float(value), "f")
