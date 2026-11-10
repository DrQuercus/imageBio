"""Gain references, against numpy references and synthetic detectors with a known answer."""

from __future__ import annotations

import numpy as np
import pytest

from imagebio.core.errors import InvalidRequestError
from imagebio.cryo.gain import (
    N_ORIENTATIONS,
    ORIENTATION_NAMES,
    apply_gain,
    check_orientation,
    fit_gain,
    flatness,
    orient,
    prepare_gain,
)


def detector(rng: np.random.Generator, shape: tuple[int, int], dose: float = 40.0):
    """A synthetic counting detector: per-pixel sensitivity, a smooth shading that is not
    symmetric, and the Poisson image of a flat illumination. Returns (raw sum, true gain)."""
    h, w = shape
    y, x = np.mgrid[0:h, 0:w]
    sensitivity = rng.normal(1.0, 0.06, shape) * (1.0 + 0.10 * x / w + 0.05 * (y / h) ** 2)
    raw = rng.poisson(dose * sensitivity).astype(np.float32)
    return raw, (1.0 / sensitivity).astype(np.float32)


# ── orientations ──────────────────────────────────────────────────────────────


def test_the_eight_orientations_are_those_of_the_square() -> None:
    a = np.arange(12).reshape(3, 4)
    expected = {
        "identity": a,
        "rot90": np.rot90(a),
        "rot180": a[::-1, ::-1],
        "rot270": np.rot90(a, 3),
        "flip_x": a[:, ::-1],
        "transpose": a.T,
        "flip_y": a[::-1, :],
        "anti_transpose": a[::-1, ::-1].T,
    }
    assert len(ORIENTATION_NAMES) == N_ORIENTATIONS == 8
    for k, name in enumerate(ORIENTATION_NAMES):
        np.testing.assert_array_equal(orient(a, k), expected[name], err_msg=name)
    images = {orient(a, k).tobytes() + bytes(orient(a, k).shape) for k in range(8)}
    assert len(images) == 8  # all different


@pytest.mark.parametrize("k", [-1, 8, 1.0, True, None])
def test_an_invalid_orientation_is_refused(k: object) -> None:
    with pytest.raises(InvalidRequestError):
        orient(np.zeros((2, 2)), k)  # type: ignore[arg-type]


# ── preparing and fitting ─────────────────────────────────────────────────────


def test_a_multiplicative_reference_is_kept_and_its_bad_values_become_defects() -> None:
    rng = np.random.default_rng(0)
    raw = rng.uniform(0.8, 1.2, (8, 8))
    bad = [(0, 2), (1, 0), (1, 1), (1, 2)]
    for position, value in zip(bad, (0.0, np.nan, -2.0, np.inf), strict=True):
        raw[position] = value
    gain = prepare_gain(raw)
    assert gain.dtype == np.float32
    expected = raw.astype(np.float32)
    for position in bad:
        expected[position] = 0
    np.testing.assert_array_equal(gain, expected)


def test_a_counts_reference_is_inverted_around_its_mean() -> None:
    counts = np.full((4, 4), 200.0)
    counts[0, 0], counts[0, 1], counts[1, 0] = 100.0, 300.0, 0.0  # the mean of the valid stays 200
    gain = prepare_gain(counts, invert=True)
    assert gain[0, 0] == pytest.approx(2.0) and gain[0, 1] == pytest.approx(200.0 / 300.0)
    assert gain[1, 0] == 0 and gain[2, 2] == pytest.approx(1.0)
    # a flat exposure seen through that detector comes out flat
    np.testing.assert_allclose((counts * gain)[counts > 0], 200.0, rtol=1e-6)


def test_a_reference_without_a_usable_pixel_is_refused() -> None:
    for bad in (np.zeros((4, 4)), np.full((4, 4), np.nan), np.zeros((2, 3, 4)), np.zeros(5)):
        with pytest.raises(InvalidRequestError):
            prepare_gain(bad)


def test_an_image_that_is_mostly_defects_is_not_a_gain_reference() -> None:
    raw = np.ones((100, 100), np.float32)
    raw[:10, :] = 0  # exactly a tenth: still accepted
    assert (prepare_gain(raw) == 0).sum() == 1000
    raw[10, 0] = 0
    with pytest.raises(InvalidRequestError, match="does not look like a gain reference"):
        prepare_gain(raw)
    with pytest.raises(InvalidRequestError, match="does not look like a gain reference"):
        prepare_gain(np.where(np.arange(10000).reshape(100, 100) % 3 == 0, 1.0, np.nan), invert=True)


