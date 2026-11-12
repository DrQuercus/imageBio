"""A filtered view at bin > 1 is reduced by Fourier cropping, then filtered (ADR 013).

The reference is the order of ADR 006: the full-resolution plane filtered, then reduced by a
Fourier crop written here with numpy alone. The two agree away from the edges; along them,
each treats the border its own way. Block means are not an option for a filtered view: the
noise they fold under the cut-off makes the image differ by up to a quarter of its contrast.
"""

from __future__ import annotations

import time
from pathlib import Path

import mrcfile
import numpy as np
import pytest

from imagebio.core.config import Settings
from imagebio.proc.binning import bin_mean
from imagebio.proc.fourier import fourier_filter
from imagebio.service.planes import PlaneService, Processing

APIX = 1.0


def naive_crop(a: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Keep the lowest frequencies of the full transform (complex FFT, no symmetry used)."""
    h, w = a.shape
    oh, ow = shape
    spectrum = np.fft.fftshift(np.fft.fft2(a.astype(np.float64)))
    top, left = h // 2 - oh // 2, w // 2 - ow // 2
    kept = spectrum[top : top + oh, left : left + ow].copy()
    if oh % 2 == 0:  # the Nyquist row of the result: both signs of the frequency, averaged
        kept[0, :] = 0.5 * (spectrum[top, left : left + ow] + spectrum[top + oh, left : left + ow])
    if ow % 2 == 0:
        kept[:, 0] = 0.5 * (spectrum[top : top + oh, left] + spectrum[top : top + oh, left + ow])
    if oh % 2 == 0 and ow % 2 == 0:
        kept[0, 0] = 0.25 * (
            spectrum[top, left]
            + spectrum[top + oh, left]
            + spectrum[top, left + ow]
            + spectrum[top + oh, left + ow]
        )
    return np.real(np.fft.ifft2(np.fft.ifftshift(kept))) * (oh * ow) / (h * w)


def micrograph(h: int, w: int, seed: int = 3) -> np.ndarray:
    """Dark blobs of 60 to 150 Å, a slope of ice thickness and strong white noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    img = 0.002 * xx.astype(np.float64)
    for _ in range(h * w // 20_000):
        cy, cx, r = rng.uniform(0, h), rng.uniform(0, w), rng.uniform(30, 75)
        img -= np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * r * r))
    return (100 + img + rng.normal(0, 3.0, img.shape)).astype(np.float32)


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, np.ndarray]:
    data = micrograph(768, 1000)
    path = tmp_path_factory.mktemp("reduced") / "m.mrc"
    with mrcfile.new(path) as m:
        m.set_data(data)
        m.voxel_size = APIX
    return path, data


def rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(a, dtype=np.float64))))


