"""Export to Fiji: a plane, or a whole stack, as an ImageJ TIFF with its calibration.

imageBio does not measure regions, threshold or count particles (ADR 009): for that, the image
is opened in Fiji. The file written here is what ImageJ itself writes: a TIFF whose description
holds the layout of the hyperstack (frames, slices, channels), the unit and the spacing, and
whose resolution tags hold the pixel size.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import tifffile

from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Calibration, DatasetInfo
from imagebio.service.planes import NO_PROCESSING, PlaneService, Processing

__all__ = ["MAX_EXPORT_BYTES", "export_file_name", "export_plane", "export_stack"]

#: Pixels of a whole stack that are exported in one file. Larger stacks are opened from the
#: file itself: the server would hold all of it in memory.
MAX_EXPORT_BYTES: Final = 2**30
#: Sample types ImageJ reads as they are; anything else is written as 32-bit floats.
_IMAGEJ_TYPES: Final = frozenset({"uint8", "uint16", "int16", "float32"})
#: Units ImageJ shows as they are written, and how many of them make one of ours.
_UNITS: Final[Mapping[str, tuple[str, float]]] = {
    "Å": ("nm", 0.1),
    "pm": ("nm", 0.001),
    "nm": ("nm", 1.0),
    "µm": ("um", 1.0),
    "mm": ("mm", 1.0),
    "cm": ("cm", 1.0),
    "m": ("m", 1.0),
    "inch": ("inch", 1.0),
}
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")


def export_file_name(name: str, suffix: str = "") -> str:
    """A file name for the download: the name of the source without its extension, reduced to
    letters, digits, dots, dashes and underscores (a file name is untrusted text, and it goes
    into a response header), then ``suffix`` and ``.tif``."""
    stem = name.rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem[1:] else stem
    stem = _UNSAFE.sub("_", stem).strip("._") or "image"
    return f"{stem[:120]}{suffix}.tif"


def _for_imagej(data: npt.NDArray[Any]) -> npt.NDArray[Any]:
    if data.dtype.name in _IMAGEJ_TYPES:
        return np.ascontiguousarray(data.astype(data.dtype.newbyteorder("="), copy=False))
    return np.ascontiguousarray(data, dtype=np.float32)


def _calibration(
    calibration: Calibration, apix: float | None
) -> tuple[dict[str, Any], tuple[float, float] | None]:
    """The ImageJ metadata and the resolution tags (pixels per unit) of a calibration.
    ``apix``: Å per pixel given by the user for a file that does not know its own."""
    x, y = calibration.x, calibration.y
    if x.known and y.known and x.unit == y.unit and x.unit in _UNITS:
        unit, factor = _UNITS[x.unit]
        size_x, size_y = x.value * factor, y.value * factor
    elif apix is not None and apix > 0:
        unit, factor = _UNITS["Å"]
        size_x = size_y = apix * factor
    else:
        return {}, None
    metadata: dict[str, Any] = {"unit": unit}
    z = calibration.z
    if z is not None and z.known and z.unit in _UNITS and _UNITS[z.unit][0] == unit:
        metadata["spacing"] = z.value * _UNITS[z.unit][1]
    return metadata, (1.0 / size_x, 1.0 / size_y)


def _write(data: npt.NDArray[Any], axes: str, calibration: Calibration, apix: float | None) -> bytes:
    metadata, resolution = _calibration(calibration, apix)
    out = io.BytesIO()
    tifffile.imwrite(
        out,
        data,
        imagej=True,
        photometric="rgb" if axes.endswith("S") else "minisblack",
        resolution=resolution,
        metadata={"axes": axes, **metadata},
    )
    return out.getvalue()


def export_plane(
    planes: PlaneService,
    path: Path,
    t: int = 0,
    c: int = 0,
    z: int = 0,
    *,
    options: Mapping[str, Any] | None = None,
    proc: Processing = NO_PROCESSING,
) -> bytes:
    """Plane ``(t, c, z)`` of ``path`` after ``proc``, at full resolution, as an ImageJ TIFF:
    what the viewer shows, values untouched (a processed plane is 32-bit floats). A colour
    picture is exported in colour."""
    info, _ = planes.info(path, options)
    proc = proc.full_resolution
    if info.rgb:
        channels = [planes.plane(path, t, k, z, options=options).array for k in range(3)]
        return _write(_for_imagej(np.stack(channels, axis=-1)), "YXS", info.calibration, proc.apix)
    plane = planes.plane(path, t, c, z, options=options, proc=proc).array
    return _write(_for_imagej(plane), "YX", info.calibration, proc.apix)


def _stack_bytes(info: DatasetInfo) -> int:
    return info.plane_nbytes * info.size_t * info.size_z * info.size_c


def export_stack(planes: PlaneService, path: Path, *, options: Mapping[str, Any] | None = None) -> bytes:
    """Every plane of ``path`` as stored, as one ImageJ hyperstack (frames, slices, channels).
    Refused above :data:`MAX_EXPORT_BYTES`."""
    info, _ = planes.info(path, options)
    if info.rgb:
        raise InvalidRequestError("a colour picture is exported plane by plane")
    if _stack_bytes(info) > MAX_EXPORT_BYTES:
        raise InvalidRequestError(
            f"the stack is too large to be exported in one file ({_stack_bytes(info) >> 20} MiB, "
            f"limit {MAX_EXPORT_BYTES >> 20} MiB): open the file itself in Fiji"
        )
    first = _for_imagej(planes.plane(path, 0, 0, 0, options=options).array)
    data = np.empty((info.size_t, info.size_z, info.size_c, info.size_y, info.size_x), first.dtype)
    for t in range(info.size_t):
        for z in range(info.size_z):
            for c in range(info.size_c):
                data[t, z, c] = planes.plane(path, t, c, z, options=options).array
    return _write(data, "TZCYX", info.calibration, None)
