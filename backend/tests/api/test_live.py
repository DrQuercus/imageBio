"""The Live mode through the API, against a simulated microscope and a simulated clock."""

from __future__ import annotations

import io
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

from fixtures.api import ALICE, BOB, Tree, build_tree, make_client
from fixtures.live import BASE
from imagebio.core.config import AlertChannel, SmtpConfig
from imagebio.live.alerts import AlertDeliveryError, Notice
from imagebio.live.simulator import MicroscopeSim, SimSettings, mrc_bytes

WATCHES = "/api/live/watches"
THUMB = "/api/live/thumb"
CAROL = {"X-Forwarded-User": "carol", "X-Forwarded-Groups": "cryoem"}


class World:
    """A server that scans only when told to, a clock that moves only when told to."""

    def __init__(self, tree: Tree, state: Path, **settings: object) -> None:
        self.tree = tree
        self.state = state
        self.settings = settings
        self.now = BASE
        self.sent: list[tuple[str, Notice]] = []  # (channel, notice) delivered by the server
        self.failures = 0  # deliveries that will fail before one succeeds
        self.broken: set[str] = set()  # channels that refuse everything
        self.client = self._client()

    def _client(self):
        client = make_client(
            self.tree,
            auth_mode="proxy",
            trust_network=True,
            state_dir=self.state,
            live_scan=False,
            **self.settings,
        )
        client.__enter__()
        live = client.app.state.live
        live.clock = lambda: self.now
        live.notifier = self  # notices come here instead of going out
        return client

    def send(self, channel: AlertChannel, notice: Notice) -> None:
        if channel.name in self.broken:
            self.refused = getattr(self, "refused", 0) + 1
            raise AlertDeliveryError(f"channel {channel.name!r}: ConnectionRefusedError")
        if self.failures > 0:
            self.failures -= 1
            raise AlertDeliveryError(f"channel {channel.name!r}: ConnectionRefusedError")
        self.sent.append((channel.name, notice))

    @property
    def live(self):
        return self.client.app.state.live

    def restart(self) -> None:
        """Stop the server and start another on the same state directory."""
        self.client.__exit__(None, None, None)
        self.client = self._client()

    def run(self, sims: list[MicroscopeSim], seconds: int) -> None:
        start = int(self.now - BASE)
        for second in range(start, start + seconds):
            self.now = BASE + second
            for sim in sims:
                sim.step(self.now)
            if second % 5 == 0:
                for report in self.live.scan_once(self.now).values():
                    assert report.error is None, report.error
        self.now = BASE + start + seconds

    def watch(self, path: str, who: dict[str, str]) -> int:
        r = self.client.post(WATCHES, params={"path": path}, headers=who)
        assert r.status_code == 200, r.text
        return int(r.json()["id"])

    def items(self, watch_id: int, who: dict[str, str], **params: object) -> dict:
        r = self.client.get(f"{WATCHES}/{watch_id}/items", params=params, headers=who)
        assert r.status_code == 200, r.text
        return r.json()

    def summary(self, watch_id: int, who: dict[str, str]) -> dict:
        r = self.client.get(f"{WATCHES}/{watch_id}", headers=who)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def world(tmp_path: Path):
    tree = build_tree(tmp_path / "data")
    for directory in ("cryo/alice_session", "public/bob_session", "public/shared"):
        (tree.base / directory).mkdir()
        os.utime(tree.base / directory, (BASE, BASE))
    w = World(tree, tmp_path / "state")
    yield w
    w.client.__exit__(None, None, None)


def sim_in(world: World, directory: str, seed: int, **settings: object) -> MicroscopeSim:
    defaults = {"rate_per_hour": 600, "pause_probability": 0.2, "seed": seed}
    return MicroscopeSim(world.tree.base / directory, SimSettings(**{**defaults, **settings}))  # type: ignore[arg-type]


# ── acceptance: two users, two sessions ───────────────────────────────────────


def test_two_users_on_two_sessions_do_not_interfere(world: World) -> None:
    """Acceptance (M3). The legacy monitor had one watch for everybody: starting or stopping
    it changed what every user saw."""
    a = sim_in(world, "cryo/alice_session", 1)
    b = sim_in(world, "public/bob_session", 2, rate_per_hour=300)
    alice = world.watch("cryo/alice_session", ALICE)
    bob = world.watch("public/bob_session", BOB)
    assert alice != bob
    world.run([a, b], 120)

    # each sees their own watch and their own session, and nothing of the other's
    for who, own, other, sim in ((ALICE, alice, bob, a), (BOB, bob, alice, b)):
        listed = world.client.get(WATCHES, headers=who).json()["watches"]
        assert [w["id"] for w in listed] == [own] and listed[0]["active"] is True
        names = {i["name"] for i in world.items(own, who)["items"]}
        assert names == set(sim.exposures)
        for url in (f"{WATCHES}/{other}", f"{WATCHES}/{other}/items", f"{WATCHES}/{other}/history"):
            r = world.client.get(url, headers=who)
            assert r.status_code == 404 and r.json()["error"]["message"] == "no such watch"
        assert world.client.delete(f"{WATCHES}/{other}", headers=who).status_code == 404
    assert world.summary(alice, ALICE)["acquired"] > world.summary(bob, BOB)["acquired"] > 0

    # Alice stops: Bob's session goes on, hers is no longer scanned but stays readable
    stopped = world.client.delete(f"{WATCHES}/{alice}", headers=ALICE).json()
    assert stopped["active"] is False and stopped["stopped_at"] == world.now
    alice_before = world.summary(alice, ALICE)
    assert alice_before["scanner"]["running"] is False
    bob_before = world.summary(bob, BOB)["acquired"]
    world.run([a, b], 120)
    assert world.summary(bob, BOB)["acquired"] > bob_before
    assert world.summary(bob, BOB)["scanner"]["running"] is True
    assert world.summary(alice, ALICE)["acquired"] == alice_before["acquired"]
    assert len(a.exposures) > alice_before["acquired"] + 10  # her microscope did go on

    # she watches again: the same watch, and what happened meanwhile is found
    assert world.watch("cryo/alice_session", ALICE) == alice
    a.stop()
    b.stop()
    world.run([a, b], 60)
    assert world.summary(alice, ALICE)["acquired"] == len(a.exposures)
    assert world.summary(bob, BOB)["acquired"] == len(b.exposures)


def test_two_users_can_watch_the_same_directory(world: World) -> None:
    shared = sim_in(world, "public/shared", 3)
    alice = world.watch("public/shared", ALICE)
    bob = world.watch("public/shared", BOB)
    assert alice != bob
    world.run([shared], 60)
    assert world.items(alice, ALICE)["items"] == world.items(bob, BOB)["items"]
    assert len(world.live.index.connection().execute("SELECT id FROM roots").fetchall()) == 1  # scanned once
    world.client.delete(f"{WATCHES}/{alice}", headers=ALICE)
    before = world.summary(bob, BOB)["acquired"]
    world.run([shared], 60)
    assert world.summary(bob, BOB)["acquired"] > before  # still scanned for Bob
    assert world.summary(bob, BOB)["scanner"]["running"] is True


# ── what a watch shows ────────────────────────────────────────────────────────


