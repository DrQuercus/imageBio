"""Cryo processing through the API: frame averages, resolution filters, power spectrum."""

from __future__ import annotations

import json
from pathlib import Path

import mrcfile
import numpy as np
import pytest
import tifffile

from fixtures.api import Tree, build_tree, decode, make_client
from imagebio.proc.binning import bin_mean
from imagebio.proc.fourier import fourier_filter

PLANE = "/api/datasets/plane"
STATS = "/api/datasets/stats"
SPECTRUM = "/api/datasets/spectrum"


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Tree:
    t = build_tree(tmp_path_factory.mktemp("cryo"))
    rng = np.random.default_rng(8)
    frames = rng.normal(100, 10, (12, 96, 128)).astype(np.float32)
    with mrcfile.new(t.base / "public/frames.mrc") as m:  # 12 frames, 1.25 Å per pixel
        m.set_data(frames)
        m.set_image_stack()
        m.voxel_size = 1.25
    x = np.mgrid[0:300, 0:300][1]
    lattice = (np.cos(2 * np.pi * x / 8.0) + rng.normal(0, 0.3, (300, 300))).astype(np.float32)
    with mrcfile.new(t.base / "public/lattice.mrc") as m:  # period 8 px = 10 Å at 1.25 Å per pixel
        m.set_data(lattice)
        m.voxel_size = 1.25
    tifffile.imwrite(t.base / "public/nocal.tif", rng.normal(50, 5, (64, 64)).astype(np.float32))
    t.frames = frames  # type: ignore[attr-defined]
    return t


@pytest.fixture(scope="module")
def client(tree: Tree):
    with make_client(tree) as c:
        yield c


def test_mean_of_frames_3_to_7_equals_the_numpy_mean(client, tree: Tree):
    frames = tree.frames  # type: ignore[attr-defined]
    r = client.get(PLANE, params={"path": "public/frames.mrc", "t": 2, "t_end": 6})  # frames 3..7, 1-based
    assert r.status_code == 200 and r.headers["x-dtype"] == "float32"
    expected = frames[2:7].astype(np.float64).mean(axis=0).astype(np.float32)
    np.testing.assert_array_equal(decode(r), expected)
    whole = client.get(PLANE, params={"path": "public/frames.mrc", "t": 0, "t_end": 11})
    np.testing.assert_allclose(decode(whole), frames.mean(axis=0), rtol=1e-6)
    single = client.get(PLANE, params={"path": "public/frames.mrc", "t": 4, "t_end": 4})
    np.testing.assert_array_equal(decode(single), frames[4])
    binned = client.get(PLANE, params={"path": "public/frames.mrc", "t": 2, "t_end": 6, "bin": 4})
    np.testing.assert_array_equal(decode(binned), bin_mean(expected, 4))
    stats = client.get(STATS, params={"path": "public/frames.mrc", "t": 2, "t_end": 6}).json()
    assert stats["std"] == pytest.approx(float(expected.astype(np.float64).std()), rel=1e-6)
    assert stats["std"] < 10 / 2  # averaging five frames reduces the noise


def test_eer_average_over_groups(client, tree: Tree):
    r = client.get(PLANE, params={"path": "cryo/movie.eer", "opt.group": 3, "t": 0, "t_end": 3})
    np.testing.assert_allclose(decode(r), tree.eer.truth() / 4, rtol=1e-6)  # mean of the 4 group sums


@pytest.mark.parametrize(
    "params", [{"t": 5, "t_end": 4}, {"t": 0, "t_end": 12}, {"t_end": -1}, {"t_end": "x"}]
)
def test_bad_frame_ranges(client, params: dict[str, object]):
    r = client.get(PLANE, params={"path": "public/frames.mrc", **params})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


def test_lowpass_in_angstrom_uses_the_file_pixel_size(client, tree: Tree):
    frames = tree.frames  # type: ignore[attr-defined]
    r = client.get(PLANE, params={"path": "public/frames.mrc", "t": 1, "lp": 20})
    reference = fourier_filter(frames[1], apix_x=1.25, lowpass=20)
    np.testing.assert_allclose(decode(r), reference, rtol=1e-5, atol=1e-4)
    both = client.get(PLANE, params={"path": "public/frames.mrc", "t": 1, "lp": 10, "hp": 80})
    np.testing.assert_allclose(
        decode(both), fourier_filter(frames[1], apix_x=1.25, lowpass=10, highpass=80), rtol=1e-5, atol=1e-4
    )
    # averaged, then filtered, then cropped and binned
    r = client.get(
        PLANE,
        params={
            "path": "public/frames.mrc",
            "t": 0,
            "t_end": 11,
            "lp": 20,
            "bin": 2,
            "x": 8,
            "y": 16,
            "w": 64,
            "h": 40,
        },
    )
    mean = frames.astype(np.float64).mean(axis=0).astype(np.float32)
    expected = bin_mean(fourier_filter(mean, apix_x=1.25, lowpass=20)[16:56, 8:72], 2)
    np.testing.assert_allclose(decode(r), expected, rtol=1e-5, atol=1e-4)
    # the statistics follow: the filtered image has far less noise
    raw = client.get(STATS, params={"path": "public/frames.mrc", "t": 1}).json()
    filtered = client.get(STATS, params={"path": "public/frames.mrc", "t": 1, "lp": 20}).json()
    assert filtered["std"] < raw["std"] / 3 and filtered["mean"] == pytest.approx(raw["mean"], abs=0.5)


