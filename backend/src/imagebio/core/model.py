"""Core data model: what every reader produces and every mode consumes.

A :class:`Dataset` is a lazy 5-D image with ImageJ-hyperstack semantics. Its axes are always
``T, C, Z, Y, X`` (missing axes have length 1) and pixels are only read one 2-D plane at a time.

Conventions
-----------
* A plane is a C-contiguous ``(Y, X)`` array in native byte order, with row 0 first, exactly as
  stored in the file (no flip, no scaling, no type conversion beyond the table below).
* RGB(A) images are exposed as ``C = 3`` (or 4) with ``rgb=True``.
* Stacks of 2-D images that are not volumes (movie frames, particle stacks, tilt series) go on
  ``T``; sections of a volume go on ``Z``.
* The physical pixel size always carries its provenance (:class:`CalibrationSource`). A reader
  that cannot find it must say so (``DEFAULT``) instead of inventing a value.
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, TypeAlias

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import InvalidRequestError, NotFoundError

AXES: Final = "TCZYX"

#: dtypes a plane may have. Readers convert anything else: bool -> uint8, float16 -> float32,
#: 64-bit integers -> float64 (with a warning in ``DatasetInfo.warnings``).
PLANE_DTYPES: Final = frozenset(
    np.dtype(t) for t in ("uint8", "int8", "uint16", "int16", "uint32", "int32", "float32", "float64")
)

Plane: TypeAlias = npt.NDArray[Any]


# ─── Calibration ──────────────────────────────────────────────────────────────


class CalibrationSource(StrEnum):
    """Where a calibration value comes from."""

    HEADER = "header"  # main file header (e.g. MRC CELLA/MX)
    EXTENDED_HEADER = "extended_header"  # e.g. FEI1/FEI2 extended header
    METADATA = "metadata"  # embedded metadata (OME-XML, ImageJ description, TIFF tags)
    SIDECAR = "sidecar"  # companion file (EPU .xml, .mdoc)
    USER = "user"  # set by the user
    DEFAULT = "default"  # unknown: placeholder value, must be shown as such


_UNIT_ALIASES: Final[Mapping[str, str]] = {
    "å": "Å", "a": "Å", "ang": "Å", "angstrom": "Å", "angstroms": "Å", "Å": "Å",
    "pm": "pm",
    "nm": "nm", "nanometer": "nm", "nanometers": "nm", "nanometre": "nm",
    "um": "µm", "µm": "µm", "μm": "µm", "micron": "µm", "microns": "µm",
    "micrometer": "µm", "micrometre": "µm",
    "mm": "mm", "cm": "cm", "m": "m",
    "in": "inch", "inch": "inch", "inches": "inch",
    "": "px", "px": "px", "pixel": "px", "pixels": "px",
    "s": "s", "sec": "s", "second": "s", "seconds": "s", "ms": "ms",
}  # fmt: skip

_ANGSTROM_PER_UNIT: Final[Mapping[str, float]] = {
    "pm": 0.01, "Å": 1.0, "nm": 10.0, "µm": 1e4, "mm": 1e7, "cm": 1e8, "m": 1e10, "inch": 2.54e8,
}  # fmt: skip


def normalize_unit(unit: str | None) -> str:
    """Canonical spelling of a unit (``"um"`` -> ``"µm"``, ``"A"`` -> ``"Å"``). Unknown units are kept."""
    raw = (unit or "").strip()
    return _UNIT_ALIASES.get(raw.lower(), raw)


@dataclass(frozen=True, slots=True)
class AxisCalibration:
    """Physical size of one step along an axis."""

    value: float
    unit: str
    source: CalibrationSource

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit", normalize_unit(self.unit))
        if not (math.isfinite(self.value) and self.value > 0):
            raise ValueError(f"calibration value must be finite and > 0, got {self.value!r}")

    @property
    def known(self) -> bool:
        return self.source is not CalibrationSource.DEFAULT and self.unit != "px"

    def to_angstrom(self) -> float | None:
        """Value in Å, or ``None`` when unknown or not a length."""
        factor = _ANGSTROM_PER_UNIT.get(self.unit)
        return self.value * factor if (factor is not None and self.known) else None

    def scaled(self, factor: float) -> AxisCalibration:
        return AxisCalibration(self.value * factor, self.unit, self.source)

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source.value,
            "known": self.known,
            "angstrom": self.to_angstrom(),
        }


#: Placeholder for an unknown pixel size.
UNKNOWN_PIXEL: Final = AxisCalibration(1.0, "px", CalibrationSource.DEFAULT)


@dataclass(frozen=True, slots=True)
class Calibration:
    """Physical calibration of a dataset. ``z`` and ``t`` are ``None`` when not applicable."""

    x: AxisCalibration = UNKNOWN_PIXEL
    y: AxisCalibration = UNKNOWN_PIXEL
    z: AxisCalibration | None = None
    t: AxisCalibration | None = None

    @classmethod
    def isotropic(cls, value: float, unit: str, source: CalibrationSource) -> Calibration:
        a = AxisCalibration(value, unit, source)
        return cls(x=a, y=a)

    @property
    def known(self) -> bool:
        return self.x.known and self.y.known

    def binned(self, factor: int) -> Calibration:
        """Calibration of the same image binned ``factor`` times in X and Y."""
        if factor < 1:
            raise ValueError("bin factor must be >= 1")
        return Calibration(self.x.scaled(factor), self.y.scaled(factor), self.z, self.t)

    def to_json(self) -> dict[str, Any]:
        return {
            "x": self.x.to_json(),
            "y": self.y.to_json(),
            "z": self.z.to_json() if self.z else None,
            "t": self.t.to_json() if self.t else None,
        }


# ─── Dataset description ──────────────────────────────────────────────────────


class StackKind(StrEnum):
    IMAGE = "image"  # a single 2-D image
    MOVIE = "movie"  # dose-fractionated frames (T)
    STACK = "stack"  # unrelated 2-D images, e.g. particles or class averages (T)
    TILT_SERIES = "tilt_series"  # (T)
    VOLUME = "volume"  # 3-D map or tomogram (Z)
    HYPERSTACK = "hyperstack"  # anything with more than one non-trivial axis among T, C, Z


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    name: str | None = None
    color: tuple[int, int, int] | None = None  # display colour hint (RGB, 0-255)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "color": list(self.color) if self.color else None}


def json_safe(obj: Any) -> Any:
    """Convert ``obj`` to plain JSON types (numpy scalars, bytes, NaN/Inf, tuples, paths...)."""
    if obj is None or isinstance(obj, bool | str | int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, np.generic):
        return json_safe(obj.item())
    if isinstance(obj, np.ndarray):
        return json_safe(obj.tolist())
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, Mapping):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [json_safe(v) for v in obj]
    if isinstance(obj, StrEnum):
        return obj.value
    return str(obj)


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    """Everything known about a dataset without reading pixels."""

    format: str  # "mrc", "tiff", "eer", "png", ...
    reader: str  # name of the reader that opened it
    size_x: int
    size_y: int
    dtype: np.dtype[Any]
    size_t: int = 1
    size_c: int = 1
    size_z: int = 1
    calibration: Calibration = field(default_factory=Calibration)
    stack_kind: StackKind = StackKind.IMAGE
    rgb: bool = False
    channels: tuple[ChannelInfo, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)  # format-specific, JSON-safe
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dtype", np.dtype(self.dtype).newbyteorder("="))
        if self.dtype not in PLANE_DTYPES:
            raise ValueError(f"unsupported plane dtype {self.dtype}")
        for axis in "tczyx":
            n = getattr(self, f"size_{axis}")
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                raise ValueError(f"size_{axis} must be an int >= 1, got {n!r}")
        if self.channels and len(self.channels) != self.size_c:
            raise ValueError("channels must be empty or have one entry per channel")
        if self.rgb and self.size_c not in (3, 4):
            raise ValueError("rgb datasets must have 3 or 4 channels")
        object.__setattr__(self, "metadata", json_safe(self.metadata))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def shape(self) -> tuple[int, int, int, int, int]:
        """``(T, C, Z, Y, X)``."""
        return (self.size_t, self.size_c, self.size_z, self.size_y, self.size_x)

    @property
    def plane_count(self) -> int:
        return self.size_t * self.size_c * self.size_z

    @property
    def plane_nbytes(self) -> int:
        return self.size_y * self.size_x * self.dtype.itemsize

    def to_json(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "reader": self.reader,
            "shape": {
                "t": self.size_t,
                "c": self.size_c,
                "z": self.size_z,
                "y": self.size_y,
                "x": self.size_x,
            },
            "dtype": self.dtype.name,
            "stack_kind": self.stack_kind.value,
            "rgb": self.rgb,
            "channels": [c.to_json() for c in self.channels],
            "calibration": self.calibration.to_json(),
            "metadata": self.metadata,
            "warnings": list(self.warnings),
        }


# ─── Dataset ──────────────────────────────────────────────────────────────────


class Dataset(ABC):
    """A lazily read image. Subclasses implement :meth:`_read_plane` and :meth:`close`.

    ``read_plane`` must be safe to call from several threads at once (use positional reads such
    as ``os.pread`` or a lock; never rely on a shared file position).
    """

    def __init__(self, path: Path, info: DatasetInfo) -> None:
        self._path = Path(path)
        self._info = info

    @property
    def info(self) -> DatasetInfo:
        return self._info

    @property
    def path(self) -> Path:
        """Server-side path. Internal: never send it to a client."""
        return self._path

    def check_index(self, t: int, c: int, z: int) -> None:
        i = self._info
        for name, value, size in (("t", t, i.size_t), ("c", c, i.size_c), ("z", z, i.size_z)):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < size:
                raise InvalidRequestError(f"plane index {name}={value!r} is out of range (0..{size - 1})")

    def read_plane(self, t: int = 0, c: int = 0, z: int = 0) -> Plane:
        """One ``(Y, X)`` plane: C-contiguous, native byte order, dtype ``info.dtype``.

        The result may be read-only and must be treated as immutable by callers.
        """
        self.check_index(t, c, z)
        plane = self._read_plane(t, c, z)
        i = self._info
        if plane.shape != (i.size_y, i.size_x):
            raise RuntimeError(
                f"{type(self).__name__} returned shape {plane.shape}, expected {(i.size_y, i.size_x)}"
            )
        if plane.dtype != i.dtype or not plane.flags.c_contiguous:
            plane = np.ascontiguousarray(plane, dtype=i.dtype)
        return plane

    @abstractmethod
    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        """Read one plane. Indices are already validated."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release file handles. Must be idempotent."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


# ─── File identity ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FileId:
    """Identity of a file's content for cache keys: a changed size or mtime is a different file."""

    path: str
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, path: str | os.PathLike[str]) -> FileId:
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            raise NotFoundError(f"file not found: {Path(path).name}") from None
        return cls(os.fspath(path), st.st_size, st.st_mtime_ns)

    @property
    def etag(self) -> str:
        return f"{self.size:x}-{self.mtime_ns:x}"