@pytest.mark.parametrize("sharp", [False, True])
@pytest.mark.parametrize(
    ("bin", "lowpass", "highpass"),
    [(2, 20.0, None), (4, 20.0, None), (4, 60.0, None), (2, 6.0, None), (4, None, 400.0), (4, 15.0, 300.0)],
)
def test_reducing_then_filtering_gives_the_reduction_of_the_filtered_plane(
    image: tuple[Path, np.ndarray], bin: int, lowpass: float | None, highpass: float | None, sharp: bool
) -> None:
    path, data = image
    svc = PlaneService(Settings())
    proc = Processing(lowpass=lowpass, highpass=highpass, sharp=sharp)
    view = svc.plane(path, bin=bin, proc=proc).array
    shape = (-(-data.shape[0] // bin), -(-data.shape[1] // bin))
    filtered = fourier_filter(data, apix_x=APIX, lowpass=lowpass, highpass=highpass, sharp=sharp)
    reference = naive_crop(filtered, shape)
    assert view.shape == reference.shape and view.dtype == np.float32
    contrast = float(reference.std())
    # 16 reduced pixels away from the edges: the same image. A high-pass keeps the mean of the
    # image extended by its mirror, which differs a little with the extension: a uniform
    # shift of the grey levels, which the automatic window absorbs.
    core = (slice(16, -16), slice(16, -16))
    diff = view[core] - reference[core]
    assert abs(float(diff.mean())) < (0.04 if highpass else 0.002) * contrast
    assert rms(diff - diff.mean()) < 0.015 * contrast
    # along the edges, each treats the border its own way; still the same picture
    assert rms(view - reference) < 0.12 * contrast
    # the block means a fast path could have used fold noise under the cut-off: kept out
    if lowpass is not None and lowpass <= 5 * APIX * bin:
        blocks = fourier_filter(
            bin_mean(data, bin), apix_x=APIX * bin, lowpass=lowpass, highpass=highpass, sharp=sharp
        )
        assert rms(blocks[core] - reference[core]) > 5 * rms(diff)


def test_block_or_fourier_binning_give_the_same_filtered_view(image: tuple[Path, np.ndarray]) -> None:
    path, _ = image
    svc = PlaneService(Settings())
    blocks = svc.plane(path, bin=4, proc=Processing(lowpass=20.0)).array
    fourier = svc.plane(path, bin=4, proc=Processing(lowpass=20.0, fourier_bin=True)).array
    np.testing.assert_array_equal(blocks, fourier)
    # without a filter, the choice still matters
    plain_blocks = svc.plane(path, bin=4).array
    plain_fourier = svc.plane(path, bin=4, proc=Processing(fourier_bin=True)).array
    assert np.abs(plain_blocks - plain_fourier).max() > 0.5


def test_the_unfiltered_reduction_is_made_once_for_every_filter(
    image: tuple[Path, np.ndarray], monkeypatch: pytest.MonkeyPatch
) -> None:
    from imagebio.service import planes

    path, _ = image
    calls: list[tuple[int, ...]] = []
    original = planes.fourier_crop

    def counting(a: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
        calls.append(a.shape)
        return original(a, shape)

    monkeypatch.setattr(planes, "fourier_crop", counting)
    svc = PlaneService(Settings())
    for lp in (40.0, 30.0, 20.0, 12.0):
        svc.plane(path, bin=4, proc=Processing(lowpass=lp))
    svc.plane(path, bin=4, proc=Processing(lowpass=20.0, highpass=500.0, sharp=True))
    svc.plane(path, bin=4, proc=Processing(fourier_bin=True))  # the unfiltered view: the same reduction
    assert calls == [(768, 1000)]


def test_regions_are_cut_from_the_filtered_reduced_plane(image: tuple[Path, np.ndarray]) -> None:
    path, data = image
    svc = PlaneService(Settings())
    proc = Processing(lowpass=25.0, sharp=True)
    whole = svc.plane(path, bin=2, proc=proc).array
    tile = svc.plane(path, bin=2, region=(200, 100, 300, 250), proc=proc)
    np.testing.assert_array_equal(tile.array, whole[50:175, 100:250])
    # off the grid of the bin: the plane filtered at full resolution, then binned by blocks
    off = svc.plane(path, bin=2, region=(201, 100, 300, 250), proc=proc).array
    reference = bin_mean(fourier_filter(data, apix_x=APIX, lowpass=25.0, sharp=True)[100:350, 201:501], 2)
    np.testing.assert_allclose(off, reference, rtol=1e-5, atol=1e-3)


def test_statistics_of_a_reduced_view_are_those_of_what_is_served(image: tuple[Path, np.ndarray]) -> None:
    path, data = image
    svc = PlaneService(Settings())
    proc = Processing(lowpass=20.0)
    view = svc.plane(path, bin=4, proc=proc).array.astype(np.float64)
    stats, _ = svc.stats(path, proc=proc, bin=4)
    assert stats.mean == pytest.approx(view.mean(), rel=1e-6)
    assert stats.std == pytest.approx(view.std(), rel=1e-4)
    assert stats.auto_lo == pytest.approx(np.percentile(view, 2), rel=1e-4)
    full, _ = svc.stats(path, proc=proc)
    assert full.n_finite == data.size and stats.n_finite == view.size
    # a 20 Å low-pass leaves almost nothing above the reduced Nyquist: the windows agree
    assert stats.auto_lo == pytest.approx(full.auto_lo, abs=0.05 * full.std)
    assert stats.auto_hi == pytest.approx(full.auto_hi, abs=0.05 * full.std)


@pytest.mark.perf
def test_moving_a_filter_on_a_4k_overview_is_fast(tmp_path: Path) -> None:
    data = np.random.default_rng(0).normal(100, 3, (4096, 4096)).astype(np.float32)
    path = tmp_path / "k.mrc"
    with mrcfile.new(path) as m:
        m.set_data(data)
        m.voxel_size = 1.0
    svc = PlaneService(Settings())
    svc.plane(path, bin=4, proc=Processing(lowpass=30.0))  # the reduction, made once
    t0 = time.perf_counter()
    for lp in (25.0, 20.0, 15.0, 12.0):
        svc.plane(path, bin=4, proc=Processing(lowpass=lp))
        svc.stats(path, proc=Processing(lowpass=lp), bin=4)
    assert (time.perf_counter() - t0) / 4 < 0.15
