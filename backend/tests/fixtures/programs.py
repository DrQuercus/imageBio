"""Programs that stand for MotionCor and CTFFIND in the tests: same way of being called (an
option list for the first, questions answered on standard input for the second), same result
files, and numbers a test can work out from the images it wrote.

What they do is steered by the name of the input: a movie or an image whose name holds
``hang``, ``crash`` or ``garbage`` makes the program do just that.
"""

from __future__ import annotations

import sys
from pathlib import Path

from imagebio.core.config import ProcessingConfig, ToolStep

__all__ = ["CTFFIND_ANSWERS", "RECORD_VARIABLE", "ctf_step", "motion_step", "processing"]

#: Name of the environment variable that names a file where each run writes what it was given.
RECORD_VARIABLE = "FAKE_PROGRAMS_RECORD"

_COMMON = """\
import json, os, sys, time
import mrcfile, numpy as np

def record(kind, **what):
    path = os.environ.get("FAKE_PROGRAMS_RECORD")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"kind": kind, **what}) + "\\n")

def misbehave(name):
    if "hang" in name:
        time.sleep(600)
    if "crash" in name:
        print("Segmentation fault while reading", name)
        sys.exit(139)
"""

_MOTION = (
    _COMMON
    + """\
args = sys.argv[1:]
option = lambda name: args[args.index(name) + 1]
movie = next(option(o) for o in ("-InMrc", "-InTiff", "-InEer") if o in args)
record("motion", argv=args, cwd=os.getcwd())
misbehave(os.path.basename(movie))
with mrcfile.open(movie, permissive=True) as m:
    frames = np.asarray(m.data, dtype=np.float32)
with mrcfile.new(option("-OutMrc"), overwrite=True) as m:
    m.set_data(frames.sum(axis=0).astype(np.float32))
star = os.path.splitext(option("-OutMrc"))[0] + ".star"
if "garbage" in os.path.basename(movie):
    open(star, "w").write("the alignment did not converge\\n")
    sys.exit(0)
with open(star, "w") as f:
    f.write("data_global_shift\\n\\nloop_\\n_rlnMicrographFrameNumber #1\\n_rlnMicrographShiftX #2\\n_rlnMicrographShiftY #3\\n")
    for i in range(len(frames)):  # each frame 0.5 px right and 0.25 px up of the one before
        f.write(f"{i + 1} {0.5 * i:.6f} {-0.25 * i:.6f}\\n")
"""
)

_CTF = (
    _COMMON
    + """\
answers = sys.stdin.read().splitlines()
image, diagnostic, apix, kv, cs, amp = answers[:6]
record("ctf", answers=answers, argv=sys.argv[1:])
misbehave(os.path.basename(image))
with mrcfile.open(image, permissive=True) as m:
    mean = float(np.asarray(m.data, dtype=np.float64).mean())
out = os.path.splitext(diagnostic)[0] + ".txt"
if "garbage" in os.path.basename(image):
    open(out, "w").write("# no rings were found\\n")
    sys.exit(0)
with open(out, "w") as f:  # the defocus says which image was read: 10000 Å plus 1000 Å per count
    f.write("# Output from CTFFind version 4.1.14\\n")
    f.write("# Columns: #1 - micrograph number; #2 - defocus 1 [Angstroms]; #3 - defocus 2; #4 - azimuth of astigmatism; #5 - additional phase shift [radians]; #6 - cross correlation; #7 - spacing (in Angstroms) up to which CTF rings were fit successfully\\n")
    f.write(f"1.000000 {10000 + 1000 * mean + 400:.6f} {10000 + 1000 * mean:.6f} 30.000000 0.000000 0.150000 {4 * float(apix):.6f}\\n")
"""
)

#: The answers the CTF step types, in the order CTFFIND asks its questions.
CTFFIND_ANSWERS = (
    "{image}",
    "{out}/diag.mrc",
    "{apix}",
    "{kv}",
    "{cs}",
    "{amp}",
    "512",
    "30",
    "5",
    "5000",
    "50000",
    "100",
    "no",
    "no",
    "no",
    "no",
    "no",
)


def _write(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body)
    return str(path)


def motion_step(directory: Path, timeout_s: float = 30.0) -> ToolStep:
    script = _write(directory, "fake_motioncor.py", _MOTION)
    common = ("-OutMrc", "{out}/aligned.mrc", "-PixSize", "{apix}", "-Patch", "5", "5", "-OutStar", "1")
    return ToolStep(
        argv=(sys.executable, script, "-InMrc", "{movie}", *common),
        formats={"tiff": (sys.executable, script, "-InTiff", "{movie}", *common)},
        result="{out}/aligned.star",
        parser="motion-star",
        image="{out}/aligned.mrc",
        timeout_s=timeout_s,
    )


def ctf_step(directory: Path, timeout_s: float = 30.0) -> ToolStep:
    script = _write(directory, "fake_ctffind.py", _CTF)
    return ToolStep(
        argv=(sys.executable, script),
        stdin=CTFFIND_ANSWERS,
        result="{out}/diag.txt",
        parser="ctffind",
        timeout_s=timeout_s,
    )


def processing(
    directory: Path, *, motion: bool = True, ctf: bool = True, **settings: object
) -> ProcessingConfig:
    """Both programs, written into ``directory`` (which must exist)."""
    timeout = float(settings.pop("timeout_s", 30.0))  # type: ignore[arg-type]
    return ProcessingConfig(
        motion=motion_step(directory, timeout) if motion else None,
        ctf=ctf_step(directory, timeout) if ctf else None,
        **settings,  # type: ignore[arg-type]
    )
