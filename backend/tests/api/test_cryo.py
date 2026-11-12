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


CTF = "/api/datasets/ctf"


@pytest.fixture(scope="module")
def thon(tree: Tree) -> Tree:
    """Two micrographs of known defocus (1.8 µm at 300 kV, 1.0 Å per pixel), one without rings."""
    from fixtures.ctf import micrograph

    rng = np.random.default_rng(61)
    with mrcfile.new(tree.base / "public/thon.mrc") as m:
        m.set_data(micrograph((1024, 1024), 1.0, 1.8, rng))
        m.voxel_size = 1.0
    tifffile.imwrite(tree.base / "public/thon_nocal.tif", micrograph((1024, 1024), 1.0, 1.2, rng))
    with mrcfile.new(tree.base / "public/flat.mrc") as m:
        m.set_data(rng.normal(10, 1, (1024, 1024)).astype(np.float32))
        m.voxel_size = 1.0
    return tree


def test_the_ctf_of_an_image_through_the_api(client, thon: Tree):
    r = client.get(CTF, params={"path": "public/thon.mrc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] is True and body["defocus_um"] == pytest.approx(1.8, rel=0.02)
    assert (body["kv"], body["cs_mm"], body["amplitude"], body["apix"]) == (300.0, 2.7, 0.07, 1.0)
    assert 3.4 < body["fit_resolution"] < 7 and body["correlation"] > 0.7 and body["tile"] == 1024
    # the first zero: where the phase reaches pi, 1 / sqrt((1 - asin(A) / pi) / (lambda * defocus))
    first_zero = 1 / np.sqrt((1 - np.arcsin(0.07) / np.pi) / (0.019687 * 1.8e4))
    assert body["zeros"][0] == pytest.approx(first_zero, rel=0.02)
    assert body["zeros"] == sorted(body["zeros"], reverse=True)
    assert (
        client.get(
            CTF, params={"path": "public/thon.mrc"}, headers={"If-None-Match": r.headers["etag"]}
        ).status_code
        == 304
    )
    # read with another microscope, the same rings mean another defocus: the parameters count
    other = client.get(CTF, params={"path": "public/thon.mrc", "kv": 200, "cs": 1.4, "amp": 0.1}).json()
    assert (other["kv"], other["cs_mm"], other["amplitude"]) == (200.0, 1.4, 0.1)
    assert other["defocus_um"] == pytest.approx(1.8 * 0.019687 / 0.025079, rel=0.04)
    assert (
        other != body
        and r.headers["etag"]
        != client.get(CTF, params={"path": "public/thon.mrc", "kv": 200}).headers["etag"]
    )

    # no rings: said so, without a number
    flat = client.get(CTF, params={"path": "public/flat.mrc"}).json()
    assert flat == {"found": False, "kv": 300.0, "cs_mm": 2.7, "amplitude": 0.07}
    # a file that does not know its pixel size needs one
    unknown = client.get(CTF, params={"path": "public/thon_nocal.tif"})
    assert unknown.status_code == 400 and "pixel size unknown" in unknown.json()["error"]["message"]
    typed = client.get(CTF, params={"path": "public/thon_nocal.tif", "apix": 1.0}).json()
    assert typed["found"] is True and typed["defocus_um"] == pytest.approx(1.2, rel=0.02)


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"path": "public/thon.mrc", "kv": 5}, 400),
        ({"path": "public/thon.mrc", "cs": -1}, 400),
        ({"path": "public/thon.mrc", "amp": 0.9}, 400),
        ({"path": "public/thon.mrc", "apix": 0}, 400),
        ({"path": "public/thon.mrc", "t": 1}, 400),
        ({"path": "public/thon.mrc", "lp": 10}, 400),  # filters do not apply to a spectrum
        ({"path": "light/rgb.png", "apix": 1.0}, 400),
        ({"path": "public"}, 400),
        ({"path": "public/missing.mrc"}, 404),
        ({"path": "../outside/secret.mrc"}, 403),
        ({"path": "cryo/session/notes.txt"}, 415),
    ],
)
def test_bad_ctf_requests(client, thon: Tree, params, status):
    r = client.get(CTF, params=params)
    assert r.status_code == status, r.text
    assert str(thon.base) not in r.text


