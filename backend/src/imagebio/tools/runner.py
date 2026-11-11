"""Run one declared program: an argument list, a time limit, and nothing left behind.

- No shell: the arguments go to the program as they are (``execve``).
- Its own session: when the time is up, or the server stops, the whole process group is
  killed, the children the program started included.
- What it prints goes to a file of its working directory; its answers on standard input come
  from a file too, so that neither side can block the other on a full pipe.
- The secrets of the server (``IMAGEBIO_*``) are not in its environment.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["LOG_NAME", "Outcome", "clean_environment", "run_program"]

LOG_NAME: Final = "program.log"
_STDIN_NAME: Final = "program.stdin"
_POLL_S: Final = 0.05
_TAIL_BYTES: Final = 2000


@dataclass(frozen=True, slots=True)
class Outcome:
    #: ok | failed (exit status not 0) | timeout | stopped (the server stops) | unavailable
    status: str
    code: int | None
    seconds: float
    #: The end of what the program printed. For the log of the server only: it may hold paths.
    tail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def public(self, timeout_s: float) -> str:
        """What a user may be told: neither a path nor a word of the program."""
        if self.status == "failed":
            return f"the program ended with status {self.code}"
        if self.status == "timeout":
            return f"the program was stopped after {timeout_s:g} s"
        if self.status == "stopped":
            return "the server was stopping"
        if self.status == "unavailable":
            return "the program could not be started"
        return "done"


def clean_environment(secret_names: Collection[str] = ()) -> dict[str, str]:
    """The environment of the server without its own secrets (``IMAGEBIO_*`` and the named
    variables): what a library path or a GPU setting needs is kept."""
    return {k: v for k, v in os.environ.items() if not k.startswith("IMAGEBIO_") and k not in secret_names}


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def run_program(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdin: str = "",
    stop: threading.Event | None = None,
    env: Mapping[str, str] | None = None,
) -> Outcome:
    """Run ``argv`` in ``cwd`` (an existing directory it may write to) for ``timeout_s`` at most,
    typing ``stdin`` to it. Never raises for what the program does."""
    started = time.monotonic()
    if os.name != "posix" or not argv:
        return Outcome("unavailable", None, 0.0)
    (cwd / _STDIN_NAME).write_text(stdin, encoding="utf-8")
    try:
        with open(cwd / _STDIN_NAME, "rb") as typed, open(cwd / LOG_NAME, "wb") as printed:
            process = subprocess.Popen(  # noqa: S603 - a list declared by the administrator, no shell
                list(argv),
                stdin=typed,
                stdout=printed,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=dict(env) if env is not None else clean_environment(),
                start_new_session=True,  # its own process group: killed as a whole
                close_fds=True,
            )
    except (OSError, ValueError):  # not there, not executable, or an argument execve refuses
        return Outcome("unavailable", None, time.monotonic() - started)
    status = "ok"
    try:
        while True:
            try:
                process.wait(_POLL_S)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started >= timeout_s:
                    status = "timeout"
                elif stop is not None and stop.is_set():
                    status = "stopped"
                else:
                    continue
                _kill_group(process.pid)
                process.wait()
                break
    finally:
        _kill_group(process.pid)  # whatever it left running, however this function is left
        if process.poll() is None:
            process.wait()
    if status == "ok" and process.returncode != 0:
        status = "failed"
    tail = ""
    with contextlib.suppress(OSError), open(cwd / LOG_NAME, "rb") as log:
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - _TAIL_BYTES))
        tail = log.read().decode("utf-8", "replace")
    return Outcome(status, process.returncode, time.monotonic() - started, tail)
