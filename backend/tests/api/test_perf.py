"""Performance budgets of milestone M1 (run with ``pytest -m perf``)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import mrcfile
import numpy as np
import psutil
import pytest

from fixtures.api import Tree, build_tree, decode, make_client

pytestmark = pytest.mark.perf


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Tree:
    return build_tree(tmp_path_factory.mktemp("perf"))


def test_warm_4k_plane_at_bin_4_is_served_under_150_ms(tree: Tree):
    frame = np.random.default_rng(0).normal(100, 10, (4096, 4096)).astype(np.float32)
    with mrcfile.new(tree.base / "public/4k.mrc", overwrite=True) as m:
        m.set_data(frame)
    with make_client(tree) as client:
        params = {"path": "public/4k.mrc", "bin": 4}
        cold0 = time.perf_counter()
        first = client.get("/api/datasets/plane", params=params)
        cold = time.perf_counter() - cold0
        assert first.status_code == 200 and decode(first).shape == (1024, 1024)
        timings = []
        for _ in range(5):
            t0 = time.perf_counter()
            r = client.get("/api/datasets/plane", params=params)
            timings.append(time.perf_counter() - t0)
            assert r.status_code == 200
        print(f"\n4K bin 4: cold {cold * 1000:.0f} ms, warm {min(timings) * 1000:.0f} ms")
        assert min(timings) < 0.150
        t0 = time.perf_counter()
        assert client.get("/api/datasets/stats", params={"path": "public/4k.mrc"}).status_code == 200
        print(f"4K stats: {(time.perf_counter() - t0) * 1000:.0f} ms")


def test_memory_stays_bounded_while_scanning_a_stack_larger_than_the_cache(tree: Tree):
    frames, side = 60, 1024  # 240 MiB of float32 planes
    cache = 32 * 2**20
    path = tree.base / "public/big.mrc"
    with mrcfile.new_mmap(path, shape=(frames, side, side), mrc_mode=2, overwrite=True) as m:
        for k in range(frames):
            m.data[k] = k
        m.set_image_stack()
    process = psutil.Process(os.getpid())
    with make_client(tree, plane_cache_bytes=cache) as client:
        client.get("/api/datasets/plane", params={"path": "public/big.mrc", "t": 0})
        before = process.memory_info().rss
        peak = before
        for t in range(frames):
            r = client.get(
                "/api/datasets/plane",
                params={"path": "public/big.mrc", "t": t},
                headers={"Accept-Encoding": "identity"},
            )
            assert r.status_code == 200 and float(decode(r)[0, 0]) == t
            peak = max(peak, process.memory_info().rss)
        growth = peak - before
        stats = client.app.state.service.cache_stats()["planes"]  # type: ignore[attr-defined]
        print(
            f"\nstack of {frames * side * side * 4 >> 20} MiB, cache {cache >> 20} MiB: RSS grew by {growth >> 20} MiB"
        )
        assert stats["bytes"] <= cache
        assert growth < cache + 48 * 2**20  # the cache plus a few transient planes, far below the stack size
    Path(path).unlink()
