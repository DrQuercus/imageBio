from __future__ import annotations

import numpy as np
import pytest

from imagebio.core.errors import InvalidRequestError
from imagebio.proc.fourier import fourier_filter, next_fast_len, power_spectrum, radial_profile

RNG = np.random.default_rng(21)


def wave(shape: tuple[int, int], period_px: float, angle: float = 0.0, amplitude: float = 1.0) -> np.ndarray:
    """A plane wave with the given period in pixels."""
    y, x = np.mgrid[: shape[0], : shape[1]]
    k = 2 * np.pi / period_px
    return (amplitude * np.cos(k * (x * np.cos(angle) + y * np.sin(angle)))).astype(np.float32)


def amplitude(a: np.ndarray, margin: int = 40) -> float:
    """Amplitude of a sinusoid, measured away from the edges."""
    core = a[margin:-margin, margin:-margin].astype(np.float64)
    return float(np.sqrt(2) * core.std())


def test_next_fast_len():
    for n in (1, 2, 7, 11, 13, 127, 257, 1000, 4097, 4128, 5761):
        m = next_fast_len(n)
        assert m >= n
        k = m
        for p in (2, 3, 5):
            while k % p == 0:
                k //= p
        assert k == 1, (n, m)
    assert next_fast_len(4096) == 4096 and next_fast_len(4097) == 4320
    assert next_fast_len(11) == 12 and next_fast_len(17) == 18 and next_fast_len(31) == 32


@pytest.mark.parametrize("apix", [0.83, 1.0, 2.5])
def test_lowpass_gain_is_one_half_at_the_cutoff(apix: float):
    cutoff = 20.0  # Å
    for resolution, expected in ((cutoff, 0.5), (2 * cutoff, 0.5**0.25), (cutoff / 2, 0.5**4)):
        a = wave((512, 512), resolution / apix, angle=0.3)
        out = fourier_filter(a, apix_x=apix, lowpass=cutoff)
        assert out.dtype == np.float32 and out.shape == a.shape
        assert amplitude(out) == pytest.approx(expected, abs=0.03), resolution


def test_highpass_gain_and_mean_preserved():
    apix, cutoff = 1.0, 80.0
    # gain at frequency f: 1 - 0.5 ** ((f / f_cutoff) ** 2)
    for resolution, expected in ((cutoff, 0.5), (cutoff / 4, 1 - 0.5**16), (cutoff * 2, 1 - 0.5**0.25)):
        a = wave((768, 768), resolution / apix) + 1000.0
        out = fourier_filter(a, apix_x=apix, highpass=cutoff)
        assert amplitude(out, margin=120) == pytest.approx(expected, abs=0.04), resolution
        assert out.mean() == pytest.approx(1000.0, abs=0.5)  # grey levels stay in place
    fine = fourier_filter(wave((512, 512), 8.0) + 50, apix_x=1.0, highpass=200.0)
    assert amplitude(fine) == pytest.approx(1.0, abs=0.02)  # detail far finer than the cut-off passes
    coarse = fourier_filter(wave((1024, 1024), 400.0) + 50, apix_x=1.0, highpass=40.0)
    assert amplitude(coarse, margin=200) < 0.05  # a slow gradient is removed


def test_bandpass_keeps_the_band():
    a = wave((512, 512), 6.0) + wave((512, 512), 30.0, angle=1.0) + wave((512, 512), 250.0, angle=2.0)
    out = fourier_filter(a, apix_x=1.0, lowpass=12.0, highpass=90.0)
    reference = fourier_filter(wave((512, 512), 30.0, angle=1.0), apix_x=1.0, lowpass=12.0, highpass=90.0)
    assert amplitude(reference) > 0.75
    residual = (out - reference)[60:-60, 60:-60]
    # what is left of the two waves outside the band: 0.5**4 = 0.06 of the fine one and
    # 1 - 0.5**0.13 = 0.09 of the coarse one
    assert np.abs(residual).max() < 0.2


def test_anisotropic_pixels_use_physical_frequencies():
    # the same 20 Å period along x (1 Å/px -> 20 px) and along y (2 Å/px -> 10 px)
    along_x, along_y = wave((256, 256), 20.0, angle=0.0), wave((256, 256), 10.0, angle=np.pi / 2)
    for a in (along_x, along_y):
        out = fourier_filter(a, apix_x=1.0, apix_y=2.0, lowpass=20.0)
        assert amplitude(out) == pytest.approx(0.5, abs=0.03)


def test_edges_do_not_wrap_around():
    """A plain FFT filter would leak the bright right edge onto the dark left edge (audit CV-18)."""
    a = np.zeros((200, 300), np.float32)
    a[:, -30:] = 1000.0
    out = fourier_filter(a, apix_x=1.0, lowpass=40.0)
    assert np.abs(out[:, :30]).max() < 1.0
    assert out[:, -10:].min() > 900.0
    naive = np.fft.irfft2(
        np.fft.rfft2(a)
        * np.exp(
            -np.log(2) * (np.fft.fftfreq(200)[:, None] ** 2 + np.fft.rfftfreq(300)[None, :] ** 2) * 1600
        ),
        s=a.shape,
    )
    assert np.abs(naive[:, :5]).max() > 50.0  # what the reflection avoids


