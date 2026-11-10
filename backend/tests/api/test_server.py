"""One test through a real server process (uvicorn), with a real HTTP client."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from fixtures.api import build_tree, decode


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path):
    tree = build_tree(tmp_path / "data")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "imagebio.api.cli", "serve", "--local", str(tree.base / "cryo"), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            proc.kill()
            pytest.fail(f"server did not start:\n{proc.stdout.read().decode() if proc.stdout else ''}")
        yield base, tree
    finally:
        proc.terminate()
        proc.wait(10)


def test_real_server_end_to_end(server):
    base, tree = server
    with httpx.Client(base_url=base, timeout=10) as client:
        assert client.get("/api/fs/roots").json() == {"roots": [{"name": "cryo", "available": True}]}
        for encoding in ("identity", "gzip", "zstd"):
            r = client.get(
                "/api/datasets/plane",
                params={"path": "cryo/movie.mrc", "t": 3},
                headers={"Accept-Encoding": encoding},
            )
            assert r.status_code == 200
            np.testing.assert_array_equal(decode(r), tree.movie[3])  # a real client decodes zstd itself
        r = client.get("/api/datasets/plane", params={"path": "cryo/movie.eer", "opt.group": 12})
        assert r.headers["content-encoding"] in ("zstd", "gzip")
        np.testing.assert_array_equal(decode(r), tree.eer.truth())
        assert "server" not in r.headers or "uvicorn" not in r.headers["server"].lower()
        # DNS rebinding: a foreign Host is refused by the running server too
        assert client.get("/api/me", headers={"Host": "evil.example"}).status_code == 403
        assert (
            client.get("/api/datasets/info", params={"path": "cryo/../outside/secret.mrc"}).status_code == 403
        )
