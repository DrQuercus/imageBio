"""What the programs write, read back: the results file of CTFFIND, the frame shifts of a
motion correction (RELION-style STAR, or the three-column log of MotionCor).

These follow the formats as published and as RELION reads them. They were written without
the programs at hand and checked on files made to those formats: see ADR 011.
"""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path
from typing import Any, Final

from imagebio.core.errors import CorruptFileError
from imagebio.cryo.star import find_table

__all__ = ["MAX_RESULT_BYTES", "PARSERS", "parse_ctffind", "parse_motion_log", "parse_motion_star"]

MAX_RESULT_BYTES: Final = 1 << 20
_MAX_FRAMES: Final = 20_000
_EARLY_STEPS: Final = 5  # the first frames, where beam-induced motion is the largest
_COLUMN: Final = re.compile(r"#\s*(\d+)\s*-?\s*([^;#]+)")
#: Columns of the CTFFIND results file when its header does not say (versions 4 and 5 agree).
_CTFFIND_ORDER: Final = ("number", "defocus_1", "defocus_2", "angle", "phase", "cc", "fit")
_CTFFIND_NAMES: Final = (  # a word of the header -> what the column is
    ("defocus 1", "defocus_1"),
    ("defocus 2", "defocus_2"),
    ("azimuth", "angle"),
    ("phase shift", "phase"),
    ("cross correlation", "cc"),
    ("spacing", "fit"),
    ("tilt axis", "tilt_axis"),
    ("tilt angle", "tilt"),
    ("thickness", "thickness"),
)


def _read(path: Path) -> str:
    with open(path, "rb") as f:
        raw = f.read(MAX_RESULT_BYTES + 1)
    if len(raw) > MAX_RESULT_BYTES:
        raise CorruptFileError("the result file is too large")
    return raw.decode("utf-8", "replace")


def _floats(line: str) -> list[float] | None:
    try:
        values = [float(v) for v in line.split()]
    except ValueError:
        return None
    return values if all(math.isfinite(v) for v in values) else None


def parse_ctffind(path: Path, apix: float | None = None) -> dict[str, Any]:
    """The fit of CTFFIND (``<output>.txt``): defocus along both axes and their mean (µm),
    astigmatism (µm) and its angle (°), phase shift (rad, when not 0), cross-correlation, and
    the resolution (Å) up to which the rings were fitted; tilt (°) and thickness (nm) when the
    program gives them (version 5). Raises :class:`CorruptFileError` when there is no such fit."""
    del apix  # CTFFIND writes ångströms: the pixel size is not needed
    names = list(_CTFFIND_ORDER)
    row: list[float] | None = None
    for line in _read(path).splitlines():
        text = line.strip()
        if text.startswith("#"):
            if "columns" in text.lower():
                found = {int(n): label.strip().lower() for n, label in _COLUMN.findall(text)}
                if found:
                    names = ["?"] * max(found)
                    for position, label in found.items():
                        names[position - 1] = next(
                            (key for word, key in _CTFFIND_NAMES if word in label), "?"
                        )
        elif text:
            values = _floats(text)
            if values is not None and len(values) >= 7:
                row = values  # the last line counts: one micrograph per run
    if row is None:
        raise CorruptFileError("no fit in the result file of the CTF program")
    got = {name: row[i] for i, name in enumerate(names[: len(row)]) if name != "?"}
    try:
        first, second = got["defocus_1"], got["defocus_2"]
    except KeyError:
        raise CorruptFileError("the result file of the CTF program names no defocus") from None
    if not (0 < first <= 5e5 and 0 < second <= 5e5):  # ångströms: 50 µm at the very most
        raise CorruptFileError("the CTF program gave a defocus that makes no sense")
    out: dict[str, Any] = {
        "defocus": round((first + second) / 2e4, 4),
        "defocus_u": round(max(first, second) / 1e4, 4),
        "defocus_v": round(min(first, second) / 1e4, 4),
        "astigmatism": round(abs(first - second) / 1e4, 4),
    }
    if "angle" in got:
        out["angle"] = round(got["angle"], 1)
    if got.get("phase"):
        out["phase"] = round(got["phase"], 3)
    if "cc" in got:
        out["cc"] = round(got["cc"], 3)
    if got.get("fit", 0) > 0:
        out["fit"] = round(got["fit"], 2)
    if "tilt" in got:
        out["tilt"] = round(got["tilt"], 1)
    if got.get("thickness", 0) > 0:
        out["thickness"] = round(got["thickness"] / 10, 1)  # Å -> nm
    return out


def _motion(shifts: list[tuple[float, float]], apix: float | None) -> dict[str, Any]:
    """How far the specimen moved during a movie, from the position of each frame (pixels)."""
    if len(shifts) < 2:
        raise CorruptFileError("the motion correction gave fewer than two frames")
    steps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(shifts)]
    scale = apix if apix is not None and apix > 0 else 1.0
    return {
        "total": round(sum(steps) * scale, 2),
        "early": round(sum(steps[:_EARLY_STEPS]) * scale, 2),
        "max_step": round(max(steps) * scale, 2),
        "frames": len(shifts),
        "unit": "A" if apix is not None and apix > 0 else "px",
    }


def parse_motion_star(path: Path, apix: float | None = None) -> dict[str, Any]:
    """The frame shifts of a RELION-style motion file (table ``global_shift``:
    ``_rlnMicrographShiftX`` and ``Y``, in pixels of the movie), summed up: total path, path of
    the first frames, largest step. In Å when ``apix`` (of the movie) is given."""
    table = find_table(path, ("rlnMicrographShiftX", "rlnMicrographShiftY"))
    if table is None:
        raise CorruptFileError("no frame shifts in the motion file")
    shifts: list[tuple[float, float]] = []
    for x, y in table.rows(("rlnMicrographShiftX", "rlnMicrographShiftY")):
        values = _floats(f"{x} {y}")
        if values is None:
            raise CorruptFileError("a frame shift of the motion file is not a number")
        shifts.append((values[0], values[1]))
        if len(shifts) > _MAX_FRAMES:
            raise CorruptFileError("too many frames in the motion file")
    return _motion(shifts, apix)


def parse_motion_log(path: Path, apix: float | None = None) -> dict[str, Any]:
    """The frame shifts of a MotionCor log (lines ``frame  x  y``, in pixels; ``#`` comments)."""
    shifts: list[tuple[float, float]] = []
    for line in _read(path).splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        values = _floats(text)
        if values is None or len(values) < 3:
            continue  # a title, or a line of words
        shifts.append((values[1], values[2]))
        if len(shifts) > _MAX_FRAMES:
            raise CorruptFileError("too many frames in the motion log")
    return _motion(shifts, apix)


PARSERS: Final = {
    "ctffind": parse_ctffind,
    "motion-star": parse_motion_star,
    "motion-log": parse_motion_log,
}