def test_fit_gain_averages_a_finer_reference_and_repeats_a_coarser_one() -> None:
    rng = np.random.default_rng(1)
    fine = rng.uniform(0.8, 1.2, (8, 12)).astype(np.float32)
    assert fit_gain(fine, (8, 12)) is fine
    np.testing.assert_allclose(fit_gain(fine, (4, 6)), fine.reshape(4, 2, 6, 2).mean(axis=(1, 3)), rtol=1e-6)
    np.testing.assert_array_equal(fit_gain(fine, (16, 24)), np.kron(fine, np.ones((2, 2), np.float32)))
    np.testing.assert_array_equal(fit_gain(fine, (32, 48)), np.kron(fine, np.ones((4, 4), np.float32)))


def test_fit_gain_averages_only_the_valid_pixels_of_a_block() -> None:
    fine = np.array([[1.0, 0.0, 0.0, 0.0], [3.0, 2.0, 0.0, 0.0]], np.float32)
    np.testing.assert_array_equal(fit_gain(fine, (1, 2)), np.array([[2.0, 0.0]], np.float32))


@pytest.mark.parametrize("shape", [(8, 10), (3, 12), (16, 12), (4, 12), (12, 18)])
def test_fit_gain_refuses_sizes_that_are_not_in_an_integer_ratio(shape: tuple[int, int]) -> None:
    with pytest.raises(InvalidRequestError, match="does not fit"):
        fit_gain(np.ones((8, 12), np.float32), shape)


# ── applying ──────────────────────────────────────────────────────────────────


def test_apply_gain_multiplies_and_returns_float32() -> None:
    rng = np.random.default_rng(2)
    plane = rng.integers(0, 60000, (16, 20)).astype(np.uint16)
    gain = rng.uniform(0.5, 1.5, (16, 20)).astype(np.float32)
    out = apply_gain(plane, gain)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, plane.astype(np.float64) * gain, rtol=1e-6)
    with pytest.raises(InvalidRequestError):
        apply_gain(plane, gain[:, :10])


def test_a_defect_takes_the_median_of_its_valid_neighbours() -> None:
    plane = np.arange(25, dtype=np.float32).reshape(5, 5)
    gain = np.ones((5, 5), np.float32)
    gain[2, 2] = 0  # isolated
    gain[0, 0] = 0  # corner: 3 neighbours
    gain[4, 3] = gain[4, 4] = 0  # two side by side on an edge
    plane[2, 2] = 9999  # a hot pixel, which is why it is a defect
    out = apply_gain(plane, gain)
    assert out[2, 2] == np.median([6, 7, 8, 11, 13, 16, 17, 18])
    assert out[0, 0] == np.median([1, 5, 6])
    assert out[4, 3] == np.median([17, 18, 19, 22])  # 24 is a defect too
    assert out[4, 4] == np.median([18, 19])
    keep = gain > 0
    np.testing.assert_array_equal(out[keep], plane[keep])


def test_dead_columns_and_clusters_are_filled_from_further_away() -> None:
    rng = np.random.default_rng(3)
    plane = rng.normal(100, 5, (40, 40)).astype(np.float32)
    gain = np.ones((40, 40), np.float32)
    gain[:, 10:13] = 0  # three dead columns: the middle one has no valid neighbour
    gain[20:27, 25:32] = 0  # a 7 x 7 dead cluster
    plane[gain == 0] = 1e6
    out = apply_gain(plane, gain)
    assert np.isfinite(out).all()
    assert out[gain == 0].max() < 130  # every defect got a plausible value
    assert out[gain == 0].min() > 70
    assert out[5, 11] == np.median(plane[3:8, 9:14][gain[3:8, 9:14] > 0])  # 5 x 5 around it


