"""The external programs in the Live mode, through the API. MotionCor and CTFFIND are played
by the programs of tests/fixtures/programs.py: what is tested is everything around them."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import mrcfile
import numpy as np
import pytest

from fixtures.api import ALICE, BOB, build_tree, make_client
from fixtures.live import BASE
from fixtures.live_world import WATCHES, World, sim_in
from fixtures.programs import GATE_KIND_VARIABLE, GATE_VARIABLE, RECORD_VARIABLE, ctf_step, processing
from imagebio.api import cli
from imagebio.core.config import ConfigError, ProcessingConfig
from imagebio.live.service import MOVIE_WAIT_S, LiveService
from imagebio.live.simulator import _epu_xml, mrc_bytes

pytestmark = pytest.mark.skipif(os.name != "posix", reason="programs are run on POSIX systems only")

SESSIONS = ("cryo/alice_session", "public/bob_session", "public/shared")


@pytest.fixture
def programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the stand-in programs are written; what they are given goes to ``record.jsonl``."""
    directory = tmp_path / "programs"
    directory.mkdir()
    monkeypatch.setenv(RECORD_VARIABLE, str(tmp_path / "record.jsonl"))
    return directory


def a_world(tmp_path: Path, **settings: object) -> World:
    tree = build_tree(tmp_path / "data")
    for directory in SESSIONS:
        (tree.base / directory).mkdir()
        os.utime(tree.base / directory, (BASE, BASE))
    return World(tree, tmp_path / "state", **settings)


