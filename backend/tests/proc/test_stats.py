from __future__ import annotations

import json
import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from imagebio.core.errors import InvalidRequestError
from imagebio.proc.stats import PlaneStats, plane_stats

RNG = np.random.default_rng(5)


def reference_histogram(values: np.ndarray, lo: float, hi: float, bins: int) -> list[int]:
    idx = np.floor((values.astype(np.float64).ravel() - lo) * (bins / (hi - lo))).astype(np.int64)
    return np.bincount(np.clip(idx, 0, bins - 1), minlength=bins).tolist()


def check_invariants(s: PlaneStats, n_total: int) -> None:
    js = json.loads(json.dumps(s.to_json(), allow_nan=False))  # every float is finite
    assert s.robust_lo < s.robust_hi and s.auto_lo < s.auto_hi and s.hist_lo < s.hist_hi
    assert sum(s.histogram) == s.n_finite == sum(js["histogram"]["counts"])
    assert s.n_finite + s.n_nan + s.n_posinf + s.n_neginf == n_total
    if s.n_finite:
        assert s.min <= s.mean <= s.max and s.std >= 0


@pytest.mark.parametrize(
    "dtype", ["uint8", "int8", "uint16", "int16", "uint32", "int32", "float32", "float64"]
)
def test_exact_moments_against_numpy(dtype: str):
    if np.dtype(dtype).kind == "f":
        a = RNG.normal(5000, 800, (301, 517)).astype(dtype)
    else:
        info = np.iinfo(dtype)
        a = RNG.integers(info.min, info.max, (301, 517), dtype=dtype, endpoint=True)
    s = plane_stats(a)
    check_invariants(s, a.size)
    ref = a.astype(np.float64)
    assert s.min == float(a.min()) and s.max == float(a.max())  # exact, also for the uint32 extremes
    assert s.mean == pytest.approx(ref.mean(), rel=1e-12, abs=1e-9)
    assert s.std == pytest.approx(ref.std(), rel=1e-9)
    assert list(s.histogram) == reference_histogram(a, s.hist_lo, s.hist_hi, 256)


def test_percentiles_are_close_to_the_full_data_ones():
    a = RNG.normal(100, 10, (1500, 1500)).astype(np.float32)  # 2.25 M values, subsampled about 1 in 3
    s = plane_stats(a)
    lo, alo, ahi, hi = np.percentile(a, [0.1, 2, 98, 99.9])
    # standard error of a quantile = sqrt(p(1-p)/n) / density: about 0.11 at 0.1 % and 0.03 at 2 %
    # for ~750k samples of N(100, 10); the tolerances are about 5 standard errors
    assert s.robust_lo == pytest.approx(lo, abs=0.5) and s.robust_hi == pytest.approx(hi, abs=0.5)
    assert s.auto_lo == pytest.approx(alo, abs=0.15) and s.auto_hi == pytest.approx(ahi, abs=0.15)
    assert plane_stats(a) == s  # deterministic


