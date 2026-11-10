"""Robust, NaN-safe statistics of a plane, for display.

One hot pixel or a few NaN must not decide how an image is shown: the display range and the
histogram come from percentiles of the finite values, never from the raw minimum and maximum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Plane

__all__ = ["PlaneStats", "plane_stats"]

_CHUNK = 1 << 20  # elements processed at a time: temporaries stay in the CPU cache


@dataclass(frozen=True, slots=True)
class PlaneStats:
    """Statistics of one plane. Every float is finite; ``lo < hi`` holds for every range."""

    min: float  # over the finite values of the whole plane (exact)
    max: float
    mean: float
    std: float
    n_finite: int
    n_nan: int
    n_posinf: int
    n_neginf: int
    robust_lo: float  # percentiles of a deterministic subsample of the finite values
    robust_hi: float
    auto_lo: float
    auto_hi: float
    hist_lo: float  # the histogram covers [hist_lo, hist_hi] = the robust range
    hist_hi: float
    histogram: tuple[int, ...]  # whole plane; values outside the range are counted in the end bins

    def to_json(self) -> dict[str, Any]:
        return {
            "min": self.min, "max": self.max, "mean": self.mean, "std": self.std,
            "n_finite": self.n_finite, "n_nan": self.n_nan,
            "n_posinf": self.n_posinf, "n_neginf": self.n_neginf,
            "robust": [self.robust_lo, self.robust_hi],
            "auto": [self.auto_lo, self.auto_hi],
            "histogram": {"lo": self.hist_lo, "hi": self.hist_hi, "counts": list(self.histogram)},
        }  # fmt: skip


def _widen(lo: float, hi: float, integer: bool) -> tuple[float, float]:
    """A strictly increasing range around ``[lo, hi]``.

    A constant image gives ``lo == hi``: integers get +/- 0.5 (one grey level), floats a
    relative margin of 1e-6 (at least 1e-6 in absolute value).
    """
    if hi > lo:
        return lo, hi
    margin = 0.5 if integer else max(abs(lo) * 1e-6, 1e-6)
    return lo - margin, hi + margin


def _percent_pair(name: str, pair: tuple[float, float]) -> tuple[float, float]:
    lo, hi = pair
    if not (0.0 <= lo < hi <= 100.0):
        raise InvalidRequestError(f"{name} percentiles must satisfy 0 <= low < high <= 100, got {pair!r}")
    return float(lo), float(hi)


def _subsample(flat: npt.NDArray[Any], width: int, max_samples: int) -> npt.NDArray[Any]:
    """Every ``step``-th value. The step never divides the image width, so the sample does not
    fall on the same columns of every row."""
    step = max(1, -(-flat.size // max_samples))
    while step > 1 and width > 1 and width % step == 0:
        step += 1
    return flat[::step]


def plane_stats(
    a: Plane,
    *,
    bins: int = 256,
    robust: tuple[float, float] = (0.1, 99.9),
    auto: tuple[float, float] = (2.0, 98.0),
    max_samples: int = 1_000_000,
) -> PlaneStats:
    """Statistics of ``a``.

    ``min``, ``max``, ``mean``, ``std`` and the histogram use every finite value of the plane.
    The percentiles (``robust``, the range of the histogram and of the contrast sliders, and
    ``auto``, the default display window) are computed on a strided subsample of at most
    about ``max_samples`` finite values, which is deterministic for a given plane.
    """
    if not isinstance(bins, int) or isinstance(bins, bool) or not 1 <= bins <= 65536:
        raise InvalidRequestError("bins must be an integer between 1 and 65536")
    if max_samples < 1:
        raise InvalidRequestError("max_samples must be >= 1")
    robust, auto = _percent_pair("robust", robust), _percent_pair("auto", auto)
    if a.dtype == np.bool_:
        a = a.view(np.uint8)
    if a.dtype.kind not in "iuf":
        raise InvalidRequestError(f"cannot compute statistics of dtype {a.dtype}")
    integer = a.dtype.kind in "iu"
    flat = np.ascontiguousarray(a).reshape(-1)
    width = a.shape[-1] if a.ndim else 1
    n_nan = n_posinf = n_neginf = 0

    with np.errstate(invalid="ignore", over="ignore"):
        all_finite = integer or not flat.size or math.isfinite(float(flat.sum(dtype=np.float64)))
    if not all_finite:
        # slow path: NaN or Inf present (or a float64 sum that overflows): keep the finite values
        with np.errstate(invalid="ignore"):
            nan = np.isnan(flat)
            n_nan = int(nan.sum())
            n_posinf = int((flat == np.inf).sum())
            n_neginf = int((flat == -np.inf).sum())
        if n_nan or n_posinf or n_neginf:
            flat = flat[np.isfinite(flat)]
            width = 0  # the finite values no longer form rows
    n = int(flat.size)
    if n == 0:
        return PlaneStats(
            0.0, 0.0, 0.0, 0.0, 0, n_nan, n_posinf, n_neginf, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, (0,) * bins
        )

    vmin, vmax = float(flat.min()), float(flat.max())
    sample = _subsample(flat, width, max_samples)
    p = np.percentile(sample, [robust[0], auto[0], auto[1], robust[1]]).tolist()
    robust_lo, robust_hi = _widen(float(p[0]), float(p[3]), integer)
    auto_lo, auto_hi = _widen(float(p[1]), float(p[2]), integer)

    # One pass over the plane, in float64, for the mean, the variance and the histogram.
    # The offset keeps the sums accurate when the values are far from zero.
    offset = float(sample[: 1 << 16].mean(dtype=np.float64))
    scale = bins / (robust_hi - robust_lo)
    counts = np.zeros(bins, np.int64)
    s1 = s2 = 0.0
    with np.errstate(invalid="ignore", over="ignore"):
        for start in range(0, n, _CHUNK):
            x = flat[start : start + _CHUNK].astype(np.float64)
            d = x - offset
            s1 += float(d.sum())
            s2 += float(np.dot(d, d))
            x -= robust_lo
            x *= scale
            np.nan_to_num(x, copy=False, nan=0.0)  # only reachable with values near the float64 limits
            np.clip(x, 0, bins - 1, out=x)  # hist_hi itself, and anything above, goes to the last bin
            counts += np.bincount(x.astype(np.intp), minlength=bins)
        shift = s1 / n
        mean = offset + shift
        std = (
            math.sqrt(max(0.0, s2 / n - shift * shift))
            if math.isfinite(s2) and math.isfinite(shift)
            else math.inf
        )
        if not (math.isfinite(mean) and math.isfinite(std)):  # the sums overflowed: rescale first
            unit = max(abs(vmin), abs(vmax))
            y = flat.astype(np.float64) / unit
            mean = float(y.mean()) * unit
            std = min(float(y.std()) * unit, float(np.finfo(np.float64).max))
    mean = min(max(mean, vmin), vmax)  # rounding must not push the mean outside the data range
    return PlaneStats(
        min=vmin, max=vmax, mean=mean, std=std,
        n_finite=n, n_nan=n_nan, n_posinf=n_posinf, n_neginf=n_neginf,
        robust_lo=robust_lo, robust_hi=robust_hi, auto_lo=auto_lo, auto_hi=auto_hi,
        hist_lo=robust_lo, hist_hi=robust_hi, histogram=tuple(counts.tolist()),
    )  # fmt: skip
