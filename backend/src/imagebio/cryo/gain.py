"""Gain references: applying them to counting-detector movies and checking their orientation.

A gain reference is the per-pixel factor that flattens a detector: ``corrected = raw * gain``.
A value of 0 (or a non-finite one) marks a defective pixel, whose value is replaced by the
median of its valid neighbours.

* ``.gain`` files written by EPU for Falcon EER movies and the ``*_gain.tiff`` files of K3 are
  multiplicative. Some references are instead flat-field *counts* images, to be turned into
  ``mean / counts``: that is ``invert=True`` (see :func:`prepare_gain`).
* The reference and the movie do not always share an orientation (acquisition and processing
  programs disagree on flips), and nothing in the files says so. :func:`check_orientation`
  tries the eight orientations of the square and compares how flat the corrected image is:
  the right gain cancels the fixed pattern of the detector, any other orientation adds a
  second copy of it.

Sources: ``docs/research/cryoem-live.md`` (section 1, gain).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Plane

__all__ = [
    "N_ORIENTATIONS",
    "ORIENTATION_NAMES",
    "OrientationCheck",
    "apply_gain",
    "check_orientation",
    "fit_gain",
    "flatness",
    "orient",
    "prepare_gain",
]

Gain = npt.NDArray[np.float32]

N_ORIENTATIONS: Final = 8

#: Orientation ``k``: mirror left-right when ``k >= 4``, then rotate by ``k % 4`` quarter turns
#: counter-clockwise (as displayed, y down: ``numpy.rot90``).
ORIENTATION_NAMES: Final = (
    "identity",
    "rot90",
    "rot180",
    "rot270",
    "flip_x",  # left-right mirror
    "transpose",  # flip_x then rot90
    "flip_y",  # flip_x then rot180: top-bottom mirror
    "anti_transpose",  # flip_x then rot270
)

_TILE: Final = 64
_MAX_DEFECT_FRACTION: Final = 0.10  # a detector has far fewer; more is another kind of image
_CONFIDENT_RATIO: Final = 1.02  # the runner-up must be at least 2 % less flat


def _check_orientation_index(k: int) -> None:
    if not isinstance(k, int) or isinstance(k, bool) or not 0 <= k < N_ORIENTATIONS:
        raise InvalidRequestError(
            f"the gain orientation must be an integer between 0 and {N_ORIENTATIONS - 1}"
        )


def orient(a: npt.NDArray[Any], k: int) -> npt.NDArray[Any]:
    """``a`` in orientation ``k`` (a view, see :data:`ORIENTATION_NAMES`)."""
    _check_orientation_index(k)
    if a.ndim != 2:
        raise InvalidRequestError("orient expects a 2-D array")
    return np.rot90(a[:, ::-1] if k >= 4 else a, k % 4)


def prepare_gain(raw: npt.NDArray[Any], *, invert: bool = False) -> Gain:
    """The multiplicative gain (float32) of a stored reference; defects become 0.

    ``invert=False``: the reference is the gain itself; values that are not finite or not
    positive are defects. ``invert=True``: the reference is a flat-field counts image and the
    gain is ``mean / counts``, the mean being taken over the valid (finite, positive) pixels.
    A file where more than a tenth of the pixels are unusable is refused.
    """
    if raw.ndim != 2:
        raise InvalidRequestError("a gain reference must be a single 2-D image")
    data = np.asarray(raw, dtype=np.float32)
    valid = np.isfinite(data) & (data > 0)
    unusable = 1.0 - float(valid.mean())
    if unusable > _MAX_DEFECT_FRACTION:
        raise InvalidRequestError(
            f"{unusable:.0%} of the pixels of this file are zero, negative or not finite: "
            "it does not look like a gain reference"
        )
    gain = np.zeros(data.shape, np.float32)
    if invert:
        mean = float(data[valid].mean(dtype=np.float64))
        np.divide(np.float32(mean), data, out=gain, where=valid)
    else:
        gain[valid] = data[valid]
    return gain


def fit_gain(gain: Gain, shape: tuple[int, int]) -> Gain:
    """``gain`` on a grid of ``shape``: as it is, block-averaged when it is an integer number
    of times finer (an 8K reference for a 4K rendering), repeated when coarser (a 4K reference
    for a super-resolution rendering). A coarse pixel is a defect only if all its pixels are."""
    gh, gw = gain.shape
    h, w = shape
    if (gh, gw) == (h, w):
        return gain
    if gh % h == 0 and gw % w == 0 and gh // h == gw // w:
        f = gh // h
        blocks = gain.reshape(h, f, w, f)
        valid = (blocks > 0).sum(axis=(1, 3))
        total = blocks.sum(axis=(1, 3), dtype=np.float64)
        mean = np.zeros((h, w), np.float64)
        np.divide(total, valid, out=mean, where=valid > 0)
        return mean.astype(np.float32)
    if h % gh == 0 and w % gw == 0 and h // gh == w // gw:
        f = h // gh
        return np.ascontiguousarray(np.repeat(np.repeat(gain, f, axis=0), f, axis=1))
    raise InvalidRequestError(
        f"the gain reference ({gw} × {gh}) does not fit the image ({w} × {h}): "
        "the sizes must be equal or in an integer ratio"
    )


_FILL_REACHES: Final = (1, 2, 3, 4)  # half-sizes of the windows tried around a defect
_FILL_CHUNK: Final = 65536  # defects handled at once: bounds the memory of the widest window


def _fill_defects(corrected: npt.NDArray[np.float32], defect: npt.NDArray[np.bool_]) -> None:
    """Replace, in place, each defective pixel by the median of the valid pixels around it:
    the 3 x 3 window, then wider ones (up to 9 x 9) inside a dead row, column or cluster.
    A defect with no valid pixel within reach becomes 0."""
    h, w = corrected.shape
    ys, xs = np.nonzero(defect)
    fill = np.zeros(len(ys), np.float32)
    remaining = np.arange(len(ys))
    for reach in _FILL_REACHES:
        span = range(-reach, reach + 1)
        offsets = [(dy, dx) for dy in span for dx in span if (dy, dx) != (0, 0)]
        still: list[npt.NDArray[np.intp]] = []
        for start in range(0, len(remaining), _FILL_CHUNK):
            chunk = remaining[start : start + _FILL_CHUNK]
            y, x = ys[chunk], xs[chunk]
            around = np.full((len(chunk), len(offsets)), np.nan, np.float32)
            for j, (dy, dx) in enumerate(offsets):
                ny, nx = y + dy, x + dx
                ok = (ny >= 0) & (ny < h) & (nx >= 0) & (nx < w)
                ok[ok] = ~defect[ny[ok], nx[ok]]
                around[ok, j] = corrected[ny[ok], nx[ok]]
            around[~np.isfinite(around)] = np.nan
            found = ~np.isnan(around).all(axis=1)
            fill[chunk[found]] = np.nanmedian(around[found], axis=1)
            still.append(chunk[~found])
        remaining = np.concatenate(still) if still else remaining[:0]
        if not remaining.size:
            break
    corrected[ys, xs] = fill


def apply_gain(plane: Plane, gain: Gain) -> Plane:
    """``plane * gain`` as float32, defective pixels (gain 0) replaced by the median of their
    valid neighbours. ``gain`` must have the shape of ``plane`` (see :func:`fit_gain`)."""
    if plane.shape != gain.shape:
        raise InvalidRequestError("the gain reference and the image do not have the same size")
    corrected: npt.NDArray[np.float32] = np.multiply(plane, gain, dtype=np.float32)
    defect = gain == 0
    if defect.any():
        _fill_defects(corrected, defect)
    return corrected


def flatness(image: npt.NDArray[Any], tile: int = _TILE) -> float:
    """How far ``image`` is from flat: the median, over ``tile`` x ``tile`` blocks, of the
    variance of a block divided by its squared mean. Lower is flatter.

    The median makes it insensitive to what only some blocks contain: a grid bar, a
    contaminant, a dead column. Blocks that are empty or not finite are ignored."""
    h, w = image.shape
    tile = max(2, min(tile, h, w))
    ny, nx = h // tile, w // tile
    mean = np.empty((ny, nx), np.float64)
    mean_sq = np.empty((ny, nx), np.float64)
    for r in range(ny):  # one row of blocks at a time: exact sums in float64, little memory
        rows = np.asarray(image[r * tile : (r + 1) * tile, : nx * tile], dtype=np.float64).reshape(
            tile, nx, tile
        )
        mean[r] = rows.mean(axis=(0, 2))
        mean_sq[r] = (rows * rows).mean(axis=(0, 2))
    usable = np.isfinite(mean) & np.isfinite(mean_sq) & (mean > 0)
    if not usable.any():
        return float("inf")
    variance = np.maximum(mean_sq[usable] - mean[usable] ** 2, 0.0)
    return float(np.median(variance / mean[usable] ** 2))


@dataclass(frozen=True, slots=True)
class OrientationCheck:
    """Result of :func:`check_orientation`.

    ``scores[k]`` is the :func:`flatness` of the image corrected with the gain in orientation
    ``k``, or ``None`` when that orientation does not fit the image. ``best`` is the flattest
    one; ``ratio`` is the score of the runner-up over that of the best, and ``confident`` tells
    whether that margin means something. ``uncorrected`` is the flatness without any gain: a
    gain that does not improve on it is not the gain of this detector.
    """

    scores: tuple[float | None, ...]
    best: int
    ratio: float
    confident: bool
    uncorrected: float

    @property
    def improves(self) -> bool:
        score = self.scores[self.best]
        return score is not None and score < self.uncorrected

    def to_json(self, current: int = 0) -> dict[str, Any]:
        return {
            "orientations": [
                {"index": k, "name": ORIENTATION_NAMES[k], "score": score}
                for k, score in enumerate(self.scores)
            ],
            "best": self.best,
            "best_name": ORIENTATION_NAMES[self.best],
            "current": current,
            "current_is_best": current == self.best,
            "ratio": self.ratio,
            "confident": self.confident,
            "uncorrected": self.uncorrected,
            "improves": self.improves,
        }


def check_orientation(image: Plane, gain: Gain, *, tile: int = _TILE) -> OrientationCheck:
    """Find the orientation of ``gain`` that flattens ``image`` best.

    ``image`` should be a sum or mean of raw frames, with enough dose for the fixed pattern of
    the detector to stand out of the shot noise; ``gain`` a multiplicative reference
    (:func:`prepare_gain`), in the orientation it is stored in. Defective pixels are left out
    of the comparison: for every orientation they take the mean of the corrected image.
    """
    if image.ndim != 2:
        raise InvalidRequestError("check_orientation expects a 2-D image")
    data = np.asarray(image, dtype=np.float32)
    finite = np.isfinite(data)
    level = float(data[finite].mean(dtype=np.float64)) if finite.any() else 0.0
    if not level > 0:
        raise InvalidRequestError("the image is empty: the gain orientation cannot be checked on it")
    scores: list[float | None] = []
    for k in range(N_ORIENTATIONS):
        try:
            fitted = fit_gain(np.ascontiguousarray(orient(gain, k)), (data.shape[0], data.shape[1]))
        except InvalidRequestError:
            scores.append(None)  # a quarter turn of a rectangular detector
            continue
        corrected = data * fitted
        masked = (fitted == 0) | ~finite
        if masked.any():
            # at the level of the corrected image, whatever the scale of the gain: any other
            # value would make outliers of the very pixels that are left out
            kept = corrected[~masked]
            corrected[masked] = kept.mean(dtype=np.float64) if kept.size else 0.0
        scores.append(flatness(corrected, tile))
    ranked = sorted((s, k) for k, s in enumerate(scores) if s is not None)
    if not ranked:
        gh, gw = gain.shape
        raise InvalidRequestError(
            f"the gain reference ({gw} × {gh}) does not fit the image ({data.shape[1]} × {data.shape[0]}) "
            "in any orientation"
        )
    best_score, best = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else float("inf")
    if best_score > 0:
        ratio = runner_up / best_score
    elif runner_up > 0:  # perfectly flat, and alone in being so
        ratio = float("inf")
    else:  # a tie between perfectly flat images tells nothing
        ratio = 1.0
    plain = np.where(finite, data, np.float32(level))
    return OrientationCheck(tuple(scores), best, ratio, ratio >= _CONFIDENT_RATIO, flatness(plain, tile))
