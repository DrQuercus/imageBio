"""Particle coordinates, to be drawn over a micrograph.

Four kinds of files are read:

- RELION ``.star``: columns ``rlnCoordinateX`` / ``rlnCoordinateY``, the centres in pixels. A
  file of a whole data set names the micrograph of each particle (``rlnMicrographName``): only
  those of the image shown are kept.
- EMAN ``.box``: one box per line, ``x y width height``, ``x y`` being a corner of the box.
- crYOLO ``.cbox``: the same boxes with a confidence, as a STAR table (``data_cryolo``) or, in
  older versions, as plain lines.
- cryoSPARC ``.cs``: a NumPy table whose ``location/center_x_frac`` and ``center_y_frac`` are
  fractions of the micrograph named by ``location/micrograph_path``.

**Convention.** A coordinate ``(x, y)`` is taken as column ``x`` and row ``y`` of the image as
it is stored, row 0 being the first row of the file, which the viewer draws at the top. Programs
that count rows from the other end exist (and some flip images when they import them): the
interface has a "flip Y" switch, and nothing is flipped silently. Not checked against files
written by the real programs yet.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import CorruptFileError, InvalidRequestError, UnsupportedFormatError
from imagebio.cryo.star import MAX_LINE_BYTES, find_table

__all__ = ["MAX_POINTS", "PICK_SUFFIXES", "Picks", "micrograph_key", "read_picks"]

PICK_SUFFIXES: Final = (".star", ".box", ".cbox", ".cs")
#: Points returned for one image: more than anybody can look at, and a bound on the answer.
MAX_POINTS: Final = 50_000
_MAX_TEXT_BYTES: Final = 2 * 2**30  # a particles.star of a large data set
_MAX_BOX_BYTES: Final = 64 * 2**20  # box files are about one micrograph
_MAX_CS_BYTES: Final = 2 * 2**30
#: What programs add to the name of a micrograph along its processing; removed to compare names.
_NAME_SUFFIXES: Final = (
    "_patch_aligned_doseweighted",
    "_rigid_aligned_doseweighted",
    "_patch_aligned",
    "_rigid_aligned",
    "_doseweighted",
    "_fractions",
    "_frames",
    "_movie",
    "_autopick",
    "_manualpick",
    "_eer",
    "_dw",
)
_CRYOSPARC_UID: Final = re.compile(r"^\d{15,}_")


def micrograph_key(name: str) -> str:
    """What two names of the same micrograph have in common: no directory, no extension, no
    cryoSPARC identifier in front, no processing suffix, lower case."""
    stem = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = _CRYOSPARC_UID.sub("", stem).lower()
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem, changed = stem[: -len(suffix)], True
    return stem


@dataclass(frozen=True, slots=True)
class Picks:
    """The particles of one image. ``x`` and ``y`` are centres, in pixels of that image."""

    format: str  # star | box | cbox | cs
    x: npt.NDArray[np.float32]
    y: npt.NDArray[np.float32]
    #: Figure of merit or confidence of each particle, when the file has one.
    score: npt.NDArray[np.float32] | None
    #: Size of the boxes in pixels (their median), when the file has boxes.
    box: float | None
    #: Particles of this image in the file, before the answer was cut at MAX_POINTS.
    total: int
    #: True when the file names micrographs: it can be kept when another image is opened.
    many: bool
    notes: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        def rounded(values: npt.NDArray[np.float32], digits: int) -> list[float]:
            return [round(float(v), digits) for v in values]

        return {
            "format": self.format,
            "count": int(self.x.size),
            "total": self.total,
            "x": rounded(self.x, 1),
            "y": rounded(self.y, 1),
            "score": rounded(self.score, 4) if self.score is not None else None,
            "box": self.box,
            "many": self.many,
            "notes": list(self.notes),
        }


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise CorruptFileError("the coordinate file has a value that is not a number") from None
    if not math.isfinite(value):
        raise CorruptFileError("the coordinate file has a value that is not finite")
    return value


class _Collector:
    """Accumulates the particles of the image, counting those beyond the limit."""

    def __init__(self) -> None:
        self.x: list[float] = []
        self.y: list[float] = []
        self.score: list[float] = []
        self.size: list[float] = []
        self.total = 0

    def add(self, x: float, y: float, score: float | None = None, size: float | None = None) -> None:
        self.total += 1
        if len(self.x) >= MAX_POINTS:
            return
        self.x.append(x)
        self.y.append(y)
        if score is not None:
            self.score.append(score)
        if size is not None:
            self.size.append(size)

    def picks(self, kind: str, many: bool, notes: list[str], shape: tuple[int, int]) -> Picks:
        x = np.asarray(self.x, np.float32)
        y = np.asarray(self.y, np.float32)
        if self.total > x.size:
            notes.append(f"{self.total} particles in the file for this image: the first {x.size} are shown")
        outside = int(np.count_nonzero((x < 0) | (y < 0) | (x > shape[1]) | (y > shape[0])))
        if outside:
            notes.append(
                f"{outside} of {x.size} particles lie outside the image: "
                "the file may be about another image, or another binning"
            )
        score = np.asarray(self.score, np.float32) if len(self.score) == x.size and x.size else None
        box = float(np.median(self.size)) if self.size else None
        return Picks(kind, x, y, score, box if box and box > 0 else None, self.total, many, tuple(notes))


def _check_size(path: Path, limit: int) -> None:
    if path.stat().st_size > limit:
        raise InvalidRequestError("the coordinate file is too large to be read")


def _read_star(path: Path, image_name: str, into: _Collector) -> tuple[bool, list[str]]:
    _check_size(path, _MAX_TEXT_BYTES)
    table = find_table(path, ("rlnCoordinateX", "rlnCoordinateY"))
    if table is None:
        raise CorruptFileError("no particle coordinates (rlnCoordinateX, rlnCoordinateY) in this STAR file")
    columns = ["rlnCoordinateX", "rlnCoordinateY"]
    many = table.has("rlnMicrographName")
    scored = table.has("rlnAutopickFigureOfMerit")
    if many:
        columns.append("rlnMicrographName")
    if scored:
        columns.append("rlnAutopickFigureOfMerit")
    key = micrograph_key(image_name)
    names: set[str] = set()
    for row in table.rows(columns):
        if many:
            name = row[2]
            if micrograph_key(name) != key:
                if len(names) < 1000:
                    names.add(name)
                continue
        into.add(_number(row[0]), _number(row[1]), _number(row[-1]) if scored else None)
    notes: list[str] = []
    if many and into.total == 0:
        notes.append(f"none of the micrographs named in the file ({len(names)} or more) is this image")
    return many, notes


def _box_line(values: list[str], into: _Collector) -> None:
    x, y, w, h = (_number(v) for v in values[:4])
    score = _number(values[4]) if len(values) >= 5 else None
    into.add(x + w / 2, y + h / 2, score, max(w, h))


def _read_boxes(path: Path, into: _Collector, *, scored: bool) -> None:
    """Plain lines ``x y width height [confidence …]``."""
    _check_size(path, _MAX_BOX_BYTES)
    with open(path, "rb") as f:
        while True:
            raw = f.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES:
                raise CorruptFileError("not a box file: a line is too long")
            values = raw.decode("utf-8", "replace").split()
            if not values or values[0].startswith("#"):
                continue
            if len(values) < 4:
                raise CorruptFileError("a box file has four numbers per line: x, y, width, height")
            _box_line(values if scored else values[:4], into)


def _read_cbox(path: Path, into: _Collector) -> None:
    _check_size(path, _MAX_BOX_BYTES)
    with open(path, "rb") as f:
        head = f.read(4096)
    if b"data_" not in head and b"loop_" not in head:
        _read_boxes(path, into, scored=True)  # the format of crYOLO before 1.8
        return
    table = find_table(path, ("CoordinateX", "CoordinateY", "Width", "Height"))
    if table is None:
        raise CorruptFileError("no boxes (CoordinateX, CoordinateY, Width, Height) in this cbox file")
    columns = ["CoordinateX", "CoordinateY", "Width", "Height"]
    scored = table.has("Confidence")
    if scored:
        columns.append("Confidence")
    for row in table.rows(columns):
        _box_line(list(row), into)


def _read_cs(path: Path, image_name: str, shape: tuple[int, int], into: _Collector) -> tuple[bool, list[str]]:
    _check_size(path, _MAX_CS_BYTES)
    try:
        table = np.load(path, mmap_mode="r", allow_pickle=False)
    except (ValueError, OSError, EOFError):
        raise CorruptFileError("not a cryoSPARC table (.cs)") from None
    names = table.dtype.names or ()
    need = ("location/center_x_frac", "location/center_y_frac")
    if table.ndim != 1 or not all(n in names for n in need):
        raise CorruptFileError("no particle locations (location/center_x_frac) in this cryoSPARC file")
    notes: list[str] = []
    many = "location/micrograph_path" in names
    rows = np.arange(table.shape[0])
    if many:
        key = micrograph_key(image_name)
        paths = np.asarray(table["location/micrograph_path"])
        unique, inverse = np.unique(paths, return_inverse=True)
        wanted = np.array(
            [
                micrograph_key(u.decode("utf-8", "replace") if isinstance(u, bytes) else str(u)) == key
                for u in unique
            ]
        )
        rows = np.flatnonzero(wanted[inverse])
        if rows.size == 0:
            notes.append(f"none of the {unique.size} micrographs named in the file is this image")
    fx = np.asarray(table["location/center_x_frac"][rows], np.float64)
    fy = np.asarray(table["location/center_y_frac"][rows], np.float64)
    if not (np.isfinite(fx).all() and np.isfinite(fy).all()):
        raise CorruptFileError("the cryoSPARC file has a location that is not finite")
    score = None
    if "pick_stats/ncc_score" in names:
        score = np.asarray(table["pick_stats/ncc_score"][rows], np.float64)
    if "location/micrograph_shape" in names and rows.size:
        recorded = tuple(int(v) for v in np.asarray(table["location/micrograph_shape"][rows[0]]).ravel()[:2])
        if recorded != shape:
            notes.append(
                f"the file was made for a micrograph of {recorded[1]} x {recorded[0]} pixels: "
                "the positions are scaled to this image"
            )
    for k in range(rows.size):
        into.add(
            float(fx[k]) * shape[1],
            float(fy[k]) * shape[0],
            float(score[k])
            if score is not None and math.isfinite(score[k])
            else (0.0 if score is not None else None),
        )
    return many, notes


def read_picks(path: Path, image_name: str, shape: tuple[int, int]) -> Picks:
    """The particles that the coordinate file ``path`` holds for the image ``image_name``,
    whose size is ``shape`` (rows, columns). The kind of file is told by its extension."""
    suffix = path.suffix.lower()
    into = _Collector()
    many = False
    notes: list[str] = []
    try:
        if suffix == ".star":
            many, notes = _read_star(path, image_name, into)
        elif suffix == ".box":
            _read_boxes(path, into, scored=False)
        elif suffix == ".cbox":
            _read_cbox(path, into)
        elif suffix == ".cs":
            many, notes = _read_cs(path, image_name, shape, into)
        else:
            raise UnsupportedFormatError(f"not a coordinate file: {', '.join(PICK_SUFFIXES)} are read")
    except FileNotFoundError:
        raise CorruptFileError("the coordinate file is no longer there") from None
    return into.picks(suffix[1:], many, notes, shape)
