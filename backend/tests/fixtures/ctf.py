"""Synthetic micrographs of known CTF, made from the textbook formula: the reference the CTF
estimate is compared with."""

from __future__ import annotations

import math

import numpy as np


def electron_wavelength(kv: float) -> float:
    """de Broglie wavelength with the relativistic correction, Å (h, m and e from CODATA)."""
    h, m, e, c = 6.62607015e-34, 9.1093837015e-31, 1.602176634e-19, 299792458.0
    volts = kv * 1e3
    return h / math.sqrt(2 * m * e * volts * (1 + e * volts / (2 * m * c * c))) * 1e10


def micrograph(
    shape: tuple[int, int],
    apix: float,
    defocus_um: float,
    rng: np.random.Generator,
    *,
    kv: float = 300.0,
    cs_mm: float = 2.7,
    amplitude: float = 0.07,
    astigmatism_um: float = 0.0,
    b_factor: float = 80.0,
    signal: float = 0.35,
) -> np.ndarray:
    """White noise seen through a contrast transfer function, plus shot noise: what a thin
    amorphous film gives. Underfocus positive; ``astigmatism_um`` is the difference between
    the two principal defoci."""
    h, w = shape
    fy = np.fft.fftfreq(h)[:, None] / apix
    fx = np.fft.rfftfreq(w)[None, :] / apix
    k2 = fx * fx + fy * fy
    defocus = (defocus_um + 0.5 * astigmatism_um * np.cos(2 * (np.arctan2(fy, fx) - 0.6))) * 1e4
    lam = electron_wavelength(kv)
    chi = np.pi * lam * k2 * (defocus - 0.5 * cs_mm * 1e7 * lam * lam * k2)
    ctf = -(math.sqrt(1 - amplitude**2) * np.sin(chi) + amplitude * np.cos(chi))
    image = np.fft.irfft2(np.fft.rfft2(rng.normal(0, 1, shape)) * ctf * np.exp(-b_factor * k2 / 4), s=shape)
    return (signal * image / image.std() + rng.normal(0, 1, shape)).astype(np.float32)
