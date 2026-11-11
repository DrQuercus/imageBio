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


def test_a_large_image_is_trimmed_from_a_subset_and_averaged_in_full() -> None:
    rng = np.random.default_rng(3)
    plane = rng.normal(100, 10, (1500, 1500)).astype(np.float32)  # more pixels than the subset holds
    mean, std = robust_mean_std(plane)  # type: ignore[misc]
    kept = trimmed(plane)
    assert mean == pytest.approx(kept.mean(), abs=0.02) and std == pytest.approx(kept.std(), abs=0.05)


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
    assert ice_ring_ratio(noise[:40, :40], 1.0) is None  # too small a field
    assert ice_ring_ratio(np.zeros((256, 256), np.float32), 1.0) is None  # nothing to compare
    holes = noise.copy()
    holes[::7, ::5] = np.nan
    assert ice_ring_ratio(holes, 1.0) == pytest.approx(1.0, abs=0.2)  # finite all the same
    assert measure_plane(noise, None).keys() == {"mean", "std"}  # no pixel size, no ring
    assert measure_plane(noise, 1.0).keys() == {"mean", "std", "ice_ring"}
    assert measure_plane(np.full((64, 64), np.nan, np.float32), 1.0) == {}


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
    image = (rng.normal(30, 2, (256, 256)) + 0.5 * rings((256, 256), 1.1, 3.67, rng)).astype(np.float32)
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
    movie = rng.poisson(3, (10, 128, 128)).astype(np.uint16)
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
