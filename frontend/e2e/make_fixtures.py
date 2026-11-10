"""Generate the data served to the end-to-end tests: python make_fixtures.py OUTPUT_DIR.

Every file has values that a test can predict from the pixel position.
"""

from __future__ import annotations

import sys
from pathlib import Path

import mrcfile
import numpy as np
import tifffile
from PIL import Image

# the synthetic EER writer of the backend tests (it does not use the reader under test)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "tests"))
from fixtures.eer import write_eer  # noqa: E402


def write_mrc(path: Path, data: np.ndarray, apix: float | None, stack: bool = False) -> None:
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(data)
        if stack:
            m.set_image_stack()
        if apix:
            m.voxel_size = apix


def main(out: Path) -> None:
    demo = out / "demo"
    (demo / "session").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    y, x = np.mgrid[0:48, 0:64]

    # value = 100 * y + x, 1.5 Å per pixel
    write_mrc(demo / "gradient.mrc", (100 * y + x).astype(np.float32), 1.5)

    # 30 frames: frame k is noise around 100 + 5 k, with a bright square moving to the right
    movie = rng.normal(100, 4, (30, 256, 256)).astype(np.float32)
    for k in range(30):
        movie[k] += 5 * k
        movie[k, 100:140, 8 * k : 8 * k + 16] += 60
    write_mrc(demo / "movie.mrc", movie, 0.83, stack=True)

    # NaN block, infinities and one hot pixel on a smooth ramp
    bad = (np.mgrid[0:128, 0:128][1] * 2.0 + 50).astype(np.float32)
    bad[10:30, 10:30] = np.nan
    bad[60, 60] = 1e9
    bad[61, 61] = np.inf
    bad[62, 62] = -np.inf
    write_mrc(demo / "nan_hot.mrc", bad, 1.0)

    # large image: value = (x + 2 y) mod 65536, to check the levels of detail
    yy, xx = np.mgrid[0:4096, 0:4096]
    write_mrc(demo / "big.mrc", ((xx + 2 * yy) % 65536).astype(np.uint16), 1.06)

    # RGB: red left half, green right half
    rgb = np.zeros((64, 64, 3), np.uint8)
    rgb[:, :32, 0] = 255
    rgb[:, 32:, 1] = 200
    Image.fromarray(rgb).save(demo / "colours.png")

    # no pixel size in the file
    tifffile.imwrite(demo / "noapix.tif", (np.arange(32 * 32) % 4096).astype(np.uint16).reshape(32, 32))

    # hyperstack T=2, Z=3, C=2: value = 1000 t + 100 z + 10 c
    hyper = np.zeros((2, 3, 2, 16, 16), np.uint16)
    for t in range(2):
        for z in range(3):
            for c in range(2):
                hyper[t, z, c] = 1000 * t + 100 * z + 10 * c
    tifffile.imwrite(demo / "hyper.tif", hyper, imagej=True, metadata={"axes": "TZCYX"})

    # Cryo mode. A lattice of period 8 px = 10 Å at 1.25 Å per pixel, in noise, around 100:
    # its power spectrum has two peaks 64 px from the centre of a 512 px spectrum.
    lx = np.mgrid[0:1024, 0:1024][1]
    lattice = 100 + np.cos(2 * np.pi * lx / 8.0) + rng.normal(0, 0.3, (1024, 1024))
    write_mrc(demo / "lattice.mrc", lattice.astype(np.float32), 1.25)

    # 10 frames: frame k (from 0) is 10 (k + 1) + x, so a mean of frames is known exactly
    sx = np.mgrid[0:32, 0:32][1]
    steps = np.stack([10 * (k + 1) + sx for k in range(10)]).astype(np.float32)
    write_mrc(demo / "steps.mrc", steps, 1.0, stack=True)

    # EER movie: 24 fractions, one electron on pixel (x=20, y=10) in every fraction
    write_eer(demo / "falcon.eer", n_fractions=24, shape=(64, 64), events=150, hot=(10 * 64 + 20,), seed=7)

    # a hostile file name must be shown as text
    write_mrc(demo / "<img src=x onerror=alert(1)>.mrc", np.ones((8, 8), np.float32), 1.0)
    write_mrc(demo / "session" / "inside.mrc", np.full((8, 8), 7, np.float32), 2.0)
    (demo / "notes.txt").write_text("not an image")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