def test_a_client_follows_a_session_by_asking_for_what_changed(world: World) -> None:
    sim = sim_in(world, "cryo/alice_session", 4, pause_probability=0.3)
    watch = world.watch("cryo/alice_session", ALICE)
    known: dict[int, dict] = {}
    since = 0
    requests = sizes = 0
    for _ in range(40):
        world.run([sim], 5)
        if world.now - BASE == 150:
            sim.stop()
        while True:
            page = world.items(watch, ALICE, since=since, limit=7)
            requests += 1
            sizes += len(page["items"])
            for item in page["items"]:
                known[item["id"]] = item
            assert page["seq"] >= since
            since = page["seq"]
            if not page["more"]:
                assert since == page["head"] or not page["items"]
                break
    everything = {i["id"]: i for i in world.items(watch, ALICE, since=0, limit=5000)["items"]}
    assert known == everything and len(known) == len(sim.exposures) == 25  # one every 6 s for 150 s
    assert all(i["state"] == "complete" for i in known.values())
    assert sizes < 6 * len(known)  # deltas: each exposure travels a few times, not at every poll
    assert world.items(watch, ALICE, since=since)["items"] == []  # nothing new: nothing sent

    item = known[min(known)]
    assert item["path"] == f"cryo/alice_session/{item['dir']}/{item['name']}.mrc"
    assert item["category"] == "data" and item["role"] == "image" and item["verified"] is True
    assert item["members"] == {
        "image": "complete",
        "movie": "complete",
        "preview": "complete",
        "xml": "complete",
    }
    assert item["meta"]["epu"]["pixel_size"] == pytest.approx(0.83) and item["time_source"] == "xml"
    assert str(world.tree.base) not in str(everything)  # logical paths only

    # the file a tile points at opens in the viewer
    info = world.client.get("/api/datasets/info", params={"path": item["path"]}, headers=ALICE)
    assert info.status_code == 200 and info.json()["info"]["shape"]["x"] == 64

    squares = world.items(watch, ALICE, category="gridsquare")["items"]
    assert len(squares) == 1 and squares[0]["category"] == "gridsquare" and squares[0]["role"] == "preview"
    both = world.items(watch, ALICE, category="data,gridsquare", limit=5000)["items"]
    assert len(both) == len(known) + 1


def test_removed_exposures_are_announced(world: World) -> None:
    session = world.tree.base / "cryo/alice_session"
    for name in ("a.mrc", "b.mrc"):
        (session / name).write_bytes(mrc_bytes(np.zeros((8, 8), np.float32), 1.0))
        os.utime(session / name, (BASE, BASE))
    os.utime(session, (BASE, BASE))
    watch = world.watch("cryo/alice_session", ALICE)
    world.run([], 20)
    page = world.items(watch, ALICE)
    assert {i["name"]: i["state"] for i in page["items"]} == {"a": "complete", "b": "complete"}
    os.remove(session / "a.mrc")
    os.utime(session, (world.now, world.now))
    world.run([], 10)
    changed = world.items(watch, ALICE, since=page["seq"])["items"]
    assert [(i["name"], i["state"]) for i in changed] == [("a", "deleted")]
    assert world.summary(watch, ALICE)["acquired"] == 1


def test_summary_and_history(world: World) -> None:
    sim = sim_in(world, "cryo/alice_session", 5, pause_probability=0.0)
    watch = world.watch("cryo/alice_session", ALICE)
    world.run([sim], 96)  # exposures start at 0, 6, … 90 s; the last scan is at 95 s
    s = world.summary(watch, ALICE)
    assert s["watch"] == {
        "id": watch,
        "path": "cryo/alice_session",
        "active": True,
        "created_at": BASE,
        "stopped_at": None,
    }
    assert s["now"] == world.now and s["seq"] > 0
    assert (
        s["scanner"]["running"] is True
        and s["scanner"]["error"] is None
        and s["scanner"]["cycle_at"] == BASE + 95
    )
    assert s["acquired"] + s["incoming"] == len(sim.exposures) == 16
    assert s["incoming"] >= 1 and s["scanner"]["pending"] >= 1  # the last ones are still being written
    assert s["counts"]["data"]["complete"] == s["acquired"] and s["counts"]["gridsquare"] == {"complete": 1}
    # all arrived in the last five minutes, but the one that was already there at the first scan
    assert s["rate"]["last_5_min"] == (s["acquired"] - 1) * 12
    assert s["rate"]["last_hour"] == s["acquired"] - 1
    assert s["last"]["name"] in sim.exposures and s["last"]["time"] <= world.now
    assert s["disk"]["total"] > s["disk"]["free"] > 0 and 0 <= s["disk"]["percent"] <= 100
    assert str(world.tree.base) not in str(s)

    sim.stop()
    world.run([sim], 600)  # ten minutes later, nothing new
    quiet = world.summary(watch, ALICE)
    assert quiet["acquired"] == 16 and quiet["incoming"] == 0
    assert quiet["rate"] == {"last_5_min": 0, "last_hour": 15}

    history = world.client.get(f"{WATCHES}/{watch}/history", params={"bucket": 30}, headers=ALICE).json()
    assert history["bucket"] == 30
    assert sum(n for _, n in history["counts"]) == 16
    assert [start for start, _ in history["counts"]] == [BASE + 30 * k for k in range(4)]
    assert [n for _, n in history["counts"]] == [5, 5, 5, 1]  # one every 6 s, the last at 90 s


def test_an_old_session_is_listed_at_once_and_does_not_count_as_arriving(world: World) -> None:
    """A session that ended two hours ago, one exposure per minute for forty minutes."""
    session = world.tree.base / "cryo/alice_session/Images-Disc1/GridSquare_1/Data"
    session.mkdir(parents=True)
    end = BASE - 7200
    for n in range(40):
        when = end - 60 * (39 - n)
        base = session / f"FoilHole_1_Data_2_{n}_20270115_05{n:02d}00"
        for suffix in (".mrc", ".xml"):
            path = Path(f"{base}{suffix}")
            path.write_bytes(mrc_bytes(np.full((8, 8), n, np.float32), 1.0) if suffix == ".mrc" else b"<x/>")
            os.utime(path, (when, when))
    for directory in (session, session.parent, session.parent.parent, session.parent.parent.parent):
        os.utime(directory, (end, end))
    watch = world.watch("cryo/alice_session", ALICE)
    world.run([], 5)  # one scan
    s = world.summary(watch, ALICE)
    # What had been at rest for a quarter of an hour when the session stopped is listed at
    # once. The last fifteen minutes could, for all the scanner knows, still be being written
    # (its clock and the file server's need not agree): they go through the completeness test.
    assert s["acquired"] == 24 and s["incoming"] == 16
    world.run([], 15)
    s = world.summary(watch, ALICE)
    assert s["acquired"] == 40 and s["incoming"] == 0
    assert s["rate"] == {"last_5_min": 0, "last_hour": 0}  # found, not arrived: acquired two hours ago
    items = world.items(watch, ALICE)["items"]
    assert [i["verified"] for i in sorted(items, key=lambda i: i["time"])] == [False] * 24 + [True] * 16
    assert all(i["size"] is not None for i in items)

    # what arrives from now on does count
    new = session / "FoilHole_1_Data_2_99_20270115_080100.mrc"
    new.write_bytes(mrc_bytes(np.zeros((8, 8), np.float32), 1.0))
    Path(str(new).replace(".mrc", ".xml")).write_bytes(b"<x/>")
    for path in (new, Path(str(new).replace(".mrc", ".xml")), session):
        os.utime(path, (world.now, world.now))
    world.run([], 20)
    assert world.summary(watch, ALICE)["rate"] == {"last_5_min": 12, "last_hour": 1}


# ── thumbnails ────────────────────────────────────────────────────────────────


