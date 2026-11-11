"""One exposure through the declared programs, played by tests/fixtures/programs.py."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import mrcfile
import numpy as np
import pytest

from fixtures.programs import RECORD_VARIABLE, processing
from imagebio.core.config import ProcessingConfig, ToolStep
from imagebio.tools.pipeline import Job, ProgramUnavailableError, process

pytestmark = pytest.mark.skipif(os.name != "posix", reason="programs are run on POSIX systems only")


def write_mrc(path: Path, data: np.ndarray) -> Path:
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(data.astype(np.float32))
    return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A movie of 6 frames of 2 counts (sum: 12) and an integrated image of 5 counts."""
    (tmp_path / "data").mkdir()
    (tmp_path / "programs").mkdir()
    write_mrc(tmp_path / "data/movie.mrc", np.full((6, 32, 32), 2.0))
    write_mrc(tmp_path / "data/image.mrc", np.full((32, 32), 5.0))
    monkeypatch.setenv(RECORD_VARIABLE, str(tmp_path / "record.jsonl"))
    return tmp_path


def job(world: Path, movie: str | None = "movie.mrc", image: str | None = "image.mrc", **more: object) -> Job:
    settings: dict = {"apix": 0.8, "kv": 300.0, "cs_mm": 2.7, "amplitude": 0.07, **more}
    return Job(
        movie=world / "data" / movie if movie else None,
        image=world / "data" / image if image else None,
        **settings,
    )


def record(world: Path) -> list[dict]:
    path = world / "record.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_movie_is_aligned_then_the_ctf_is_read_on_the_sum(world: Path) -> None:
    config = processing(world / "programs")
    result = process(job(world), config, world / "work")
    assert result is not None
    # the shifts the motion program wrote: five steps of (0.5, -0.25) px, at 0.8 Å per pixel
    step = math.hypot(0.5, 0.25) * 0.8
    assert result["motion"] == {
        "total": pytest.approx(5 * step, abs=0.006),
        "early": pytest.approx(5 * step, abs=0.006),
        "max_step": pytest.approx(step, abs=0.006),
        "frames": 6,
        "unit": "A",
    }
    # the CTF program read the aligned sum (12 counts), not the image of the microscope (5)
    assert result["ctf"]["defocus_v"] == pytest.approx(2.2) and result["ctf"]["defocus_u"] == pytest.approx(
        2.24
    )
    assert result["ctf"]["astigmatism"] == pytest.approx(0.04) and result["ctf"]["fit"] == pytest.approx(3.2)
    assert result["seconds"] >= 0

    motion, ctf = record(world)
    run_dir = Path(motion["cwd"])
    assert run_dir.parent == world / "work" and not run_dir.exists()  # a directory per run, removed
    assert list((world / "work").iterdir()) == []
    assert motion["argv"] == [
        "-InMrc", str(world / "data/movie.mrc"), "-OutMrc", f"{run_dir}/aligned.mrc",
        "-PixSize", "0.8", "-Patch", "5", "5", "-OutStar", "1",
    ]  # fmt: skip
    assert ctf["answers"][:6] == [
        f"{run_dir}/aligned.mrc",
        f"{run_dir}/diag.mrc",
        "0.8",
        "300",
        "2.7",
        "0.07",
    ]
    assert ctf["answers"][6:] == ["512", "30", "5", "5000", "50000", "100", "no", "no", "no", "no", "no"]


def test_each_step_applies_to_what_the_exposure_has(world: Path) -> None:
    config = processing(world / "programs")
    # no movie: nothing to align, the CTF is read on the image of the microscope
    alone = process(job(world, movie=None), config, world / "work")
    assert alone is not None and "motion" not in alone and alone["ctf"]["defocus_v"] == pytest.approx(1.5)
    # a movie and nothing else: the sum is all there is
    only_movie = process(job(world, image=None), config, world / "work")
    assert only_movie is not None and only_movie["ctf"]["defocus_v"] == pytest.approx(2.2)
    # only one of the two programs declared
    assert set(process(job(world), processing(world / "programs", ctf=False), world / "work") or {}) == {
        "motion",
        "seconds",
    }
    no_motion = process(job(world), processing(world / "programs", motion=False), world / "work")
    assert (
        no_motion is not None
        and "motion" not in no_motion
        and no_motion["ctf"]["defocus_v"] == pytest.approx(1.5)
    )
    # the argument list declared for a format is the one used for it
    (world / "data/movie.tiff").write_bytes((world / "data/movie.mrc").read_bytes())
    process(job(world, movie="movie.tiff"), config, world / "work")
    assert record(world)[-2]["argv"][0] == "-InTiff"


