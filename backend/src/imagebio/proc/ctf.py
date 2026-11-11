"""A quick estimate of the contrast transfer function of a micrograph: its defocus, and the
resolution up to which the Thon rings of its power spectrum are found where the defocus says.

The power spectrum of an image of a thin specimen is modulated by ``sin²(χ(k) + φ)``, with

    χ(k) = π λ k² (Δf - ½ Cs λ² k²)        φ = asin(amplitude contrast)

``k`` being the spatial frequency (1/Å), ``λ`` the electron wavelength, ``Δf`` the defocus
(positive: underfocus) and ``Cs`` the spherical aberration. Here the spectrum is averaged
over all directions, its smooth background removed, and the defocus is the one whose rings
match the oscillations that remain.

This is a one-dimensional fit: **astigmatism is not measured** (it blurs the rings of the
averaged spectrum, and so shortens the resolution of the fit), nor is a phase plate. It is
meant to follow a collection (is the defocus the one asked for? how far do the rings go?),
not to replace CTFFIND or Gctf for processing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import InvalidRequestError
from imagebio.proc.fourier import power_spectrum, radial_profile

__all__ = ["CtfEstimate", "ctf_zeros", "estimate_ctf", "wavelength"]

_FIELD: Final = 2048  # the spectrum is taken on a central field of at most this side
_MIN_FIELD: Final = 512
_COARSEST: Final = 30.0  # Å: below this frequency the background is not smooth
_FINEST: Final = 3.5  # Å: rings are not looked for beyond
_NYQUIST_SHARE: Final = 0.9
_SAMPLES_PER_RING: Final = 3.0  # points of the averaged spectrum a ring must cover to count
_MIN_SAMPLES: Final = 40
_COARSE_STEP_UM: Final = 0.01
_FINE_STEP_UM: Final = 0.0005
_BACKGROUND_DEGREE: Final = 4
_RING_AGREEMENT: Final = 0.5  # correlation of a pair of rings with the model for them to count
_MIN_RINGS: Final = 3
_MIN_CORRELATION: Final = 0.3
#: Correlation times the square root of the number of points. Measured on spectra of pure
#: noise (72 images, three pixel sizes): never above 6; on rings one can barely see: 9 or more.
_MIN_SCORE: Final = 7.5


def wavelength(kv: float) -> float:
    """Relativistic wavelength of an electron accelerated by ``kv`` kilovolts, in Å."""
    volts = kv * 1e3
    return 12.2643 / math.sqrt(volts + 0.97845e-6 * volts * volts)


def _phase(
    k: npt.NDArray[np.float64], defocus_a: Any, lam: float, cs_a: float, amplitude: float
) -> npt.NDArray[np.float64]:
    k2 = k * k
    phase: npt.NDArray[np.float64] = np.pi * lam * k2 * (defocus_a - 0.5 * cs_a * lam * lam * k2) + math.asin(
        amplitude
    )
    return phase


@dataclass(frozen=True, slots=True)
class CtfEstimate:
    """What the Thon rings of an image say."""

    defocus_um: float  # positive: underfocus
    #: Resolution (Å) up to which the rings of the spectrum agree with that defocus.
    fit_resolution: float
    #: Correlation of the oscillations of the spectrum with the model, over the fitted range.
    correlation: float
    #: The correlation times the square root of the number of points it was taken on: how far
    #: the match stands above what noise gives (about 3 at most).
    score: float
    apix: float
    kv: float
    cs_mm: float
    amplitude: float
    #: Side of the spectrum the fit was made on (pixels).
    tile: int

    def to_json(self) -> dict[str, Any]:
        return {
            "defocus_um": round(self.defocus_um, 4),
            "fit_resolution": round(self.fit_resolution, 2),
            "correlation": round(self.correlation, 3),
            "score": round(self.score, 1),
            "apix": self.apix,
            "kv": self.kv,
            "cs_mm": self.cs_mm,
            "amplitude": self.amplitude,
            "tile": self.tile,
            "zeros": [round(z, 3) for z in ctf_zeros(self)],
        }


def ctf_zeros(estimate: CtfEstimate, finest: float | None = None) -> list[float]:
    """Resolutions (Å, coarsest first) of the zeros of the transfer function, down to
    ``finest`` (default: the Nyquist limit of the image)."""
    lam = wavelength(estimate.kv)
    limit = 1.0 / (finest if finest is not None else 2 * estimate.apix)
    k = np.linspace(1e-4, limit, 8192)
    phase = _phase(k, estimate.defocus_um * 1e4, lam, estimate.cs_mm * 1e7, estimate.amplitude)
    order = np.floor(phase / np.pi)
    zeros: list[float] = []
    for i in np.flatnonzero(np.diff(order) != 0).tolist():
        # the phase crosses a multiple of pi between two samples: where, by interpolation
        target = max(order[i], order[i + 1]) * np.pi
        share = (target - phase[i]) / (phase[i + 1] - phase[i])
        zeros.append(float(1.0 / (k[i] + share * (k[i + 1] - k[i]))))
    return zeros[:200]


def _check(name: str, value: float, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise InvalidRequestError(f"{name} must be between {low:g} and {high:g}")


def estimate_ctf(
    plane: npt.NDArray[Any],
    apix: float,
    *,
    kv: float = 300.0,
    cs_mm: float = 2.7,
    amplitude: float = 0.07,
    min_defocus_um: float = 0.2,
    max_defocus_um: float = 8.0,
) -> CtfEstimate | None:
    """The defocus of ``plane`` (``apix`` Å per pixel) from its Thon rings, or None when its
    spectrum shows none that a defocus between the two bounds explains."""
    _check("the pixel size (Å)", apix, 0.05, 20.0)
    _check("kv", kv, 20.0, 1500.0)
    _check("cs (mm)", cs_mm, 0.0, 20.0)
    _check("the amplitude contrast", amplitude, 0.0, 0.5)
    _check("the smallest defocus (µm)", min_defocus_um, 0.05, 50.0)
    _check("the largest defocus (µm)", max_defocus_um, min_defocus_um, 50.0)
    if plane.ndim != 2:
        raise InvalidRequestError("estimate_ctf expects a 2-D plane")
    h, w = plane.shape
    side_y, side_x = min(h, _FIELD), min(w, _FIELD)
    if min(side_y, side_x) < _MIN_FIELD:
        return None  # too small a field for rings to be told from noise
    field = plane[(h - side_y) // 2 :, (w - side_x) // 2 :][:side_y, :side_x]
    tile = 1024 if min(side_y, side_x) >= 1024 else 512
    power, _ = power_spectrum(field, tile=tile)
    frequency, profile = radial_profile(power)
    k = frequency / apix  # 1/Å
    lowest, highest = 1.0 / _COARSEST, min(1.0 / _FINEST, _NYQUIST_SHARE * 0.5 / apix)
    usable = (k >= lowest) & (k <= highest) & np.isfinite(profile) & (profile > 0)
    if int(usable.sum()) < _MIN_SAMPLES:
        return None
    k = k[usable]
    logp = np.log(profile[usable])

    # the smooth fall of the spectrum is removed, then the oscillations are brought to a
    # constant size: only where they are matters, not how strong
    x = (k - k[0]) / (k[-1] - k[0]) * 2 - 1
    residual = logp - np.polynomial.chebyshev.chebval(
        x, np.polynomial.chebyshev.chebfit(x, logp, _BACKGROUND_DEGREE)
    )
    width = max(5, k.size // 8)
    box = np.ones(width) / width
    padded = np.pad(residual * residual, width, mode="reflect")
    envelope = np.sqrt(np.convolve(padded, box, mode="same")[width:-width])
    signal = residual / np.maximum(envelope, 1e-12)
    if not np.isfinite(signal).all() or float(signal.std()) == 0.0:
        return None

    lam = wavelength(kv)
    cs_a = cs_mm * 1e7
    step_k = float(k[1] - k[0])

    def scores(
        defocus_um: npt.NDArray[np.float64],
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """For each defocus: its score and its correlation with the oscillations, on the part
        of the spectrum where its rings are wide enough to be sampled."""
        defocus_a = defocus_um[:, None] * 1e4
        model = np.sin(_phase(k[None, :], defocus_a, lam, cs_a, amplitude)) ** 2
        # width of a ring at k: 1 / (2 λ Δf k) (the Cs term matters little here)
        sampled = (1.0 / (2 * lam * defocus_a * np.maximum(k[None, :], 1e-9))) >= _SAMPLES_PER_RING * step_k
        count = sampled.sum(axis=1)
        safe = np.maximum(count, 1)
        mean_m = (model * sampled).sum(axis=1) / safe
        mean_s = (signal[None, :] * sampled).sum(axis=1) / safe
        dm = (model - mean_m[:, None]) * sampled
        ds = (signal[None, :] - mean_s[:, None]) * sampled
        denominator = np.sqrt((dm * dm).sum(axis=1) * (ds * ds).sum(axis=1))
        correlation = np.where(denominator > 0, (dm * ds).sum(axis=1) / np.maximum(denominator, 1e-30), 0.0)
        correlation = np.where(count >= _MIN_SAMPLES, correlation, 0.0)
        return correlation * np.sqrt(count), correlation

    coarse = np.arange(min_defocus_um, max_defocus_um + _COARSE_STEP_UM / 2, _COARSE_STEP_UM)
    score, _ = scores(coarse)
    best = float(coarse[int(np.argmax(score))])
    fine = np.arange(
        max(min_defocus_um, best - 2 * _COARSE_STEP_UM),
        min(max_defocus_um, best + 2 * _COARSE_STEP_UM) + _FINE_STEP_UM / 2,
        _FINE_STEP_UM,
    )
    score, correlation = scores(fine)
    index = int(np.argmax(score))
    defocus_um = float(fine[index])
    if float(correlation[index]) < _MIN_CORRELATION or float(score[index]) < _MIN_SCORE:
        return None

    # how far the rings follow: pairs of rings, from the coarsest, as long as they agree
    phase = _phase(k, defocus_um * 1e4, lam, cs_a, amplitude)
    model = np.sin(phase) ** 2
    pair = np.floor(phase / (2 * np.pi)).astype(np.int64)
    reached, rings = None, 0
    for number in np.unique(pair):
        inside = pair == number
        if int(inside.sum()) < 4:
            if rings:
                break  # rings too fine to be sampled: the fit stops here
            continue
        m, s = model[inside], signal[inside]
        spread = float(m.std() * s.std())
        agreement = float(np.mean((m - m.mean()) * (s - s.mean())) / spread) if spread > 0 else 0.0
        if agreement < _RING_AGREEMENT:
            if rings:
                break
            continue  # the coarsest rings may be lost in the background: look further
        rings += 2
        reached = float(k[inside][-1])
    if reached is None or rings < _MIN_RINGS:
        return None
    return CtfEstimate(
        defocus_um=defocus_um,
        fit_resolution=1.0 / reached,
        correlation=float(correlation[index]),
        score=float(score[index]),
        apix=float(apix),
        kv=float(kv),
        cs_mm=float(cs_mm),
        amplitude=float(amplitude),
        tile=int(power.shape[0]),
    )