def test_subsample_does_not_alias_with_the_image_width():
    # columns alternate between two levels: a step dividing the width would only see one of them
    a = np.tile(np.array([0, 1000], np.float32), (1024, 512))  # 1024 x 1024, step would be 2
    s = plane_stats(a, max_samples=a.size // 2)
    assert s.robust_lo < 100 and s.robust_hi > 900


def test_hot_pixel_and_nan_do_not_move_the_display_range():
    clean = RNG.normal(100, 5, (512, 512)).astype(np.float32)
    dirty = clean.copy()
    dirty[10, 10] = 1e9
    dirty[20, 20] = -1e9
    dirty[30, 30:40] = np.nan
    dirty[40, 40] = np.inf
    dirty[41, 41] = -np.inf
    a, b = plane_stats(clean), plane_stats(dirty)
    check_invariants(b, dirty.size)
    assert (b.n_nan, b.n_posinf, b.n_neginf) == (10, 1, 1)
    assert b.max == 1e9 and b.min == -1e9  # the true extremes are still reported
    for name in ("robust_lo", "robust_hi", "auto_lo", "auto_hi"):
        assert getattr(b, name) == pytest.approx(getattr(a, name), abs=0.5), name
    assert b.histogram[0] >= 1 and b.histogram[-1] >= 1  # outliers are counted in the end bins


def test_all_nan_and_empty_planes():
    for a in (np.full((4, 4), np.nan, np.float32), np.full((2, 2), np.inf), np.zeros((0, 5), np.float32)):
        s = plane_stats(a)
        check_invariants(s, a.size)
        assert s.n_finite == 0 and (s.min, s.max, s.mean, s.std) == (0, 0, 0, 0)
        assert (s.robust_lo, s.robust_hi, s.auto_lo, s.auto_hi) == (0, 1, 0, 1)


def test_constant_images_get_a_non_empty_range():
    s = plane_stats(np.full((8, 8), 7, np.uint16))
    assert (s.robust_lo, s.robust_hi) == (6.5, 7.5) and s.std == 0 and sum(s.histogram) == 64
    s = plane_stats(np.full((8, 8), 2.5e6, np.float32))
    assert s.robust_lo < 2.5e6 < s.robust_hi and s.robust_hi - s.robust_lo == pytest.approx(5.0)
    s = plane_stats(np.zeros((3, 3), np.float32))
    assert s.robust_lo < 0 < s.robust_hi
    s = plane_stats(np.array([[42.0]]))
    check_invariants(s, 1)
    assert s.min == s.max == s.mean == 42.0


def test_large_offsets_keep_the_variance_accurate():
    a = (1e7 + RNG.normal(0, 1, (256, 256))).astype(np.float64)
    assert plane_stats(a).std == pytest.approx(a.std(), rel=1e-6)


def test_extreme_float64_values_stay_finite():
    a = np.array([[1e308, -1e308], [1e308, 0.0]])
    s = plane_stats(a)
    check_invariants(s, 4)
    assert math.isfinite(s.std) and s.max == 1e308


def test_bool_and_custom_bins():
    a = np.zeros((10, 10), bool)
    a[:3] = True
    s = plane_stats(a, bins=2)
    assert s.mean == pytest.approx(0.3) and list(s.histogram) == [70, 30]


@pytest.mark.parametrize("kw", [{"bins": 0}, {"bins": 1.5}, {"robust": (50, 50)}, {"auto": (-1, 99)},
                                {"robust": (0, 101)}, {"max_samples": 0}])  # fmt: skip
def test_parameter_validation(kw: dict[str, object]):
    with pytest.raises(InvalidRequestError):
        plane_stats(np.zeros((4, 4), np.float32), **kw)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        plane_stats(np.zeros((2, 2), np.complex64))


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    h=st.integers(1, 12), w=st.integers(1, 12), seed=st.integers(0, 2**16),
    specials=st.lists(st.sampled_from([np.nan, np.inf, -np.inf, 1e30, -1e30, 0.0]), max_size=6),
)  # fmt: skip
def test_property_invariants(h: int, w: int, seed: int, specials: list[float]):
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 1, (h, w)).astype(np.float32)
    for k, value in enumerate(specials):
        a.flat[(k * 7) % a.size] = value
    s = plane_stats(a, bins=16, max_samples=50)
    check_invariants(s, a.size)
    finite = a[np.isfinite(a)]
    if finite.size:
        assert s.min == float(finite.min()) and s.max == float(finite.max())


@pytest.mark.perf
def test_stats_of_a_4k_plane_are_fast():
    import time

    for dtype in ("float32", "uint16"):
        a = RNG.normal(1000, 100, (4096, 4096)).astype(dtype)
        plane_stats(a)
        t0 = time.perf_counter()
        plane_stats(a)
        assert time.perf_counter() - t0 < 0.3
