"""Measurements on the images of a session, against plain numpy on the same pixels."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from imagebio.core.config import Settings
from imagebio.live.metrics import ICE_BAND, ICE_SIDES, ice_ring_ratio, measure, measure_plane, robust_mean_std
from imagebio.live.simulator import mrc_bytes
from imagebio.service.planes import PlaneService


def trimmed(plane: np.ndarray) -> np.ndarray:
    """The definition, written out: the finite pixels between the 0.1th and 99.9th percentile."""
    values = np.asarray(plane, np.float64).ravel()
    values = values[np.isfinite(values)]
    low, high = np.percentile(values, [0.1, 99.9])
    return values[(values >= low) & (values <= high)]


def test_mean_and_spread_are_those_of_the_pixels_between_the_extreme_percentiles() -> None:
    rng = np.random.default_rng(1)
    plane = rng.normal(50, 4, (300, 400)).astype(np.float32)
    mean, std = robust_mean_std(plane)  # type: ignore[misc]
    kept = trimmed(plane)
    assert mean == pytest.approx(kept.mean(), rel=1e-6) and std == pytest.approx(kept.std(), rel=1e-6)
    counts = rng.poisson(20, (128, 128)).astype(np.uint16)  # stored counts, not floats
    assert robust_mean_std(counts)[0] == pytest.approx(trimmed(counts).mean(), rel=1e-6)  # type: ignore[index]
    flat = np.full((64, 64), 7, np.int16)
    assert robust_mean_std(flat) == (7.0, 0.0)


def test_hot_pixels_and_holes_in_the_data_do_not_move_the_mean() -> None:
    rng = np.random.default_rng(2)
    clean = rng.poisson(20, (512, 512)).astype(np.float32)
    dirty = clean.copy()
    hot = rng.choice(dirty.size, 60, replace=False)
    dirty.flat[hot] = 60000.0  # what a plain mean would follow: 20 becomes 33
    dirty[10, :40] = np.nan
    dirty[11, :3] = (np.inf, -np.inf, np.nan)
    assert abs(float(np.nanmean(np.where(np.isfinite(dirty), dirty, np.nan))) - 20) > 10
    mean, std = robust_mean_std(dirty)  # type: ignore[misc]
    assert mean == pytest.approx(clean.mean(), abs=0.05) and std == pytest.approx(clean.std(), abs=0.1)
    assert robust_mean_std(np.full((8, 8), np.nan, np.float32)) is None


def test_a_large_image_is_measured_on_an_evenly_spaced_subset() -> None:
    rng = np.random.default_rng(3)
    plane = rng.normal(100, 10, (2500, 3000)).astype(
        np.float32
    )  # 7.5 million pixels: one out of two each way
    mean, std = robust_mean_std(plane)  # type: ignore[misc]
    subset = trimmed(plane[::2, ::2])
    assert mean == pytest.approx(subset.mean(), rel=1e-6) and std == pytest.approx(subset.std(), rel=1e-6)
    whole = trimmed(plane)  # and that says the same as all the pixels, to a thousandth of the spread
    assert mean == pytest.approx(whole.mean(), abs=0.03) and std == pytest.approx(whole.std(), abs=0.03)
    just_under = rng.normal(100, 10, (2048, 2048)).astype(np.float32)  # four million: all of them
    assert robust_mean_std(just_under)[0] == pytest.approx(trimmed(just_under).mean(), rel=1e-6)  # type: ignore[index]


def naive_ice_ratio(plane: np.ndarray, apix: float) -> float:
    """The ratio from one periodogram of the whole image: no tiles, no window."""
    data = np.asarray(plane, np.float64)
    power = np.abs(np.fft.fft2(data - data.mean())) ** 2
    fy = np.fft.fftfreq(data.shape[0])[:, None]
    fx = np.fft.fftfreq(data.shape[1])[None, :]
    frequency = np.hypot(fy, fx)

    def band(limits: tuple[float, float]) -> float:
        fine, coarse = limits
        return float(power[(frequency >= apix / coarse) & (frequency <= apix / fine)].mean())

    return band(ICE_BAND) / ((band(ICE_SIDES[0]) + band(ICE_SIDES[1])) / 2)


def rings(shape: tuple[int, int], apix: float, spacing: float, rng: np.random.Generator) -> np.ndarray:
    """Plane waves of period ``spacing`` Å in many directions: what a powder of crystals gives."""
    y, x = np.mgrid[: shape[0], : shape[1]].astype(np.float64)
    out = np.zeros(shape)
    for angle in rng.uniform(0, np.pi, 24):
        k = 2 * np.pi * apix / spacing
        out += np.cos(k * (x * np.cos(angle) + y * np.sin(angle)) + rng.uniform(0, 2 * np.pi))
    return out


def test_the_ice_ring_ratio_is_one_on_noise_and_follows_a_plain_periodogram() -> None:
    rng = np.random.default_rng(4)
    noise = rng.normal(0, 1, (1024, 1024))
    flat = ice_ring_ratio(noise.astype(np.float32), 1.0)
    assert flat == pytest.approx(1.0, abs=0.03) and naive_ice_ratio(noise, 1.0) == pytest.approx(
        1.0, abs=0.03
    )

    for spacing in (3.90, 3.67, 3.44):  # the rings of hexagonal ice
        icy = noise + 0.15 * rings(noise.shape, 1.0, spacing, rng)
        ratio = ice_ring_ratio(icy.astype(np.float32), 1.0)
        assert ratio is not None and ratio > 1.5
        assert ratio == pytest.approx(naive_ice_ratio(icy, 1.0), rel=0.15)
    # at another pixel size the band is elsewhere in the spectrum
    icy = noise + 0.15 * rings(noise.shape, 0.83, 3.67, rng)
    assert ice_ring_ratio(icy.astype(np.float32), 0.83) == pytest.approx(naive_ice_ratio(icy, 0.83), rel=0.15)
    assert ice_ring_ratio(icy.astype(np.float32), 1.3) == pytest.approx(
        1.0, abs=0.1
    )  # read with a wrong size
    # periodic signal that is not ice (a 5 Å lattice) leaves the ratio alone
    other = noise + 0.15 * rings(noise.shape, 1.0, 5.0, rng)
    assert ice_ring_ratio(other.astype(np.float32), 1.0) == pytest.approx(1.0, abs=0.05)


def test_the_ice_ring_ratio_is_not_given_when_it_cannot_be_measured() -> None:
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 1, (512, 512)).astype(np.float32)
    assert ice_ring_ratio(noise, 1.52) is not None  # Nyquist at 3.04 Å: the fine side is just there
    assert ice_ring_ratio(noise, 1.6) is None  # too coarse a pixel: no fine side to compare to
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        assert ice_ring_ratio(noise, bad) is None
    assert ice_ring_ratio(noise[:511, :], 1.0) is None  # too small a field
    assert ice_ring_ratio(np.zeros((512, 512), np.float32), 1.0) is None  # nothing to compare
    holes = noise.copy()
    holes[::7, ::5] = np.nan
    assert ice_ring_ratio(holes, 1.0) == pytest.approx(1.0, abs=0.2)  # finite all the same
    assert measure_plane(noise, None).keys() == {"mean", "std"}  # no pixel size, no ring
    assert measure_plane(noise, 1.0).keys() == {"mean", "std", "ice_ring"}
    assert measure_plane(np.full((64, 64), np.nan, np.float32), 1.0) == {}


def test_plain_noise_is_never_called_crystalline_ice() -> None:
    """Regression: on a 64-pixel image the bands held a hundred points of the spectrum, the
    ratio wandered by 20 %, and one image of plain noise in five was above the 1.2 at which the
    interface marks crystalline ice. Small images get no ratio; the others stay close to 1."""
    rng = np.random.default_rng(9)
    for side in (64, 256, 511):
        assert ice_ring_ratio(rng.normal(0, 1, (side, side)).astype(np.float32), 0.83) is None
    for apix in (0.83, 0.5, 1.3):  # the smaller the pixel, the fewer points in the bands
        ratios = np.array(
            [ice_ring_ratio(rng.normal(30, 3, (512, 512)).astype(np.float32), apix) for _ in range(60)],
            dtype=np.float64,
        )
        assert ratios.min() > 0.83 and ratios.max() < 1.17, apix
        assert ratios.mean() == pytest.approx(1.0, abs=0.02)
    field = np.array(  # what a real image gives: a field of 2048 pixels, counts of a few electrons
        [ice_ring_ratio(rng.poisson(2, (2048, 2048)).astype(np.uint16), 0.83) for _ in range(4)],
        dtype=np.float64,
    )
    assert field.min() > 0.98 and field.max() < 1.02


def test_a_large_image_is_measured_on_its_central_field() -> None:
    rng = np.random.default_rng(6)
    plane = rng.normal(0, 1, (2500, 2300)).astype(np.float32)
    plane[:200] += 3 * rings((200, 2300), 1.0, 3.67, rng).astype(np.float32)  # ice at the edge only
    assert ice_ring_ratio(plane, 1.0) == pytest.approx(1.0, abs=0.05)
    plane[1100:1400] += rings((300, 2300), 1.0, 3.67, rng).astype(np.float32)  # ice in the middle
    assert ice_ring_ratio(plane, 1.0) > 1.5  # type: ignore[operator]


@pytest.fixture
def planes():
    service = PlaneService(Settings())
    yield service
    service.close()


def test_an_image_file_is_measured_with_its_own_pixel_size(tmp_path: Path, planes: PlaneService) -> None:
    rng = np.random.default_rng(7)
    image = (rng.normal(30, 2, (512, 512)) + 0.5 * rings((512, 512), 1.1, 3.67, rng)).astype(np.float32)
    (tmp_path / "a.mrc").write_bytes(mrc_bytes(image, 1.1))
    measured = measure(planes, tmp_path / "a.mrc", "image", apix=9.9)  # the header knows better
    assert measured.keys() == {"mean", "std", "ice_ring"}
    assert measured["mean"] == pytest.approx(trimmed(image).mean(), rel=1e-6)
    assert measured["std"] == pytest.approx(trimmed(image).std(), rel=1e-6)
    assert (
        measured["ice_ring"] == pytest.approx(naive_ice_ratio(image, 1.1), rel=0.2)
        and measured["ice_ring"] > 1.5
    )

    # a file that does not know its pixel size takes the one of its metadata, or does without
    from PIL import Image

    Image.fromarray(np.clip(image * 4, 0, 255).astype(np.uint8)).save(tmp_path / "a.png")
    assert "ice_ring" not in measure(planes, tmp_path / "a.png", "image")
    assert measure(planes, tmp_path / "a.png", "image", apix=1.1)["ice_ring"] > 1.3
    Image.fromarray(np.zeros((32, 32, 3), np.uint8)).save(tmp_path / "rgb.png")
    assert measure(planes, tmp_path / "rgb.png", "image") == {}  # a picture has no counts


def test_a_movie_is_measured_on_its_first_frames_and_says_so(tmp_path: Path, planes: PlaneService) -> None:
    rng = np.random.default_rng(8)
    movie = rng.poisson(3, (10, 512, 512)).astype(np.uint16)
    movie[5:] += 50  # frames that are not read must not count
    (tmp_path / "m.mrc").write_bytes(mrc_bytes(movie, 0.9, stack=True))
    measured = measure(planes, tmp_path / "m.mrc", "movie")
    first = movie[:4].astype(np.float64).mean(axis=0)
    assert measured["mean"] == pytest.approx(trimmed(first).mean() * 10, rel=1e-5)
    assert measured["estimated"] is True and measured["frames"] == 10 and "std" not in measured
    assert measured["ice_ring"] == pytest.approx(1.0, abs=0.15)
    # the same file taken as an image: its first frame, nothing estimated
    as_image = measure(planes, tmp_path / "m.mrc", "image")
    assert (
        as_image["mean"] == pytest.approx(trimmed(movie[0]).mean(), rel=1e-5) and "estimated" not in as_image
    )
