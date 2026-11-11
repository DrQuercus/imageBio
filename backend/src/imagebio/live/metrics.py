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
from imagebio.service.planes import MAX_BIN, PlaneService, Processing

__all__ = [
    "ICE_BAND",
    "ICE_SIDES",
    "ice_ring_ratio",
    "measure",
    "measure_drift",
    "measure_plane",
    "robust_mean_std",
    "shift_between",
]

#: Resolutions (Å) between which crystalline ice diffracts.
ICE_BAND: Final = (3.40, 3.95)
#: The two bands the ice band is compared to, on its fine and on its coarse side (Å).
ICE_SIDES: Final = ((3.05, 3.30), (4.10, 4.60))
#: Share of the pixels left out at each end before the mean is taken (hot and dead pixels).
_TRIM_PERCENT: Final = 0.1
_MAX_SAMPLES: Final = 2**22  # pixels of a large image that mean and spread are taken from
_FIELD: Final = 2048  # the spectrum is taken on a central field of at most this side
#: Under this side the bands hold too few points of the spectrum for the ratio to mean anything.
#: Measured on plain noise at 0.83 Å per pixel, its spread is 20 % on a 64-pixel image (one in
#: five would be called crystalline), 7 % at 256 pixels, 3 % at 512 and 0.5 % on the 2048-pixel
#: field of a real micrograph.
_MIN_FIELD: Final = 512
_TILE: Final = 512
_MOVIE_FRAMES: Final = 4  # frames of a movie that are averaged, as for its thumbnail
_DRIFT_FRAMES: Final = 4  # frames averaged at each end of a movie to see how far it moved
_DRIFT_SIDE: Final = 1024  # the two averages are binned to about this side before being compared
_DRIFT_SIGMA: Final = 0.08  # low-pass of the correlation, cycles per binned pixel
_DRIFT_MIN_RATIO: Final = 1.6  # the correlation peak over the best value away from it
_DRIFT_PEAK_RADIUS: Final = 10  # pixels around the peak that belong to it


def robust_mean_std(plane: npt.NDArray[Any]) -> tuple[float, float] | None:
    """Mean and standard deviation of the finite pixels that lie between the 0.1th and the
    99.9th percentile (bounds included): a few hot pixels do not move them. A large image is
    measured on an evenly spaced subset of its pixels (one out of ``n`` along both axes, about
    four million of them): the mean of that many pixels is known to better than a thousandth
    of their spread. None when no pixel is finite."""
    data = np.asarray(plane)
    if data.ndim == 2:
        step = max(1, math.ceil(math.sqrt(data.size / _MAX_SAMPLES)))
        data = data[::step, ::step]
    values = np.asarray(data, dtype=np.float64 if data.dtype.itemsize > 4 else np.float32).reshape(-1)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    low, high = (float(v) for v in np.percentile(values, (_TRIM_PERCENT, 100 - _TRIM_PERCENT)))
    kept = values[(values >= low) & (values <= high)]
    if kept.size == 0:
        return None
    return float(kept.mean(dtype=np.float64)), float(kept.std(dtype=np.float64))


def ice_ring_ratio(plane: npt.NDArray[Any], apix: float) -> float | None:
    """Mean power in the ice band over the mean power of the two bands around it, from the
    averaged periodogram of the central field. None when the pixel is too coarse for the
    fine side of the band to exist (above about 1.5 Å), when the image is too small for the
    ratio to mean anything, or when there is nothing to compare."""
    if not (math.isfinite(apix) and apix > 0) or 2 * apix > ICE_SIDES[0][0]:
        return None
    h, w = plane.shape
    side_y, side_x = min(h, _FIELD), min(w, _FIELD)
    y0, x0 = (h - side_y) // 2, (w - side_x) // 2
    field = plane[y0 : y0 + side_y, x0 : x0 + side_x]
    if min(field.shape) < _MIN_FIELD:
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


