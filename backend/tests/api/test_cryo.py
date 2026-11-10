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


# ── gain references ───────────────────────────────────────────────────────────

GAIN_CHECK = "/api/cryo/gain/check"
HOT = (40, 70)  # y, x of a pixel that fires in every fraction


@pytest.fixture(scope="module")
def detector(tree: Tree) -> dict[str, np.ndarray]:
    """An EER movie from a detector with a fixed pattern, and its gain reference stored in
    several ways. Returns the true gain and the expected mean of the raw frames."""
    from fixtures.eer import write_eer

    rng = np.random.default_rng(31)
    shape = (128, 128)
    y, x = np.mgrid[0:128, 0:128]
    sensitivity = np.clip(rng.normal(1.0, 0.2, shape), 0.4, 1.6) * (
        1.0 + 0.3 * x / 128 + 0.15 * (y / 128) ** 2
    )
    eer = write_eer(
        tree.base / "cryo/pattern.eer",
        n_fractions=24,
        shape=shape,
        events=4000,  # 0.24 electron per pixel and per fraction: 5.9 per pixel in the movie
        hot=(HOT[0] * 128 + HOT[1],),
        sensitivity=sensitivity,
        seed=5,
    )
    gain = (sensitivity.mean() / sensitivity).astype(np.float32)
    gain[HOT] = 0  # the reference knows the hot pixel
    tifffile.imwrite(tree.base / "cryo/good.gain", gain)
    tifffile.imwrite(tree.base / "cryo/flipped.gain", gain[::-1, :])
    tifffile.imwrite(tree.base / "cryo/fine.gain", np.kron(gain, np.ones((2, 2), np.float32)))
    tifffile.imwrite(tree.base / "cryo/wrong_size.gain", np.ones((100, 128), np.float32))
    tifffile.imwrite(tree.base / "light/other.gain", gain)
    counts = np.where(gain > 0, 1000.0 / np.where(gain > 0, gain, 1), 0).astype(np.float32)  # a flat field
    with mrcfile.new(tree.base / "cryo/flat_counts.mrc") as m:
        m.set_data(counts)
    return {"gain": gain, "mean": eer.truth() / 24.0}