def test_a_wave_at_the_cutoff_comes_out_at_half_amplitude(client, tree: Tree):
    # lattice.mrc: period 10 Å. Low-pass at 10 Å -> gain 0.5 +/- 0.03 (acceptance criterion of M4)
    raw = decode(client.get(PLANE, params={"path": "public/lattice.mrc"}))
    out = decode(client.get(PLANE, params={"path": "public/lattice.mrc", "lp": 10}))
    column_profile = lambda a: a[40:-40, 40:-40].mean(axis=0)  # noqa: E731 - averages the noise out
    gain = column_profile(out).std() / column_profile(raw).std()
    assert gain == pytest.approx(0.5, abs=0.03)


def test_filtering_needs_a_pixel_size(client):
    r = client.get(PLANE, params={"path": "public/nocal.tif", "lp": 20})
    assert r.status_code == 400 and "pixel size unknown" in r.json()["error"]["message"]
    assert client.get(STATS, params={"path": "public/nocal.tif", "lp": 20}).status_code == 400
    ok = client.get(PLANE, params={"path": "public/nocal.tif", "lp": 20, "apix": 2.0})
    assert ok.status_code == 200
    other = client.get(PLANE, params={"path": "public/nocal.tif", "lp": 20, "apix": 4.0})
    assert not np.array_equal(decode(ok), decode(other))  # the user's pixel size is part of the cache key
    assert ok.headers["etag"] != other.headers["etag"]
    for bad in ({"lp": 0}, {"lp": -3}, {"hp": "x"}, {"apix": 0}, {"lp": 30, "hp": 20, "apix": 1}):
        assert client.get(PLANE, params={"path": "public/nocal.tif", **bad}).status_code == 400, bad


def test_processing_is_part_of_the_etag(client):
    base = {"path": "public/frames.mrc", "t": 1}
    tags = {
        json.dumps(extra): client.get(PLANE, params={**base, **extra}).headers["etag"]
        for extra in ({}, {"lp": 20}, {"lp": 10}, {"hp": 50}, {"t_end": 3}, {"t_end": 3, "lp": 20})
    }
    assert len(set(tags.values())) == len(tags)
    again = client.get(PLANE, params={**base, "lp": 20}, headers={"If-None-Match": tags['{"lp": 20}']})
    assert again.status_code == 304


def test_power_spectrum_peak_and_profile(client):
    r = client.get(SPECTRUM, params={"path": "public/lattice.mrc", "tile": 128})
    assert r.status_code == 200 and (r.headers["x-dtype"], r.headers["x-shape"]) == ("float32", "128,128")
    power = decode(r)
    y, x = np.unravel_index(int(np.argmax(power)), power.shape)
    # period 10 Å at 1.25 Å per pixel: radius = side * apix / d = 16 px (acceptance: within 0.5 px)
    assert float(np.hypot(y - 64, x - 64)) == pytest.approx(128 * 1.25 / 10.0, abs=0.5)
    profile = client.get(SPECTRUM + "/profile", params={"path": "public/lattice.mrc", "tile": 128}).json()
    assert profile["side"] == 128 and len(profile["frequency"]) == len(profile["power"]) == 65
    assert profile["frequency"][-1] == 0.5
    assert int(np.argmax(profile["power"][1:])) + 1 == 16
    etag = r.headers["etag"]
    assert (
        client.get(
            SPECTRUM, params={"path": "public/lattice.mrc", "tile": 128}, headers={"If-None-Match": etag}
        ).status_code
        == 304
    )
    # of an average of frames, and limits
    assert (
        client.get(
            SPECTRUM, params={"path": "public/frames.mrc", "t": 0, "t_end": 11, "tile": 64}
        ).status_code
        == 200
    )
    for bad in ({"tile": 8}, {"tile": 4096}, {"tile": 65}, {"lp": 20}, {"t": 99}):
        assert client.get(SPECTRUM, params={"path": "public/frames.mrc", **bad}).status_code == 400, bad


def test_spectrum_respects_authorisation(tree: Tree):
    from fixtures.api import ALICE, BOB

    with make_client(tree, auth_mode="proxy") as c:
        for url in (SPECTRUM, SPECTRUM + "/profile"):
            assert c.get(url, params={"path": "cryo/movie.mrc"}, headers=ALICE).status_code == 200
            assert c.get(url, params={"path": "cryo/movie.mrc"}, headers=BOB).status_code == 403
            assert c.get(url, params={"path": "cryo/movie.mrc"}).status_code == 401


def test_the_average_does_not_fill_the_cache_with_raw_frames(tree: Tree, tmp_path: Path):
    frames = tree.frames  # type: ignore[attr-defined]
    with make_client(tree, plane_cache_bytes=3 * frames[0].nbytes) as c:
        c.get(PLANE, params={"path": "public/frames.mrc", "t": 3})  # one raw frame cached
        r = c.get(PLANE, params={"path": "public/frames.mrc", "t": 0, "t_end": 11})
        np.testing.assert_allclose(decode(r), frames.mean(axis=0), rtol=1e-6)
        stats = c.app.state.service.cache_stats()["planes"]  # type: ignore[attr-defined]
        assert stats["items"] == 2 and stats["evictions"] == 0  # the frame seen before, and the average
