"""Thumbnails: a small JPEG of an image, made by averaging, never by skipping pixels.

The legacy monitor built its thumbnails by taking one pixel out of N: on a noisy micrograph
that keeps all the noise and none of the signal (``docs/audit/AUDIT.md``, LIVE). Here the
plane is binned by block means (the plane service), then stretched between robust percentiles.
"""

from __future__ import annotations

import io
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from imagebio.service.planes import MAX_BIN, PlaneService, Processing

__all__ = ["MAX_PX", "MIN_PX", "render_thumbnail"]

MIN_PX: Final = 32
MAX_PX: Final = 512
_MOVIE_FRAMES: Final = 4  # frames averaged for the thumbnail of a movie
_PERCENTILES: Final = (0.5, 99.5)
_JPEG_QUALITY: Final = 85


def _to_bytes(plane: npt.NDArray[np.generic]) -> npt.NDArray[np.uint8]:
    """Grey levels 0..255 between robust percentiles; pixels that are not finite are black."""
    data = np.asarray(plane, dtype=np.float32)
    finite = np.isfinite(data)
    if not finite.any():
        return np.zeros(data.shape, np.uint8)
    lo, hi = (float(v) for v in np.percentile(data[finite], _PERCENTILES))
    if hi <= lo:
        lo, hi = float(data[finite].min()), float(data[finite].max())
    scale = 255.0 / (hi - lo) if hi > lo else 0.0
    out = np.clip((np.where(finite, data, lo) - lo) * scale, 0, 255)
    return np.asarray(out, dtype=np.uint8)


def render_thumbnail(
    planes: PlaneService,
    path: Path,
    px: int,
    *,
    t: int | None = None,
    z: int | None = None,
    options: Mapping[str, Any] | None = None,
) -> bytes:
    """A JPEG of ``path`` whose longer side is at most ``px`` pixels.

    A movie is shown as the mean of its first frames, a volume as its middle section, a colour
    picture in colour. With ``t`` or ``z`` it is that one plane instead (an image of a stack,
    a section of a volume), stretched on its own values. Raises the errors of the plane service
    (corrupt file, unknown format, no such plane…).
    """
    px = max(MIN_PX, min(MAX_PX, int(px)))
    info, _ = planes.info(path, options)
    factor = min(MAX_BIN, max(1, math.ceil(max(info.size_x, info.size_y) / px)))
    one = t is not None or z is not None
    t0 = t if t is not None else 0
    z = z if z is not None else info.size_z // 2
    if info.rgb:
        channels = [
            _to_bytes(planes.plane(path, t0, c, z, bin=factor, options=options).array) for c in range(3)
        ]
        image = Image.fromarray(np.stack(channels, axis=-1), mode="RGB")
    else:
        frames = 1 if one else min(info.size_t, _MOVIE_FRAMES)
        proc = Processing(t_end=frames - 1) if frames > 1 else Processing()
        plane = planes.plane(path, t0, 0, z, bin=factor, options=options, proc=proc).array
        image = Image.fromarray(_to_bytes(plane), mode="L")
    if max(image.size) > px:  # the binning factor is capped: finish with an area average
        ratio = px / max(image.size)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        image = image.resize(size, Image.Resampling.BOX)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=_JPEG_QUALITY)
    return out.getvalue()