def test_dc_and_constant_images():
    a = RNG.normal(500, 30, (301, 517)).astype(np.float32)  # odd sizes
    out = fourier_filter(a, apix_x=1.06, lowpass=15.0)
    assert out.shape == a.shape and out.std() < a.std() / 3
    assert out.mean() == pytest.approx(a.mean(), abs=0.5)
    flat = np.full((64, 80), 7.0, np.float32)
    np.testing.assert_allclose(fourier_filter(flat, apix_x=1.0, lowpass=10.0), flat, atol=1e-3)
    np.testing.assert_allclose(fourier_filter(flat, apix_x=1.0, highpass=30.0), flat, atol=1e-3)
    tiny = np.arange(6, dtype=np.float32).reshape(1, 6)
    assert fourier_filter(tiny, apix_x=1.0, lowpass=4.0).shape == (1, 6)


def test_no_filter_returns_the_values_and_integers_are_accepted():
    a = RNG.integers(0, 60000, (32, 32), dtype=np.uint16)
    np.testing.assert_array_equal(fourier_filter(a, apix_x=1.0), a.astype(np.float32))
    assert fourier_filter(a, apix_x=1.0, lowpass=8.0).dtype == np.float32


def test_nan_and_inf_do_not_poison_the_image():
    a = RNG.normal(100, 5, (128, 128)).astype(np.float32)
    a[10, 10], a[20, 20], a[30, 30] = np.nan, np.inf, -np.inf
    out = fourier_filter(a, apix_x=1.0, lowpass=10.0)
    assert np.isnan(out[10, 10]) and np.isposinf(out[20, 20]) and np.isneginf(out[30, 30])
    rest = np.isfinite(a)
    assert np.isfinite(out[rest]).all() and out[rest].mean() == pytest.approx(100, abs=1)


@pytest.mark.parametrize("kw", [
    {"lowpass": 0}, {"lowpass": -5}, {"highpass": float("nan")}, {"lowpass": float("inf")},
    {"lowpass": 30, "highpass": 20}, {"lowpass": 20, "highpass": 20}, {"lowpass": True},
])  # fmt: skip
def test_parameter_validation(kw: dict[str, float]):
    with pytest.raises(InvalidRequestError):
        fourier_filter(np.zeros((16, 16), np.float32), apix_x=1.0, **kw)


def test_unknown_pixel_size_is_refused():
    for apix in (0, -1, float("nan")):
        with pytest.raises(InvalidRequestError, match="pixel size"):
            fourier_filter(np.zeros((16, 16), np.float32), apix_x=apix, lowpass=10)
    with pytest.raises(InvalidRequestError):
        fourier_filter(np.zeros((4, 4, 4), np.float32), apix_x=1.0, lowpass=10)


# ── power spectrum ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("apix", "d"), [(1.0, 10.0), (0.83, 20.0), (1.35, 3.7), (2.0, 7.3)])
def test_a_lattice_of_period_d_peaks_at_n_apix_over_d(apix: float, d: float):
    tile = 256
    a = wave((700, 900), d / apix, angle=0.4) + RNG.normal(0, 0.2, (700, 900)).astype(np.float32)
    power, n_tiles = power_spectrum(a, tile=tile)
    assert power.shape == (tile, tile) and power.dtype == np.float32 and n_tiles > 4
    c = tile // 2
    y, x = np.unravel_index(int(np.argmax(power)), power.shape)
    assert float(np.hypot(y - c, x - c)) == pytest.approx(tile * apix / d, abs=0.5)
    freq, profile = radial_profile(power)
    assert len(freq) == c + 1 and freq[0] == 0 and freq[-1] == 0.5
    peak = int(np.argmax(profile[1:])) + 1
    assert peak == pytest.approx(tile * apix / d, abs=1.0)
    assert freq[peak] == pytest.approx(apix / d, abs=1.0 / tile)


def test_spectrum_is_symmetric_and_ignores_the_mean():
    a = RNG.normal(0, 1, (300, 300)).astype(np.float32)
    p0, _ = power_spectrum(a, tile=128)
    p1, _ = power_spectrum(a + 5000, tile=128)
    np.testing.assert_allclose(p1, p0, rtol=2e-2, atol=1e-4)
    np.testing.assert_allclose(p0[1:, 1:], p0[1:, 1:][::-1, ::-1], rtol=1e-4, atol=1e-6)  # point symmetry
    assert p0[64, 64] < p0.mean()  # the zero frequency is removed with the mean
    assert p0.mean() == pytest.approx(1.0, rel=0.15)  # white noise of variance 1: unit power


def test_small_images_tiles_cap_and_non_finite_pixels():
    power, n = power_spectrum(RNG.normal(0, 1, (50, 70)).astype(np.float32), tile=512)
    assert power.shape == (50, 50) and n >= 1
    big = np.zeros((4096, 4096), np.float32)
    _, n_big = power_spectrum(big, tile=64)
    assert n_big <= 400
    a = RNG.normal(0, 1, (128, 128)).astype(np.float32)
    a[5, 5] = np.nan
    assert np.isfinite(power_spectrum(a, tile=64)[0]).all()
    for kw in ({"tile": 15}, {"tile": 8}, {"tile": 5000}, {"overlap": 1.0}, {"tile": 64.0}):
        with pytest.raises(InvalidRequestError):
            power_spectrum(a, **kw)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        power_spectrum(np.zeros((4, 4), np.float32))


@pytest.mark.perf
def test_fourier_speed_on_a_4k_plane():
    import time

    a = RNG.normal(100, 10, (4096, 4096)).astype(np.float32)
    t0 = time.perf_counter()
    fourier_filter(a, apix_x=1.0, lowpass=20.0)
    lp = time.perf_counter() - t0
    t0 = time.perf_counter()
    power_spectrum(a, tile=512)
    ps = time.perf_counter() - t0
    print(f"\n4K low-pass: {lp * 1000:.0f} ms, power spectrum: {ps * 1000:.0f} ms")
    assert lp < 3.0 and ps < 3.0