def test_a_step_that_fails_says_so_and_the_other_goes_on(world: Path) -> None:
    config = processing(world / "programs", timeout_s=2.0)
    for name in ("crash_movie.mrc", "hang_movie.mrc", "garbage_movie.mrc"):
        (world / "data" / name).write_bytes((world / "data/movie.mrc").read_bytes())
    crashed = process(job(world, movie="crash_movie.mrc"), config, world / "work")
    assert crashed is not None and crashed["motion"] == {"error": "the program ended with status 139"}
    assert crashed["ctf"]["defocus_v"] == pytest.approx(1.5)  # on the image of the microscope, then

    started = time.monotonic()
    hung = process(job(world, movie="hang_movie.mrc", image=None), config, world / "work")
    assert hung is not None and time.monotonic() - started < 15
    assert hung["motion"] == {"error": "the program was stopped after 2 s"}
    assert hung["ctf"] == {"error": "there is no micrograph to work on"}

    garbage = process(job(world, movie="garbage_movie.mrc"), config, world / "work")
    assert garbage is not None and garbage["motion"] == {
        "error": "the program gave no result that can be read"
    }
    # nothing of a failed run stays, and no reason given to a user names a path or quotes the program
    assert list((world / "work").iterdir()) == []
    for result in (crashed, hung, garbage):
        assert str(world) not in json.dumps(result) and "Segmentation" not in json.dumps(result)

    write_mrc(world / "data/garbage_image.mrc", np.full((32, 32), 5.0))
    no_fit = process(job(world, movie=None, image="garbage_image.mrc"), config, world / "work")
    assert no_fit is not None and no_fit["ctf"] == {"error": "the program gave no result that can be read"}


def test_what_the_programs_need_and_is_not_known_stops_the_step_before_it_runs(world: Path) -> None:
    config = processing(world / "programs")
    unknown = process(job(world, apix=None), config, world / "work")
    assert unknown is not None
    assert unknown["motion"] == {"error": "the pixel size is not known"} == unknown["ctf"]
    assert record(world) == []  # neither program was started
    # a line break in a file name would be one more answer typed to the CTF program
    hostile = world / "data" / "image\nno\n.mrc"
    hostile.write_bytes((world / "data/image.mrc").read_bytes())
    refused = process(job(world, movie=None, image=hostile.name), config, world / "work")
    assert refused is not None
    assert refused["ctf"] == {"error": "a file name holds characters that cannot be given to a program"}
    assert record(world) == []


def test_a_result_outside_the_directory_of_the_run_is_not_read(world: Path, tmp_path: Path) -> None:
    """A program that leaves a link as its result: what it points to is not the server's to read."""
    secret = tmp_path / "secret.txt"
    secret.write_text("1.0 18000.0 18000.0 0.0 0.0 0.1 3.0\n")
    script = world / "programs" / "linking.py"
    script.write_text(
        "import os, sys\nanswers = sys.stdin.read().splitlines()\n"
        f"os.symlink({str(secret)!r}, os.path.splitext(answers[1])[0] + '.txt')\n"
    )
    config = processing(world / "programs", motion=False)
    step = config.ctf
    assert step is not None
    linking = type(config)(
        ctf=type(step)(
            argv=(step.argv[0], str(script)), stdin=step.stdin, result=step.result, parser="ctffind"
        )
    )
    result = process(job(world, movie=None), linking, world / "work")
    assert result is not None and result["ctf"] == {"error": "the program wrote no result file"}


def test_a_program_that_is_not_installed_is_not_held_against_the_exposure(world: Path) -> None:
    """Regression. A mistyped path in the configuration gave every exposure the final result
    "the program could not be started": once the path was put right, nothing was run again."""
    absent = ProcessingConfig(
        ctf=ToolStep(
            argv=(str(world / "programs/not_installed"),),
            stdin=("{image}",),
            result="{out}/d.txt",
            parser="ctffind",
        )
    )
    with pytest.raises(ProgramUnavailableError):
        process(job(world, movie=None), absent, world / "work")
    assert list((world / "work").iterdir()) == []  # the directory of the run is removed all the same
