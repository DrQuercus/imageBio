"""SerialEM ``.mdoc`` metadata (also written by Tomo5 for tilt series).

An mdoc is a text file of ``Key = Value`` lines: global ones first, then sections opened by a
bracketed line such as ``[ZValue = 3]`` (one per image of a stack), ``[FrameSet = 0]`` (one per
saved movie) or ``[T = free text]`` (titles). Values are kept as text; :class:`MdocSection`
converts on demand, and :meth:`MdocSection.summary` gives the fields a live monitor uses in
fixed units.

Format reference: the SerialEM documentation ("Autodoc and mdoc files") and the key list of
``docs/research/cryoem-live.md``, section 2.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from imagebio.core.errors import CorruptFileError

__all__ = ["MAX_MDOC_BYTES", "Mdoc", "MdocSection", "parse_mdoc", "read_mdoc"]

MAX_MDOC_BYTES: Final = 32 * 2**20
_MAX_SECTIONS: Final = 100_000
_SECTION: Final = re.compile(r"\[\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*?)\s*\]\s*$")
_ITEM: Final = re.compile(r"\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=\s*(.*?)\s*$")
_DATE_FORMATS: Final = ("%d-%b-%y %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%b-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _floats(text: str | None) -> list[float] | None:
    if not text:
        return None
    try:
        values = [float(part) for part in text.split()]
    except ValueError:
        return None
    return values if values and all(math.isfinite(v) for v in values) else None


@dataclass(frozen=True, slots=True)
class MdocSection:
    """One bracketed section: ``[kind = value]`` and its items."""

    kind: str  # "ZValue", "FrameSet", "MontSection"…
    value: str
    items: dict[str, str] = field(default_factory=dict)

    def text(self, key: str) -> str | None:
        return self.items.get(key) or None

    def number(self, key: str) -> float | None:
        values = _floats(self.items.get(key))
        return values[0] if values else None

    def numbers(self, key: str) -> list[float] | None:
        return _floats(self.items.get(key))

    def acquired_at(self) -> float | None:
        """``DateTime`` as seconds since the epoch. SerialEM writes the local time of the
        microscope PC, without a zone: it is taken as the local time of this server."""
        text = " ".join((self.items.get("DateTime") or "").split())
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        return None

    def summary(self) -> dict[str, Any]:
        """The fields of this section a monitor uses; absent ones are left out.

        ``pixel_size`` in Å, ``defocus`` and ``target_defocus`` in µm, ``dose`` in e/Å²,
        ``exposure_time`` in s, ``tilt_angle`` in degrees, ``stage`` (x, y in µm, z in µm).
        """
        out: dict[str, Any] = {}
        for name, key in (
            ("tilt_angle", "TiltAngle"),
            ("pixel_size", "PixelSpacing"),
            ("defocus", "Defocus"),
            ("target_defocus", "TargetDefocus"),
            ("dose", "ExposureDose"),
            ("exposure_time", "ExposureTime"),
            ("magnification", "Magnification"),
            ("prior_dose", "PriorRecordDose"),
            ("frames", "NumSubFrames"),
        ):
            value = self.number(key)
            if value is not None:
                out[name] = int(value) if name == "frames" else value
        position = self.numbers("StagePosition")
        if position and len(position) >= 2:
            stage = {"x": position[0], "y": position[1]}
            z = self.number("StageZ")
            if z is not None:
                stage["z"] = z
            out["stage"] = stage
        when = self.acquired_at()
        if when is not None:
            out["acquired_at"] = when
        frames_path = self.text("SubFramePath")
        if frames_path:
            # a Windows path on the microscope PC: only the file name means something here
            out["frames_file"] = re.split(r"[\\/]", frames_path)[-1]
        return out


@dataclass(frozen=True, slots=True)
class Mdoc:
    header: dict[str, str]
    titles: tuple[str, ...]
    sections: tuple[MdocSection, ...]

    def images(self) -> tuple[MdocSection, ...]:
        """The sections that describe an image or a movie, in file order."""
        return tuple(s for s in self.sections if s.kind in ("ZValue", "FrameSet"))

    def pixel_size(self) -> float | None:
        """Å per pixel: of the first image section that gives it, else the global value."""
        for section in self.images():
            value = section.number("PixelSpacing")
            if value and value > 0:
                return value
        values = _floats(self.header.get("PixelSpacing"))
        return values[0] if values and values[0] > 0 else None


def parse_mdoc(data: bytes, name: str = "file.mdoc") -> Mdoc:
    """Parse the content of an mdoc file. Lines that are not ``Key = Value`` are ignored; a file
    that holds none at all is not an mdoc (:class:`CorruptFileError`)."""
    if len(data) > MAX_MDOC_BYTES:
        raise CorruptFileError(f"{name}: too large to be an mdoc file")
    if b"\0" in data[:4096]:
        raise CorruptFileError(f"{name}: binary data, not an mdoc file")
    header: dict[str, str] = {}
    titles: list[str] = []
    sections: list[MdocSection] = []
    current = header
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        opened = _SECTION.match(line)
        if opened:
            kind, value = opened.group(1), opened.group(2)
            if kind == "T":
                titles.append(value)
                continue
            if len(sections) >= _MAX_SECTIONS:
                raise CorruptFileError(f"{name}: too many sections for an mdoc file")
            items: dict[str, str] = {}
            sections.append(MdocSection(kind, value, items))
            current = items
            continue
        item = _ITEM.match(line)
        if item and len(current) < 10_000:
            current[item.group(1)] = item.group(2)
    if not header and not sections:
        raise CorruptFileError(f"{name}: no 'Key = Value' line, not an mdoc file")
    return Mdoc(header, tuple(titles), tuple(sections))


def read_mdoc(path: Path) -> Mdoc:
    """Read and parse an mdoc file. Raises :class:`CorruptFileError` (file name only)."""
    try:
        with path.open("rb") as f:
            data = f.read(MAX_MDOC_BYTES + 1)
    except OSError:
        raise CorruptFileError(f"{path.name}: cannot be read") from None
    return parse_mdoc(data, path.name)
