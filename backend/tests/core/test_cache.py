from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from imagebio.core.cache import ByteLRU, DatasetPool, options_key
from imagebio.core.errors import CorruptFileError, NotFoundError
from imagebio.core.model import Dataset, DatasetInfo, Plane


def arr(nbytes: int, fill: int = 0) -> np.ndarray:
    return np.full(nbytes, fill, np.uint8)


# ── ByteLRU ──────────────────────────────────────────────────────────────────


def test_lru_order_and_byte_bound():
    c = ByteLRU(100)
    c.put("a", arr(40, 1))
    c.put("b", arr(40, 2))
    assert c.get("a") is not None  # "a" is now the most recent
    c.put("c", arr(40, 3))  # evicts "b"
    assert c.get("b") is None and c.get("a") is not None and c.get("c") is not None
    s = c.stats()
    assert s["bytes"] == 80 and s["items"] == 2 and s["evictions"] == 1 and s["max_bytes"] == 100
    c.put("a", arr(10))  # replacing an entry updates the accounting
    assert c.stats()["bytes"] == 50


def test_values_are_read_only_and_oversized_ones_are_not_stored():
    c = ByteLRU(100)
    value = arr(10)
    c.put("k", value)
    stored = c.get("k")
    assert stored is not None and not stored.flags.writeable
    with pytest.raises(ValueError):
        stored[0] = 1
    c.put("big", arr(101))
    assert c.get("big") is None and c.stats()["bytes"] == 10
    out = c.get_or_load("big", lambda: arr(500, 7))  # still returned to the caller
    assert out.nbytes == 500 and c.get("big") is None


def test_zero_capacity_still_loads():
    c = ByteLRU(0)
    assert c.get_or_load("k", lambda: arr(8, 3))[0] == 3
    assert c.stats()["items"] == 0
    with pytest.raises(ValueError):
        ByteLRU(-1)


def test_small_view_of_a_big_buffer_is_copied_so_accounting_is_honest():
    big = np.zeros((1000, 1000), np.uint8)
    c = ByteLRU(10_000)
    c.put("view", big[:10, :10])
    stored = c.get("view")
    assert stored is not None and stored.base is None and stored.nbytes == 100
    raw = bytes(64)
    c.put("exact", np.frombuffer(raw, np.uint8))  # a view of exactly its buffer is kept as is
    kept = c.get("exact")
    assert kept is not None and kept.base is not None


def test_discard_and_clear():
    c = ByteLRU(1000)
    for k in range(5):
        c.put(("file", k), arr(10))
    c.put(("other", 0), arr(10))
    assert c.discard(lambda key: key[0] == "file") == 5  # type: ignore[index]
    assert c.stats()["bytes"] == 10
    c.clear()
    assert c.stats()["bytes"] == 0 and c.stats()["items"] == 0


def test_single_flight_under_contention():
    c = ByteLRU(10_000)
    calls = 0
    gate = threading.Barrier(16)
    release = threading.Event()

    def loader() -> np.ndarray:
        nonlocal calls
        calls += 1
        release.wait(5)
        return arr(100, 9)

    results: list[np.ndarray] = []

    def work() -> None:
        gate.wait(5)
        results.append(c.get_or_load("k", loader))

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(5)
    assert calls == 1 and len(results) == 16 and all(r is results[0] for r in results)
    assert c.stats()["misses"] == 1


def test_loader_exception_reaches_every_waiter_and_leaves_nothing_behind():
    c = ByteLRU(10_000)
    started, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def failing() -> np.ndarray:
        started.set()
        release.wait(5)
        raise CorruptFileError("x.mrc: broken")

    def work() -> None:
        try:
            c.get_or_load("k", failing)
        except CorruptFileError as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(6)]
    threads[0].start()
    started.wait(5)
    for t in threads[1:]:
        t.start()
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(5)
    assert len(errors) == 6
    assert c.get_or_load("k", lambda: arr(4, 1))[0] == 1  # the key is not poisoned


