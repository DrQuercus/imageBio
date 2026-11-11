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
from fixtures.programs import RECORD_VARIABLE, processing
from imagebio.live.service import MOVIE_WAIT_S
from imagebio.live.simulator import mrc_bytes

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
    assert list((tmp_path / "state/work").iterdir()) == []  # nothing of the runs is kept
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
