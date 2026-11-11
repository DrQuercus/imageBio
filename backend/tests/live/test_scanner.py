"""The scanner: what it looks at, what it records, and what survives a restart."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import tifffile

from fixtures.live import BASE, Watch
from imagebio.live.scanner import ScanSettings
from imagebio.live.simulator import MicroscopeSim, SimSettings, mrc_bytes

IMAGE = mrc_bytes(np.arange(64, dtype=np.float32).reshape(8, 8), 1.0)


def put(root: Path, rel: str, data: bytes, when: float) -> None:
    """Write a file as if at time ``when``: its date and its directory's follow."""
    path = root / rel
    made = []
    for parent in reversed(path.relative_to(root).parents[:-1]):
        if not (root / parent).is_dir():
            (root / parent).mkdir()
            made.append(root / parent)
    new = not path.exists()
    path.write_bytes(data)
    os.utime(path, (when, when))
    for directory in made:
        os.utime(directory, (when, when))
        os.utime(directory.parent, (when, when))
    if new:
        os.utime(path.parent, (when, when))


def cycles(scanner, start: float, stop: float, step: float = 5.0):
    report = None
    now = start
    while now <= stop:
        report = scanner.cycle(now)
        assert report.error is None, report.error
        now += step
    return report


# ── acceptance: 50 000 files without looking at each one ──────────────────────


def fill_session(watch: Watch, when: float, squares: int = 50, per_square: int = 334) -> int:
    """An EPU tree of empty files, every directory last changed at ``when``; returns the number of files."""
    for s in range(squares):
        data = watch.root / f"Images-Disc1/GridSquare_{s}/Data"
        data.mkdir(parents=True)
        for n in range(per_square):
            base = (
                data
                / f"FoilHole_{s}_Data_{n}_{n}_20270115_{n // 3600 % 24:02d}{n // 60 % 60:02d}{n % 60:02d}"
            )
            for suffix in (".mrc", ".xml", "_Fractions.mrc"):
                path = f"{base}{suffix}"
                open(path, "wb").close()
                os.utime(path, (when, when))
        for directory in (data, data.parent):
            os.utime(directory, (when, when))
    os.utime(watch.root / "Images-Disc1", (when, when))
    return squares * per_square * 3


def test_fifty_thousand_files_are_indexed_from_directory_listings(watch: Watch) -> None:
    """Acceptance (M3): 50 000 files are indexed without scanning each file individually.
    A session that ended two days ago, and one square still being written."""
    total = fill_session(watch, BASE - 2 * 86400)
    assert total == 50_100
    put(
        watch.root,
        "Images-Disc1/GridSquare_live/Data/FoilHole_9_Data_1_1_20270115_075955.mrc",
        IMAGE,
        BASE - 5,
    )

    scanner = watch.scanner()
    report = scanner.cycle(BASE)
    assert report.error is None and report.new_files == total + 1
    n_dirs = 1 + 1 + 2 * 50 + 2  # root, Images-Disc1, a square and its Data, the live square
    assert watch.fs.listings == n_dirs
    # one stat per directory, two for the only recent file (found, then followed): not one per file
    assert watch.fs.stats == n_dirs + 2
    exposures = watch.exposures()
    assert len(exposures) == 50 * 334 + 1
    settled = [e for e in exposures.values() if e["state"] == "complete"]
    assert len(settled) == 50 * 334
    assert all(e["verified"] == 0 and e["primary_role"] == "image" and e["size"] is None for e in settled)
    assert all(
        e["time_source"] == "name" and e["time"] is not None for e in settled
    )  # ordered without a stat
    live = exposures["FoilHole_9_Data_1_1_20270115_075955"]
    assert live["state"] == "incoming" and live["size"] == len(IMAGE)
    assert len({e["seq"] for e in exposures.values()}) == len(
        exposures
    )  # committed in batches, numbered once

    # a quiet cycle looks at the directories, the recent file, and nothing else
    watch.fs.reset()
    report = scanner.cycle(BASE + 5)
    assert report.new_files == 0
    assert (
        watch.fs.stats == n_dirs + 1 and watch.fs.listings <= 3
    )  # only what changed recently is listed again

    # one new file: one directory listed again, one more stat
    put(
        watch.root,
        "Images-Disc1/GridSquare_7/Data/FoilHole_7_Data_999_999_20270115_080010.mrc",
        IMAGE,
        BASE + 10,
    )
    watch.fs.reset()
    report = scanner.cycle(BASE + 10)
    assert report.new_files == 1
    assert watch.fs.stats == n_dirs + 2 + 1  # directories, two files being followed, the stat of the new one
    assert watch.fs.listings <= 4

    # and nothing stays "just changed" for ever: half a minute later, no directory is listed at all
    cycles(scanner, BASE + 15, BASE + 55)
    watch.fs.reset()
    cycles(scanner, BASE + 60, BASE + 120)
    assert watch.fs.listings == 0 and watch.fs.stats == 13 * (n_dirs + 2)


