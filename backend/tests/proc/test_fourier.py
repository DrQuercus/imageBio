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


@pytest.mark.parametrize(
    "kw",
    [
        {"lowpass": 20, "apix_x": 1e-320},  # found in review: OverflowError, a 500 for the client
        {"lowpass": 1e300, "apix_x": 1e-300},
        {"highpass": 1.7e308, "apix_x": 0.5},
        {"lowpass": 2e19, "apix_x": 1.0},  # found in review: every pixel came out NaN
        {"highpass": 1e20, "apix_x": 1e30},
        {"lowpass": 1e-4, "apix_x": 1.0},
        {"lowpass": 20, "apix_x": 1.0, "apix_y": 1e9},
    ],
)
def test_extreme_cutoffs_and_pixel_sizes_are_refused(kw: dict[str, float]):
    with pytest.raises(InvalidRequestError):
        fourier_filter(np.ones((24, 32), np.float32), **kw)


def test_the_widest_accepted_values_give_finite_images():
    rng = np.random.default_rng(12)
    a = rng.normal(100, 10, (48, 64)).astype(np.float32)
    for apix in (1e-4, 1.0, 1e7):
        flat = fourier_filter(a, apix_x=apix, lowpass=1e7)
        assert np.isfinite(flat).all()
        kept = fourier_filter(a, apix_x=apix, highpass=1e7)
        # the grey level stays: it is the mean of the mirror-padded image, close to the image's own
        assert np.isfinite(kept).all() and kept.mean() == pytest.approx(a.mean(), abs=0.5)
    nothing = fourier_filter(a, apix_x=1e-4, lowpass=1e7)  # a cut-off of 1 mm on a field of 6 pm
    assert nothing.std() < 1e-3 and nothing.mean() == pytest.approx(a.mean(), abs=0.5)
    np.testing.assert_allclose(fourier_filter(a, apix_x=1.0, lowpass=1e-3), a, atol=1e-3)  # everything passes


def test_a_cutoff_coarser_than_the_field_of_view_does_not_inflate_the_transform(monkeypatch):
    """Found in review: the padding grew to the whole image on each side, nine times the area
    (72 times the plane in memory at 4096 x 4096), for a cut-off beyond the field of view."""
    import imagebio.proc.fourier as fourier

    shapes: list[tuple[int, ...]] = []
    real_rfft2 = np.fft.rfft2

    def spy(x, *args, **kwargs):
        shapes.append(x.shape)
        return real_rfft2(x, *args, **kwargs)

    monkeypatch.setattr(fourier.np.fft, "rfft2", spy)
    rng = np.random.default_rng(13)
    a = rng.normal(100, 10, (200, 300)).astype(np.float32)
    for kw in (
        {"lowpass": 1e6},
        {"highpass": 1e6},
        {"lowpass": 400.0},
        {"lowpass": 300.0, "highpass": 5000.0},
    ):
        out = fourier_filter(a, apix_x=1.0, **kw)
        assert np.isfinite(out).all() and out.shape == a.shape
    assert len(shapes) == 4
    for h, w in shapes:
        assert h <= next_fast_len(2 * 200) and w <= next_fast_len(
            2 * 300
        )  # at most half the image on each side
    # an ordinary cut-off still pads by its own reach, not by half the image
    shapes.clear()
    fourier_filter(a, apix_x=1.0, lowpass=20.0)
    assert shapes[0][0] <= next_fast_len(200 + 2 * 15) and shapes[0][1] <= next_fast_len(300 + 2 * 15)


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


# ── Fourier cropping ──────────────────────────────────────────────────────────


