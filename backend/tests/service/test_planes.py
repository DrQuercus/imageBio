from __future__ import annotations

import os
from pathlib import Path

import mrcfile
import numpy as np
import pytest

from fixtures.eer import write_eer
from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError, NotFoundError
from imagebio.io.readers import mrc as mrc_module
from imagebio.proc.binning import bin_mean
from imagebio.service.planes import PlaneService


def write_stack(path: Path, data: np.ndarray, voxel: float = 2.0) -> Path:
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(data)
        if data.ndim == 3:
            m.set_image_stack()
        m.voxel_size = voxel
    return path


@pytest.fixture
def stack(tmp_path: Path) -> tuple[Path, np.ndarray]:
    data = np.random.default_rng(1).normal(50, 5, (4, 40, 60)).astype(np.float32)
    return write_stack(tmp_path / "s.mrc", data), data


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int]]:
    """Every plane really read from an MRC file."""
    log: list[tuple[int, int, int]] = []
    original = mrc_module.MrcDataset._read_plane

    def counting(self: mrc_module.MrcDataset, t: int, c: int, z: int) -> np.ndarray:
        log.append((t, c, z))
        return original(self, t, c, z)

    monkeypatch.setattr(mrc_module.MrcDataset, "_read_plane", counting)
    return log


def test_identical_requests_hit_the_cache(stack, reads):
    path, data = stack
    svc = PlaneService(Settings())
    a = svc.plane(path, 1)
    b = svc.plane(path, 1)
    assert a.array is b.array and reads == [(1, 0, 0)]
    assert not a.array.flags.writeable
    np.testing.assert_array_equal(a.array, data[1])
    assert a.bin == 1 and a.region == (0, 0, 60, 40) and a.calibration.x.value == pytest.approx(2.0)
    # a binned view and the statistics reuse the cached raw plane
    binned = svc.plane(path, 1, bin=4)
    np.testing.assert_array_equal(binned.array, bin_mean(data[1], 4))
    assert binned.calibration.x.value == pytest.approx(8.0) and binned.calibration.x.source.value == "header"
    stats, _ = svc.stats(path, 1)
    assert stats.n_finite == 2400 and reads == [(1, 0, 0)]
    assert svc.stats(path, 1)[0] is stats
    svc.close()


def test_region_then_bin(stack):
    path, data = stack
    svc = PlaneService(Settings())
    r = svc.plane(path, 2, region=(5, 3, 31, 20), bin=3)
    np.testing.assert_array_equal(r.array, bin_mean(data[2][3:23, 5:36], 3))
    assert r.region == (5, 3, 31, 20) and r.array.flags.c_contiguous
    full = svc.plane(path, 2, region=(0, 0, 60, 40))
    assert full.array is svc.plane(path, 2).array  # the full region is the raw plane itself
    for bad in ((0, 0, 61, 1), (-1, 0, 1, 1), (0, 0, 0, 1), (59, 39, 2, 1)):
        with pytest.raises(InvalidRequestError):
            svc.plane(path, 2, region=bad)
    for bad_bin in (0, 65, 1.5, True):
        with pytest.raises(InvalidRequestError):
            svc.plane(path, 2, bin=bad_bin)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        svc.plane(path, 4)
    svc.close()


def test_a_changed_file_is_read_again(stack, reads):
    path, data = stack
    svc = PlaneService(Settings())
    old = svc.plane(path, 0)
    new_data = data + 1000
    write_stack(path, new_data)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    new = svc.plane(path, 0)
    assert new.file_id != old.file_id and len(reads) == 2
    np.testing.assert_array_equal(new.array, new_data[0])
    assert svc.stats(path, 0)[0].mean == pytest.approx(float(new_data[0].mean()), rel=1e-6)
    assert svc.cache_stats()["datasets"]["open"] == 1  # the stale dataset was closed
    path.unlink()
    with pytest.raises(NotFoundError):
        svc.plane(path, 0)
    svc.close()


def test_reader_options_are_part_of_the_cache_key(tmp_path: Path):
    f = write_eer(tmp_path / "m.eer", n_fractions=12)
    svc = PlaneService(Settings())
    by4 = svc.plane(f.path, 0, options={"group": 4})
    by6 = svc.plane(f.path, 0, options={"group": 6})
    np.testing.assert_array_equal(by4.array, f.truth(0, 4))
    np.testing.assert_array_equal(by6.array, f.truth(0, 6))
    assert svc.info(f.path, {"group": 4})[0].size_t == 3 and svc.info(f.path, {"group": 6})[0].size_t == 2
    assert svc.plane(f.path, 0, options={"group": 4}).array is by4.array
    svc.close()


def test_limits(stack):
    path, _ = stack
    svc = PlaneService(Settings(max_response_pixels=500))
    with pytest.raises(InvalidRequestError, match="larger bin or request a region"):
        svc.plane(path, 0)
    assert svc.plane(path, 0, bin=3).array.shape == (14, 20)
    svc.max_plane_bytes = 100
    with pytest.raises(InvalidRequestError, match="plane too large to read"):
        svc.plane(path, 1, bin=3)  # a plane that is not cached yet
    svc.close()


def test_cache_is_bounded_in_bytes(stack):
    path, data = stack
    svc = PlaneService(Settings(plane_cache_bytes=2 * data[0].nbytes))
    for t in range(4):
        svc.plane(path, t)
    s = svc.cache_stats()["planes"]
    assert s["bytes"] <= 2 * data[0].nbytes and s["items"] == 2 and s["evictions"] == 2
    svc.close()