def test_a_session_of_today_costs_one_look_per_file_once(watch: Watch) -> None:
    """Directories that changed within the day may hold a file still being written, which their
    own date does not tell: each file is looked at once. Those at rest are then left alone."""
    end = BASE - 3 * 3600  # the session ended three hours ago: one exposure every 6 s for 50 minutes
    data = watch.root / "Images-Disc1/GridSquare_1/Data"
    data.mkdir(parents=True)
    for n in range(500):
        when = end - 6 * (499 - n)
        for suffix in (".mrc", ".xml"):
            path = data / f"FoilHole_1_Data_{n}_{n}_20270115_04{n // 60:02d}{n % 60:02d}{suffix}"
            path.write_bytes(IMAGE if suffix == ".mrc" else b"<MicroscopeImage/>")
            os.utime(path, (when, when))
    for directory in (data, data.parent, data.parent.parent):
        os.utime(directory, (end, end))
    recent = sum(1 for n in range(500) if 6 * (499 - n) <= 900)  # the last quarter of an hour
    assert recent == 151

    scanner = watch.scanner()
    report = scanner.cycle(BASE)
    assert report.new_files == 1000
    assert watch.fs.stats == 4 + 1000 + 2 * recent  # directories, each file once, the recent ones followed
    exposures = watch.exposures()
    assert sum(1 for e in exposures.values() if e["state"] == "complete") == 500 - recent
    assert all(e["size"] == len(IMAGE) for e in exposures.values())  # looked at: their size is known
    cycles(scanner, BASE + 5, BASE + 15)  # the recent ones pass the completeness test
    exposures = watch.exposures()
    assert all(e["state"] == "complete" for e in exposures.values())
    assert sum(1 for e in exposures.values() if e["verified"]) == recent
    cycles(scanner, BASE + 20, BASE + 320)  # until what was just completed is no longer looked at again
    watch.fs.reset()
    cycles(scanner, BASE + 325, BASE + 370)
    assert watch.fs.stats == 10 * 4 and watch.fs.listings == 0  # and then only the directories


# ── acceptance: nothing is lost after a crash ─────────────────────────────────