def shift_between(first: npt.NDArray[Any], last: npt.NDArray[Any]) -> tuple[float, float] | None:
    """How far the content of ``last`` is displaced from that of ``first``, in pixels
    (``dy``, ``dx``), from the peak of their cross-correlation; None when no peak stands out
    (nothing in the images but noise).

    What the two images share without it having moved, the fixed pattern of the detector
    (pixels a little more or less sensitive than their neighbours), correlates at zero shift
    exactly and would pull the answer there: the correlation at zero shift is replaced by the
    mean of its neighbours before the peak is looked for, as motion-correction programs do.
    The correlation is then smoothed (the specimen is coarse, the noise is not) and the peak
    is refined to a fraction of a pixel with a parabola through its neighbours.
    """
    if first.shape != last.shape or first.ndim != 2 or min(first.shape) < 128:
        return None  # too small a field to tell a displacement from chance
    h, w = first.shape
    window = np.hanning(h)[:, None] * np.hanning(w)[None, :]
    spectra = []
    for image in (first, last):
        data = np.asarray(image, dtype=np.float64)
        finite = np.isfinite(data)
        if not finite.any():
            return None
        fill = float(data[finite].mean())
        data = np.where(finite, data, fill) - fill
        spectra.append(np.fft.rfft2(data * window))
    correlation = np.fft.irfft2(np.conj(spectra[0]) * spectra[1], s=(h, w))
    around = [correlation[dy, dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]
    correlation[0, 0] = float(np.mean(around))
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.rfftfreq(w)[None, :]
    smooth = np.exp(-(fy * fy + fx * fx) / (2 * _DRIFT_SIGMA**2))
    correlation = np.fft.irfft2(np.fft.rfft2(correlation) * smooth, s=(h, w))
    # the peak is looked for among the shifts a specimen can have drifted by (an eighth of the
    # field), and must stand clear of everything else there: on noise the best and the second
    # best are within a quarter of each other, on a real displacement the peak is about twice
    # the rest
    centred = np.fft.fftshift(correlation)
    cy, cx = h // 2, w // 2
    ry, rx = max(2, h // 8), max(2, w // 8)
    region = centred[cy - ry : cy + ry + 1, cx - rx : cx + rx + 1]
    peak_y, peak_x = (int(v) for v in np.unravel_index(int(np.argmax(region)), region.shape))
    top = float(region[peak_y, peak_x])
    yy, xx = np.ogrid[: region.shape[0], : region.shape[1]]
    elsewhere = region[(yy - peak_y) ** 2 + (xx - peak_x) ** 2 > _DRIFT_PEAK_RADIUS**2]
    floor = float(np.median(centred))
    if elsewhere.size == 0 or top - floor < _DRIFT_MIN_RATIO * max(float(elsewhere.max()) - floor, 0.0):
        return None
    if top <= floor:
        return None

    def refine(below: float, at: float, above: float) -> float:
        curve = below - 2 * at + above
        return 0.0 if curve >= 0 else max(-0.5, min(0.5, 0.5 * (below - above) / curve))

    y, x = cy - ry + peak_y, cx - rx + peak_x  # in the centred correlation: never on its border
    dy = peak_y - ry + refine(float(centred[y - 1, x]), top, float(centred[y + 1, x]))
    dx = peak_x - rx + refine(float(centred[y, x - 1]), top, float(centred[y, x + 1]))
    return dy, dx


def measure_drift(planes: PlaneService, path: Path) -> dict[str, Any]:
    """How far the specimen moved between the first and the last frames of the movie ``path``:
    ``drift`` and ``drift_unit`` ("A", or "px" of the movie when its pixel size is unknown).
    Empty when the movie has too few frames or shows nothing to follow. The mean of a few frames
    at each end is read, binned, and compared (:func:`shift_between`); this is a coarse look at
    whole-frame motion, not a motion correction."""
    info, _ = planes.info(path)
    if info.rgb or info.size_t < 4:
        return {}
    frames = min(_DRIFT_FRAMES, info.size_t // 2)
    factor = min(MAX_BIN, max(1, math.ceil(max(info.size_x, info.size_y) / _DRIFT_SIDE)))
    z = info.size_z // 2
    first = planes.plane(path, 0, 0, z, bin=factor, proc=Processing(t_end=frames - 1)).array
    start = info.size_t - frames
    last = planes.plane(path, start, 0, z, bin=factor, proc=Processing(t_end=info.size_t - 1)).array
    shift = shift_between(first, last)
    if shift is None:
        return {}
    pixels = math.hypot(shift[0], shift[1]) * factor
    apix = info.calibration.x.to_angstrom()
    if apix is None:
        return {"drift": round(pixels, 2), "drift_unit": "px"}
    return {"drift": round(pixels * apix, 2), "drift_unit": "A"}
