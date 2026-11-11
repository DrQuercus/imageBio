"""Generate the data served to the end-to-end tests: python make_fixtures.py OUTPUT_DIR.

Every file has values that a test can predict from the pixel position. The directory also gets
the configuration of the test server (``imagebio.toml``): the same data root as the standalone
mode would give, and a live monitor that scans every second so that tests need not wait.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import mrcfile
import numpy as np
import tifffile
from PIL import Image

# the synthetic EER writer of the backend tests (it does not use the reader under test)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "tests"))
from fixtures.eer import write_eer  # noqa: E402

from imagebio.live.simulator import MicroscopeSim, SimSettings  # noqa: E402

#: Exposures of the finished session the Live tests open.
LIVE_EXPOSURES = 120


def write_mrc(path: Path, data: np.ndarray, apix: float | None, stack: bool = False) -> None:
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(data)
        if stack:
            m.set_image_stack()
        if apix:
            m.voxel_size = apix


def write_session(directory: Path) -> None:
    """An EPU-like session that ended two days ago: one exposure every six seconds."""
    shutil.rmtree(directory, ignore_errors=True)
    start = time.time() - 2 * 86400
    sim = MicroscopeSim(directory, SimSettings(rate_per_hour=600, pause_probability=0.0, seed=11))
    second = 0
    while len(sim.exposures) < LIVE_EXPOSURES:
        sim.step(start + second)
        second += 1
    sim.stop()
    while sim.busy:
        sim.step(start + second)
        second += 1


def main(out: Path) -> None:
    out = out.resolve()
    demo = out / "demo"
    (demo / "session").mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out / "state", ignore_errors=True)  # no watch left over from a previous run
    (out / "imagebio.toml").write_text(
        f'[live]\nstate_dir = "{out / "state"}"\ninterval = 1\nmin_stable = 2\n\n'
        # alerts go to a receiver that the Live tests start on this port
        '[[alerts.channels]]\nname = "essai"\nkind = "webhook"\nurl = "http://127.0.0.1:8766/hook"\n\n'
        f'[[roots]]\nname = "demo"\npath = "{demo}"\n'
    )
    write_session(demo / "epu_session")
    rng = np.random.default_rng(42)

    # outside the data roots: an image of crystalline ice, which a Live test drops into the session.
    # Plane waves of period 3.67 Å in many directions, at the pixel size of the session's images.
    (out / "spare").mkdir(exist_ok=True)
    yy, xx = np.mgrid[0:512, 0:512].astype(np.float64)
    icy = rng.normal(30, 3, (512, 512))
    for angle in rng.uniform(0, np.pi, 24):
        k = 2 * np.pi * 0.83 / 3.67
        icy += 0.6 * np.cos(k * (xx * np.cos(angle) + yy * np.sin(angle)) + rng.uniform(0, 2 * np.pi))
    write_mrc(out / "spare" / "icy.mrc", icy.astype(np.float32), 0.83)

    # two micrographs with particles at known places, a box file for the first one and a
    # RELION file for both
    (demo / "picks").mkdir(exist_ok=True)
    for name in ("mic_001", "mic_002"):
        write_mrc(demo / "picks" / f"{name}.mrc", rng.normal(50, 5, (256, 256)).astype(np.float32), 1.2)
    (demo / "picks" / "mic_001.box").write_text("44 44 40 40\n172 44 40 40\n108 172 40 40\n")  # centres 64,64 192,64 128,192
    (demo / "picks" / "particles.star").write_text(
        "data_optics\n\nloop_\n_rlnOpticsGroup #1\n_rlnImagePixelSize #2\n1 1.2\n\n"
        "data_particles\n\nloop_\n_rlnCoordinateX #1\n_rlnCoordinateY #2\n_rlnMicrographName #3\n"
        "_rlnAutopickFigureOfMerit #4\n"
        "60.0 200.0 MotionCorr/job002/mic_001.mrc 0.9\n200.0 200.0 MotionCorr/job002/mic_001.mrc 0.2\n"
        "128.0 128.0 MotionCorr/job002/mic_002.mrc 0.5\n"
    )

    # class averages as RELION writes them: plane k holds the value 10 k, with a disc so that it
    # has something to show; the model file gives each class its share of the particles
    (demo / "Class2D").mkdir(exist_ok=True)
    cy, cx = np.mgrid[0:64, 0:64]
    disc = ((cx - 32) ** 2 + (cy - 32) ** 2 < 20**2).astype(np.float32)
    averages = np.stack([np.full((64, 64), 10.0 * k, np.float32) + (k + 1) * disc * (cx > 20 + 3 * k) for k in range(8)])
    write_mrc(demo / "Class2D" / "run_it010_classes.mrcs", averages, 2.4, stack=True)
    shares = [0.05, 0.10, 0.00, 0.15, 0.02, 0.40, 0.20, 0.08]
    resolutions = ["9.1", "8.0", "inf", "7.2", "15.0", "6.4", "6.9", "11.3"]
    rows = "".join(
        f"{k + 1:06d}@Class2D/job008/run_it010_classes.mrcs {shares[k]:.6f} {resolutions[k]}\n" for k in range(8)
    )
    (demo / "Class2D" / "run_it010_model.star").write_text(
        "data_model_general\n\n_rlnNrClasses 8\n\ndata_model_classes\n\nloop_\n_rlnReferenceImage #1\n"
        "_rlnClassDistribution #2\n_rlnEstimatedResolution #3\n" + rows
    )

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

    # A detector with a fixed pattern (per-pixel sensitivity and shading) and a hot pixel, its
    # gain reference stored as it should be and stored upside down.
    falcon = demo / "falcon"
    falcon.mkdir(exist_ok=True)
    gy, gx = np.mgrid[0:128, 0:128]
    sensitivity = np.clip(rng.normal(1.0, 0.2, (128, 128)), 0.4, 1.6) * (1.0 + 0.3 * gx / 128 + 0.15 * (gy / 128) ** 2)
    write_eer(falcon / "pattern.eer", n_fractions=24, shape=(128, 128), events=4000, hot=(40 * 128 + 70,),
              sensitivity=sensitivity, seed=5)
    gain = (sensitivity.mean() / sensitivity).astype(np.float32)
    gain[40, 70] = 0
    tifffile.imwrite(falcon / "good.gain", gain)
    tifffile.imwrite(falcon / "flipped.gain", gain[::-1, :])
    write_mrc(falcon / "aligned.mrc", np.ones((16, 16), np.float32), 1.0)

    # a hostile file name must be shown as text
    write_mrc(demo / "<img src=x onerror=alert(1)>.mrc", np.ones((8, 8), np.float32), 1.0)
    write_mrc(demo / "session" / "inside.mrc", np.full((8, 8), 7, np.float32), 2.0)
    (demo / "notes.txt").write_text("not an image")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