def test_nothing_is_lost_when_the_server_dies(watch: Watch) -> None:
    """Acceptance (M3): after a brutal stop, the index is as it was, and what happened while
    the server was down is found."""
    sim = MicroscopeSim(watch.root, SimSettings(rate_per_hour=600, pause_probability=0.2, seed=5))
    scanner = watch.scanner()
    for second in range(0, 300):
        sim.step(BASE + second)
        if second % 5 == 0:
            scanner.cycle(BASE + second)
    before = watch.exposures()
    complete_before = {n for n, e in before.items() if e["state"] == "complete"}
    seq_before = watch.rows("SELECT seq FROM roots")[0]["seq"]
    assert len(complete_before) > 30 and scanner.pending_count > 0

    # the server dies: no shutdown, the scanner object is simply gone. A process that died in
    # the middle of a cycle leaves an open transaction, which the database discards.
    doomed = sqlite3.connect(watch.index._path, isolation_level=None)
    doomed.execute("BEGIN IMMEDIATE")
    doomed.execute("UPDATE exposures SET state = 'deleted'")
    doomed.close()  # never committed
    del scanner
    for second in range(300, 420):  # the microscope goes on for two minutes
        sim.step(BASE + second)
    sim.stop()

    reborn = watch.scanner()
    assert watch.exposures().keys() == before.keys()  # the index is as it was
    watch.fs.reset()
    for second in range(420, 520):
        sim.step(BASE + second)
        if second % 5 == 0:
            assert reborn.cycle(BASE + second).error is None
    after = watch.exposures()
    assert complete_before <= {n for n, e in after.items() if e["state"] == "complete"}
    assert {n for n, e in after.items() if e["category"] == "data"} == set(
        sim.exposures
    )  # none missed, none twice
    assert set(sim.completed) == watch.files("complete").keys() and not sim.partial()
    assert not watch.files("pending")
    assert watch.rows("SELECT seq FROM roots")[0]["seq"] > seq_before  # numbers go on, never back
    seqs = [e["seq"] for e in after.values()]
    assert len(set(seqs)) == len(seqs)
    # what was complete before the crash was not checked again, file by file
    assert all(after[n]["completed_at"] == before[n]["completed_at"] for n in complete_before)


def test_a_file_being_written_at_the_crash_is_not_trusted_on_restart(watch: Watch) -> None:
    put(watch.root, "a.mrc", IMAGE[:1500], BASE)
    first = watch.scanner()
    cycles(first, BASE, BASE + 5)
    assert watch.files("pending").keys() == {"a.mrc"}
    put(watch.root, "a.mrc", IMAGE, BASE + 6)  # finished while the server is down
    second = watch.scanner()
    second.cycle(BASE + 60)  # a minute later: the size has long been stable, but this scanner saw it once
    assert watch.files("pending").keys() == {"a.mrc"}
    second.cycle(BASE + 65)
    assert watch.files("pending").keys() == {"a.mrc"}  # twice, five seconds apart: not ten yet
    second.cycle(BASE + 70)
    assert watch.files("complete").keys() == {"a.mrc"}


# ── the life of files ─────────────────────────────────────────────────────────