def test_the_server_knows_its_microscope(thon: Tree):
    with make_client(thon, kv=200.0, cs_mm=1.4, amplitude_contrast=0.1) as glacios:
        body = glacios.get(CTF, params={"path": "public/thon.mrc"}).json()
        assert (body["kv"], body["cs_mm"], body["amplitude"]) == (200.0, 1.4, 0.1)
        assert glacios.get(CTF, params={"path": "public/thon.mrc", "kv": 300}).json()["kv"] == 300.0


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


def test_a_plane_can_be_binned_in_fourier_space(client, tree: Tree):
    frames = tree.frames  # type: ignore[attr-defined]
    path = {"path": "public/frames.mrc", "t": 3}
    whole = client.get(PLANE, params={**path, "bin": 4, "fbin": "true"})
    assert whole.status_code == 200 and whole.headers["x-dtype"] == "float32"
    assert whole.headers["x-shape"] == "24,32" and whole.headers["x-bin"] == "4"
    reference = naive_fourier_crop(frames[3], (24, 32))
    np.testing.assert_allclose(decode(whole), reference, atol=2e-3)
    blocks = client.get(PLANE, params={**path, "bin": 4})
    assert whole.headers["etag"] != blocks.headers["etag"]  # another answer for a cache
    assert np.abs(decode(whole) - decode(blocks)).max() > 0.5  # and another image: not block means

    # a region is cut out of the plane reduced as a whole: tiles agree with the overview
    region = client.get(PLANE, params={**path, "bin": 4, "fbin": "true", "x": 32, "y": 16, "w": 64, "h": 40})
    assert region.headers["x-shape"] == "10,16" and region.headers["x-region"] == "32,16,64,40"
    np.testing.assert_array_equal(decode(region), decode(whole)[4:14, 8:24])
    edge = client.get(PLANE, params={**path, "bin": 4, "fbin": "true", "x": 96, "y": 64, "w": 32, "h": 32})
    np.testing.assert_array_equal(decode(edge), decode(whole)[16:24, 24:32])
    off = client.get(PLANE, params={**path, "bin": 4, "fbin": "true", "x": 30, "y": 16, "w": 64, "h": 40})
    assert off.status_code == 400 and "multiple of the bin" in off.json()["error"]["message"]

    # after the mean of frames; a filter comes after the reduction, at the pitch of the reduced
    # grid (ADR 013), with or without fbin (tests/service/test_reduced_filter.py: same image)
    mean = frames[2:7].astype(np.float64).mean(axis=0).astype(np.float32)
    averaged = client.get(
        PLANE, params={"path": "public/frames.mrc", "t": 2, "t_end": 6, "bin": 2, "fbin": "true"}
    )
    np.testing.assert_allclose(decode(averaged), naive_fourier_crop(mean, (48, 64)), atol=2e-3)
    expected = fourier_filter(naive_fourier_crop(frames[3], (48, 64)), apix_x=2.5, lowpass=10)
    for fbin in ("true", "false"):
        filtered = client.get(PLANE, params={**path, "lp": 10, "bin": 2, "fbin": fbin})
        np.testing.assert_allclose(decode(filtered), expected, atol=2e-3)

    # at full resolution there is nothing to reduce: the stored values, as without it
    full = client.get(PLANE, params={**path, "fbin": "true"})
    np.testing.assert_array_equal(decode(full), frames[3])
    # the statistics are those of the plane itself, whatever the way it is shown
    assert (
        client.get(STATS, params={**path, "fbin": "true"}).json()["mean"]
        == client.get(STATS, params=path).json()["mean"]
    )
    assert client.get(PLANE, params={**path, "bin": 4, "fbin": "perhaps"}).status_code == 400


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
    # averaged, reduced in Fourier space, filtered at the reduced pitch, then cut (ADR 013)
    region = {"path": "public/frames.mrc", "t": 0, "t_end": 11, "lp": 20, "bin": 2, "y": 16, "w": 64, "h": 40}
    r = client.get(PLANE, params={**region, "x": 8})
    mean = frames.astype(np.float64).mean(axis=0).astype(np.float32)
    expected = fourier_filter(naive_fourier_crop(mean, (48, 64)), apix_x=2.5, lowpass=20)[8:28, 4:36]
    np.testing.assert_allclose(decode(r), expected, rtol=1e-5, atol=2e-3)
    # a region off the grid of the bin: filtered at full resolution, cropped, binned by blocks
    r = client.get(PLANE, params={**region, "x": 9})
    expected = bin_mean(fourier_filter(mean, apix_x=1.25, lowpass=20)[16:56, 9:73], 2)
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
    assert body["listed_defects"] is None  # this reference carries no defect list
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


