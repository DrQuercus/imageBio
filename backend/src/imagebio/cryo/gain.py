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
    """
    if raw.ndim != 2:
        raise InvalidRequestError("a gain reference must be a single 2-D image")
    data = np.asarray(raw, dtype=np.float32)
    valid = np.isfinite(data) & (data > 0)
    if not valid.any():
        raise InvalidRequestError("the gain reference has no usable pixel")
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
        return np.divide(total, valid, out=np.zeros((h, w), np.float64), where=valid > 0).astype(np.float32)
    if h % gh == 0 and w % gw == 0 and h // gh == w // gw:
        f = h // gh
        return np.ascontiguousarray(np.repeat(np.repeat(gain, f, axis=0), f, axis=1))
    raise InvalidRequestError(
        f"the gain reference ({gw} × {gh}) does not fit the image ({w} × {h}): "
        "the sizes must be equal or in an integer ratio"
    )


_NEIGHBOURS: Final = tuple((dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0))


def _fill_defects(corrected: npt.NDArray[np.float32], defect: npt.NDArray[np.bool_]) -> None:
    """Replace, in place, each defective pixel by the median of the valid pixels around it:
    the 8 neighbours, or a wider window (up to 9 x 9) inside a dead row, column or cluster."""
    h, w = corrected.shape
    ys, xs = np.nonzero(defect)
    around = np.full((len(ys), len(_NEIGHBOURS)), np.nan, np.float32)
    for j, (dy, dx) in enumerate(_NEIGHBOURS):
        ny, nx = ys + dy, xs + dx
        ok = (ny >= 0) & (ny < h) & (nx >= 0) & (nx < w)
        ok[ok] = ~defect[ny[ok], nx[ok]]
        around[ok, j] = corrected[ny[ok], nx[ok]]
    around[~np.isfinite(around)] = np.nan
    alone = np.isnan(around).all(axis=1)
    fill = np.zeros(len(ys), np.float32)
    fill[~alone] = np.nanmedian(around[~alone], axis=1)
    for i in np.nonzero(alone)[0].tolist():  # no valid neighbour: look further
        y, x = int(ys[i]), int(xs[i])
        for reach in (2, 3, 4):
            y0, y1, x0, x1 = (
                max(0, y - reach),
                min(h, y + reach + 1),
                max(0, x - reach),
                min(w, x + reach + 1),
            )
            window = corrected[y0:y1, x0:x1][~defect[y0:y1, x0:x1]]
            window = window[np.isfinite(window)]
            if window.size:
                fill[i] = np.median(window)
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
    of the comparison: they are set to the mean, in the image and for every orientation.
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
        corrected[(fitted == 0) | ~finite] = np.float32(level)
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
    ratio = runner_up / best_score if best_score > 0 else float("inf")
    plain = np.where(finite, data, np.float32(level))
    return OrientationCheck(tuple(scores), best, ratio, ratio >= _CONFIDENT_RATIO, flatness(plain, tile))