def test_a_plain_directory_of_images_needs_no_metadata_file(watch: Watch) -> None:
    put(watch.root, "one.mrc", IMAGE, BASE)
    put(watch.root, "two.tif", b"", BASE)
    tifffile.imwrite(watch.root / "two.tif", np.zeros((16, 16), np.uint16))
    os.utime(watch.root / "two.tif", (BASE, BASE))
    put(watch.root, "three.jpg", b"\xff\xd8 not much of a picture \xff\xd9", BASE)
    put(watch.root, "notes.txt", b"hello", BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    exposures = watch.exposures()
    assert set(exposures) == {"one", "two", "three"}
    assert all(
        e["state"] == "complete" and e["category"] == "image" and e["verified"] == 1
        for e in exposures.values()
    )
    assert [exposures[n]["primary_role"] for n in ("one", "two", "three")] == ["image", "image", "preview"]
    assert exposures["one"]["time_source"] == "mtime" and exposures["one"]["time"] == BASE


def test_removed_renamed_and_returning_files(watch: Watch) -> None:
    put(watch.root, "sub/a.mrc", IMAGE, BASE)
    put(watch.root, "sub/b.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    assert {n: e["state"] for n, e in watch.exposures().items()} == {"a": "complete", "b": "complete"}
    seq = {n: e["seq"] for n, e in watch.exposures().items()}

    os.remove(watch.root / "sub/a.mrc")
    os.rename(watch.root / "sub/b.mrc", watch.root / "sub/c.mrc")
    os.utime(watch.root / "sub", (BASE + 60, BASE + 60))
    scanner.cycle(BASE + 60)
    states = {n: e["state"] for n, e in watch.exposures().items()}
    assert states == {"a": "deleted", "b": "deleted", "c": "incoming"}  # clients are told what is gone
    assert all(watch.exposures()[n]["seq"] > seq[n] for n in ("a", "b"))

    put(watch.root, "sub/a.mrc", IMAGE, BASE + 70)  # the same name comes back
    cycles(scanner, BASE + 70, BASE + 85)
    assert {n: e["state"] for n, e in watch.exposures().items()} == {
        "a": "complete",
        "b": "deleted",
        "c": "complete",
    }
    assert len(watch.rows("SELECT id FROM exposures")) == 3  # the same exposure, not a second one


def test_a_removed_directory_takes_its_files_with_it(watch: Watch) -> None:
    for name in ("x/deep/a.mrc", "x/b.mrc", "y/c.mrc"):
        put(watch.root, name, IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    import shutil

    shutil.rmtree(watch.root / "x")
    os.utime(watch.root, (BASE + 60, BASE + 60))
    scanner.cycle(BASE + 60)
    assert {n: e["state"] for n, e in watch.exposures().items()} == {
        "a": "deleted",
        "b": "deleted",
        "c": "complete",
    }
    put(watch.root, "x/deep/a.mrc", IMAGE, BASE + 100)  # and can come back
    cycles(scanner, BASE + 100, BASE + 115)
    assert watch.exposures()["a"]["state"] == "complete"


def test_a_file_that_changes_after_it_looked_complete_is_followed_again(watch: Watch) -> None:
    """The legacy monitor never looked at a file again: size and thumbnail stayed wrong."""
    put(watch.root, "a.tif", b"", BASE)
    tifffile.imwrite(watch.root / "a.tif", np.zeros((2, 16, 16), np.uint16))  # a valid TIFF of two pages...
    os.utime(watch.root / "a.tif", (BASE, BASE))
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    first = watch.exposures()["a"]
    assert first["state"] == "complete"
    tifffile.imwrite(watch.root / "a.tif", np.zeros((9, 16, 16), np.uint16))  # ...that was not finished
    os.utime(watch.root / "a.tif", (BASE + 40, BASE + 40))
    scanner.cycle(BASE + 40)
    again = watch.exposures()["a"]
    assert again["state"] == "incoming" and again["seq"] > first["seq"]
    assert again["note"] == "changed after it looked complete"
    cycles(scanner, BASE + 45, BASE + 55)
    final = watch.exposures()["a"]
    assert final["state"] == "complete" and json.loads(str(final["meta"]))["image"]["frames"] == 9
    assert final["size"] == os.path.getsize(watch.root / "a.tif")


def test_a_truncated_file_says_why_and_completes_when_the_rest_arrives(watch: Watch) -> None:
    put(watch.root, "a.mrc", IMAGE[:1100], BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 30)
    stuck = watch.exposures()["a"]
    assert stuck["state"] == "incoming" and "truncated or still being written" in str(stuck["note"])
    assert str(watch.root) not in str(stuck["note"])
    checks_before = scanner.cycle(BASE + 35).checks
    assert checks_before == 0  # the same bytes are not read again at every cycle
    put(watch.root, "a.mrc", IMAGE, BASE + 40)
    cycles(scanner, BASE + 40, BASE + 55)
    assert watch.exposures()["a"]["state"] == "complete" and watch.exposures()["a"]["note"] is None


def test_files_that_stay_broken_are_looked_at_less_often(watch: Watch) -> None:
    """A transfer that died leaves partial files for ever: they must not cost a round trip to
    the storage at every cycle, yet still complete if the rest arrives one day."""
    for n in range(50):
        put(watch.root, f"dead_{n}.mrc", IMAGE[:1100], BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 290)
    watch.fs.reset()
    cycles(scanner, BASE + 295, BASE + 300)
    assert watch.fs.stats == 2 * (1 + 50)  # five minutes: still followed at every cycle
    watch.fs.reset()
    cycles(scanner, BASE + 305, BASE + 900)  # 120 cycles in ten minutes
    assert 120 + 50 * 9 <= watch.fs.stats <= 120 + 50 * 11  # the directory each time, each file once a minute
    assert len(watch.files("pending")) == 50
    put(watch.root, "dead_7.mrc", IMAGE, BASE + 905)
    cycles(scanner, BASE + 905, BASE + 990)
    assert watch.files("complete").keys() == {"dead_7.mrc"}


def test_serialem_images_take_their_metadata_from_the_mdoc(watch: Watch) -> None:
    put(watch.root, "tilt_001.mrc", IMAGE, BASE)
    mdoc = b"PixelSpacing = 2.14\n[ZValue = 0]\nTiltAngle = -3.0\nExposureDose = 3.05\nDefocus = -3.9\nDateTime = 10-Nov-26  10:15:07\n"
    put(watch.root, "tilt_001.mrc.mdoc", mdoc, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    exposure = watch.exposures()["tilt_001"]
    assert exposure["state"] == "complete"
    meta = json.loads(str(exposure["meta"]))
    assert (
        meta["mdoc"]["tilt_angle"] == -3.0 and meta["mdoc"]["dose"] == 3.05 and meta["mdoc"]["sections"] == 1
    )
    assert exposure["time_source"] == "mdoc"
    assert json.loads(str(exposure["members"])) == {"image": "complete", "mdoc": "complete"}


def test_metadata_alone_is_not_an_exposure_to_show(watch: Watch) -> None:
    put(watch.root, "Data/FoilHole_1_Data_2_3_20270115_080000.xml", b"<MicroscopeImage/>", BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    assert watch.exposures() == {}
    assert len(watch.exposures(listed=False)) == 1
    put(watch.root, "Data/FoilHole_1_Data_2_3_20270115_080000.mrc", IMAGE, BASE + 20)
    cycles(scanner, BASE + 20, BASE + 35)
    assert watch.exposures()["FoilHole_1_Data_2_3_20270115_080000"]["state"] == "complete"


def test_links_hidden_files_and_strange_names_are_left_alone(watch: Watch, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mrc").write_bytes(IMAGE)
    put(watch.root, "real.mrc", IMAGE, BASE)
    put(watch.root, ".hidden/inside.mrc", IMAGE, BASE)
    put(watch.root, ".partial.mrc.Xa81", IMAGE, BASE)
    os.symlink(outside, watch.root / "link_to_directory")
    os.symlink(outside / "secret.mrc", watch.root / "link_to_file.mrc")
    os.symlink(watch.root, watch.root / "loop")
    (watch.root / "line\nbreak.mrc").write_bytes(IMAGE)
    with open(os.fsencode(watch.root) + b"/not-\xff-text.mrc", "wb") as f:
        f.write(IMAGE)
    os.utime(watch.root, (BASE, BASE))
    scanner = watch.scanner()
    report = cycles(scanner, BASE, BASE + 10)
    assert report is not None and report.error is None
    assert set(watch.exposures()) == {"real"}
    assert [r["rel"] for r in watch.rows("SELECT rel FROM dirs")] == [""]


def test_limits_on_depth_and_number_of_files(watch: Watch) -> None:
    watch.settings = ScanSettings(max_depth=2, max_files=3)
    put(watch.root, "a/b/c/too_deep.mrc", IMAGE, BASE)
    for n in range(5):
        put(watch.root, f"a/img_{n}.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    assert len(watch.files()) == 3
    assert sorted(r["rel"] for r in watch.rows("SELECT rel FROM dirs")) == ["", "a", "a/b"]


def test_a_failed_cycle_leaves_the_index_as_it_was(watch: Watch, monkeypatch: pytest.MonkeyPatch) -> None:
    put(watch.root, "a.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    before = watch.exposures()
    put(watch.root, "b.mrc", IMAGE, BASE + 20)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"disk on fire at {watch.root}")

    monkeypatch.setattr(scanner, "_refresh", boom)
    report = scanner.cycle(BASE + 20)
    assert report.error == "internal error (RuntimeError)"  # no server path in what clients may read
    assert watch.exposures() == before
    assert watch.rows("SELECT error FROM roots")[0]["error"] == report.error
    monkeypatch.undo()
    cycles(scanner, BASE + 25, BASE + 40)
    assert set(watch.exposures()) == {"a", "b"} and watch.rows("SELECT error FROM roots")[0]["error"] is None
    assert watch.exposures()["b"]["state"] == "complete"


def test_every_change_gets_a_new_number(watch: Watch) -> None:
    sim = MicroscopeSim(watch.root, SimSettings(rate_per_hour=1200, pause_probability=0.3, seed=6))
    scanner = watch.scanner()
    known: dict[str, dict[str, object]] = {}
    last = 0
    for second in range(0, 200):
        sim.step(BASE + second)
        if second == 120:
            sim.stop()
        if second % 5:
            continue
        scanner.cycle(BASE + second)
        # what a client does: ask for what changed since the last number it has
        changed = watch.rows(
            "SELECT * FROM exposures WHERE seq > ? AND primary_name IS NOT NULL ORDER BY seq", last
        )
        for row in changed:
            known[str(row["name"])] = row
            last = int(str(row["seq"]))
        assert known == watch.exposures()  # following the changes is enough to know everything
    assert len(known) > 30 and all(e["state"] == "complete" for e in known.values())


# ── found by the independent review of the Live mode ──────────────────────────


def test_a_link_that_replaces_an_indexed_directory_is_not_followed(watch: Watch, tmp_path: Path) -> None:
    """High. ``stat`` of a path ending with a separator follows a link: a directory swapped for
    a link to somewhere else was listed, and the names and headers of what lies there indexed."""
    outside = tmp_path / "outside" / "deep"
    outside.mkdir(parents=True)
    (outside / "secret.mrc").write_bytes(IMAGE)
    (outside.parent / "top_secret.mrc").write_bytes(IMAGE)
    put(watch.root, "x/deep/inside.mrc", IMAGE, BASE)
    put(watch.root, "y/other.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    assert set(watch.exposures()) == {"inside", "other"}

    import shutil

    for swap in ("x", "y"):  # a directory with a sub-directory, and a leaf
        shutil.rmtree(watch.root / swap)
        os.symlink(outside.parent, watch.root / swap)
    os.utime(watch.root, (BASE + 60, BASE + 60))
    cycles(scanner, BASE + 60, BASE + 75)
    listed = {n: e["state"] for n, e in watch.exposures().items()}
    assert listed == {"inside": "deleted", "other": "deleted"}  # gone, and nothing from outside
    assert not {"secret", "top_secret"} & set(watch.exposures(listed=False))
    cycles(scanner, BASE + 700, BASE + 715)  # the full rescan does not follow it either
    assert "secret" not in watch.exposures() and "top_secret" not in watch.exposures()

    # a new scanner on the same index, and a link that was there from the start
    again = watch.scanner()
    cycles(again, BASE + 720, BASE + 735)
    assert set(watch.exposures()) == {"inside", "other"}
    assert all(e["state"] == "deleted" for e in watch.exposures().values())


def test_a_file_swapped_for_a_link_is_not_opened(watch: Watch, tmp_path: Path) -> None:
    secret = tmp_path / "secret.mrc"
    secret.write_bytes(IMAGE)
    put(watch.root, "a.mrc", IMAGE[:1100], BASE)  # partial: it stays followed
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 15)
    os.remove(watch.root / "a.mrc")
    os.symlink(secret, watch.root / "a.mrc")  # same name, now a link to a complete file elsewhere
    cycles(scanner, BASE + 20, BASE + 60)  # the directory's date did not change: no new listing
    assert watch.files("complete") == {}
    os.utime(watch.root, (BASE + 70, BASE + 70))
    cycles(scanner, BASE + 70, BASE + 90)
    assert watch.exposures()["a"]["state"] == "deleted"


def test_a_file_growing_in_a_quiet_directory_is_not_taken_as_complete_at_first_sight(watch: Watch) -> None:
    """Medium. The date of a directory tells when an entry was last added, not whether a file
    in it is being written: a tilt series appended to for an hour sat in a "quiet" directory."""
    put(watch.root, "stack/ts_001.mrc", IMAGE[:1100], BASE - 1200)  # created 20 minutes ago…
    put(watch.root, "elsewhere/recent.mrc", IMAGE, BASE - 2)
    os.utime(watch.root / "stack/ts_001.mrc", (BASE - 1, BASE - 1))  # …and written to a second ago
    scanner = watch.scanner()
    scanner.cycle(BASE)
    assert watch.files("pending").keys() == {"stack/ts_001.mrc", "elsewhere/recent.mrc"}
    for k in range(1, 5):  # it grows for twenty more seconds
        put(watch.root, "stack/ts_001.mrc", IMAGE[: 1100 + k], BASE + 5 * k)
        scanner.cycle(BASE + 5 * k)
        assert "stack/ts_001.mrc" in watch.files("pending")
    put(watch.root, "stack/ts_001.mrc", IMAGE, BASE + 25)
    cycles(scanner, BASE + 25, BASE + 40)
    assert watch.exposures()["ts_001"]["state"] == "complete" and watch.exposures()["ts_001"]["verified"] == 1


@pytest.mark.parametrize("ahead", [1200, 20 * 3600])
def test_a_date_in_the_future_does_not_make_everything_else_look_old(watch: Watch, ahead: int) -> None:
    """Medium. One entry dated ahead of the clock was taken as "the time on the file server":
    a file written a second ago then looked twenty minutes old, hence complete."""
    put(watch.root, "misdated.mrc", IMAGE, BASE + ahead)
    put(watch.root, "new.mrc", IMAGE[:1100], BASE - 1)  # half written
    os.utime(watch.root, (BASE + ahead, BASE + ahead))
    scanner = watch.scanner()
    scanner.cycle(BASE)
    assert "new.mrc" in watch.files("pending")
    cycles(scanner, BASE + 5, BASE + 30)
    assert watch.files("complete").keys() == {"misdated.mrc"}  # complete itself, by the test


def test_a_failed_cycle_does_not_make_the_scanner_start_from_scratch(
    watch: Watch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium. After one failing cycle the scanner took what arrived next for something that
    had been there all along (not counted as arriving), and forgot what it had just completed."""
    put(watch.root, "a.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    assert watch.exposures()["a"]["state"] == "complete"

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(scanner, "_refresh", boom)
    put(watch.root, "b.mrc", IMAGE, BASE + 15)
    assert scanner.cycle(BASE + 15).error is not None
    monkeypatch.undo()
    cycles(scanner, BASE + 20, BASE + 35)
    b = watch.exposures()["b"]
    assert b["state"] == "complete" and b["arrived"] == 1  # it did arrive while watching
    # and "a", completed 25 seconds before, is still looked at: it changes, the index follows
    put(watch.root, "a.mrc", IMAGE + b"more", BASE + 40)
    scanner.cycle(BASE + 40)
    assert watch.exposures()["a"]["state"] == "incoming"


def test_a_file_completed_just_before_a_restart_is_still_looked_at_again(watch: Watch) -> None:
    put(watch.root, "a.tif", b"", BASE)
    tifffile.imwrite(watch.root / "a.tif", np.zeros((2, 16, 16), np.uint16))
    os.utime(watch.root / "a.tif", (BASE, BASE))
    cycles(watch.scanner(), BASE, BASE + 10)
    assert watch.exposures()["a"]["state"] == "complete"
    tifffile.imwrite(watch.root / "a.tif", np.zeros((9, 16, 16), np.uint16))  # it was not finished
    os.utime(watch.root / "a.tif", (BASE + 20, BASE + 20))
    reborn = watch.scanner()  # the server restarted in between
    cycles(reborn, BASE + 25, BASE + 45)
    final = watch.exposures()["a"]
    assert final["state"] == "complete" and final["size"] == os.path.getsize(watch.root / "a.tif")
    assert json.loads(str(final["meta"]))["image"]["frames"] == 9


def test_a_storage_hiccup_during_the_full_rescan_deletes_nothing(watch: Watch) -> None:
    """Medium. One input/output error on a listing at the ten-minute rescan removed the whole
    sub-tree from the index; it came back ten minutes later as a burst of new arrivals."""
    for n in range(5):
        put(watch.root, f"Images/Data/img_{n}.mrc", IMAGE, BASE)
    put(watch.root, "Images/Data/deep/x.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    before = watch.exposures()
    assert len(before) == 6 and all(e["state"] == "complete" for e in before.values())

    real = watch.fs.scandir

    def flaky(path: str):
        if path.rstrip("/").endswith("Data"):
            raise OSError(5, "Input/output error")
        return real(path)

    watch.fs.scandir = flaky  # type: ignore[method-assign]
    report = scanner.cycle(BASE + 700)  # the full rescan
    assert report.full and report.error is None and report.removed == 0
    assert watch.exposures() == before
    watch.fs.scandir = real  # type: ignore[method-assign]
    cycles(scanner, BASE + 705, BASE + 1400)
    assert watch.exposures() == before  # same completion times: nothing "arrived" again

    # really gone is still gone
    import shutil

    shutil.rmtree(watch.root / "Images/Data")
    os.utime(watch.root / "Images", (BASE + 1500, BASE + 1500))
    scanner.cycle(BASE + 1500)
    assert all(e["state"] == "deleted" for e in watch.exposures().values())


def test_the_index_is_not_locked_while_the_tree_is_listed(watch: Watch) -> None:
    """Medium. A cycle held the write lock from its first directory listing to its end: on a
    large tree every other writer (starting a watch, saving alert rules) waited, then failed."""
    put(watch.root, "a.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    seen: list[bool] = []
    real = watch.fs.scandir

    def spy(path: str):
        seen.append(watch.index.connection().in_transaction)
        return real(path)

    watch.fs.scandir = spy  # type: ignore[method-assign]
    scanner.cycle(BASE)
    assert seen == [False]  # the full listing happens before the transaction


def test_a_busy_index_skips_a_cycle_and_nothing_else(watch: Watch, monkeypatch: pytest.MonkeyPatch) -> None:
    put(watch.root, "a.mrc", IMAGE, BASE)
    scanner = watch.scanner()
    cycles(scanner, BASE, BASE + 10)
    put(watch.root, "b.mrc", IMAGE, BASE + 15)

    import contextlib

    @contextlib.contextmanager
    def locked():
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(watch.index, "write", locked)
    report = scanner.cycle(BASE + 15)
    assert report.error == "the live index is busy"
    monkeypatch.undo()
    cycles(scanner, BASE + 20, BASE + 35)
    b = watch.exposures()["b"]
    assert b["state"] == "complete" and b["arrived"] == 1
    assert watch.rows("SELECT error FROM roots")[0]["error"] is None


def test_the_first_scan_of_a_large_tree_is_committed_in_batches(
    watch: Watch, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imagebio.live.scanner as module

    monkeypatch.setattr(module, "_CHECKPOINT_FILES", 40)
    old = BASE - 3 * 86400
    for d in range(10):
        for n in range(10):
            put(watch.root, f"d{d}/img_{d}_{n}.mrc", IMAGE, old)
        os.utime(watch.root / f"d{d}", (old, old))
    os.utime(watch.root, (old, old))
    commits: list[int] = []
    real = module.Scanner._checkpoint

    def counting(self, db, report):
        real(self, db, report)
        commits.append(len(watch.rows("SELECT id FROM exposures WHERE seq > 0")))

    monkeypatch.setattr(module.Scanner, "_checkpoint", counting)
    report = watch.scanner().cycle(BASE)
    assert report.error is None and report.new_files == 100
    assert commits == [40, 80]  # numbered and committed as it goes
    exposures = watch.exposures()
    assert len(exposures) == 100 and len({e["seq"] for e in exposures.values()}) == 100
    assert watch.rows("SELECT seq FROM roots")[0]["seq"] == 100