def test_defects_are_filled_like_a_naive_loop_does() -> None:
    """Reference: for each defect, the median of the valid pixels of the smallest window, from
    3 x 3 to 9 x 9, that holds one; 0 when none does."""
    rng = np.random.default_rng(6)
    plane = rng.normal(100, 20, (60, 70)).astype(np.float32)
    gain = rng.uniform(0.8, 1.2, (60, 70)).astype(np.float32)
    gain[rng.random((60, 70)) < 0.08] = 0  # scattered, some of them touching
    gain[:, 33:36] = 0
    gain[10:21, 40:51] = 0  # 11 x 11: its centre is out of reach
    gain[0, 0] = gain[59, 69] = gain[0, 69] = 0
    plane[5, 5] = np.nan  # a valid pixel that is not finite is not a neighbour to trust
    gain[5, 6] = 0
    product = (plane * gain).astype(np.float32)
    expected = product.copy()
    for y, x in zip(*np.nonzero(gain == 0), strict=True):
        expected[y, x] = 0
        for reach in (1, 2, 3, 4):
            y0, y1, x0, x1 = (
                max(0, y - reach),
                min(60, y + reach + 1),
                max(0, x - reach),
                min(70, x + reach + 1),
            )
            window = product[y0:y1, x0:x1][gain[y0:y1, x0:x1] > 0]
            window = window[np.isfinite(window)]
            if window.size:
                expected[y, x] = np.median(window)
                break
    out = apply_gain(plane, gain)
    np.testing.assert_array_equal(out, expected)
    assert out[15, 45] == 0  # the centre of the large cluster


def test_a_cluster_too_large_to_fill_is_zero_not_garbage() -> None:
    plane = np.full((32, 32), 50, np.float32)
    gain = np.ones((32, 32), np.float32)
    gain[4:28, 4:28] = 0
    plane[gain == 0] = np.nan
    out = apply_gain(plane, gain)
    assert np.isfinite(out).all()
    assert out[16, 16] == 0
    assert out[4, 4] == 50


# ── flatness and orientation ──────────────────────────────────────────────────


def test_flatness_is_the_relative_variance_of_blocks() -> None:
    rng = np.random.default_rng(4)
    image = rng.normal(100, 5, (256, 320))
    blocks = image[:256, :320].reshape(4, 64, 5, 64)
    expected = np.median(blocks.var(axis=(1, 3)) / blocks.mean(axis=(1, 3)) ** 2)
    assert flatness(image) == pytest.approx(expected, rel=1e-9)
    assert flatness(np.full((128, 128), 7.0)) == 0.0
    assert flatness(np.zeros((128, 128))) == float("inf")  # nothing to measure


def test_flatness_ignores_what_only_a_few_blocks_contain() -> None:
    rng = np.random.default_rng(5)
    image = rng.normal(100, 5, (512, 512))
    reference = flatness(image)
    image[:, 100] = 1e6  # a hot column crosses 8 of the 64 blocks
    image[300:364, 300:364] = np.nan
    assert flatness(image) == pytest.approx(reference, rel=0.15)


@pytest.mark.parametrize("stored", range(N_ORIENTATIONS))
def test_a_gain_stored_in_any_orientation_is_found(stored: int) -> None:
    """Acceptance (M4): a gain deliberately flipped or rotated is detected."""
    rng = np.random.default_rng(10 + stored)
    raw, gain = detector(rng, (512, 512))
    # the file holds the gain in orientation `stored`: undoing it is the inverse transform
    on_disk = np.ascontiguousarray(orient(gain, stored))
    check = check_orientation(raw, on_disk)
    np.testing.assert_array_equal(orient(on_disk, check.best), gain)  # the best orientation restores it
    assert check.confident
    assert check.improves
    assert (check.best == 0) == (stored == 0)
    assert all(s is not None and s > 0 for s in check.scores)
    assert check.ratio > 1.05
    body = check.to_json(current=0)
    assert body["best_name"] == ORIENTATION_NAMES[check.best]
    assert body["current_is_best"] is (stored == 0)
    assert [o["index"] for o in body["orientations"]] == list(range(8))


def test_the_right_gain_gives_shot_noise_and_the_wrong_one_adds_the_pattern() -> None:
    rng = np.random.default_rng(20)
    raw, gain = detector(rng, (512, 512), dose=50.0)
    check = check_orientation(raw, np.ascontiguousarray(gain[::-1, :]))  # stored upside down
    assert ORIENTATION_NAMES[check.best] == "flip_y"
    best = check.scores[check.best]
    assert best is not None
    # corrected, a flat Poisson image has a relative variance close to 1 / dose
    assert best == pytest.approx(1 / 50.0, rel=0.15)
    assert check.uncorrected > best
    assert (
        check.scores[0] is not None and check.scores[0] > check.uncorrected
    )  # as stored: worse than no gain