def test_different_keys_load_concurrently():
    c = ByteLRU(10_000)
    both_inside = threading.Barrier(2)

    def loader() -> np.ndarray:
        both_inside.wait(5)  # would time out if loaders were serialised
        return arr(10)

    threads = [threading.Thread(target=c.get_or_load, args=(k, loader)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert c.stats()["items"] == 2


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    ops=st.lists(
        st.tuples(st.sampled_from(["put", "get", "load", "discard"]), st.integers(0, 9), st.integers(0, 60)),
        max_size=60,
    )
)
def test_byte_bound_holds_after_every_operation(ops: list[tuple[str, int, int]]):
    c = ByteLRU(100)
    for op, key, size in ops:
        if op == "put":
            c.put(key, arr(size))
        elif op == "get":
            c.get(key)
        elif op == "load":
            assert c.get_or_load(key, lambda size=size: arr(size)).dtype == np.uint8
        else:
            c.discard(lambda k, key=key: k == key)
        s = c.stats()
        assert 0 <= s["bytes"] <= 100
        assert s["bytes"] == sum(v.nbytes for v in c._data.values())


def test_concurrent_stress_keeps_invariants():
    c = ByteLRU(4096)
    errors: list[BaseException] = []

    def work(seed: int) -> None:
        rng = np.random.default_rng(seed)
        try:
            for _ in range(400):
                key = int(rng.integers(0, 40))
                out = c.get_or_load(key, lambda key=key: arr(64 + key, key))
                assert out[0] == key and out.nbytes == 64 + key
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    assert c.stats()["bytes"] <= 4096


def test_options_key_is_canonical():
    assert options_key(None) == options_key({}) == ""
    assert options_key({"group": 4, "superres": 1}) == options_key({"superres": 1, "group": 4})
    assert options_key({"group": 4}) != options_key({"group": 5})


# ── DatasetPool ──────────────────────────────────────────────────────────────


class FakeDataset(Dataset):
    def __init__(self, path: Path, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(
            path, DatasetInfo(format="fake", reader="fake", size_x=1, size_y=1, dtype=np.dtype("u1"))
        )
        self.options = options
        self.closed = 0

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        return np.zeros((1, 1), "u1")

    def close(self) -> None:
        self.closed += 1


class Opener:
    def __init__(self) -> None:
        self.opened: list[FakeDataset] = []
        self.delay = 0.0
        self.fail = False

    def __call__(self, path: Path, options: Mapping[str, Any] | None) -> Dataset:
        time.sleep(self.delay)
        if self.fail:
            raise CorruptFileError(f"{path.name}: broken")
        ds = FakeDataset(path, options)
        self.opened.append(ds)
        return ds


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / f"f{i}.bin" for i in range(5)]
    for p in paths:
        p.write_bytes(b"data")
    return paths


def test_pool_reuses_and_keys_on_options(files: list[Path]):
    opener = Opener()
    pool = DatasetPool(4, opener)
    with pool.lease(files[0]) as a:
        pass
    with pool.lease(files[0]) as b:
        assert a is b
    with pool.lease(files[0], {"group": 2}) as c:
        assert c is not a
    assert len(opener.opened) == 2 and pool.stats()["open"] == 2
    assert all(ds.closed == 0 for ds in opener.opened)


def test_pool_reopens_a_changed_file_and_closes_the_stale_one(files: list[Path]):
    opener = Opener()
    pool = DatasetPool(4, opener)
    with pool.lease(files[0]) as old:
        pass
    files[0].write_bytes(b"changed content")
    with pool.lease(files[0]) as new:
        assert new is not old
    assert old.closed == 1 and new.closed == 0 and pool.stats()["open"] == 1  # type: ignore[attr-defined]
    st_ = files[1].stat()
    with pool.lease(files[1]) as first:
        pass
    os.utime(files[1], ns=(st_.st_atime_ns, st_.st_mtime_ns + 1_000_000_000))  # same size, new mtime
    with pool.lease(files[1]) as second:
        assert second is not first


def test_pool_evicts_least_recently_used_idle_datasets(files: list[Path]):
    opener = Opener()
    pool = DatasetPool(2, opener)
    for p in files[:2]:
        with pool.lease(p):
            pass
    with pool.lease(files[0]):  # refresh f0: f1 is now the oldest
        pass
    with pool.lease(files[2]):
        pass
    by_name = {ds.path.name: ds for ds in opener.opened}
    assert by_name["f1.bin"].closed == 1 and by_name["f0.bin"].closed == 0 and by_name["f2.bin"].closed == 0
    assert pool.stats()["open"] == 2 and pool.stats()["evictions"] == 1


def test_leased_datasets_are_never_closed_under_the_user(files: list[Path]):
    opener = Opener()
    pool = DatasetPool(1, opener)
    with pool.lease(files[0]) as a:
        with pool.lease(files[1]) as b:  # nested lease of another file: over the limit, nothing idle
            assert a.closed == 0 and b.closed == 0  # type: ignore[attr-defined]
            assert pool.stats()["open"] == 2
        assert a.closed == 0  # type: ignore[attr-defined]
    assert pool.stats()["open"] == 1
    assert sorted(ds.closed for ds in opener.opened) == [0, 1]

    # stale while leased: closed at release, not before
    with pool.lease(files[2]) as held:
        files[2].write_bytes(b"new content!")
        with pool.lease(files[2]) as fresh:
            assert fresh is not held and held.closed == 0  # type: ignore[attr-defined]
    assert held.closed == 1  # type: ignore[attr-defined]


def test_close_all(files: list[Path]):
    opener = Opener()
    pool = DatasetPool(4, opener)
    with pool.lease(files[0]) as idle:
        pass
    with pool.lease(files[1]) as busy:
        pool.close_all()
        assert idle.closed == 1 and busy.closed == 0  # type: ignore[attr-defined]
    assert busy.closed == 1 and pool.stats()["open"] == 0  # type: ignore[attr-defined]
    with pool.lease(files[0]) as again:  # the pool still works
        assert again is not idle


def test_single_flight_open(files: list[Path]):
    opener = Opener()
    opener.delay = 0.1
    pool = DatasetPool(4, opener)
    seen: list[Dataset] = []
    gate = threading.Barrier(8)

    def work() -> None:
        gate.wait(5)
        with pool.lease(files[0]) as ds:
            seen.append(ds)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(opener.opened) == 1 and len(seen) == 8 and all(ds is seen[0] for ds in seen)


def test_open_errors_propagate_and_leave_no_entry(files: list[Path], tmp_path: Path):
    opener = Opener()
    opener.fail = True
    opener.delay = 0.05
    pool = DatasetPool(4, opener)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            with pool.lease(files[0]):
                pass
        except CorruptFileError as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(errors) == 4 and pool.stats()["open"] == 0 and pool.stats()["leased"] == 0
    opener.fail = False
    with pool.lease(files[0]) as ds:
        assert ds.closed == 0  # type: ignore[attr-defined]
    with pytest.raises(NotFoundError), pool.lease(tmp_path / "missing.bin"):
        pass
    with pytest.raises(ValueError):
        DatasetPool(0)


def test_pool_stress(files: list[Path]):
    opener = Opener()
    pool = DatasetPool(2, opener)
    errors: list[BaseException] = []

    def work(seed: int) -> None:
        rng = np.random.default_rng(seed)
        try:
            for _ in range(200):
                with pool.lease(files[int(rng.integers(0, 5))]) as ds:
                    assert ds.closed == 0  # type: ignore[attr-defined]
                    ds.read_plane()
                    assert ds.closed == 0  # type: ignore[attr-defined]
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    pool.close_all()
    assert all(ds.closed == 1 for ds in opener.opened)  # every dataset closed exactly once
