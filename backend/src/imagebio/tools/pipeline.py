"""What is run on one exposure: the motion correction on its movie, then the CTF estimation
on the sum that wrote, or on the image the microscope integrated.

Each run gets a directory of its own, removed when it ends: nothing of a run is kept but the
numbers read from its result files. A step that fails says so in a few words a user may read;
what the program printed goes to the log of the server.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from imagebio.core.config import ProcessingConfig, ToolStep
from imagebio.core.errors import CorruptFileError
from imagebio.tools.parsers import PARSERS
from imagebio.tools.runner import Outcome, run_program
from imagebio.tools.templates import MissingValueError, UnsafeValueError, fill, number

__all__ = ["Job", "process"]

log = logging.getLogger(__name__)

_UNKNOWN: Final[Mapping[str, str]] = {
    "apix": "the pixel size is not known",
    "kv": "the accelerating voltage is not known",
    "dose": "the dose per frame is not known",
    "frames": "the number of frames is not known",
    "movie": "the exposure has no movie",
    "image": "there is no micrograph to work on",
}


@dataclass(frozen=True, slots=True)
class Job:
    """One exposure, as the programs need it. Paths are those of the server."""

    movie: Path | None
    #: The image the microscope integrated, when there is one.
    image: Path | None
    apix: float | None
    kv: float
    cs_mm: float
    amplitude: float
    #: Dose per frame (e/Å²) and number of frames, when the acquisition says.
    dose: float | None = None
    frames: int | None = None


class _StepError(Exception):
    """A step gave no result; the message may be shown to a user."""


class _Stopped(Exception):
    """The server is stopping: the run does not count."""


def _filled(template: str, values: Mapping[str, str | None]) -> str:
    try:
        return fill(template, values)
    except MissingValueError as e:
        raise _StepError(_UNKNOWN.get(e.name, "a value the program needs is not known")) from None
    except UnsafeValueError:
        raise _StepError("a file name holds characters that cannot be given to a program") from None


def _inside(path: str, directory: Path) -> Path | None:
    """``path`` if it is a file of ``directory`` once links are followed, else None."""
    real = Path(path).resolve()
    return real if real.is_file() and directory.resolve() in real.parents else None


def _result(template: str, values: Mapping[str, str | None], directory: Path) -> Path:
    """The result file of a step. Its name may hold ``*`` (a program that names its log after
    its input): the first match, in the directory of the run only."""
    wanted = _filled(template, values)
    found: Path | None
    if "*" in Path(wanted).name:
        matches = sorted(Path(wanted).parent.glob(Path(wanted).name))
        found = next((m for m in (_inside(str(p), directory) for p in matches) if m is not None), None)
    else:
        found = _inside(wanted, directory)
    if found is None:
        raise _StepError("the program wrote no result file")
    return found


def _run(
    kind: str,
    step: ToolStep,
    argv: tuple[str, ...],
    values: Mapping[str, str | None],
    directory: Path,
    apix: float | None,
    stop: threading.Event | None,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Run one step in ``directory`` and read its result."""
    command = [_filled(a, values) for a in argv]
    typed = "".join(_filled(line, values) + "\n" for line in step.stdin)
    outcome: Outcome = run_program(
        command, cwd=directory, timeout_s=step.timeout_s, stdin=typed, stop=stop, env=env
    )
    if outcome.status == "stopped":
        raise _Stopped
    if not outcome.ok:
        log.warning("%s step: %s (status %s)\n%s", kind, outcome.status, outcome.code, outcome.tail)
        raise _StepError(outcome.public(step.timeout_s))
    try:
        return PARSERS[step.parser](_result(step.result, values, directory), apix)
    except (CorruptFileError, OSError) as e:
        log.warning("%s step: its result cannot be read (%s)\n%s", kind, e, outcome.tail)
        raise _StepError("the program gave no result that can be read") from None


def process(
    job: Job,
    config: ProcessingConfig,
    work_root: Path,
    *,
    stop: threading.Event | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Run the declared steps on ``job``. Returns ``{"motion": {...}, "ctf": {...}, "seconds":
    ...}``: a step that gave no result holds ``{"error": <why>}``, one that does not apply (no
    movie, no micrograph) is absent. None when the server stopped meanwhile."""
    started = time.monotonic()
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=work_root))  # 0700, a name nobody knows
    out: dict[str, Any] = {}
    values: dict[str, str | None] = {
        "movie": str(job.movie) if job.movie is not None else None,
        "image": str(job.image) if job.image is not None else None,
        "out": str(directory),
        "apix": number(job.apix),
        "kv": number(job.kv),
        "cs": number(job.cs_mm),
        "amp": number(job.amplitude),
        "dose": number(job.dose),
        "frames": str(job.frames) if job.frames is not None else None,
    }
    try:
        if config.motion is not None and job.movie is not None:
            step = config.motion
            argv = step.formats.get(job.movie.suffix.lower().lstrip("."), step.argv)
            try:
                out["motion"] = _run("motion", step, argv, values, directory, job.apix, stop, env)
                aligned = _inside(_filled(step.image, values), directory) if step.image else None
                if aligned is not None:
                    values["image"] = str(aligned)
            except _StepError as e:
                out["motion"] = {"error": str(e)}
        if config.ctf is not None and (values["image"] is not None or "motion" in out):
            try:
                out["ctf"] = _run("ctf", config.ctf, config.ctf.argv, values, directory, job.apix, stop, env)
            except _StepError as e:
                out["ctf"] = {"error": str(e)}
    except _Stopped:
        return None
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    out["seconds"] = round(time.monotonic() - started, 1)
    return out