def test_a_thumbnail_is_an_average_not_one_pixel_out_of_n(world: World) -> None:
    """A weak, smooth signal under strong noise, like a micrograph: block means bring it out,
    taking one pixel out of sixteen (what the legacy monitor did) keeps only the noise."""
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:1024, 0:1024]
    signal = 10.0 * np.sin(2 * np.pi * x / 512) * np.cos(2 * np.pi * y / 512)
    noisy = (1000 + signal + rng.normal(0, 30, (1024, 1024))).astype(np.float32)
    path = world.tree.base / "cryo/micrograph.mrc"
    path.write_bytes(mrc_bytes(noisy, 1.0))

    r = world.client.get(THUMB, params={"path": "cryo/micrograph.mrc", "size": 64}, headers=ALICE)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    thumb = np.asarray(Image.open(io.BytesIO(r.content)), dtype=np.float64)
    assert thumb.shape == (64, 64)
    truth = signal.reshape(64, 16, 64, 16).mean(axis=(1, 3))
    assert np.corrcoef(thumb.ravel(), truth.ravel())[0, 1] > 0.9  # noise divided by 16, signal kept
    decimated = noisy[::16, ::16]
    assert abs(np.corrcoef(decimated.ravel(), truth.ravel())[0, 1]) < 0.3  # what skipping pixels would show

    # the reference: block means, stretched between the 0.5th and 99.5th percentiles
    means = noisy.astype(np.float64).reshape(64, 16, 64, 16).mean(axis=(1, 3))
    lo, hi = np.percentile(means, (0.5, 99.5))
    expected = np.clip((means - lo) / (hi - lo) * 255, 0, 255)
    assert np.abs(thumb - expected).mean() < 6  # grey levels out of 255: what JPEG costs on a noisy image


