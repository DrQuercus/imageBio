from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import PLANE_DTYPES
from imagebio.proc.binning import bin_mean, binned_shape, crop

RNG = np.random.default_rng(11)


def naive_bin(a: np.ndarray, f: int) -> np.ndarray:
    """Reference: plain Python loops in float64."""
    h, w = a.shape
    out = np.empty((-(-h // f), -(-w // f)), np.float64)
    for i in range(out.shape[0]):
        for j in range(out.shape[1]):
            block = a[i * f : (i + 1) * f, j * f : (j + 1) * f].astype(np.float64)
            out[i, j] = block.sum() / block.size
    return out.astype(np.float32)


def sample(dtype: np.dtype, shape: tuple[int, int]) -> np.ndarray:
    if dtype.kind == "f":
        return RNG.normal(1000, 300, shape).astype(dtype)
    info = np.iinfo(dtype)
    return RNG.integers(info.min, info.max, shape, dtype=dtype, endpoint=True)


@pytest.mark.parametrize("dtype", sorted(PLANE_DTYPES, key=str))
@pytest.mark.parametrize("factor", [2, 3, 4, 8, 9, 16])
def test_matches_naive_reference_for_every_dtype(dtype: np.dtype, factor: int):
    for shape in ((33, 47), (32, 48), (1, 50), (50, 1), (17, 17)):
        a = sample(dtype, shape)
        out = bin_mean(a, factor)
        assert out.dtype == np.float32 and out.shape == binned_shape(*shape, factor)
        np.testing.assert_allclose(out, naive_bin(a, factor), rtol=1e-6)


def test_factor_one_returns_the_input_itself():
    a = sample(np.dtype("uint16"), (5, 7))
    assert bin_mean(a, 1) is a


def test_integer_extremes_do_not_overflow():
    for dtype in ("uint32", "int32", "int16", "uint16", "uint8", "int8"):
        info = np.iinfo(dtype)
        for value in (info.max, info.min):
            a = np.full((64, 64), value, dtype)
            np.testing.assert_array_equal(bin_mean(a, 64), np.float32([[value]]))
            np.testing.assert_array_equal(bin_mean(a, 4), np.full((16, 16), value, np.float32))


def test_factor_larger_than_the_image_gives_its_mean():
    a = sample(np.dtype("float32"), (5, 9))
    out = bin_mean(a, 100)
    assert out.shape == (1, 1)
    assert out[0, 0] == pytest.approx(a.astype(np.float64).mean(), rel=1e-6)


def test_partial_edge_blocks_average_only_existing_pixels():
    a = np.arange(5 * 7, dtype=np.uint8).reshape(5, 7)
    out = bin_mean(a, 4)
    assert out.shape == (2, 2)
    assert out[0, 1] == a[:4, 4:].mean() and out[1, 0] == a[4:, :4].mean() and out[1, 1] == a[4:, 4:].mean()


def test_nan_and_inf_propagate_to_their_block_only():
    a = np.ones((8, 8), np.float32)
    a[0, 0], a[5, 5] = np.nan, np.inf
    out = bin_mean(a, 4)
    assert np.isnan(out[0, 0]) and np.isposinf(out[1, 1]) and out[0, 1] == 1 and out[1, 0] == 1


def test_bool_planes():
    a = np.zeros((4, 4), bool)
    a[:2] = True
    np.testing.assert_array_equal(bin_mean(a, 2), np.float32([[1, 1], [0, 0]]))


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "2", None])
def test_factor_validation(bad: object):
    with pytest.raises(InvalidRequestError):
        bin_mean(np.zeros((4, 4), np.uint8), bad)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        binned_shape(4, 4, bad)  # type: ignore[arg-type]


def test_not_a_plane():
    with pytest.raises(InvalidRequestError):
        bin_mean(np.zeros((2, 2, 2), np.uint8), 2)


@settings(max_examples=150, deadline=None, derandomize=True)
@given(h=st.integers(1, 24), w=st.integers(1, 24), f=st.integers(1, 30), seed=st.integers(0, 2**16))
def test_property_matches_reference(h: int, w: int, f: int, seed: int):
    a = np.random.default_rng(seed).integers(0, 65535, (h, w), dtype=np.uint16)
    np.testing.assert_allclose(bin_mean(a, f), naive_bin(a, f) if f > 1 else a, rtol=1e-6)


def test_crop_is_a_validated_view():
    a = np.arange(30).reshape(5, 6)
    region = crop(a, 1, 2, 3, 2)
    np.testing.assert_array_equal(region, a[2:4, 1:4])
    assert np.shares_memory(region, a)
    np.testing.assert_array_equal(crop(a, 0, 0, 6, 5), a)
    for bad in ((0, 0, 7, 1), (0, 0, 1, 6), (-1, 0, 1, 1), (0, -1, 1, 1), (6, 0, 1, 1), (0, 0, 0, 1),
                (0, 0, 1, 0), (5, 4, 2, 1), (0.0, 0, 1, 1), (True, 0, 1, 1)):  # fmt: skip
        with pytest.raises(InvalidRequestError):
            crop(a, *bad)  # type: ignore[arg-type]


@pytest.mark.perf
def test_binning_a_4k_plane_is_fast():
    import time

    for dtype in ("float32", "uint16"):
        a = sample(np.dtype(dtype), (4096, 4096))
        bin_mean(a, 4)
        t0 = time.perf_counter()
        bin_mean(a, 4)
        assert time.perf_counter() - t0 < 0.15
