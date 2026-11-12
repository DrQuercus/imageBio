"""Quality reports of a watched session, through the API: made when asked, when the watch
stops and when the collection goes silent; kept per watch; read only by their owner."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest

from fixtures.api import ALICE, BOB, build_tree
from fixtures.ctf import micrograph
from fixtures.live import BASE
from fixtures.live_world import WATCHES, World, sim_in
from imagebio.core.config import ConfigError, Settings
from imagebio.live.simulator import mrc_bytes

HOSTILE = "<img src=x onerror=alert(1)>"


@pytest.fixture
def world(tmp_path: Path):
    tree = build_tree(tmp_path / "data")
    for directory in ("cryo/session", "public/shared"):
        (tree.base / directory).mkdir(exist_ok=True)
        os.utime(tree.base / directory, (BASE, BASE))
    w = World(tree, tmp_path / "state", live_report_idle_s=600.0)
    yield w
    w.client.__exit__(None, None, None)


def follow(world: World, session: str, who: dict[str, str]) -> int:
    watch = world.watch(session, who)
    world.run([], 5)
    return watch


def arrive(world: World, session: str, images: dict[str, np.ndarray]) -> None:
    directory = world.tree.base / session
    for name, pixels in images.items():
        (directory / f"{name}.mrc").write_bytes(mrc_bytes(pixels.astype(np.float32), 1.0))
        os.utime(directory / f"{name}.mrc", (world.now, world.now))
    os.utime(directory, (world.now, world.now))
    world.run([], 40)


def reports(world: World, watch: int, who: dict[str, str] = ALICE) -> dict:
    r = world.client.get(f"{WATCHES}/{watch}/reports", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def make(world: World, watch: int, who: dict[str, str] = ALICE) -> dict:
    r = world.client.post(f"{WATCHES}/{watch}/reports", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def get(world: World, watch: int, report: int, kind: str, who: dict[str, str] = ALICE, **params: object):
    return world.client.get(f"{WATCHES}/{watch}/reports/{report}/{kind}", params=params, headers=who)


def test_a_report_is_made_when_asked_and_holds_the_session(world: World) -> None:
    rng = np.random.default_rng(3)
    watch = follow(world, "public/shared", ALICE)
    world.client.put(f"{WATCHES}/{watch}/quality", params={"fit": 6}, headers=ALICE)
    arrive(
        world,
        "public/shared",
        {"a": micrograph((1024, 1024), 1.0, 1.8, rng), HOSTILE: micrograph((1024, 1024), 1.0, 2.4, rng)},
    )
    arrive(world, "public/shared", {"noise": rng.normal(5, 1, (600, 600))})

    made = make(world, watch)
    assert (made["trigger"], made["exposures"], made["created_at"]) == ("manual", 3, world.now)
    listing = reports(world, watch)
    assert (
        listing["auto"] is True
        and listing["idle_min"] == 10
        and [r["id"] for r in listing["reports"]] == [made["id"]]
    )
    assert world.summary(watch, ALICE)["reports"] == {"count": 1, "last": made["id"], "auto": True}

    numbers = get(world, watch, made["id"], "json")
    assert numbers.status_code == 200 and numbers.headers["content-type"] == "application/json"
    report = numbers.json()
    assert report["schema"] == "imagebio.live-report" and report["session"]["path"] == "public/shared"
    assert report["counts"]["exposures"] == 3 and report["counts"]["measured"] == 3
    assert report["quality"]["judged"] == 3  # the one without rings is not held against having none
    assert {c["name"] for c in report["best"]} == {"a", HOSTILE}

    page = get(world, watch, made["id"], "html")
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert page.headers["content-disposition"].startswith('attachment; filename="rapport_shared_')
    text = page.text
    assert "public/shared" in text and HOSTILE not in text and "&lt;img src=x onerror=alert(1)&gt;" in text
    assert text.count('src="data:image/jpeg;base64,') == 2  # the two images with rings, as thumbnails

    shown = get(world, watch, made["id"], "html", view="true")  # to read in the browser
    assert "content-disposition" not in shown.headers
    assert shown.headers["content-security-policy"].startswith("sandbox; default-src 'none'")
    assert shown.content == page.content

    table = get(world, watch, made["id"], "csv")
    assert table.headers["content-disposition"].startswith('attachment; filename="images_shared_')
    assert table.text.splitlines()[0].startswith("name,path,time") and len(table.text.splitlines()) == 4
    archive = zipfile.ZipFile(io.BytesIO(get(world, watch, made["id"], "zip").content))
    assert sorted(archive.namelist()) == ["exposures.csv", "report.html", "report.json"]
    assert json.loads(archive.read("report.json")) == report

    # asked again at once: the report just made; a moment later, a new one
    assert make(world, watch)["id"] == made["id"]
    world.run([], 15)
    assert make(world, watch)["id"] != made["id"] and len(reports(world, watch)["reports"]) == 2


def test_only_the_owner_of_a_watch_reads_its_reports(world: World) -> None:
    mine = follow(world, "public/shared", ALICE)
    theirs = follow(world, "public/shared", BOB)
    report = make(world, mine)["id"]
    for who, watch in ((BOB, mine), (ALICE, theirs)):
        assert world.client.get(f"{WATCHES}/{watch}/reports/{report}/html", headers=who).status_code == 404
    assert world.client.get(f"{WATCHES}/{mine}/reports", headers=BOB).status_code == 404
    assert world.client.post(f"{WATCHES}/{mine}/reports", headers=BOB).status_code == 404
    assert reports(world, theirs, BOB)["reports"] == []  # the same directory, but not the same watch
    for path in (f"{report}/pdf", f"{report}/../../etc", "0/html", f"{report + 99}/html", "x/html"):
        assert world.client.get(f"{WATCHES}/{mine}/reports/{path}", headers=ALICE).status_code in (
            400,
            404,
            422,
        ), path
    assert get(world, mine, report, "html", other=1).status_code == 400
    assert world.client.post(f"{WATCHES}/{mine}/reports").status_code == 401
    # another site cannot have one made on a user's behalf
    forged = world.client.post(
        f"{WATCHES}/{mine}/reports", headers={**ALICE, "Origin": "https://evil.example"}
    )
    assert forged.status_code == 403


def test_a_report_is_made_when_the_watch_ends_unless_told_not_to(world: World) -> None:
    rng = np.random.default_rng(4)
    ended = follow(world, "public/shared", ALICE)
    quiet = follow(world, "cryo/session", ALICE)
    arrive(world, "public/shared", {"a": rng.normal(5, 1, (64, 64))})
    off = world.client.put(f"{WATCHES}/{quiet}/reports", params={"auto": "false"}, headers=ALICE)
    assert off.status_code == 200 and off.json()["auto"] is False
    world.client.delete(f"{WATCHES}/{ended}", headers=ALICE)
    world.client.delete(f"{WATCHES}/{quiet}", headers=ALICE)
    made = reports(world, ended)["reports"]
    assert [r["trigger"] for r in made] == ["stop"] and made[0]["exposures"] == 1
    assert reports(world, quiet)["reports"] == []
    assert get(world, ended, made[0]["id"], "json").json()["session"]["stopped_at"] == world.now

    # forgotten: its reports go with it, files included
    folder = world.state / "reports"
    assert len(list(folder.iterdir())) == 1
    world.client.delete(f"{WATCHES}/{ended}", params={"forget": "true"}, headers=ALICE)
    assert list(folder.iterdir()) == []


def test_a_report_is_made_when_the_collection_goes_silent(world: World) -> None:
    sim = sim_in(world, "public/shared", 9, pause_probability=0.0, frames=0)
    watch = follow(world, "public/shared", ALICE)
    world.run([sim], 120)
    sim.stop()
    world.run([sim], 30)
    assert reports(world, watch)["reports"] == []
    world.run([], 690)  # ten minutes without an image (looked at once a minute): the session's report
    first = reports(world, watch)["reports"]
    assert [r["trigger"] for r in first] == ["idle"] and first[0]["exposures"] == len(sim.exposures)
    world.run([], 900)
    assert len(reports(world, watch)["reports"]) == 1  # not again for the same silence

    arrive(world, "public/shared", {"late": np.full((64, 64), 3.0)})  # the collection went on
    world.run([], 700)
    again = reports(world, watch)["reports"]
    assert [r["trigger"] for r in again] == ["idle", "idle"] and again[0]["exposures"] == len(
        sim.exposures
    ) + 1


def test_reports_are_kept_up_to_a_limit_and_a_lost_one_is_not_listed(world: World) -> None:
    watch = follow(world, "public/shared", ALICE)
    made = []
    for _ in range(22):
        made.append(make(world, watch)["id"])
        world.now += 11
    kept = [r["id"] for r in reports(world, watch)["reports"]]
    assert kept == made[::-1][:20] and len(list((world.state / "reports").iterdir())) == 20
    assert get(world, watch, made[0], "html").status_code == 404  # the oldest went

    newest = (world.state / "reports").joinpath(
        sorted(p.name for p in (world.state / "reports").iterdir())[-1]
    )
    for f in newest.iterdir():
        f.unlink()
    newest.rmdir()  # what a restore on another disk looks like: the index without the files
    assert made[-1] not in [r["id"] for r in reports(world, watch)["reports"]]
    assert get(world, watch, made[-1], "json").status_code == 404


def test_the_silence_before_a_report_is_configured(tmp_path: Path) -> None:
    cfg = tmp_path / "imagebio.toml"
    for text, seconds in (
        ("", 7200.0),
        ("report_after_idle_min = 0", 0.0),
        ("report_after_idle_min = 45", 2700.0),
    ):
        cfg.write_text(f"[live]\n{text}\n")
        assert Settings.load(cfg).live_report_idle_s == seconds
    for bad in ("report_after_idle_min = 1", "report_after_idle_min = 20000", 'report_after_idle_min = "1h"'):
        cfg.write_text(f"[live]\n{bad}\n")
        with pytest.raises((ConfigError, ValueError)):
            Settings.load(cfg)