def naive_fourier_crop(a: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """The low frequencies of the whole complex transform, cut out around its centre. Along an
    axis reduced to an even size, the frequencies +n/2 and -n/2 of the image both become the
    Nyquist frequency of the result: it gets half of each."""
    spectrum = np.fft.fftshift(np.fft.fft2(a.astype(np.float64)))
    for axis, size in enumerate(shape):
        n = spectrum.shape[axis]
        centre = n // 2
        kept = np.take(spectrum, np.arange(centre - size // 2, centre - size // 2 + size), axis=axis)
        if size % 2 == 0 and size < n:
            first = [slice(None)] * 2
            first[axis] = 0  # the frequency -size/2
            kept[tuple(first)] = 0.5 * (kept[tuple(first)] + np.take(spectrum, centre + size // 2, axis=axis))
        spectrum = kept
    out = np.fft.ifft2(np.fft.ifftshift(spectrum))
    assert np.abs(out.imag).max() < 1e-9 * max(1.0, np.abs(out.real).max())  # a real image again
    return out.real * (shape[0] * shape[1]) / a.size


@pytest.mark.parametrize(
    ("shape", "to"),
    [
        ((64, 64), (32, 32)),
        ((64, 96), (16, 24)),
        ((65, 97), (33, 49)),  # odd to odd
        ((65, 97), (32, 48)),  # odd to even
        ((64, 96), (21, 31)),  # even to odd
        ((50, 70), (50, 35)),  # one axis only
        ((48, 48), (47, 1)),
    ],
)
def test_fourier_crop_keeps_the_low_frequencies_of_the_transform(shape, to) -> None:
    from imagebio.proc.fourier import fourier_crop

    rng = np.random.default_rng(31)
    image = rng.normal(100, 20, shape).astype(np.float32)
    out = fourier_crop(image, to)
    assert out.shape == to and out.dtype == np.float32
    np.testing.assert_allclose(out, naive_fourier_crop(image, to), rtol=0, atol=2e-3)
    assert float(out.mean(dtype=np.float64)) == pytest.approx(float(image.mean(dtype=np.float64)), abs=1e-3)


def test_fourier_crop_keeps_coarse_detail_whole_and_removes_what_cannot_be_sampled() -> None:
    from imagebio.proc.binning import bin_mean
    from imagebio.proc.fourier import fourier_crop

    x = np.mgrid[0:240, 0:240][1].astype(np.float64)
    coarse = np.cos(2 * np.pi * x / 6.0)  # period 6 pixels: 3 pixels once binned by 2, above Nyquist
    fine = np.cos(2 * np.pi * x / 3.0)  # period 3 pixels: 1.5 once binned, cannot be sampled
    image = (10 + coarse + fine).astype(np.float32)
    kept = fourier_crop(image, (120, 120)).astype(np.float64) - 10
    expected = np.cos(2 * np.pi * np.mgrid[0:120, 0:120][1] / 3.0)  # the coarse wave, on the new grid
    np.testing.assert_allclose(kept, expected, atol=1e-4)  # full amplitude, the fine wave gone
    # a mean of blocks loses a part of the coarse wave and folds the fine one back
    blocks = bin_mean(image, 2).astype(np.float64) - 10
    assert np.abs(blocks - np.cos(2 * np.pi * (np.mgrid[0:120, 0:120][1] + 0.25) / 3.0)).max() > 0.1


def test_fourier_crop_edge_cases() -> None:
    from imagebio.core.errors import InvalidRequestError
    from imagebio.proc.fourier import fourier_crop

    rng = np.random.default_rng(32)
    counts = rng.integers(0, 4000, (40, 60), dtype=np.uint16)
    same = fourier_crop(counts, (40, 60))
    np.testing.assert_array_equal(same, counts.astype(np.float32))  # nothing to remove
    np.testing.assert_allclose(fourier_crop(counts, (1, 1)), [[counts.mean()]], rtol=1e-5)
    holes = rng.normal(5, 1, (32, 32)).astype(np.float32)
    holes[3, 4], holes[10, 10] = np.nan, np.inf
    out = fourier_crop(holes, (16, 16))
    assert np.isfinite(out).all() and abs(float(out.mean()) - 5) < 0.2
    assert np.isfinite(fourier_crop(np.full((8, 8), np.nan, np.float32), (4, 4))).all()
    for bad in ((41, 60), (40, 61), (0, 10), (10, 0)):
        with pytest.raises(InvalidRequestError):
            fourier_crop(counts, bad)
    with pytest.raises(InvalidRequestError):
        fourier_crop(np.zeros((2, 4, 4), np.float32), (2, 2))  # type: ignore[arg-type]
