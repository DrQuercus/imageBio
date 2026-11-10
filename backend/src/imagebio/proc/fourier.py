"""Fourier-space processing for cryo-EM display: resolution filters in Å, power spectrum.

Filters are Gaussian in frequency, defined by the *resolution* at which their gain is one half:
a 20 Å low-pass keeps what is coarser than 20 Å. Frequencies are physical (cycles per Å), so
the pixel size is required, and anisotropic pixels are handled.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt

from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Plane

__all__ = ["fourier_filter", "next_fast_len", "power_spectrum", "radial_profile"]

_LN2 = math.log(2.0)
_MAX_TILES = 400


def next_fast_len(n: int) -> int:
    """The smallest 5-smooth number >= n (sizes the FFT is fast for)."""
    if n <= 6:
        return max(1, n)
    best = 1 << math.ceil(math.log2(n))
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p2 = 1 << max(0, math.ceil(math.log2(n / p35)))
            candidate = p2 * p35
            if n <= candidate < best:
                best = candidate
            p35 *= 3
        p5 *= 5
    return best


def _resolution(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise InvalidRequestError(f"{name} must be a resolution in Å, greater than 0")
    return float(value)


def fourier_filter(
    a: Plane,
    *,
    apix_x: float,
    apix_y: float | None = None,
    lowpass: float | None = None,
    highpass: float | None = None,
) -> Plane:
    """Gaussian low-pass, high-pass or band-pass, with cut-offs given as resolutions in Å.

    * ``lowpass=d`` removes detail finer than ``d`` Å: gain 0.5 at 1/d, 1 at zero frequency.
    * ``highpass=d`` removes variations coarser than ``d`` Å: gain 0.5 at 1/d. The mean of the
      image is kept, so the grey levels stay where they were.
    * Both: a band-pass (``lowpass`` must be the finer resolution).

    The image is extended by mirror reflection before the transform, so that opposite edges do
    not bleed into each other (a plain FFT treats the image as periodic). NaN and Inf pixels
    are replaced by the mean of the finite ones for the transform and put back afterwards.
    Returns float32, same shape.
    """
    lowpass, highpass = _resolution("lowpass", lowpass), _resolution("highpass", highpass)
    apix_y = apix_x if apix_y is None else apix_y
    for value in (apix_x, apix_y):
        if not (isinstance(value, int | float) and math.isfinite(value) and value > 0):
            raise InvalidRequestError("the pixel size must be known (Å per pixel) to filter by resolution")
    if a.ndim != 2:
        raise InvalidRequestError("fourier_filter expects a 2-D plane")
    if lowpass is not None and highpass is not None and lowpass >= highpass:
        raise InvalidRequestError(
            "band-pass: the low-pass resolution must be finer (smaller) than the high-pass one"
        )
    if lowpass is None and highpass is None:
        return np.asarray(a, dtype=np.float32)

    data = np.asarray(a, dtype=np.float32)
    finite = np.isfinite(data)
    all_finite = bool(finite.all())
    if not all_finite:
        fill = float(data[finite].mean(dtype=np.float64)) if finite.any() else 0.0
        data = np.where(finite, data, np.float32(fill))

    h, w = data.shape
    # Reach of the filter in real space: a Gaussian of half-gain resolution d has a standard
    # deviation of d * sqrt(ln 2) / (pi * sqrt 2) Å ~ 0.19 d. Four of them on each side.
    reach = max(lowpass or 0.0, highpass or 0.0) * 0.75
    pad_y = min(h - 1, math.ceil(reach / apix_y)) if h > 1 else 0
    pad_x = min(w - 1, math.ceil(reach / apix_x)) if w > 1 else 0
    big_h, big_w = next_fast_len(h + 2 * pad_y), next_fast_len(w + 2 * pad_x)
    top, left = (big_h - h) // 2, (big_w - w) // 2
    top, left = min(top, h - 1), min(left, w - 1)  # reflection cannot exceed the image itself
    bottom, right = min(big_h - h - top, h - 1), min(big_w - w - left, w - 1)
    padded = (
        np.pad(data, ((top, bottom), (left, right)), mode="reflect")
        if (top or bottom or left or right)
        else data
    )
    ph, pw = padded.shape

    fy = (np.fft.fftfreq(ph) / apix_y).astype(np.float32)  # cycles per Å
    fx = (np.fft.rfftfreq(pw) / apix_x).astype(np.float32)
    r2 = fy[:, None] ** 2 + fx[None, :] ** 2
    gain = np.ones_like(r2)
    if lowpass is not None:
        gain *= np.exp(-_LN2 * r2 * np.float32(lowpass * lowpass))
    if highpass is not None:
        gain *= 1.0 - np.exp(-_LN2 * r2 * np.float32(highpass * highpass))
        gain[0, 0] = 1.0  # keep the mean
    spectrum = np.fft.rfft2(padded)
    spectrum *= gain
    out = np.fft.irfft2(spectrum, s=(ph, pw))[top : top + h, left : left + w].astype(np.float32)
    if not all_finite:
        out[~finite] = np.asarray(a, dtype=np.float32)[~finite]
    return np.ascontiguousarray(out)


def _starts(length: int, tile: int, step: int) -> list[int]:
    starts = list(range(0, length - tile + 1, step))
    if starts[-1] != length - tile:
        starts.append(length - tile)  # the last tile is flush with the edge
    return starts


def power_spectrum(a: Plane, *, tile: int = 512, overlap: float = 0.5) -> tuple[npt.NDArray[np.float32], int]:
    """Averaged periodogram: mean of ``|FFT|^2`` over overlapping, Hann-windowed tiles.

    Returns ``(power, n_tiles)``. ``power`` is ``tile`` x ``tile`` with the zero frequency at
    ``[tile // 2, tile // 2]``; a period of ``d`` Å lies at ``tile * apix / d`` pixels from it.
    The tile is reduced to the largest even size that fits a small image.
    """
    if a.ndim != 2:
        raise InvalidRequestError("power_spectrum expects a 2-D plane")
    if not isinstance(tile, int) or isinstance(tile, bool) or not 16 <= tile <= 4096 or tile % 2:
        raise InvalidRequestError("tile must be an even integer between 16 and 4096")
    if not 0.0 <= overlap < 1.0:
        raise InvalidRequestError("overlap must be in [0, 1)")
    h, w = a.shape
    tile = min(tile, (min(h, w) // 2) * 2)
    if tile < 8:
        raise InvalidRequestError("the image is too small for a power spectrum")
    step = max(1, round(tile * (1.0 - overlap)))
    ys, xs = _starts(h, tile, step), _starts(w, tile, step)
    while len(ys) * len(xs) > _MAX_TILES:  # huge images: spread fewer tiles over the whole field
        step *= 2
        ys, xs = _starts(h, tile, step), _starts(w, tile, step)
    hann = np.hanning(tile + 1)[:-1].astype(np.float32)  # periodic window
    window = hann[:, None] * hann[None, :]
    norm = float((window.astype(np.float64) ** 2).sum())
    half = tile // 2 + 1
    acc = np.zeros((tile, half), np.float64)  # a real image has a point-symmetric spectrum: half is enough
    for y in ys:
        for x in xs:
            block: npt.NDArray[Any] = np.asarray(a[y : y + tile, x : x + tile], dtype=np.float32)
            finite = np.isfinite(block)
            if not finite.all():
                fill = float(block[finite].mean(dtype=np.float64)) if finite.any() else 0.0
                block = np.where(finite, block, np.float32(fill))
            block = (block - block.mean(dtype=np.float64).astype(np.float32)) * window
            f = np.fft.rfft2(block)
            acc += f.real.astype(np.float64) ** 2 + f.imag.astype(np.float64) ** 2
    n = len(ys) * len(xs)
    full = np.empty((tile, tile), np.float64)
    full[:, :half] = acc
    mirrored = (-np.arange(tile)) % tile  # P(ky, kx) = P(-ky, -kx)
    full[:, half:] = acc[mirrored][:, tile // 2 - 1 : 0 : -1]
    return np.fft.fftshift(full / (n * norm)).astype(np.float32), n


def radial_profile(power: npt.NDArray[Any]) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Rotational average of a centred power spectrum.

    Returns ``(frequency, mean_power)``: frequencies in cycles per pixel from 0 to 0.5 (Nyquist),
    one value per pixel of radius.
    """
    n = power.shape[0]
    if power.ndim != 2 or power.shape[1] != n or n < 2:
        raise InvalidRequestError("radial_profile expects a square spectrum")
    c = n // 2
    y, x = np.ogrid[:n, :n]
    ring = np.rint(np.hypot(y - c, x - c)).astype(np.intp)
    keep = ring <= c
    sums = np.bincount(ring[keep], weights=power[keep].astype(np.float64), minlength=c + 1)
    counts = np.bincount(ring[keep], minlength=c + 1)
    return np.arange(c + 1) / n, sums / np.maximum(counts, 1)
