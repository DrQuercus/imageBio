"""Cheap measurements on each image of a watched session: no motion correction, no CTF fit.

- **Mean and standard deviation of the counts**, hot pixels left out. The mean tells how much
  ice the beam went through (aperture-limited scattering: ``t = Λ · ln(I0 / I)``, ``I0`` being
  the counts through an empty hole): the interface compares each image to the brightest of its
  session.
- **Ice-ring ratio**: the power around 3.4-3.9 Å, where crystalline ice diffracts (hexagonal
  ice at 3.90, 3.67 and 3.44 Å, cubic ice at 3.67 Å), over the power on both sides of that
  band. About 1 on vitreous ice; well above when the ice is crystalline or contaminated.

Everything is measured on the image the tile shows: the integrated image when there is one,
otherwise the mean of the first frames of the movie (its counts are then scaled to the whole
exposure, and said to be an estimate).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from imagebio.proc.fourier import power_spectrum, radial_profile
from imagebio.service.planes import PlaneService, Processing

__all__ = ["ICE_BAND", "ICE_SIDES", "ice_ring_ratio", "measure", "measure_plane", "robust_mean_std"]

#: Resolutions (Å) between which crystalline ice diffracts.
ICE_BAND: Final = (3.40, 3.95)
#: The two bands the ice band is compared to, on its fine and on its coarse side (Å).
ICE_SIDES: Final = ((3.05, 3.30), (4.10, 4.60))
#: Share of the pixels left out at each end before the mean is taken (hot and dead pixels).
_TRIM_PERCENT: Final = 0.1
_MAX_SAMPLES: Final = 2**20  # pixels the trimming thresholds are estimated from
_FIELD: Final = 2048  # the spectrum is taken on a central field of at most this side
_TILE: Final = 512
_MOVIE_FRAMES: Final = 4  # frames of a movie that are averaged, as for its thumbnail


def robust_mean_std(plane: npt.NDArray[Any]) -> tuple[float, float] | None:
    """Mean and standard deviation of the finite pixels that lie between the 0.1th and the
    99.9th percentile (bounds included): a few hot pixels do not move them. The percentiles are
    estimated on an evenly spaced subset of a large image. None when no pixel is finite."""
    data = np.asarray(plane)
    if data.dtype.kind != "f":
        data = data.astype(np.float32)
    flat = data.reshape(-1)
    step = max(1, math.ceil(flat.size / _MAX_SAMPLES))
    sample = flat[::step]
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return None
    low, high = (float(v) for v in np.percentile(sample, (_TRIM_PERCENT, 100 - _TRIM_PERCENT)))
    kept = flat[(flat >= low) & (flat <= high)]  # comparisons are false for NaN
    if kept.size == 0:
        return None
    return float(kept.mean(dtype=np.float64)), float(kept.std(dtype=np.float64))


def ice_ring_ratio(plane: npt.NDArray[Any], apix: float) -> float | None:
    """Mean power in the ice band over the mean power of the two bands around it, from the
    averaged periodogram of the central field. None when the pixel is too coarse for the
    fine side of the band to exist (above about 1.5 Å), or when there is nothing to compare."""
    if not (math.isfinite(apix) and apix > 0) or 2 * apix > ICE_SIDES[0][0]:
        return None
    h, w = plane.shape
    side_y, side_x = min(h, _FIELD), min(w, _FIELD)
    y0, x0 = (h - side_y) // 2, (w - side_x) // 2
    field = plane[y0 : y0 + side_y, x0 : x0 + side_x]
    if min(field.shape) < 64:
        return None
    power, _ = power_spectrum(field, tile=_TILE)
    frequency, profile = radial_profile(power)

    def band(limits: tuple[float, float]) -> float | None:
        fine, coarse = limits  # Å; a period d lies at apix / d cycles per pixel
        inside = (frequency >= apix / coarse) & (frequency <= apix / fine) & np.isfinite(profile)
        return float(profile[inside].mean()) if inside.any() else None

    ice, fine_side, coarse_side = band(ICE_BAND), band(ICE_SIDES[0]), band(ICE_SIDES[1])
    if ice is None or fine_side is None or coarse_side is None:
        return None
    around = (fine_side + coarse_side) / 2
    if not (math.isfinite(ice) and math.isfinite(around) and around > 0):
        return None
    return ice / around


def measure_plane(plane: npt.NDArray[Any], apix: float | None) -> dict[str, Any]:
    """The measurements of one image; a value that cannot be measured is left out."""
    out: dict[str, Any] = {}
    stats = robust_mean_std(plane)
    if stats is not None:
        out["mean"], out["std"] = stats
    if apix is not None:
        ratio = ice_ring_ratio(plane, apix)
        if ratio is not None:
            out["ice_ring"] = round(ratio, 4)
    return out


def measure(planes: PlaneService, path: Path, role: str, apix: float | None = None) -> dict[str, Any]:
    """Measure the image file ``path`` (``role``: "image" or "movie"). ``apix``: the pixel size
    to use when the file does not know its own (Å). Raises the errors of the plane service."""
    info, _ = planes.info(path)
    if info.rgb:
        return {}  # a picture, not counts
    frames = min(info.size_t, _MOVIE_FRAMES) if role == "movie" else 1
    proc = Processing(t_end=frames - 1) if frames > 1 else Processing()
    plane = planes.plane(path, 0, 0, info.size_z // 2, proc=proc).array
    known = info.calibration.x.to_angstrom()
    out = measure_plane(plane, known if known is not None else apix)
    if role == "movie" and info.size_t > 1 and "mean" in out:
        # per frame so far, and from the first frames only: the exposure is all its frames
        out["mean"] *= info.size_t
        del out["std"]  # the spread of a few averaged frames says nothing of the whole exposure
        out["estimated"] = True
        out["frames"] = info.size_t
    return out
