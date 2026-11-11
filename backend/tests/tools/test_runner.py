"""The runner against programs written for the occasion: what it passes, what it stops."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from imagebio.tools.runner import LOG_NAME, clean_environment, run_program

pytestmark = pytest.mark.skipif(os.name != "posix", reason="programs are run on POSIX systems only")


def program(tmp_path: Path, body: str) -> list[str]:
    """A program made of ``body`` (Python), as an argument list."""
    script = tmp_path / f"program_{abs(hash(body))}.py"
    script.write_text("import json, os, sys, time, subprocess\n" + body)
    return [sys.executable, str(script)]


def gone(pid: int) -> bool:
    """Whether a process no longer runs (a zombie nobody reaped does not run)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def test_arguments_and_answers_reach_the_program_as_they_are(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    echo = program(
        tmp_path,
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}, open('seen.json', 'w'))\n",
    )
    hostile = [
        "a b",
        "$(touch pwned)",
        "; touch pwned",
        "`touch pwned`",
        "-x",
        "é'\"",
        "*",
        "> pwned",
        "| cat",
    ]
    outcome = run_program([*echo, *hostile], cwd=work, timeout_s=20, stdin="first\nno\n3.5\n")
    assert outcome.ok and outcome.code == 0
    seen = json.loads((work / "seen.json").read_text())
    assert seen["argv"] == hostile  # one argument each, nothing interpreted
    assert seen["stdin"] == "first\nno\n3.5\n" and Path(seen["cwd"]) == work
    assert not list(tmp_path.rglob("pwned"))  # no shell ever saw them


def test_a_failure_is_reported_with_its_status_not_its_words(tmp_path: Path) -> None:
    failing = program(tmp_path, "print('cannot open /srv/secret/movie.mrc'); sys.exit(3)\n")
    outcome = run_program(failing, cwd=tmp_path, timeout_s=20)
    assert (outcome.status, outcome.code) == ("failed", 3)
    assert "/srv/secret" in outcome.tail  # for the log of the server
    assert outcome.public(20) == "the program ended with status 3"  # for the user
    crashed = run_program(program(tmp_path, "os.kill(os.getpid(), 11)\n"), cwd=tmp_path, timeout_s=20)
    assert crashed.status == "failed" and crashed.code == -11

    missing = run_program([str(tmp_path / "no_such_program"), "x"], cwd=tmp_path, timeout_s=5)
    assert missing.status == "unavailable" and "no_such_program" not in missing.public(5)
    not_a_program = tmp_path / "data.txt"
    not_a_program.write_text("not executable")
    assert run_program([str(not_a_program)], cwd=tmp_path, timeout_s=5).status == "unavailable"
    assert run_program([], cwd=tmp_path, timeout_s=5).status == "unavailable"
    assert run_program([sys.executable, "a\0b"], cwd=tmp_path, timeout_s=5).status == "unavailable"


def test_a_program_that_hangs_is_killed_with_its_children(tmp_path: Path) -> None:
    hanging = program(
        tmp_path,
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "open('pids', 'w').write(f'{os.getpid()} {child.pid}')\n"
        "time.sleep(600)\n",
    )
    started = time.monotonic()
    outcome = run_program(hanging, cwd=tmp_path, timeout_s=1.5)
    assert outcome.status == "timeout" and time.monotonic() - started < 10
    assert outcome.public(1.5) == "the program was stopped after 1.5 s"
    parent, child = (int(p) for p in (tmp_path / "pids").read_text().split())
    deadline = time.monotonic() + 5
    while not (gone(parent) and gone(child)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert gone(parent) and gone(child)  # the whole group, not only the program itself


def test_children_left_behind_by_a_program_that_ended_are_killed_too(tmp_path: Path) -> None:
    leaving = program(
        tmp_path,
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "open('pid', 'w').write(str(child.pid))\n",
    )
    assert run_program(leaving, cwd=tmp_path, timeout_s=20).ok
    child = int((tmp_path / "pid").read_text())
    deadline = time.monotonic() + 5
    while not gone(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert gone(child)


def test_the_server_stopping_stops_the_program(tmp_path: Path) -> None:
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    outcome = run_program(program(tmp_path, "time.sleep(600)\n"), cwd=tmp_path, timeout_s=300, stop=stop)
    assert outcome.status == "stopped" and time.monotonic() - started < 10


def test_neither_side_blocks_on_what_the_other_does_not_read(tmp_path: Path) -> None:
    chatty = program(tmp_path, "sys.stdout.write('x' * 5_000_000); sys.stdout.write('THE END')\n")
    outcome = run_program(chatty, cwd=tmp_path, timeout_s=30, stdin="answer\n" * 200_000)  # never read
    assert outcome.ok and outcome.tail.endswith("THE END") and len(outcome.tail) <= 2000
    assert (tmp_path / LOG_NAME).stat().st_size == 5_000_007


def test_the_secrets_of_the_server_are_not_handed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGEBIO_PROXY_SECRET", "proxy-secret")
    monkeypatch.setenv("IMAGEBIO_METRICS_TOKEN", "metrics-token")
    monkeypatch.setenv("MAIL_PASSWORD", "smtp-password")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    printing = program(tmp_path, "json.dump(dict(os.environ), open('env.json', 'w'))\n")
    assert run_program(
        printing, cwd=tmp_path, timeout_s=20, env=clean_environment(secret_names={"MAIL_PASSWORD"})
    ).ok
    seen = json.loads((tmp_path / "env.json").read_text())
    assert not any(name.startswith("IMAGEBIO_") for name in seen) and "MAIL_PASSWORD" not in seen
    assert seen["CUDA_VISIBLE_DEVICES"] == "1" and "PATH" in seen  # what a program needs is kept
    (tmp_path / "env.json").unlink()
    assert run_program(printing, cwd=tmp_path, timeout_s=20).ok  # the default environment is the clean one
    assert "IMAGEBIO_PROXY_SECRET" not in json.loads((tmp_path / "env.json").read_text())