# ── found by the independent review of this code ──────────────────────────────


@pytest.mark.parametrize(
    "params",
    [
        {"lp": 20, "apix": "1e-320"},
        {"lp": "1e300", "apix": "1e-300"},
        {"hp": "1.7e308"},
        {"lp": "2e19"},
        {"hp": "1e20", "apix": "1e30"},
    ],
)
def test_extreme_filter_values_are_a_client_error(client, params):
    for url in (PLANE, STATS):
        r = client.get(url, params={"path": "public/frames.mrc", **params})
        assert r.status_code == 400, (url, r.text)
        assert r.json()["error"]["code"] == "invalid_request"


def test_a_frame_average_does_not_bypass_the_plane_size_limit(tree: Tree):
    """The limit on the plane read into memory was only checked for single frames."""
    with make_client(tree) as limited:
        service = limited.app.state.service
        service.max_plane_bytes = (
            96 * 128 * 4 * 2 - 1
        )  # a float32 frame fits, its float64 accumulator does not
        assert limited.get(PLANE, params={"path": "public/frames.mrc"}).status_code == 200
        for url in (PLANE, STATS, SPECTRUM):
            r = limited.get(url, params={"path": "public/frames.mrc", "t": 0, "t_end": 3})
            assert r.status_code == 400 and "too large" in r.json()["error"]["message"], url
        r = limited.get(GAIN_CHECK, params={"path": "public/frames.mrc", "gain": "public/frames.mrc"})
        assert r.status_code == 400 and "too large" in r.json()["error"]["message"]
        service.max_plane_bytes = 96 * 128 * 4 - 1  # not even one frame
        assert limited.get(PLANE, params={"path": "public/frames.mrc"}).status_code == 400


def test_a_power_beyond_float32_does_not_break_the_profile(client, tree: Tree):
    huge = np.random.default_rng(5).normal(0, 1e20, (64, 64)).astype(np.float32)
    tifffile.imwrite(tree.base / "public/huge.tif", huge)
    profile = client.get(SPECTRUM + "/profile", params={"path": "public/huge.tif", "tile": 32})
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["side"] == 32 and len(body["power"]) == 17
    assert any(p is None for p in body["power"])  # not a number JSON can carry
    assert client.get(SPECTRUM, params={"path": "public/huge.tif", "tile": 32}).status_code == 200


def test_sharp_filters_and_statistics_of_a_reduced_view(client, tree: Tree):
    frames = tree.frames  # type: ignore[attr-defined]
    path = {"path": "public/frames.mrc", "t": 1}
    soft = client.get(PLANE, params={**path, "lp": 10})
    sharp = client.get(PLANE, params={**path, "lp": 10, "sharp": "true"})
    assert sharp.status_code == 200 and sharp.headers["etag"] != soft.headers["etag"]
    np.testing.assert_allclose(
        decode(sharp), fourier_filter(frames[1], apix_x=1.25, lowpass=10, sharp=True), rtol=1e-5, atol=1e-4
    )
    # without a filter the profile means nothing: the same answer, the same tag
    plain = client.get(PLANE, params=path)
    assert client.get(PLANE, params={**path, "sharp": "true"}).headers["etag"] == plain.headers["etag"]

    # the statistics of what a reduced view shows, for a view that follows a filter
    view = decode(client.get(PLANE, params={**path, "lp": 10, "bin": 4}))
    reduced = client.get(STATS, params={**path, "lp": 10, "bin": 4})
    full = client.get(STATS, params={**path, "lp": 10})
    assert reduced.status_code == 200 and reduced.headers["etag"] != full.headers["etag"]
    assert reduced.json()["mean"] == pytest.approx(float(view.astype(np.float64).mean()), rel=1e-6)
    assert reduced.json()["n_finite"] == view.size and full.json()["n_finite"] == frames[1].size
    for bad in (0, 65, "x"):
        assert client.get(STATS, params={**path, "bin": bad}).status_code == 400