def record(tmp_path: Path) -> list[dict]:
    path = tmp_path / "record.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ask(world: World, watch: int, who: dict[str, str], on: bool = True) -> dict:
    r = world.client.put(f"{WATCHES}/{watch}/processing", params={"on": str(on).lower()}, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def follow(world: World, session: str, who: dict[str, str], on: bool = True) -> int:
    """Watch a session and let a first scan see it as it is: what is written afterwards
    arrives under the eyes of the scanner. Then ask for the programs, or not."""
    watch = world.watch(session, who)
    world.run([], 5)
    if on:
        ask(world, watch, who)
    return watch


def arrive(world: World, session: str, images: dict[str, np.ndarray]) -> None:
    """Write files into a watched session now, and scan until they are complete."""
    directory = world.tree.base / session
    for name, data in images.items():
        (directory / name).write_bytes(mrc_bytes(data.astype(np.float32), 1.25, stack=data.ndim == 3))
        os.utime(directory / name, (world.now, world.now))
    os.utime(directory, (world.now, world.now))
    world.run([], 30)


def run_all(world: World) -> int:
    """Run the programs on everything that waits, then let the scanners record the results."""
    count = 0
    while world.live.process_once(world.now):
        count += 1
        assert count < 500
    world.run([], 5)
    return count


def flat(value: float, frames: int = 0) -> np.ndarray:
    return np.full((frames, 32, 32) if frames else (32, 32), value)


def test_movies_are_aligned_and_their_ctf_read_as_they_arrive(tmp_path: Path, programs: Path) -> None:
    """The whole chain on a session written by the simulated microscope."""
    world = a_world(tmp_path, processing=processing(programs))
    sim = sim_in(world, "cryo/alice_session", 3, pause_probability=0.0, shape=(64, 64), frames=4)
    watch = world.watch("cryo/alice_session", ALICE)
    assert world.summary(watch, ALICE)["processing"] == {
        "available": True, "steps": ["motion", "ctf"], "on": False, "done": 0, "waiting": 0,
    }  # fmt: skip
    assert ask(world, watch, ALICE)["on"] is True
    world.run([], 5)  # a first scan of the empty session: what comes next arrives under its eyes
    world.run([sim], 90)
    sim.stop()
    world.run([sim], 60)
    before = world.items(watch, ALICE, limit=5000)
    assert len(before["items"]) == len(sim.exposures) >= 10
    assert all(item["processing"] is None for item in before["items"])
    assert world.summary(watch, ALICE)["processing"]["waiting"] == len(sim.exposures)

    assert run_all(world) == len(sim.exposures)
    changed = world.items(watch, ALICE, since=before["seq"], limit=5000)["items"]
    assert len(changed) == len(sim.exposures)  # each result is a change clients are told about
    for item in changed:
        image = world.tree.base / item["path"]
        movie = next(image.parent.glob(f"{item['name']}*Fractions.mrc"))
        with mrcfile.open(movie) as m:
            total = np.asarray(m.data, np.float64).sum(axis=0)
        apix = item["meta"]["epu"]["pixel_size"]
        result = item["processing"]
        # four frames, each (0.5, 0.25) px from the one before: what the motion program wrote
        assert result["motion"]["total"] == pytest.approx(3 * math.hypot(0.5, 0.25) * apix, abs=0.006)
        assert (result["motion"]["frames"], result["motion"]["unit"]) == (4, "A")
        # the CTF program was given the sum of the frames: its defocus says the mean it read
        assert result["ctf"]["defocus_v"] == pytest.approx((10000 + 1000 * total.mean()) / 1e4, abs=2e-4)
        assert result["ctf"]["astigmatism"] == pytest.approx(0.04, abs=2e-4)
        assert result["ctf"]["fit"] == pytest.approx(4 * apix, abs=0.006)
    done = world.summary(watch, ALICE)["processing"]
    assert (done["done"], done["waiting"]) == (len(sim.exposures), 0)
    assert world.live.process_once(world.now) is False  # nothing is run twice
    assert not list((tmp_path / "state/work").rglob("run-*"))  # nothing of the runs is kept
    # what the programs were told: the files of the server, the pixel size of the acquisition
    runs = record(tmp_path)
    assert len(runs) == 2 * len(sim.exposures)
    assert all(
        run["argv"][run["argv"].index("-PixSize") + 1] == "0.83" for run in runs if run["kind"] == "motion"
    )
    assert all(run["answers"][2:6] == ["0.83", "300", "2.7", "0.07"] for run in runs if run["kind"] == "ctf")
    world.client.__exit__(None, None, None)


def test_nothing_runs_unless_declared_by_the_administrator_and_asked_for_by_the_user(
    tmp_path: Path, programs: Path
) -> None:
    bare = a_world(tmp_path / "bare")
    watch = follow(bare, "public/shared", ALICE, on=False)
    assert bare.summary(watch, ALICE)["processing"] == {
        "available": False, "steps": [], "on": False, "done": 0, "waiting": 0,
    }  # fmt: skip
    refused = bare.client.put(f"{WATCHES}/{watch}/processing", params={"on": "true"}, headers=ALICE)
    assert (
        refused.status_code == 400
        and "no processing program is declared" in refused.json()["error"]["message"]
    )
    assert ask(bare, watch, ALICE, on=False)["on"] is False  # not asking is always possible
    arrive(bare, "public/shared", {"a.mrc": flat(5)})
    assert bare.live.process_once(bare.now + 1000) is False
    bare.client.__exit__(None, None, None)

    world = a_world(tmp_path / "declared", processing=processing(programs, motion=False))
    watch = follow(world, "public/shared", ALICE, on=False)
    theirs = follow(world, "public/bob_session", BOB, on=False)
    arrive(world, "public/shared", {"a.mrc": flat(5)})
    assert world.live.process_once(world.now) is False and record(tmp_path) == []  # declared, not asked for
    # a watch is its user's: nobody else turns the programs on for it
    for who, target in ((BOB, watch), (ALICE, theirs), (ALICE, 999)):
        r = world.client.put(f"{WATCHES}/{target}/processing", params={"on": "true"}, headers=who)
        assert r.status_code == 404, (who, target)
    for params in ({}, {"on": "maybe"}, {"on": "true", "slots": 8}):
        assert (
            world.client.put(f"{WATCHES}/{watch}/processing", params=params, headers=ALICE).status_code == 400
        )
    assert world.client.put(f"{WATCHES}/{watch}/processing", params={"on": "true"}).status_code == 401
    assert ask(world, watch, ALICE)["waiting"] == 1
    assert run_all(world) == 1
    item = world.items(watch, ALICE)["items"][0]
    assert item["processing"]["ctf"]["defocus_v"] == pytest.approx(1.5)  # 5 counts; pixel size from the file
    assert record(tmp_path)[0]["answers"][2] == "1.25"
    # no longer asking: what arrives next is left alone, what was done stays
    assert ask(world, watch, ALICE, on=False) == {
        "available": True, "steps": ["ctf"], "on": False, "done": 1, "waiting": 0,
    }  # fmt: skip
    arrive(world, "public/shared", {"b.mrc": flat(6)})
    assert world.live.process_once(world.now) is False
    world.client.__exit__(None, None, None)


def test_a_session_found_already_written_is_not_gone_through(tmp_path: Path, programs: Path) -> None:
    world = a_world(tmp_path, processing=processing(programs, motion=False))
    session = world.tree.base / "public/shared"
    for n in range(5):  # written before anybody watched
        (session / f"old_{n}.mrc").write_bytes(mrc_bytes(flat(3).astype(np.float32), 1.25))
        os.utime(session / f"old_{n}.mrc", (BASE - 50, BASE - 50))
    os.utime(session, (BASE - 50, BASE - 50))
    watch = world.watch("public/shared", ALICE)
    ask(world, watch, ALICE)
    world.run([], 30)
    assert len(world.items(watch, ALICE)["items"]) == 5
    assert world.summary(watch, ALICE)["processing"]["waiting"] == 0 and run_all(world) == 0
    arrive(world, "public/shared", {"new.mrc": flat(4)})
    assert run_all(world) == 1
    by_name = {item["name"]: item["processing"] for item in world.items(watch, ALICE)["items"]}
    assert by_name["new"]["ctf"]["defocus_v"] == pytest.approx(1.4)
    assert all(by_name[f"old_{n}"] is None for n in range(5))
    world.client.__exit__(None, None, None)


def test_a_program_that_crashes_or_hangs_is_set_apart_and_the_service_goes_on(
    tmp_path: Path, programs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (M7): the crash or the blocking of an external program is isolated."""
    monkeypatch.setenv("IMAGEBIO_METRICS_TOKEN", "t0ken")
    world = a_world(tmp_path, processing=processing(programs, motion=False, timeout_s=3.0), metrics=True)
    watch = follow(world, "public/shared", ALICE)
    arrive(world, "public/shared", {"crash_a.mrc": flat(5), "hang_b.mrc": flat(5), "good_c.mrc": flat(7)})

    ran: list[int] = []
    worker = threading.Thread(
        target=lambda: ran.append(sum(1 for _ in iter(lambda: world.live.process_once(world.now), False)))
    )
    worker.start()
    slowest, asked = 0.0, 0
    while worker.is_alive():  # one of the three hangs for three seconds: the server answers meanwhile
        started = time.monotonic()
        assert world.client.get("/healthz").status_code == 200
        assert world.client.get(f"{WATCHES}/{watch}", headers=ALICE).status_code == 200
        assert world.client.get(f"{WATCHES}/{watch}/items", headers=ALICE).status_code == 200
        slowest, asked = max(slowest, time.monotonic() - started), asked + 1
        time.sleep(0.05)
    worker.join()
    assert ran == [3] and asked >= 5 and slowest < 1.5

    world.run([], 5)
    by_name = {item["name"]: item["processing"] for item in world.items(watch, ALICE)["items"]}
    assert by_name["crash_a"] == {
        "ctf": {"error": "the program ended with status 139"},
        "seconds": pytest.approx(0, abs=3),
    }
    assert by_name["hang_b"]["ctf"] == {"error": "the program was stopped after 3 s"}
    assert by_name["good_c"]["ctf"]["defocus_v"] == pytest.approx(1.7)
    # what failed is not tried again for ever, and says nothing of the server
    assert world.live.process_once(world.now) is False
    assert world.summary(watch, ALICE)["processing"]["done"] == 3
    assert str(tmp_path) not in json.dumps(by_name) and "Segmentation" not in json.dumps(by_name)
    assert all(
        not Path(f"/proc/{run['pid']}").exists() for run in record(tmp_path)
    )  # the one that hung is gone
    text = world.client.get("/metrics", headers={"Authorization": "Bearer t0ken"}).text
    world.client.__exit__(None, None, None)
    assert 'imagebio_live_program_runs_total{outcome="failed"} 2\n' in text
    assert 'imagebio_live_program_runs_total{outcome="ok"} 1\n' in text
    assert "imagebio_live_program_runs_in_progress 0\n" in text


def test_users_take_turns_whatever_each_one_has_waiting(tmp_path: Path, programs: Path) -> None:
    """Acceptance (M7): a user cannot keep the programs to themselves."""
    world = a_world(tmp_path, processing=processing(programs, motion=False))
    for session, who in (
        ("cryo/alice_session", ALICE),
        ("public/shared", ALICE),
        ("public/bob_session", BOB),
    ):
        follow(world, session, who)
    for session, count in (("cryo/alice_session", 6), ("public/shared", 6), ("public/bob_session", 2)):
        directory = world.tree.base / session
        for n in range(count):
            (directory / f"image_{n}.mrc").write_bytes(mrc_bytes(flat(5).astype(np.float32), 1.25))
            os.utime(directory / f"image_{n}.mrc", (world.now, world.now))
        os.utime(directory, (world.now, world.now))
    world.run([], 30)
    assert run_all(world) == 14
    order = [Path(run["answers"][0]).parent.name for run in record(tmp_path)]
    owner = {"alice_session": "alice", "shared": "alice", "bob_session": "bob"}
    # alice has twelve images waiting in two sessions, bob two in one: he is served every other
    # time until he has nothing left, not after her twelve
    assert [owner[name] for name in order[:4]] == ["alice", "bob", "alice", "bob"]
    assert {order[0], order[2]} == {"alice_session", "shared"}  # and her sessions take turns too
    assert order.count("bob_session") == 2 and len(order) == 14
    world.client.__exit__(None, None, None)


def test_an_exposure_waits_for_its_movie_and_is_run_again_when_one_comes_late(
    tmp_path: Path, programs: Path
) -> None:
    world = a_world(tmp_path, processing=processing(programs))
    watch = follow(world, "public/shared", ALICE)
    arrive(world, "public/shared", {"a.mrc": flat(5)})
    # an image and no movie yet: the motion step may still have something to do
    assert world.live.process_once(world.now) is False
    world.run([], int(MOVIE_WAIT_S))
    assert run_all(world) == 1  # nothing came: the CTF is read on the image of the microscope
    first = world.items(watch, ALICE)["items"][0]
    assert (
        first["processing"]["ctf"]["defocus_v"] == pytest.approx(1.5) and "motion" not in first["processing"]
    )

    arrive(world, "public/shared", {"a_Fractions.mrc": flat(2, frames=6)})  # the movie, at last
    again = world.items(watch, ALICE)["items"][0]
    assert again["members"]["movie"] == "complete" and again["processing"] is None  # to be done again
    assert run_all(world) == 1
    final = world.items(watch, ALICE)["items"][0]["processing"]
    assert final["motion"]["frames"] == 6
    assert final["ctf"]["defocus_v"] == pytest.approx(2.2)  # on the sum of the six frames: 12 counts
    world.client.__exit__(None, None, None)


def test_the_workers_of_a_real_server_run_the_programs_and_stop_them_with_it(
    tmp_path: Path, programs: Path
) -> None:
    """Background threads and the real clock: results come by themselves, and stopping the
    server kills the program that is running."""
    tree = build_tree(tmp_path / "data")
    session = tree.base / "public/shared"
    session.mkdir()
    client = make_client(
        tree,
        auth_mode="proxy",
        trust_network=True,
        state_dir=tmp_path / "state",
        live_interval_s=0.5,
        live_min_stable_s=1.0,
        processing=processing(programs, motion=False, timeout_s=300.0),
    )
    with client:
        watch = client.post(WATCHES, params={"path": "public/shared"}, headers=ALICE).json()["id"]
        assert (
            client.put(f"{WATCHES}/{watch}/processing", params={"on": "true"}, headers=ALICE).status_code
            == 200
        )
        time.sleep(1.2)  # a first scan: what comes next arrives under its eyes
        (session / "good.mrc").write_bytes(mrc_bytes(flat(9).astype(np.float32), 1.25))
        deadline = time.monotonic() + 60
        result = None
        while result is None and time.monotonic() < deadline:
            time.sleep(0.3)
            items = client.get(f"{WATCHES}/{watch}/items", headers=ALICE).json()["items"]
            result = items[0]["processing"] if items else None
        assert result is not None and result["ctf"]["defocus_v"] == pytest.approx(1.9)

        (session / "hang.mrc").write_bytes(mrc_bytes(flat(9).astype(np.float32), 1.25))
        while len(record(tmp_path)) < 2 and time.monotonic() < deadline:
            time.sleep(0.3)
        hanging = record(tmp_path)[-1]
        assert Path(hanging["answers"][0]).name == "hang.mrc" and Path(f"/proc/{hanging['pid']}").exists()
        stopping = time.monotonic()
    assert time.monotonic() - stopping < 20  # not the 300 s of its time limit
    gone = time.monotonic() + 5
    while Path(f"/proc/{hanging['pid']}").exists() and time.monotonic() < gone:
        time.sleep(0.05)
    assert not Path(f"/proc/{hanging['pid']}").exists()
    assert not list((tmp_path / "state/work").glob("run-*"))


# ── regressions: what an independent review of this code found ────────────────


def put(world: World, session: str, files: dict[str, bytes]) -> None:
    """Write files into a session with the date of the simulated clock."""
    directory = world.tree.base / session
    for name, data in files.items():
        (directory / name).write_bytes(data)
        os.utime(directory / name, (world.now, world.now))
    os.utime(directory, (world.now, world.now))


def scan(world: World, seconds: int) -> set[str | None]:
    """Like ``World.run``, but gives the errors of the cycles instead of asserting on them."""
    errors: set[str | None] = set()
    start = int(world.now - BASE)
    for second in range(start, start + seconds):
        world.now = BASE + second
        if second % 5 == 0:
            errors |= {report.error for report in world.live.scan_once(world.now).values()}
    world.now = BASE + start + seconds
    return errors


def mrc(value: float, frames: int = 0) -> bytes:
    return mrc_bytes(flat(value, frames).astype(np.float32), 1.25, stack=bool(frames))


def held(world: World, gate: Path, meanwhile) -> None:  # type: ignore[no-untyped-def]
    """Run the programs on one exposure in a thread; while the stand-in is held in the middle
    of its run (it has read its input, not yet written its result), do ``meanwhile``."""
    worker = threading.Thread(target=lambda: world.live.process_once(world.now))
    worker.start()
    deadline = time.monotonic() + 30
    while not Path(f"{gate}.reached").exists():
        assert time.monotonic() < deadline, "the program never got to its gate"
        time.sleep(0.02)
    meanwhile()
    gate.touch()
    worker.join(30)
    assert not worker.is_alive()


def test_a_metadata_file_out_of_all_proportion_does_not_stop_the_scanner(
    tmp_path: Path, programs: Path
) -> None:
    """Regression. A pixel size of 1.5e298 m in the XML of an exposure made the motion an
    infinite number of ångströms. It could not be written to the index, the cycle that tried
    failed, kept the result for the next one, and so on for ever: nothing of the directory
    was indexed any more, for anybody."""
    world = a_world(tmp_path, processing=processing(programs), live_metrics=False)
    watch = follow(world, "public/shared", ALICE)
    xml = _epu_xml(world.now, 0.83, 40.0, 1.0, (0.0, 0.0), 6)
    assert b"8.300000E-11" in xml
    hostile = xml.replace(b"8.300000E-11", b"1.5E298")
    put(world, "public/shared", {"a.xml": hostile, "a.mrc": mrc(5), "a_Fractions.mrc": mrc(2, frames=6)})
    assert scan(world, 30) == {None}
    assert world.live.process_once(world.now) is True
    assert scan(world, 30) == {None}  # the scanner goes on
    result = world.items(watch, ALICE)["items"][0]["processing"]
    # that pixel size is nobody's: the one the image itself gives (1.25 Å) was used
    assert result["motion"] == {
        "total": pytest.approx(5 * math.hypot(0.5, 0.25) * 1.25, abs=0.006),
        "early": pytest.approx(5 * math.hypot(0.5, 0.25) * 1.25, abs=0.006),
        "max_step": pytest.approx(math.hypot(0.5, 0.25) * 1.25, abs=0.006),
        "frames": 6,
        "unit": "A",
    }
    assert record(tmp_path)[0]["argv"][record(tmp_path)[0]["argv"].index("-PixSize") + 1] == "1.25"
    put(world, "public/shared", {"b.mrc": mrc(7)})  # what comes afterwards is indexed as ever
    assert scan(world, 40) == {None}
    assert {item["name"] for item in world.items(watch, ALICE)["items"]} == {"a", "b"}
    world.client.__exit__(None, None, None)


def test_a_result_that_cannot_be_written_never_reaches_a_cycle(
    tmp_path: Path, programs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, one level down: whatever the programs or their parsers come to give, a number
    that is not finite is refused when it is handed over, not in the middle of a cycle."""
    from imagebio.live import service as service_module

    world = a_world(tmp_path, processing=processing(programs, motion=False), live_metrics=False)
    watch = follow(world, "public/shared", ALICE)
    arrive(world, "public/shared", {"a.mrc": flat(5)})
    monkeypatch.setattr(
        service_module,
        "process",
        lambda *a, **kw: {"motion": {"total": float("inf")}, "ctf": {"fit": float("nan")}},
    )
    assert world.live.process_once(world.now) is True
    assert scan(world, 20) == {None}
    assert world.items(watch, ALICE)["items"][0]["processing"] == {"error": "the programs could not be run"}
    assert world.live.process_once(world.now) is False
    world.client.__exit__(None, None, None)


def test_a_file_that_is_no_longer_what_was_indexed_is_given_to_no_program(
    tmp_path: Path, programs: Path
) -> None:
    """Regression. Between two cycles, an indexed image was replaced by a link to a file
    outside every data root, then put back: the CTF program was given the other file, and
    its user read a result worked out from an image that was not theirs to read. A named pipe
    in its place blocked the worker for good, before any program was started."""
    world = a_world(tmp_path, processing=processing(programs, motion=False), live_metrics=False)
    watch = follow(world, "public/shared", ALICE)
    outside = tmp_path / "elsewhere" / "not_for_alice.mrc"
    outside.parent.mkdir()
    outside.write_bytes(mrc(42))
    put(world, "public/shared", {"mine.mrc": mrc(5)})
    assert scan(world, 30) == {None}
    session = world.tree.base / "public/shared"

    os.rename(session / "mine.mrc", session / ".kept")
    os.symlink(outside, session / "mine.mrc")
    world.live.process_once(world.now)  # a worker, before the next cycle of the scanner
    assert record(tmp_path) == []  # no program was given the link
    os.unlink(session / "mine.mrc")

    os.mkfifo(session / "mine.mrc")
    worker = threading.Thread(target=lambda: world.live.process_once(world.now), daemon=True)
    worker.start()
    worker.join(10)
    blocked = worker.is_alive()
    if blocked:  # let it go, so that the test ends
        os.close(os.open(session / "mine.mrc", os.O_WRONLY | os.O_NONBLOCK))
        worker.join(10)
    os.unlink(session / "mine.mrc")
    assert not blocked and record(tmp_path) == []

    os.rename(session / ".kept", session / "mine.mrc")  # the image again, same size, same date
    assert scan(world, 10) == {None}
    assert run_all(world) == 1
    result = world.items(watch, ALICE)["items"][0]["processing"]
    assert result["ctf"]["defocus_v"] == pytest.approx(1.5)  # her 5 counts, not the 42 of the other file
    world.client.__exit__(None, None, None)


def test_a_file_is_used_only_as_a_regular_file_reached_through_no_link(tmp_path: Path) -> None:
    root = tmp_path / "session"
    (root / "Data").mkdir(parents=True)
    (root / "Data" / "a.mrc").write_bytes(b"x")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "b.mrc").write_bytes(b"y")
    os.symlink(tmp_path / "elsewhere" / "b.mrc", root / "Data" / "link.mrc")
    os.symlink(tmp_path / "elsewhere", root / "Linked")
    os.mkfifo(root / "Data" / "pipe.mrc")
    plain = LiveService._plain_file
    assert plain(root, "Data", "a.mrc") == root / "Data" / "a.mrc"
    assert plain(root, "", "Data") is None  # a directory
    for rel, name in (("Data", "link.mrc"), ("Linked", "b.mrc"), ("Data", "pipe.mrc"), ("Data", "gone.mrc")):
        assert plain(root, rel, name) is None, (rel, name)
    # a watched directory that is itself reached through a link is still its own
    os.symlink(root, tmp_path / "alias")
    assert plain(tmp_path / "alias", "Data", "a.mrc") == tmp_path / "alias" / "Data" / "a.mrc"


def test_a_result_is_for_the_movie_it_was_worked_out_from(
    tmp_path: Path, programs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression. A result was tied to the image alone. A movie that arrived while the CTF
    was read on the image was then never aligned; a movie written again while it was being
    aligned kept the result of the one before."""
    gate = tmp_path / "gate"
    monkeypatch.setenv(GATE_VARIABLE, str(gate))
    monkeypatch.setenv(GATE_KIND_VARIABLE, "ctf")
    world = a_world(tmp_path, processing=processing(programs), live_metrics=False)
    watch = follow(world, "public/shared", ALICE)
    put(world, "public/shared", {"a.mrc": mrc(5)})
    assert scan(world, 30 + int(MOVIE_WAIT_S)) == {None}  # no movie after two minutes: the image alone

    def the_movie_arrives() -> None:
        put(world, "public/shared", {"a_Fractions.mrc": mrc(2, frames=6)})
        assert scan(world, 30) == {None}
        assert world.items(watch, ALICE)["items"][0]["members"]["movie"] == "complete"

    held(world, gate, the_movie_arrives)
    assert scan(world, 10) == {None}
    assert (
        world.items(watch, ALICE)["items"][0]["processing"] is None
    )  # the result of the image alone is dropped
    gate.unlink()
    Path(f"{gate}.reached").unlink()

    def the_movie_is_written_again() -> None:  # a transfer that started over: nine frames of 3 counts
        put(world, "public/shared", {"a_Fractions.mrc": mrc(3, frames=9)})
        assert scan(world, 40) == {None}

    monkeypatch.setenv(GATE_KIND_VARIABLE, "motion")
    held(world, gate, the_movie_is_written_again)  # ... while the six-frame movie is aligned
    assert scan(world, 10) == {None}
    assert world.items(watch, ALICE)["items"][0]["processing"] is None  # not the result of the six frames
    monkeypatch.delenv(GATE_VARIABLE)
    assert run_all(world) == 1
    final = world.items(watch, ALICE)["items"][0]["processing"]
    assert final["motion"]["frames"] == 9 and final["ctf"]["defocus_v"] == pytest.approx(3.7)  # 27 counts
    assert world.live.process_once(world.now) is False
    world.client.__exit__(None, None, None)


def test_a_movie_that_never_completes_does_not_keep_its_image_waiting_for_ever(
    tmp_path: Path, programs: Path
) -> None:
    """Regression. An exposure with a movie still being written was never picked, whatever
    the time: a transfer cut short kept its image from the CTF step for good, even on a
    server that aligns no movie at all."""
    truncated = mrc(2, frames=6)[:-2000]  # the header promises more than the file holds
    for name, declared, waits in (
        ("aligning", processing(programs), True),
        ("ctf_only", processing(programs, motion=False), False),
    ):
        world = a_world(tmp_path / name, processing=declared, live_metrics=False)
        watch = follow(world, "public/shared", ALICE)
        put(world, "public/shared", {"a.mrc": mrc(5), "a_Fractions.mrc": truncated})
        assert scan(world, 30) == {None}
        item = world.items(watch, ALICE)["items"][0]
        assert (item["state"], item["members"]["movie"]) == ("complete", "pending")
        if waits:  # the movie may still come: two minutes, no more
            assert world.live.process_once(world.now) is False
            assert scan(world, int(MOVIE_WAIT_S)) == {None}
        assert run_all(world) == 1, name
        result = world.items(watch, ALICE)["items"][0]["processing"]
        assert "motion" not in result and result["ctf"]["defocus_v"] == pytest.approx(1.5), name
        world.client.__exit__(None, None, None)


def test_only_the_runs_of_this_server_are_cleared_from_a_shared_work_directory(
    tmp_path: Path, programs: Path
) -> None:
    """Regression. At start, every ``run-*`` directory of ``work_dir`` was removed: with a
    scratch disk shared with other programs, theirs went too."""
    scratch = tmp_path / "scratch"
    (scratch / "run-2026-11-10_relion").mkdir(parents=True)
    (scratch / "run-2026-11-10_relion" / "job.star").write_text("data_\n")
    (scratch / "notes").mkdir()
    tree = build_tree(tmp_path / "data")

    world = a_world(tmp_path / "w", processing=processing(programs, motion=False, work_dir=scratch))
    corner = world.live._work  # where this server keeps its runs: a directory of its own
    world.client.__exit__(None, None, None)
    assert corner.parent == scratch and corner.name.startswith("imagebio-runs-")
    (corner / "run-left-behind").mkdir(parents=True)  # what it left when it was killed
    with make_client(  # a server that starts for real, on the same state directory
        tree,
        auth_mode="proxy",
        trust_network=True,
        state_dir=tmp_path / "w" / "state",
        processing=processing(programs, motion=False, work_dir=scratch),
    ):
        pass
    assert not (corner / "run-left-behind").exists()  # its own leftovers are cleared
    assert (scratch / "run-2026-11-10_relion" / "job.star").is_file() and (scratch / "notes").is_dir()


def test_switching_the_programs_off_and_on_does_not_start_a_running_exposure_again(
    tmp_path: Path, programs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression. With two programs at once, an idle worker that looked for work while the
    session had the programs switched off forgot the exposure the other worker was running;
    switched on again, the same exposure was started a second time next to the first."""
    gate = tmp_path / "gate"
    monkeypatch.setenv(GATE_VARIABLE, str(gate))
    world = a_world(tmp_path, processing=processing(programs, motion=False, slots=2), live_metrics=False)
    watch = follow(world, "public/shared", ALICE)
    put(world, "public/shared", {"only.mrc": mrc(5)})
    assert scan(world, 30) == {None}

    def off_then_on() -> None:
        ask(world, watch, ALICE, on=False)
        assert world.live.process_once(world.now) is False  # the other worker, idle, looks for work
        ask(world, watch, ALICE, on=True)
        assert world.live.process_once(world.now) is False  # and again: the exposure is being run

    held(world, gate, off_then_on)
    assert len(Path(f"{gate}.reached").read_text().split()) == 1  # one program, not two
    before = world.items(watch, ALICE)["seq"]
    assert scan(world, 10) == {None}
    after = world.items(watch, ALICE)
    assert after["seq"] - before == 1 and after["items"][0]["processing"]["ctf"][
        "defocus_v"
    ] == pytest.approx(1.5)
    assert len(record(tmp_path)) == 1 and world.live.process_once(world.now) is False
    world.client.__exit__(None, None, None)


def test_a_program_that_is_not_installed_fails_no_exposure_and_stops_the_server_from_starting(
    tmp_path: Path, programs: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression. With a mistyped path, every exposure got "the program could not be
    started" as its final result; the path corrected, nothing was run again."""
    step = ctf_step(programs)
    late = programs / "ctffind"  # not there yet
    absent = ProcessingConfig(
        ctf=type(step)(argv=(str(late),), stdin=step.stdin, result=step.result, parser="ctffind")
    )
    world = a_world(tmp_path, processing=absent, live_metrics=False)
    watch = follow(world, "public/shared", ALICE)
    arrive(world, "public/shared", {"a.mrc": flat(5), "b.mrc": flat(6)})
    with pytest.raises(ConfigError, match="is not there, or cannot be run"):
        world.live.check()  # what `imagebio serve` does before it starts
    assert world.live.process_once(world.now) is False
    assert world.live.process_once(world.now) is False  # nothing is tried for a while
    assert scan(world, 10) == {None}
    assert all(
        item["processing"] is None for item in world.items(watch, ALICE)["items"]
    )  # nothing held against them
    assert world.summary(watch, ALICE)["processing"]["waiting"] == 2

    late.write_text(f"#!{step.argv[0]}\n" + Path(step.argv[1]).read_text())  # installed at last
    late.chmod(0o755)
    world.live.check()
    world.live._paused_until = 0.0  # the pause is over
    assert run_all(world) == 2
    by_name = {item["name"]: item["processing"] for item in world.items(watch, ALICE)["items"]}
    assert by_name["a"]["ctf"]["defocus_v"] == pytest.approx(1.5) and by_name["b"]["ctf"][
        "defocus_v"
    ] == pytest.approx(1.6)
    world.client.__exit__(None, None, None)

    # and from the command line: one line, before the server starts
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("the server must not start"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(
        '[processing.ctf]\nargv = ["/opt/no/such/ctffind"]\nstdin = ["{image}"]\n'
        'result = "{out}/d.txt"\nparser = "ctffind"\n'
    )
    assert cli.main(["serve", "--config", str(cfg)]) == 2
    assert "/opt/no/such/ctffind is not there, or cannot be run" in capsys.readouterr().err