def corrected(mean: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """Reference correction: multiply, then the hot pixel takes the median of its 8 neighbours."""
    out = (mean * gain).astype(np.float32)
    y, x = HOT
    around = np.delete(out[y - 1 : y + 2, x - 1 : x + 2].ravel(), 4)
    out[y, x] = np.median(around)
    return out


MOVIE = {"path": "cryo/pattern.eer", "opt.group": 1}
WHOLE = {**MOVIE, "t": 0, "t_end": 23}


def test_a_flipped_gain_is_detected(client, detector):
    """Acceptance (M4): a synthetic EER with a deliberately flipped gain is detected."""
    r = client.get(GAIN_CHECK, params={**MOVIE, "gain": "cryo/flipped.gain"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["best_name"] == "flip_y" and body["best"] == 6
    assert body["current"] == 0 and body["current_is_best"] is False
    assert body["confident"] is True and body["improves"] is True
    assert body["ratio"] > 1.05
    assert body["frames"] == 20 and body["defects"] == 1
    assert body["gain_shape"] == {"x": 128, "y": 128}
    scores = [o["score"] for o in body["orientations"]]
    assert len(scores) == 8 and min(scores) == scores[6]
    assert scores[0] > body["uncorrected"] > scores[6]  # as stored it makes things worse than no gain

    fixed = client.get(GAIN_CHECK, params={**MOVIE, "gain": "cryo/flipped.gain", "gain_orient": 6}).json()
    assert fixed["current_is_best"] is True and fixed["best"] == 6


def test_a_gain_in_the_right_orientation_is_confirmed(client, detector):
    body = client.get(GAIN_CHECK, params={**MOVIE, "gain": "cryo/good.gain"}).json()
    assert body["best_name"] == "identity" and body["current_is_best"] is True
    assert body["confident"] is True and body["improves"] is True
    fine = client.get(GAIN_CHECK, params={**MOVIE, "gain": "cryo/fine.gain"}).json()  # a 2x finer reference
    assert fine["best_name"] == "identity" and fine["gain_shape"] == {"x": 256, "y": 256}
    assert fine["defects"] == 4


def test_the_gain_is_applied_and_the_defect_filled(client, detector):
    expected = corrected(detector["mean"], detector["gain"])
    r = client.get(PLANE, params={**WHOLE, "gain": "cryo/good.gain"})
    assert r.status_code == 200 and r.headers["x-dtype"] == "float32"
    np.testing.assert_allclose(decode(r), expected, rtol=1e-5)
    assert decode(r)[HOT] < 20  # 24 raw counts on the hot pixel, a handful around it

    # the flipped file gives the same image once told how it is stored, and a different one otherwise
    turned = client.get(PLANE, params={**WHOLE, "gain": "cryo/flipped.gain", "gain_orient": 6})
    np.testing.assert_allclose(decode(turned), expected, rtol=1e-5)
    as_stored = client.get(PLANE, params={**WHOLE, "gain": "cryo/flipped.gain"})
    assert not np.allclose(decode(as_stored), expected, rtol=1e-2)
    assert turned.headers["etag"] != as_stored.headers["etag"] != r.headers["etag"]

    # a finer reference is averaged down, a counts reference is inverted
    fine = client.get(PLANE, params={**WHOLE, "gain": "cryo/fine.gain"})
    np.testing.assert_allclose(decode(fine), expected, rtol=1e-5)
    counts = client.get(PLANE, params={**WHOLE, "gain": "cryo/flat_counts.mrc", "gain_inv": "true"})
    np.testing.assert_allclose(decode(counts), expected, rtol=1e-4)

    # a single frame, binned: gain first, binning after
    one = client.get(PLANE, params={**MOVIE, "t": 3, "gain": "cryo/good.gain", "bin": 4})
    assert one.status_code == 200
    raw = decode(client.get(PLANE, params={**MOVIE, "t": 3})).astype(np.float32)
    np.testing.assert_allclose(decode(one), bin_mean(corrected(raw, detector["gain"]), 4), rtol=1e-5)


def test_the_gain_makes_the_image_flatter(client, detector):
    raw = client.get(STATS, params=WHOLE).json()
    good = client.get(STATS, params={**WHOLE, "gain": "cryo/good.gain"}).json()
    bad = client.get(STATS, params={**WHOLE, "gain": "cryo/flipped.gain"}).json()
    assert good["std"] < raw["std"] < bad["std"]
    assert good["max"] < raw["max"]  # the hot pixel is gone


def test_a_coarse_gain_is_repeated_for_super_resolution(client, detector):
    r = client.get(PLANE, params={**WHOLE, "opt.superres": 1, "gain": "cryo/good.gain", "bin": 2})
    assert r.status_code == 200 and r.headers["x-shape"] == "128,128"
    plain = decode(client.get(PLANE, params={**WHOLE, "opt.superres": 1})).astype(np.float64)
    gain = np.kron(detector["gain"], np.ones((2, 2)))
    keep = np.ones((128, 128), bool)
    keep[HOT] = False  # the 2 x 2 defect is filled from its neighbours
    np.testing.assert_allclose(
        decode(r)[keep], bin_mean((plain * gain).astype(np.float32), 2)[keep], rtol=1e-5
    )


def test_the_spectrum_takes_the_gain(client, detector):
    plain = client.get(SPECTRUM, params={**WHOLE, "tile": 64})
    flat = client.get(SPECTRUM, params={**WHOLE, "tile": 64, "gain": "cryo/good.gain"})
    assert plain.status_code == flat.status_code == 200
    assert plain.headers["etag"] != flat.headers["etag"]
    # the fixed pattern is white noise on top of the shot noise: correcting it lowers the floor
    assert np.median(decode(flat)) < np.median(decode(plain))


def test_a_replaced_gain_file_is_read_again(client, tree: Tree, detector):
    import os

    path = tree.base / "cryo/replaced.gain"
    tifffile.imwrite(path, np.full((128, 128), 2.0, np.float32))
    first = client.get(PLANE, params={**WHOLE, "gain": "cryo/replaced.gain"})
    np.testing.assert_allclose(decode(first), detector["mean"] * 2.0, rtol=1e-6)
    tifffile.imwrite(path, np.full((128, 128), 3.0, np.float32))
    os.utime(path, ns=(1, 1))  # same size: only the date tells
    second = client.get(PLANE, params={**WHOLE, "gain": "cryo/replaced.gain"})
    np.testing.assert_allclose(decode(second), detector["mean"] * 3.0, rtol=1e-6)
    assert first.headers["etag"] != second.headers["etag"]


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"gain": "cryo/wrong_size.gain"}, 400, "invalid_request"),
        ({"gain": "cryo/good.gain", "gain_orient": 8}, 400, "invalid_request"),
        ({"gain": "cryo/good.gain", "gain_orient": -1}, 400, "invalid_request"),
        ({"gain": "cryo/good.gain", "gain_inv": "maybe"}, 400, "invalid_request"),
        ({"gain": "cryo/missing.gain"}, 404, "not_found"),
        ({"gain": "cryo/session"}, 400, "invalid_request"),  # a directory
        ({"gain": "cryo/session/notes.txt"}, 415, "unsupported_format"),
        ({"gain": "light/rgb.png"}, 403, "access_denied"),  # a root this user cannot read
        ({"gain": "../outside/secret.mrc"}, 403, "access_denied"),  # outside the roots: same refusal
        ({"gain": "cryo/../../outside/secret.mrc"}, 403, "access_denied"),
        ({"gain": "/etc/passwd"}, 403, "access_denied"),
        ({"gain": ""}, 400, "invalid_request"),
    ],
)
def test_bad_gain_requests(tree: Tree, detector, params, status, code):
    from fixtures.api import ALICE

    with make_client(tree, auth_mode="proxy", trust_network=True) as proxied:  # alice: cryoem, not lm
        for url in (PLANE, STATS, SPECTRUM, GAIN_CHECK):
            r = proxied.get(url, params={**MOVIE, **params}, headers=ALICE)
            assert r.status_code == status, (url, r.text)
            assert r.json()["error"]["code"] == code
            assert str(tree.base) not in r.text


def test_a_colour_picture_is_not_a_gain(client, detector):
    r = client.get(PLANE, params={**WHOLE, "gain": "light/rgb.png"})
    assert r.status_code == 400 and "gain reference" in r.json()["error"]["message"]


def test_the_check_needs_a_gain(client, detector):
    r = client.get(GAIN_CHECK, params=MOVIE)
    assert r.status_code == 400 and "gain" in r.json()["error"]["message"]
    r = client.get(GAIN_CHECK, params={**MOVIE, "gain": "cryo/good.gain", "t": 3})
    assert r.status_code == 400  # unknown parameter
