"""A small tree of data roots with real files, and helpers for API tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mrcfile
import numpy as np
import tifffile
import zstandard
from fastapi.testclient import TestClient
from PIL import Image

from fixtures.eer import EerFile, write_eer
from imagebio.api.app import create_app
from imagebio.core.config import RootConfig, Settings
from imagebio.io.registry import Registry

ALICE = {"X-Forwarded-User": "alice", "X-Forwarded-Groups": "cryoem, staff"}
BOB = {"X-Forwarded-User": "bob", "X-Forwarded-Groups": "lm"}


@dataclass
class Tree:
    base: Path
    movie: np.ndarray  # cryo/movie.mrc      float32 (5, 48, 64), 1.25 Å/px
    hyper: np.ndarray  # light/hyper.tif     uint16 TZCYX (2, 3, 2, 20, 30)
    rgb: np.ndarray  # light/rgb.png       uint8 (16, 24, 3)
    gray16: np.ndarray  # public/gray16.tif   uint16 (33, 47)
    eer: EerFile  # cryo/movie.eer      12 fractions of 32 x 48

    def roots(self) -> tuple[RootConfig, ...]:
        return (
            RootConfig("cryo", self.base / "cryo", frozenset({"cryoem"})),
            RootConfig("light", self.base / "light", frozenset({"lm"})),
            RootConfig("public", self.base / "public"),
        )


def build_tree(base: Path) -> Tree:
    rng = np.random.default_rng(3)
    for d in ("cryo/session", "light", "public", "outside"):
        (base / d).mkdir(parents=True)
    movie = rng.normal(100, 10, (5, 48, 64)).astype(np.float32)
    with mrcfile.new(base / "cryo/movie.mrc") as m:
        m.set_data(movie)
        m.set_image_stack()  # frames of a movie, not a volume
        m.voxel_size = 1.25
    with mrcfile.new(base / "outside/secret.mrc") as m:
        m.set_data(np.full((4, 4), 7, np.float32))
    hyper = rng.integers(0, 65535, (2, 3, 2, 20, 30), dtype=np.uint16)  # T Z C Y X
    tifffile.imwrite(base / "light/hyper.tif", hyper, imagej=True, metadata={"axes": "TZCYX"})
    rgb = rng.integers(0, 255, (16, 24, 3), dtype=np.uint8)
    Image.fromarray(rgb).save(base / "light/rgb.png")
    gray16 = rng.integers(0, 65535, (33, 47), dtype=np.uint16)
    tifffile.imwrite(base / "public/gray16.tif", gray16)
    eer = write_eer(base / "cryo/movie.eer", n_fractions=12)
    (base / "cryo/session/notes.txt").write_text("not an image")
    (base / "cryo/corrupt.mrc").write_bytes(b"this is not an MRC file" * 10)
    return Tree(base, movie, hyper, rgb, gray16, eer)


def make_client(tree: Tree, *, registry: Registry | None = None, **settings: Any) -> TestClient:
    """A client on ``http://localhost`` (an allowed Host) that returns 500s instead of raising.
    Its state (settings page, Live index) is next to the tree, never the developer's own."""
    settings.setdefault("state_dir", tree.base.parent / "state")
    cfg = Settings(roots=tree.roots(), **settings)
    return TestClient(create_app(cfg, registry), base_url="http://localhost", raise_server_exceptions=False)


def decode(response: Any) -> np.ndarray:
    """The plane of a ``/api/datasets/plane`` response."""
    dtype = np.dtype(response.headers["x-dtype"]).newbyteorder("<")
    shape = tuple(int(n) for n in response.headers["x-shape"].split(","))
    raw = response.content
    expected = dtype.itemsize * shape[0] * shape[1]
    if len(raw) != expected and response.headers.get("content-encoding") == "zstd":
        # Starlette's TestClient hands zstd bodies over undecoded (a real HTTP client decodes them:
        # see test_server.py, which goes through uvicorn)
        raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=expected)
    return np.frombuffer(raw, dtype).reshape(shape)