def test_a_rectangular_detector_only_tries_the_orientations_that_fit() -> None:
    rng = np.random.default_rng(21)
    raw, gain = detector(rng, (384, 512))
    check = check_orientation(raw, np.ascontiguousarray(gain[:, ::-1]))
    assert ORIENTATION_NAMES[check.best] == "flip_x"
    fits = [s is not None for s in check.scores]
    assert fits == [True, False, True, False, True, False, True, False]
    assert check.confident
    # stored turned by a quarter: the reference is 512 x 384 and only the quarter turns fit
    turned = check_orientation(raw, np.ascontiguousarray(np.rot90(gain)))
    assert ORIENTATION_NAMES[turned.best] == "rot270"
    assert [s is not None for s in turned.scores] == [False, True, False, True, False, True, False, True]


def test_defects_do_not_mislead_the_check() -> None:
    rng = np.random.default_rng(22)
    raw, gain = detector(rng, (512, 512))
    gain[:, 77] = 0  # a dead column, hot in the raw image
    gain[rng.integers(0, 512, 300), rng.integers(0, 512, 300)] = 0
    raw[gain == 0] = 60000
    check = check_orientation(raw, np.ascontiguousarray(gain.T))
    assert ORIENTATION_NAMES[check.best] == "transpose"
    assert check.confident


@pytest.mark.parametrize("scale", [1.0, 0.1, 0.01, 0.001, 50.0])
def test_the_scale_of_the_gain_does_not_mislead_the_check(scale: float) -> None:
    """Found in review: left-out pixels were set to the level of the raw image, which made
    outliers of them once the gain was far from 1, and the wrong orientation won."""
    for seed in range(4):
        rng = np.random.default_rng(100 + seed)
        raw, gain = detector(rng, (512, 512))
        gain[rng.integers(0, 512, 300), rng.integers(0, 512, 300)] = 0
        raw[gain == 0] = 60000  # hot pixels, known to the reference
        check = check_orientation(raw, np.ascontiguousarray(gain.T) * np.float32(scale))
        assert ORIENTATION_NAMES[check.best] == "transpose", (scale, seed)
        assert check.confident


def test_a_perfectly_flat_tie_is_not_a_confident_answer() -> None:
    """Found in review: with every score at 0 the margin was infinite, hence 'confident'."""
    check = check_orientation(np.ones((128, 128), np.float32), np.ones((128, 128), np.float32))
    assert all(s == 0 for s in check.scores)
    assert check.ratio == 1.0 and not check.confident and not check.improves


def test_a_finer_reference_is_checked_on_a_coarser_image() -> None:
    rng = np.random.default_rng(23)
    raw, gain = detector(rng, (256, 256))
    fine = np.kron(gain, np.ones((2, 2), np.float32))  # an "8K" reference for a "4K" movie
    check = check_orientation(raw, np.ascontiguousarray(fine[::-1, ::-1]))
    assert ORIENTATION_NAMES[check.best] == "rot180"


def test_a_gain_that_tells_nothing_is_not_trusted() -> None:
    rng = np.random.default_rng(24)
    raw = rng.poisson(40.0, (256, 256)).astype(np.float32)
    check = check_orientation(raw, np.ones((256, 256), np.float32))  # every orientation is the same
    assert not check.confident
    assert check.ratio == pytest.approx(1.0)
    assert not check.improves


def test_check_orientation_refuses_what_it_cannot_judge() -> None:
    with pytest.raises(InvalidRequestError, match="empty"):
        check_orientation(np.zeros((64, 64), np.float32), np.ones((64, 64), np.float32))
    with pytest.raises(InvalidRequestError, match="does not fit"):
        check_orientation(np.ones((64, 64), np.float32), np.ones((48, 100), np.float32))
    with pytest.raises(InvalidRequestError):
        check_orientation(np.ones((2, 64, 64), np.float32), np.ones((64, 64), np.float32))