def test_thumbnail_sizes_kinds_and_cache(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    base = world.tree.base
    rng = np.random.default_rng(8)
    wide = rng.normal(100, 10, (300, 1000)).astype(np.float32)
    (base / "cryo/wide.mrc").write_bytes(mrc_bytes(wide, 1.0))
    r = world.client.get(THUMB, params={"path": "cryo/wide.mrc", "size": 128}, headers=ALICE)
    assert Image.open(io.BytesIO(r.content)).size == (125, 38)  # binned 8 times, sides rounded up
    small = world.client.get(THUMB, params={"path": "cryo/movie.mrc"}, headers=ALICE)  # 5 frames of 48 x 64
    picture = Image.open(io.BytesIO(small.content))
    assert picture.size == (64, 48) and picture.mode == "L"
    eer = world.client.get(THUMB, params={"path": "cryo/movie.eer"}, headers=ALICE)
    assert eer.status_code == 200 and Image.open(io.BytesIO(eer.content)).size == (48, 32)
    colour = world.client.get(THUMB, params={"path": "light/rgb.png"}, headers=BOB)
    assert Image.open(io.BytesIO(colour.content)).mode == "RGB"
    odd = np.full((16, 16), np.nan, np.float32)
    odd[4:8, 4:8] = 5.0
    tifffile.imwrite(base / "public/nan.tif", odd)
    assert world.client.get(THUMB, params={"path": "public/nan.tif"}, headers=ALICE).status_code == 200

    # the ETag follows the content of the file; a cached thumbnail is not computed again
    assert (
        world.client.get(
            THUMB,
            params={"path": "cryo/wide.mrc", "size": 128},
            headers={**ALICE, "If-None-Match": r.headers["etag"]},
        ).status_code
        == 304
    )
    import imagebio.live.service as service

    def never(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("the thumbnail was computed again")

    monkeypatch.setattr(service, "render_thumbnail", never)
    again = world.client.get(THUMB, params={"path": "cryo/wide.mrc", "size": 128}, headers=ALICE)
    assert again.content == r.content and again.headers["etag"] == r.headers["etag"]
    world.restart()  # kept in the index: a restart does not lose it
    after = world.client.get(THUMB, params={"path": "cryo/wide.mrc", "size": 128}, headers=ALICE)
    assert after.status_code == 200 and after.content == r.content
    monkeypatch.undo()
    (base / "cryo/wide.mrc").write_bytes(mrc_bytes(wide[:, ::-1], 1.0))  # the file changes: a new thumbnail
    os.utime(base / "cryo/wide.mrc", (BASE + 99, BASE + 99))
    changed = world.client.get(THUMB, params={"path": "cryo/wide.mrc", "size": 128}, headers=ALICE)
    assert changed.headers["etag"] != r.headers["etag"] and changed.content != r.content


def test_the_thumbnail_caches_keep_to_their_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the index only dropped old thumbnails every 200 writes, a tenth at a time,
    and held several times its limit; two requests for one thumbnail counted its size twice
    in memory, for good."""
    tree = build_tree(tmp_path / "data")
    session = tree.base / "public/session"
    session.mkdir()
    rng = np.random.default_rng(5)
    for n in range(40):
        (session / f"{n:02d}.mrc").write_bytes(mrc_bytes(rng.normal(0, 1, (64, 64)).astype(np.float32), 1.0))
    cap = 20_000
    with make_client(tree, state_dir=tmp_path / "state", live_scan=False, thumb_cache_bytes=cap) as client:
        live = client.app.state.live
        db = live.index.connection()
        sizes = []
        for n in range(40):
            r = client.get(THUMB, params={"path": f"public/session/{n:02d}.mrc", "size": 64})
            assert r.status_code == 200
            sizes.append(len(r.content))
            stored = db.execute("SELECT COALESCE(SUM(LENGTH(data)), 0), COUNT(*) FROM thumbs").fetchone()
            assert stored[0] <= cap, n
        assert sum(sizes) > 2 * cap  # the limit was really reached
        assert stored[0] >= cap // 2 and stored[1] >= 5  # and the cache is not emptied either
        kept = [row[0] for row in db.execute("SELECT path FROM thumbs ORDER BY path")]
        assert kept[-1].endswith("39.mrc") and not any(k.endswith("00.mrc") for k in kept)  # oldest first

        # two requests for the same new thumbnail at the same time: counted once
        import imagebio.live.service as service

        (session / "new.mrc").write_bytes(mrc_bytes(rng.normal(0, 1, (64, 64)).astype(np.float32), 1.0))
        both = threading.Barrier(2, timeout=10)
        render = service.render_thumbnail

        def together(*args: object, **kwargs: object) -> bytes:
            both.wait()
            return render(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(service, "render_thumbnail", together)
        real = session / "new.mrc"
        threads = [threading.Thread(target=live.thumbnail, args=(real, 64)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert live._thumb_bytes == sum(len(v) for v in live._thumbs.values())


@pytest.mark.parametrize(
    ("params", "who", "status", "code"),
    [
        ({"path": "cryo/movie.mrc"}, BOB, 403, "access_denied"),
        ({"path": "../outside/secret.mrc"}, ALICE, 403, "access_denied"),
        ({"path": "cryo/missing.mrc"}, ALICE, 404, "not_found"),
        ({"path": "cryo/session"}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/corrupt.mrc"}, ALICE, 422, "corrupt_file"),
        ({"path": "cryo/session/notes.txt"}, ALICE, 415, "unsupported_format"),
        ({"path": "cryo/movie.mrc", "size": 16}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/movie.mrc", "size": 4096}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/movie.mrc", "bin": 4}, ALICE, 400, "invalid_request"),
        ({}, ALICE, 400, "invalid_request"),
    ],
)
def test_bad_thumbnail_requests(world: World, params, who, status, code) -> None:
    r = world.client.get(THUMB, params=params, headers=who)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code and str(world.tree.base) not in r.text


# ── who may watch what ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "who", "status", "code"),
    [
        ("cryo/alice_session", BOB, 403, "access_denied"),  # a root of another group
        ("../outside", ALICE, 403, "access_denied"),
        ("/etc", ALICE, 403, "access_denied"),
        ("cryo/nowhere", ALICE, 404, "not_found"),
        ("cryo/movie.mrc", ALICE, 400, "invalid_request"),  # a file is not a session
        ("", ALICE, 400, "invalid_request"),
    ],
)
def test_bad_watch_requests(world: World, path, who, status, code) -> None:
    r = world.client.post(WATCHES, params={"path": path}, headers=who)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code and str(world.tree.base) not in r.text
    assert world.client.get(WATCHES, headers=who).json()["watches"] == []


def test_requests_need_an_identity_and_known_parameters(world: World) -> None:
    assert world.client.get(WATCHES).status_code == 401
    assert world.client.post(WATCHES, params={"path": "public"}).status_code == 401
    watch = world.watch("public", ALICE)
    for url, params in (
        (WATCHES, {"all": 1}),
        (f"{WATCHES}/{watch}", {"verbose": 1}),
        (f"{WATCHES}/{watch}/items", {"since": -1}),
        (f"{WATCHES}/{watch}/items", {"limit": 0}),
        (f"{WATCHES}/{watch}/items", {"limit": 100000}),
        (f"{WATCHES}/{watch}/items", {"category": "everything"}),
        (f"{WATCHES}/{watch}/items", {"category": ""}),
        (f"{WATCHES}/{watch}/history", {"bucket": 0}),
        (f"{WATCHES}/not-a-number", {}),
    ):
        r = world.client.get(url, params=params, headers=ALICE)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request", (url, params, r.text)
    assert world.client.get(f"{WATCHES}/999999", headers=ALICE).status_code == 404


def test_losing_access_to_a_root_ends_access_to_its_index(world: World) -> None:
    sim = sim_in(world, "cryo/alice_session", 9)
    watch = world.watch("cryo/alice_session", CAROL)
    world.run([sim], 30)
    assert world.items(watch, CAROL)["items"]
    moved = {"X-Forwarded-User": "carol", "X-Forwarded-Groups": "lm"}  # carol left the cryo-EM group
    for url in (f"{WATCHES}/{watch}", f"{WATCHES}/{watch}/items", f"{WATCHES}/{watch}/history"):
        r = world.client.get(url, headers=moved)
        assert r.status_code == 403 and r.json()["error"]["code"] == "access_denied"
    assert (
        world.client.delete(f"{WATCHES}/{watch}", headers=moved).status_code == 200
    )  # she may still stop it


def test_a_user_cannot_watch_without_limit(world: World) -> None:
    for n in range(8):
        (world.tree.base / f"public/s{n}").mkdir()
        world.watch(f"public/s{n}", ALICE)
    (world.tree.base / "public/s8").mkdir()
    r = world.client.post(WATCHES, params={"path": "public/s8"}, headers=ALICE)
    assert r.status_code == 400 and "stop one first" in r.json()["error"]["message"]
    assert (
        world.client.post(WATCHES, params={"path": "public/s3"}, headers=ALICE).status_code == 200
    )  # already hers
    assert (
        world.client.post(WATCHES, params={"path": "public/s8"}, headers=BOB).status_code == 200
    )  # Bob has his own count
    first = world.client.get(WATCHES, headers=ALICE).json()["watches"][-1]["id"]
    world.client.delete(f"{WATCHES}/{first}", headers=ALICE)
    assert world.client.post(WATCHES, params={"path": "public/s8"}, headers=ALICE).status_code == 200


def index_counts(world: World) -> dict[str, int]:
    db = world.live.index.connection()
    tables = ("roots", "watches", "dirs", "exposures", "files")
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_a_forgotten_watch_takes_its_index_with_it(world: World) -> None:
    """Regression: nothing ever left the index: every directory ever watched stayed in it."""
    sim = sim_in(world, "public/shared", 30, pause_probability=0.0)
    alice = world.watch("public/shared", ALICE)
    bob = world.watch("public/shared", BOB)
    other = world.watch("public/bob_session", BOB)
    world.run([sim], 60)
    full = index_counts(world)
    assert full["roots"] == 2 and full["exposures"] > 5 and full["files"] > 20

    # stopping keeps everything: the session can still be looked at, and started again
    assert world.client.delete(f"{WATCHES}/{alice}", headers=ALICE).json()["forgotten"] is False
    assert index_counts(world) == full
    assert world.items(alice, ALICE)["items"]
    # forgetting removes the watch; the directory stays indexed for Bob, who still watches it
    gone = world.client.delete(f"{WATCHES}/{alice}", params={"forget": "true"}, headers=ALICE)
    assert gone.status_code == 200 and gone.json()["forgotten"] is True and gone.json()["id"] == alice
    assert world.client.get(WATCHES, headers=ALICE).json()["watches"] == []
    assert world.client.get(f"{WATCHES}/{alice}", headers=ALICE).status_code == 404
    assert index_counts(world) == {**full, "watches": 2}
    assert (
        world.client.delete(f"{WATCHES}/{bob}", params={"forget": "true"}, headers=ALICE).status_code == 404
    )

    # the last watch on the directory goes: so does what was indexed of it, and only of it
    assert world.client.delete(f"{WATCHES}/{bob}", params={"forget": "true"}, headers=BOB).status_code == 200
    assert index_counts(world) == {"roots": 1, "watches": 1, "dirs": 1, "exposures": 0, "files": 0}
    assert [w["id"] for w in world.client.get(WATCHES, headers=BOB).json()["watches"]] == [other]
    again = world.watch("public/shared", BOB)  # and watching it again starts from nothing, and works
    world.run([sim], 30)
    assert len(world.items(again, BOB)["items"]) >= full["exposures"]
    bad = world.client.delete(f"{WATCHES}/{again}", params={"forget": "perhaps"}, headers=BOB)
    assert bad.status_code == 400


def test_only_the_last_stopped_watches_are_kept(world: World) -> None:
    """Regression: stopped watches, and the index of their directories, piled up without limit."""
    from imagebio.live.service import MAX_STOPPED_WATCHES

    (world.tree.base / "public/s0").mkdir()
    kept_by_bob = world.watch("public/s0", BOB)
    ids = []
    for n in range(MAX_STOPPED_WATCHES + 3):
        (world.tree.base / f"public/s{n}").mkdir(exist_ok=True)
        (world.tree.base / f"public/s{n}/a.mrc").write_bytes(mrc_bytes(np.zeros((8, 8), np.float32), 1.0))
        ids.append(world.watch(f"public/s{n}", ALICE))
        world.run([], 5)
        world.client.delete(f"{WATCHES}/{ids[-1]}", headers=ALICE)
    listed = [w["id"] for w in world.client.get(WATCHES, headers=ALICE).json()["watches"]]
    assert sorted(listed) == ids[3:]  # the three oldest were forgotten
    counts = index_counts(world)
    # their directories left the index, except the one somebody else still watches
    assert counts["roots"] == MAX_STOPPED_WATCHES + 1 and counts["exposures"] == MAX_STOPPED_WATCHES + 1
    assert world.items(kept_by_bob, BOB)["items"]


@pytest.mark.parametrize(
    "url",
    [
        f"{WATCHES}/99999999999999999999",
        f"{WATCHES}/99999999999999999999/items",
        f"{WATCHES}/99999999999999999999/alerts",
        f"{WATCHES}/{{watch}}/items?since=99999999999999999999",
        f"{WATCHES}/0",
        f"{WATCHES}/-3/history",
    ],
)
def test_numbers_no_index_can_hold_are_refused(world: World, url: str) -> None:
    """Regression: an integer beyond 64 bits reached SQLite and came back as a 500."""
    watch = world.watch("public/shared", ALICE)
    r = world.client.get(url.format(watch=watch), headers=ALICE)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    r = world.client.delete(f"{WATCHES}/99999999999999999999", headers=ALICE)
    assert r.status_code == 400
    assert world.client.get(f"{WATCHES}/{2**63 - 1}", headers=ALICE).status_code == 404  # fits: unknown


def test_a_busy_index_asks_to_come_back(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a request that had to write while a scanner held the index for long waited
    thirty seconds and got "500 internal error"."""
    monkeypatch.setattr("imagebio.live.index._BUSY_TIMEOUT_S", 0.2)
    watch = world.watch("public/shared", ALICE)
    writer = sqlite3.connect(world.state / "live.sqlite", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")  # somebody is in the middle of a long write
    try:
        r = world.client.post(WATCHES, params={"path": "public/bob_session"}, headers=ALICE)
        assert r.status_code == 503 and r.json()["error"]["code"] == "live_index_busy"
        assert r.headers["retry-after"] == "5" and "sqlite" not in r.text.lower()
        assert world.client.delete(f"{WATCHES}/{watch}", headers=ALICE).status_code == 503
        assert world.summary(watch, ALICE)["watch"]["active"] is True  # reading goes on meanwhile
        assert world.items(watch, ALICE)["items"] == []
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert world.client.post(WATCHES, params={"path": "public/bob_session"}, headers=ALICE).status_code == 200


def test_the_same_host_on_another_port_is_another_site(world: World, tmp_path: Path) -> None:
    """Regression: the Origin of a request was compared by host name only: any other program
    listening on this computer (http://localhost:9999) could start and stop watches."""

    def start(client, headers: dict[str, str]):
        return client.post(WATCHES, params={"path": "public/shared"}, headers=headers)

    with make_client(build_tree(tmp_path / "standalone")) as alone:  # Host: localhost, plain http
        (tmp_path / "standalone/public/shared").mkdir()
        for origin in (
            "http://localhost:9999",
            "https://localhost",
            "http://localhost.:8080",
            "http://[::1]",
        ):
            r = start(alone, {"Origin": origin})
            assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site_request", origin
        for origin in ("http://localhost", "http://LOCALHOST:80", "http://localhost."):
            assert start(alone, {"Origin": origin}).status_code == 200, origin
        # the port of the application itself, when it is not the default one
        assert start(alone, {"Origin": "http://localhost:8000", "Host": "localhost:8000"}).status_code == 200
        assert start(alone, {"Origin": "http://localhost", "Host": "localhost:8000"}).status_code == 403
        # in standalone mode nobody is trusted to say what the application is called
        forged = {
            "Origin": "https://evil.example",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
        }
        assert start(alone, forged).status_code == 403

    # behind the proxy: the Host it passes on, or what it says the outside calls the application
    for headers, status in (
        ({"Origin": "http://localhost:9999"}, 403),
        ({"Origin": "https://localhost:8443"}, 403),
        ({"Origin": "https://localhost"}, 200),  # the proxy does not say the scheme: either one
        ({"Origin": "https://localhost", "X-Forwarded-Proto": "http"}, 403),
        ({"Origin": "https://localhost", "X-Forwarded-Proto": "https"}, 200),
        ({"Origin": "https://imagebio.lab.example"}, 403),
        ({"Origin": "https://imagebio.lab.example", "X-Forwarded-Host": "imagebio.lab.example"}, 200),
        ({"Origin": "https://imagebio.lab.example:8443", "X-Forwarded-Host": "imagebio.lab.example"}, 403),
        (
            {"Origin": "https://imagebio.lab.example:8443", "X-Forwarded-Host": "imagebio.lab.example:8443"},
            200,
        ),
    ):
        assert start(world.client, {**ALICE, **headers}).status_code == status, headers


def test_another_site_cannot_start_or_stop_a_watch(world: World) -> None:
    """The identity comes from the proxy, not from a secret of the page: a request that changes
    something must come from the application itself."""
    watch = world.watch("public", ALICE)
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost.evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site", "Origin": "http://localhost"},
    ):
        for request in (
            lambda h: world.client.post(WATCHES, params={"path": "public/shared"}, headers={**ALICE, **h}),
            lambda h: world.client.delete(f"{WATCHES}/{watch}", headers={**ALICE, **h}),
        ):
            r = request(headers)
            assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site_request", headers
    assert [w["active"] for w in world.client.get(WATCHES, headers=ALICE).json()["watches"]] == [True]
    # the application itself, and a script that is not a browser
    same = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
    assert (
        world.client.post(WATCHES, params={"path": "public/shared"}, headers={**ALICE, **same}).status_code
        == 200
    )
    assert world.client.delete(f"{WATCHES}/{watch}", headers=ALICE).status_code == 200
    # reading is not concerned
    assert world.client.get(WATCHES, headers={**ALICE, "Origin": "https://evil.example"}).status_code == 200


# ── acceptance: a restart loses nothing ───────────────────────────────────────


def test_watches_and_their_index_survive_a_restart(world: World) -> None:
    sim = sim_in(world, "cryo/alice_session", 10)
    watch = world.watch("cryo/alice_session", ALICE)
    stopped = world.watch("public/shared", ALICE)
    world.client.delete(f"{WATCHES}/{stopped}", headers=ALICE)
    world.run([sim], 90)
    before = world.items(watch, ALICE, limit=5000)
    assert before["items"]

    world.restart()
    listed = {w["id"]: w["active"] for w in world.client.get(WATCHES, headers=ALICE).json()["watches"]}
    assert listed == {watch: True, stopped: False}
    assert world.items(watch, ALICE, limit=5000) == before  # as it was
    for _ in range(60):  # the microscope went on while the server was down
        world.now += 1
        sim.step(world.now)
    sim.stop()
    world.run([sim], 60)
    after = world.items(watch, ALICE, limit=5000)["items"]
    assert {i["name"] for i in after} == set(sim.exposures)
    assert all(i["state"] == "complete" for i in after)
    assert world.items(watch, ALICE, since=before["seq"])["items"]  # and a client catches up from its number


def test_background_scanning_starts_and_stops_with_the_watches(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "data")
    session = tree.base / "public/session"
    session.mkdir()
    (session / "a.mrc").write_bytes(mrc_bytes(np.zeros((8, 8), np.float32), 1.0))
    with make_client(tree, state_dir=tmp_path / "state", live_interval_s=0.5) as client:
        watch = client.post(WATCHES, params={"path": "public/session"}).json()["id"]
        deadline = time.monotonic() + 10
        items: list[dict] = []
        while not items and time.monotonic() < deadline:
            time.sleep(0.1)
            items = client.get(f"{WATCHES}/{watch}/items").json()["items"]
        assert [i["name"] for i in items] == ["a"]  # found without anybody asking for a scan
        live = client.app.state.live
        assert client.get(f"{WATCHES}/{watch}").json()["scanner"]["running"] is True
        thread = next(iter(live._threads.values()))[0]
        client.delete(f"{WATCHES}/{watch}")
        thread.join(timeout=10)
        assert not thread.is_alive() and not live._threads
        assert client.get(f"{WATCHES}/{watch}").json()["scanner"]["running"] is False
        client.post(WATCHES, params={"path": "public/session"})
        threads = [t for t, _ in live._threads.values()]
    for t in threads:  # leaving the server stops the scanners
        t.join(timeout=10)
        assert not t.is_alive()


def test_a_directory_never_has_two_scanners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: stopping a watch and starting it again while a cycle was running left the
    old scanner finishing its cycle next to the new one, both writing the same directory."""
    from imagebio.live.scanner import CycleReport

    gate = threading.Event()
    inside = threading.Event()
    made: list[object] = []
    together = [0, 0]  # scanners in a cycle now, and the most there ever were
    guard = threading.Lock()

    class SlowScanner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            made.append(self)

        def cycle(self, now: float) -> CycleReport:
            with guard:
                together[0] += 1
                together[1] = max(together)
            inside.set()
            gate.wait(20)
            with guard:
                together[0] -= 1
            return CycleReport()

    monkeypatch.setattr("imagebio.live.service.Scanner", SlowScanner)
    tree = build_tree(tmp_path / "data")
    (tree.base / "public/session").mkdir()
    with make_client(tree, state_dir=tmp_path / "state", live_interval_s=0.5) as client:
        live = client.app.state.live
        watch = client.post(WATCHES, params={"path": "public/session"}).json()["id"]
        assert inside.wait(10)  # the first scanner is in the middle of a cycle
        client.delete(f"{WATCHES}/{watch}")
        assert client.post(WATCHES, params={"path": "public/session"}).json()["id"] == watch
        time.sleep(0.5)
        assert len(made) == 1  # the new one waits for the old one to be done
        inside.clear()
        gate.set()
        assert inside.wait(10)  # then takes over
        deadline = time.monotonic() + 10
        while len(made) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(made) == 2 and together[1] == 1
        assert [t.is_alive() for t, _ in live._threads.values()] == [True]


# ── alerts ────────────────────────────────────────────────────────────────────

CHANNELS = (
    AlertChannel("phone", "ntfy", "https://ntfy.example/a-secret-topic"),
    AlertChannel("hook", "webhook", "https://hooks.example/T0P/SECRET"),
    AlertChannel("team", "email", to=("cryoem@lab.example",)),
)


@pytest.fixture
def alerting(tmp_path: Path):
    """A world whose server has three alert channels; what it sends lands in ``world.sent``."""
    tree = build_tree(tmp_path / "data")
    for directory in ("cryo/alice_session", "public/shared"):
        (tree.base / directory).mkdir()
        os.utime(tree.base / directory, (BASE, BASE))
    w = World(tree, tmp_path / "state", alert_channels=CHANNELS, smtp=SmtpConfig("smtp.lab.example"))
    yield w
    w.client.__exit__(None, None, None)


def set_alerts(world: World, watch: int, who: dict[str, str], **params: object):
    return world.client.put(f"{WATCHES}/{watch}/alerts", params=params, headers=who)


def last_arrival(world: World) -> float:
    row = (
        world.live.index.connection()
        .execute("SELECT MAX(completed_at) FROM exposures WHERE arrived = 1")
        .fetchone()
    )
    return float(row[0])


def run_until(world: World, sims: list[MicroscopeSim], seconds: int, wanted: int) -> float:
    """Advance until ``wanted`` notices were sent; returns the time of the cycle that sent the last."""
    for _ in range(seconds):
        world.run(sims, 1)
        if len(world.sent) >= wanted:
            return world.now - 1  # the cycle ran at the second just simulated
    raise AssertionError(f"{len(world.sent)} notice(s) after {seconds} s, {wanted} expected")


def test_an_alert_leaves_within_two_scan_cycles_of_its_threshold(alerting: World) -> None:
    """Acceptance (M3): alerts leave in less than two scan cycles."""
    sim = sim_in(alerting, "cryo/alice_session", 21, pause_probability=0.0)
    watch = alerting.watch("cryo/alice_session", ALICE)
    assert set_alerts(alerting, watch, ALICE, channels="phone", no_image=2).status_code == 200
    alerting.run([sim], 60)
    sim.stop()
    alerting.run([sim], 30)  # the last images finish and are accepted
    assert alerting.sent == []  # images are arriving: nothing to say
    threshold = last_arrival(alerting) + 120

    sent_at = run_until(alerting, [sim], 300, 1)
    assert threshold <= sent_at <= threshold + 2 * 5  # not before, and within two cycles of five seconds
    channel, notice = alerting.sent[0]
    assert channel == "phone" and notice.condition == "no_image" and notice.firing
    assert notice.title == "imageBio : plus d'image"
    assert notice.body.startswith("cryo/alice_session — Aucune image depuis 2 min (dernière image à ")
    assert notice.body.endswith(f"{len(sim.exposures)} images dans la session.")
    assert str(alerting.tree.base) not in notice.body
    assert alerting.summary(watch, ALICE)["alerts"] == ["no_image"]

    alerting.run([sim], 600)  # ten more minutes of silence: said once, not at every cycle
    assert len(alerting.sent) == 1

    # the microscope starts again: one notice says so, within two cycles of the first image accepted
    again = sim_in(alerting, "cryo/alice_session", 22, pause_probability=0.0)
    before = last_arrival(alerting)
    resolved_at = run_until(alerting, [again], 120, 2)
    assert last_arrival(alerting) > before
    assert resolved_at <= last_arrival(alerting) + 2 * 5
    channel, notice = alerting.sent[1]
    assert channel == "phone" and notice.condition == "no_image" and not notice.firing
    assert notice.title == "imageBio : les images arrivent de nouveau"
    assert alerting.summary(watch, ALICE)["alerts"] == []


def fake_disk(world: World, free_bytes: float) -> dict[str, float]:
    """Make the server believe its volume has 1000 GB, of which ``free["bytes"]`` are free."""
    free = {"bytes": free_bytes}
    world.live.disk_usage = lambda path: type(
        "Usage", (), {"total": 1000e9, "free": free["bytes"], "used": 0}
    )()
    return free


def test_disk_and_rate_alerts(alerting: World) -> None:
    sim = sim_in(alerting, "cryo/alice_session", 23, pause_probability=0.0)
    watch = alerting.watch("cryo/alice_session", ALICE)
    free = fake_disk(alerting, 500e9)
    set_alerts(alerting, watch, ALICE, channels="phone,hook", disk_free=5, rate_drop=50)
    alerting.run([sim], 420)
    assert alerting.sent == []
    assert alerting.summary(watch, ALICE)["disk"] == {"total": 1000e9, "free": 500e9, "percent": 50.0}

    free["bytes"] = 32e9  # 3.2 % left
    alerting.run([sim], 10)
    assert [(c, n.condition, n.firing) for c, n in alerting.sent] == [
        ("phone", "disk", True),
        ("hook", "disk", True),
    ]
    assert "Il reste 3.2 % d'espace libre (32 Go)." in alerting.sent[0][1].body
    free["bytes"] = 400e9
    alerting.run([sim], 110)  # room is back, but not for long enough to say so
    assert len(alerting.sent) == 2 and alerting.summary(watch, ALICE)["alerts"] == ["disk"]
    alerting.run([sim], 20)
    assert [(c, n.firing) for c, n in alerting.sent[2:]] == [("phone", False), ("hook", False)]
    assert alerting.sent[2][1].title == "imageBio : de la place est revenue sur le disque"
    assert alerting.summary(watch, ALICE)["alerts"] == []

    # the microscope stops: the rate over five minutes falls far under the hour's, and stays there
    del alerting.sent[:]
    sim.stop()
    alerting.run([sim], 280)
    assert alerting.sent == []
    alerting.run([sim], 160)
    assert [(c, n.condition, n.firing) for c, n in alerting.sent] == [
        ("phone", "rate_drop", True),
        ("hook", "rate_drop", True),
    ]
    assert "images/h sur 5 minutes, contre" in alerting.sent[0][1].body


def test_a_rate_is_back_when_images_arrive_not_when_the_hour_has_emptied(alerting: World) -> None:
    """Regression: an hour after the microscope stopped, the hour held no image either, the
    comparison no longer applied, and "le débit est revenu" was sent about a dead session."""
    sim = sim_in(alerting, "cryo/alice_session", 24, pause_probability=0.0)
    watch = alerting.watch("cryo/alice_session", ALICE)
    set_alerts(alerting, watch, ALICE, channels="phone", rate_drop=50)
    alerting.run([sim], 420)
    sim.stop()
    alerting.run([sim], 600)
    assert [(n.condition, n.firing) for _, n in alerting.sent] == [("rate_drop", True)]
    alerting.run([], 2 * 3600)  # nothing comes: nothing "came back"
    assert len(alerting.sent) == 1 and alerting.summary(watch, ALICE)["alerts"] == ["rate_drop"]
    again = sim_in(alerting, "cryo/alice_session", 25, pause_probability=0.0)
    alerting.run([again], 300)
    assert [(n.condition, n.firing) for _, n in alerting.sent[1:]] == [("rate_drop", False)]
    assert alerting.sent[1][1].title == "imageBio : le débit est revenu"


def test_a_value_that_hovers_around_its_threshold_is_told_once(alerting: World) -> None:
    """Regression: a disk going back and forth across its threshold sent two notices at every
    scan cycle. Leaving a condition takes a clear margin, and two minutes."""
    watch = alerting.watch("cryo/alice_session", ALICE)
    free = fake_disk(alerting, 500e9)
    set_alerts(alerting, watch, ALICE, channels="phone", disk_free=5)
    for second in range(0, 900, 5):
        free["bytes"] = 49e9 if second % 10 == 0 else 51e9  # 4.9 %, 5.1 %, 4.9 %, …
        alerting.run([], 5)
    assert [(n.condition, n.firing) for _, n in alerting.sent] == [("disk", True)]
    free["bytes"] = 53e9  # above the threshold, not clearly: still not resolved
    alerting.run([], 300)
    assert len(alerting.sent) == 1
    for second in range(0, 300, 5):  # clearly above, but only every other cycle
        free["bytes"] = 80e9 if second % 10 == 0 else 49e9
        alerting.run([], 5)
    assert len(alerting.sent) == 1
    free["bytes"] = 80e9
    alerting.run([], 130)
    assert [(n.condition, n.firing) for _, n in alerting.sent] == [("disk", True), ("disk", False)]


def test_a_notice_that_cannot_be_delivered_is_tried_again_then_given_up(alerting: World) -> None:
    watch = alerting.watch("cryo/alice_session", ALICE)
    set_alerts(alerting, watch, ALICE, channels="phone", no_image=1)
    alerting.failures = 2
    alerting.run([], 65)  # the threshold is crossed at 60 s; cycles at 60 and 65 fail to deliver
    assert alerting.sent == []
    state = alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json()["firing"]
    assert state == [
        {
            "condition": "no_image",
            "since": BASE + 60,
            "notified": False,
            "error": "channel 'phone': ConnectionRefusedError",
        }
    ]
    alerting.run([], 10)  # the third attempt goes through
    assert len(alerting.sent) == 1
    state = alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json()["firing"]
    assert state[0]["notified"] is True and state[0]["error"] is None
    alerting.run([], 60)
    assert len(alerting.sent) == 1

    # a channel that never answers is not hammered for ever
    other = alerting.watch("public/shared", ALICE)
    set_alerts(alerting, other, ALICE, channels="hook", no_image=1)
    alerting.failures = 10**6
    alerting.run([], 600)
    attempts = (
        alerting.live.index.connection()
        .execute("SELECT attempts FROM alert_states WHERE watch_id = ?", (other,))
        .fetchone()[0]
    )
    assert attempts == 5 and alerting.failures == 10**6 - 5


def test_a_failing_channel_does_not_make_the_others_hear_it_again(alerting: World) -> None:
    """Regression: with one channel down, the notice was sent again to the working ones at
    each of the five attempts, and the "resolved" notice never left."""
    watch = alerting.watch("cryo/alice_session", ALICE)
    free = fake_disk(alerting, 30e9)
    set_alerts(alerting, watch, ALICE, channels="phone,hook,team", disk_free=5)
    alerting.broken = {"phone"}
    alerting.run([], 120)
    assert [(c, n.firing) for c, n in alerting.sent] == [("hook", True), ("team", True)]  # once each
    assert alerting.refused == 5  # the one that fails is tried again, then left alone
    state = alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json()["firing"]
    assert state == [
        {
            "condition": "disk",
            "since": BASE,
            "notified": False,
            "error": "channel 'phone': ConnectionRefusedError",
        }
    ]

    # it ends: those who were told are told, the other never heard of it
    alerting.broken = {"hook"}
    free["bytes"] = 500e9
    alerting.run([], 130)
    assert [(c, n.firing) for c, n in alerting.sent[2:]] == [("team", False)]
    assert alerting.summary(watch, ALICE)["alerts"] == []
    # and a "resolved" that could not be delivered is not lost: it leaves when the channel is back
    alerting.broken = set()
    alerting.run([], 10)
    assert [(c, n.firing) for c, n in alerting.sent[3:]] == [("hook", False)]
    alerting.run([], 60)
    assert len(alerting.sent) == 4
    assert alerting.live.index.connection().execute("SELECT COUNT(*) FROM alert_states").fetchone()[0] == 0


def test_a_watch_started_again_counts_silence_from_then(alerting: World) -> None:
    """Regression: a watch stopped for the night kept the date its alerts were set, and
    announced "no image for 10 h" the second it was started again."""
    watch = alerting.watch("cryo/alice_session", ALICE)
    set_alerts(alerting, watch, ALICE, channels="phone", no_image=5)
    alerting.client.delete(f"{WATCHES}/{watch}", headers=ALICE)
    alerting.now += 10 * 3600
    assert alerting.watch("cryo/alice_session", ALICE) == watch
    assert alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json()["set_at"] == alerting.now
    restarted = alerting.now
    alerting.run([], 290)
    assert alerting.sent == []
    sent_at = run_until(alerting, [], 30, 1)
    assert restarted + 300 <= sent_at <= restarted + 310
    assert "Aucune image depuis 5 min" in alerting.sent[0][1].body


def test_alerts_stop_for_a_watch_nobody_looks_at_any_more(alerting: World) -> None:
    """Regression: rules kept sending for ever, even to somebody who no longer opened the
    session, or who had lost the right to read it. They last a week past the last visit."""
    watch = alerting.watch("public/shared", ALICE)
    free = fake_disk(alerting, 500e9)
    set_alerts(alerting, watch, ALICE, channels="phone", disk_free=5)
    for _ in range(6):  # six days, with a look at the page every day
        alerting.now += 86400
        alerting.summary(watch, ALICE)
        alerting.run([], 5)
    alerting.now += 6 * 86400  # six more days: the last visit is recent enough
    free["bytes"] = 10e9
    alerting.run([], 10)
    assert [(n.condition, n.firing) for _, n in alerting.sent] == [("disk", True)]
    assert set_alerts(alerting, watch, ALICE, channels="phone", disk_free=5).status_code == 200

    alerting.restart()  # the last visit is remembered across a restart
    fake_disk(alerting, 10e9)
    del alerting.sent[:]
    alerting.now += 8 * 86400  # more than a week without anybody
    alerting.run([], 30)
    assert alerting.sent == []
    alerting.summary(watch, ALICE)  # she comes back
    alerting.run([], 10)
    assert [(n.condition, n.firing) for _, n in alerting.sent] == [("disk", True)]

    # a visit that is refused is not a visit
    del alerting.sent[:]
    set_alerts(alerting, watch, ALICE, channels="phone", disk_free=5)
    alerting.now += 8 * 86400
    assert alerting.client.get(f"{WATCHES}/{watch}", headers=BOB).status_code == 404
    alerting.run([], 30)
    assert alerting.sent == []


def test_rules_are_per_watch_and_survive_a_restart(alerting: World) -> None:
    alice = alerting.watch("public/shared", ALICE)
    bob = alerting.watch("public/shared", BOB)
    set_alerts(alerting, alice, ALICE, channels="phone", no_image=1)
    set_alerts(alerting, bob, BOB, channels="team", no_image=3)
    alerting.run([], 70)
    assert [(c, n.condition) for c, n in alerting.sent] == [("phone", "no_image")]  # Alice's rule only
    alerting.restart()
    alerting.run([], 60)
    assert len(alerting.sent) == 1  # said before the restart: not said again
    alerting.run([], 60)
    assert [(c, n.condition) for c, n in alerting.sent] == [("phone", "no_image"), ("team", "no_image")]

    # a stopped watch says nothing more, and has nothing left to resolve
    alerting.client.delete(f"{WATCHES}/{alice}", headers=ALICE)
    assert alerting.client.get(f"{WATCHES}/{alice}/alerts", headers=ALICE).json()["firing"] == []
    session = alerting.tree.base / "public/shared"
    (session / "new.mrc").write_bytes(mrc_bytes(np.zeros((8, 8), np.float32), 1.0))
    for path in (session / "new.mrc", session):
        os.utime(path, (alerting.now, alerting.now))
    alerting.run([], 30)
    assert [(c, n.firing) for c, n in alerting.sent[2:]] == [
        ("team", False)
    ]  # resolved for Bob, silence for Alice


def test_the_scanner_failing_is_an_alert_of_its_own(alerting: World) -> None:
    watch = alerting.watch("cryo/alice_session", ALICE)
    set_alerts(alerting, watch, ALICE, channels="phone")  # no threshold: only the monitor itself
    alerting.run([], 10)
    assert alerting.sent == []
    with alerting.live.index.write() as db:
        db.execute("UPDATE roots SET error = 'internal error (OSError)'")
    assert [n.condition for n in alerting.live.check_alerts(1, alerting.now)] == ["scanner"]
    assert alerting.sent[0][1].title == "imageBio : la surveillance échoue"
    assert "internal error (OSError)" in alerting.sent[0][1].body
    alerting.run([], 5)  # a cycle that works clears the error
    assert alerting.summary(watch, ALICE)["scanner"]["error"] is None
    assert len(alerting.sent) == 1  # a scanner that fails now and then is not told at every cycle
    alerting.run([], 60)
    assert [(n.condition, n.firing) for _, n in alerting.sent] == [("scanner", True), ("scanner", False)]


def test_alert_rules_through_the_api(alerting: World) -> None:
    channels = alerting.client.get("/api/live/alerts/channels", headers=ALICE)
    assert channels.json() == {
        "channels": [
            {"name": "phone", "kind": "ntfy"},
            {"name": "hook", "kind": "webhook"},
            {"name": "team", "kind": "email"},
        ]
    }
    watch = alerting.watch("cryo/alice_session", ALICE)
    empty = alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json()
    assert empty["rules"] == {
        "channels": [],
        "no_image_min": None,
        "disk_free_pct": None,
        "rate_drop_pct": None,
    }
    assert empty["firing"] == [] and empty["set_at"] is None

    r = set_alerts(alerting, watch, ALICE, channels="phone, team", no_image=10, disk_free=5, rate_drop=40)
    assert r.status_code == 200
    body = r.json()
    assert body["rules"] == {
        "channels": ["phone", "team"],
        "no_image_min": 10.0,
        "disk_free_pct": 5.0,
        "rate_drop_pct": 40.0,
    }
    assert body["set_at"] == alerting.now
    assert alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json() == body
    for text in (channels.text, r.text):  # names only: an address is a secret
        assert "ntfy.example" not in text and "SECRET" not in text and "lab.example" not in text

    for params in (
        {"channels": "nowhere"},
        {"channels": "phone", "no_image": 0},
        {"channels": "phone", "no_image": 100000},
        {"channels": "phone", "disk_free": 80},
        {"channels": "phone", "rate_drop": 100},
        {"channels": "phone", "no_image": "soon"},
        {"channels": "phone", "url": "https://evil.example"},  # users do not give addresses
    ):
        bad = set_alerts(alerting, watch, ALICE, **params)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_request", params
    assert alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=ALICE).json() == body  # unchanged

    # somebody else's watch does not exist; another site cannot change the rules
    assert set_alerts(alerting, watch, BOB, channels="phone").status_code == 404
    assert alerting.client.get(f"{WATCHES}/{watch}/alerts", headers=BOB).status_code == 404
    evil = alerting.client.put(
        f"{WATCHES}/{watch}/alerts",
        params={"channels": ""},
        headers={**ALICE, "Origin": "https://evil.example"},
    )
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "cross_site_request"

    assert set_alerts(alerting, watch, ALICE).json()["rules"]["channels"] == []  # nothing: alerts off


def test_a_test_notice_tells_which_channels_work(alerting: World) -> None:
    watch = alerting.watch("cryo/alice_session", ALICE)
    url = f"{WATCHES}/{watch}/alerts/test"
    none = alerting.client.post(url, headers=ALICE)
    assert none.status_code == 400 and "choose at least one" in none.json()["error"]["message"]
    set_alerts(alerting, watch, ALICE, channels="phone,hook")
    alerting.failures = 1
    r = alerting.client.post(url, headers=ALICE)
    assert r.status_code == 200
    assert r.json() == {
        "results": [
            {"channel": "phone", "delivered": False, "error": "channel 'phone': ConnectionRefusedError"},
            {"channel": "hook", "delivered": True, "error": None},
        ]
    }
    assert [(c, n.condition, n.title) for c, n in alerting.sent] == [
        ("hook", "test", "imageBio : essai des alertes")
    ]
    assert "cryo/alice_session" in alerting.sent[0][1].body
    again = alerting.client.post(url, headers=ALICE)
    assert (
        again.status_code == 400 and "wait" in again.json()["error"]["message"]
    )  # not a way to flood a phone
    alerting.now += 11
    assert alerting.client.post(url, headers=ALICE).status_code == 200
    assert alerting.client.post(url, headers=BOB).status_code == 404
    # regression: the limit was per watch, so eight watches sent eight times the notices at once
    other = alerting.watch("public/shared", ALICE)
    set_alerts(alerting, other, ALICE, channels="phone")
    flood = alerting.client.post(f"{WATCHES}/{other}/alerts/test", headers=ALICE)
    assert flood.status_code == 400 and "wait" in flood.json()["error"]["message"]
    bob = alerting.watch("public/shared", BOB)  # somebody else has their own limit
    set_alerts(alerting, bob, BOB, channels="hook")
    assert alerting.client.post(f"{WATCHES}/{bob}/alerts/test", headers=BOB).status_code == 200
    alerting.now += 11
    assert alerting.client.post(url, headers={**ALICE, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_alerts_are_judged_by_the_background_scanner_too(tmp_path: Path) -> None:
    """The real loop: a scanning thread, and a clock that jumps two minutes ahead."""
    tree = build_tree(tmp_path / "data")
    session = tree.base / "public/session"
    session.mkdir()
    sent: list[tuple[str, Notice]] = []

    class Recorder:
        def send(self, channel: AlertChannel, notice: Notice) -> None:
            sent.append((channel.name, notice))

    shift = [0.0]
    with make_client(
        tree, state_dir=tmp_path / "state", live_interval_s=0.5, alert_channels=CHANNELS[:1]
    ) as client:
        live = client.app.state.live
        live.notifier = Recorder()
        live.clock = lambda: time.time() + shift[0]
        watch = client.post(WATCHES, params={"path": "public/session"}).json()["id"]
        assert (
            client.put(f"{WATCHES}/{watch}/alerts", params={"channels": "phone", "no_image": 1}).status_code
            == 200
        )
        time.sleep(1.5)  # a few cycles: nothing to say yet
        assert sent == []
        shift[0] = 120.0
        deadline = time.monotonic() + 10
        while not sent and time.monotonic() < deadline:
            time.sleep(0.1)
        assert [(c, n.condition, n.firing) for c, n in sent] == [("phone", "no_image", True)]
        assert client.get(f"{WATCHES}/{watch}").json()["alerts"] == ["no_image"]
