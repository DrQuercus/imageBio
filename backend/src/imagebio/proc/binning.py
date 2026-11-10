"""Downsampling by binning, and cropping.

Reducing an image always *averages* the pixels of each block: picking one pixel out of N
(decimation) aliases the shot noise of a micrograph over the signal one wants to see.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Plane

__all__ = ["bin_mean", "binned_shape", "crop"]

_VIEW_SUM_MAX_FACTOR = 8  # up to 64 strided views are faster than a reshaped sum


def _check_factor(factor: int) -> None:
    if not isinstance(factor, int) or isinstance(factor, bool) or factor < 1:
        raise InvalidRequestError(f"bin factor must be an integer >= 1, got {factor!r}")


def binned_shape(height: int, width: int, factor: int) -> tuple[int, int]:
    """Shape of a ``height`` x ``width`` plane binned ``factor`` times (partial edge blocks count)."""
    _check_factor(factor)
    return (-(-height // factor), -(-width // factor))


def _block_sums(a: Plane, factor: int, acc: type[np.generic]) -> npt.NDArray[Any]:
    """Sums of the ``factor`` x ``factor`` blocks of ``a``, whose sides are multiples of ``factor``."""
    h, w = a.shape[0] // factor, a.shape[1] // factor
    if factor <= _VIEW_SUM_MAX_FACTOR:
        out = np.zeros((h, w), acc)
        for i in range(factor):
            for j in range(factor):
                out += a[i::factor, j::factor]
        return out
    return np.asarray(a.reshape(h, factor, w, factor).sum(axis=(1, 3), dtype=acc))


def bin_mean(a: Plane, factor: int) -> Plane:
    """Mean of each ``factor`` x ``factor`` block, as float32.

    * The result has shape ``binned_shape(*a.shape, factor)``: blocks cut by the right and bottom
      edges average only the pixels that exist.
    * ``factor == 1`` returns ``a`` itself (same dtype, no copy).
    * Sums are accumulated in int64 (integer input, exact) or float64, then divided: the result
      equals a float64 reference rounded to float32.
    * NaN and Inf propagate to the blocks that contain them.
    """
    _check_factor(factor)
    if a.ndim != 2:
        raise InvalidRequestError("bin_mean expects a 2-D plane")
    if factor == 1:
        return a
    if a.dtype == np.bool_:
        a = a.view(np.uint8)
    acc: type[np.generic] = np.int64 if a.dtype.kind in "iu" else np.float64
    h, w = a.shape
    oh, ow = binned_shape(h, w, factor)
    fh, fw = h // factor, w // factor  # complete blocks
    sums = np.empty((oh, ow), np.float64)
    counts = np.full((oh, ow), factor * factor, np.float64)
    with np.errstate(invalid="ignore", over="ignore"):
        sums[:fh, :fw] = _block_sums(a[: fh * factor, : fw * factor], factor, acc)
        if ow > fw:  # right edge: columns that do not fill a block
            strip = a[: fh * factor, fw * factor :]
            sums[:fh, fw] = strip.reshape(fh, factor * strip.shape[1]).sum(axis=1, dtype=acc)
            counts[:fh, fw] = factor * strip.shape[1]
        if oh > fh:  # bottom edge
            strip = a[fh * factor :, : fw * factor]
            sums[fh, :fw] = strip.reshape(strip.shape[0], fw, factor).sum(axis=(0, 2), dtype=acc)
            counts[fh, :fw] = factor * strip.shape[0]
            if ow > fw:
                corner = a[fh * factor :, fw * factor :]
                sums[fh, fw] = corner.sum(dtype=acc)
                counts[fh, fw] = corner.size
        sums /= counts
        return sums.astype(np.float32)


def crop(a: Plane, x: int, y: int, w: int, h: int) -> Plane:
    """The ``w`` x ``h`` region whose top-left pixel is ``(x, y)``, as a view."""
    for name, value in (("x", x), ("y", y), ("w", w), ("h", h)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidRequestError(f"region {name} must be an integer, got {value!r}")
    height, width = a.shape
    if w < 1 or h < 1 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise InvalidRequestError(
            f"region x={x}, y={y}, w={w}, h={h} is not inside the {width} × {height} plane"
        )
    return a[y : y + h, x : x + w]
