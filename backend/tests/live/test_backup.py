"""Backup and restore: the exercise of losing the server's state and getting it back."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from fixtures.api import ALICE, BOB, build_tree, make_client
from imagebio.api import cli
from imagebio.core.config import AlertChannel, ConfigError, Settings
from imagebio.live.backup import INDEX_NAME, MANIFEST_NAME, backup, restore
from imagebio.live.index import SCHEMA_VERSION
from imagebio.live.simulator import mrc_bytes

WATCHES = "/api/live/watches"
CHANNELS = (AlertChannel("phone", "ntfy", "https://ntfy.example/a-topic"),)


def serve(tree, state: Path):
    return make_client(
        tree, auth_mode="proxy", trust_network=True, state_dir=state, live_scan=False, alert_channels=CHANNELS
    )


def snapshot(client) -> dict:
    """What users see of the Live mode: watches, their settings, what was indexed."""
    out = {}
    for who, name in ((ALICE, "alice"), (BOB, "bob")):
        watches = client.get(WATCHES, headers=who).json()["watches"]
        out[name] = {
            w["path"]: {
                "watch": w,
                "alerts": client.get(f"{WATCHES}/{w['id']}/alerts", headers=who).json()["rules"],
                "summary": {
                    k: v
                    for k, v in client.get(f"{WATCHES}/{w['id']}", headers=who).json().items()
                    if k in ("quality", "ice", "acquired", "seq")
                },
                "items": client.get(f"{WATCHES}/{w['id']}/items", headers=who).json()["items"],
            }
            for w in watches
        }
    return out


def test_the_state_of_a_server_is_lost_and_restored(tmp_path: Path) -> None:
    """Acceptance (M8): a restore exercise. A server in use is backed up while it runs; its
    state directory is destroyed; the backup is restored; a new server shows what the old did."""
    tree = build_tree(tmp_path / "data")
    session = tree.base / "public/session"
    session.mkdir()
    start = 1_800_000_000.0
    for n in range(3):  # written just before the first scan, by the clock the scans are given
        (session / f"image_{n}.mrc").write_bytes(mrc_bytes(np.full((16, 16), 10.0 * n, np.float32), 1.0))
        os.utime(session / f"image_{n}.mrc", (start - 5, start - 5))
    os.utime(session, (start - 5, start - 5))
    state = tmp_path / "state"
    config = tmp_path / "imagebio.toml"
    config.write_text(f'[live]\nstate_dir = "{state}"\n')
    settings = Settings.load(config)

    with serve(tree, state) as client:
        live = client.app.state.live
        alice = client.post(WATCHES, params={"path": "public/session"}, headers=ALICE).json()["id"]
        client.post(WATCHES, params={"path": "public"}, headers=BOB)
        client.put(f"{WATCHES}/{alice}/alerts", params={"channels": "phone", "no_image": 30}, headers=ALICE)
        client.put(f"{WATCHES}/{alice}/quality", params={"fit": 5, "ring": 1.2}, headers=ALICE)
        client.put(f"{WATCHES}/{alice}/ice", params={"i0": 55.5, "mfp": 395}, headers=ALICE)
        for second in (0, 20, 40, 60):
            live.scan_once(1_800_000_000.0 + second)
        before = snapshot(client)
        assert len(before["alice"]["public/session"]["items"]) == 3
        assert before["alice"]["public/session"]["items"][0]["metrics"] is not None

        made = backup(settings, tmp_path / "backups", config)  # while the server runs
        manifest = json.loads((made / MANIFEST_NAME).read_text())
        assert manifest["index"]["schema_version"] == SCHEMA_VERSION and manifest["index"]["watches"] == 2
        assert manifest["config"] is True and (made / "imagebio.toml").read_text() == config.read_text()
        with pytest.raises(ConfigError, match="in use: stop the server"):
            restore(settings, made, force=True)  # not under a running server

    shutil.rmtree(state)  # the disaster
    with serve(tree, state) as empty:
        assert empty.get(WATCHES, headers=ALICE).json()["watches"] == []
    shutil.rmtree(state)

    restored = restore(settings, made)
    assert restored["created"] == manifest["created"]
    with serve(tree, state) as client:
        assert snapshot(client) == before  # watches, rules, thresholds, calibration, images and measurements
        # and the server goes on from there
        client.app.state.live.scan_once(1_800_000_100.0)
        assert client.get(f"{WATCHES}/{alice}", headers=ALICE).json()["acquired"] == 3


def test_a_restore_does_not_destroy_what_is_there(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "data")
    state = tmp_path / "state"
    settings = Settings(state_dir=state)
    with serve(tree, state) as client:
        client.post(WATCHES, params={"path": "public"}, headers=ALICE)
        made = backup(settings, tmp_path / "backups")
        client.post(WATCHES, params={"path": "cryo"}, headers=ALICE)  # after the backup
    with pytest.raises(ConfigError, match="already a Live index here"):
        restore(settings, made)
    restore(settings, made, force=True)
    aside = [p for p in state.iterdir() if ".before-restore-" in p.name]
    assert len(aside) == 1  # the index that was there is kept
    with sqlite3.connect(aside[0]) as kept:
        assert kept.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 2
    with serve(tree, state) as client:
        assert [w["path"] for w in client.get(WATCHES, headers=ALICE).json()["watches"]] == ["public"]
    assert made.stat().st_mode & 0o077 == 0  # a backup may hold the configuration: its owner's only


def test_a_damaged_index_can_be_replaced(tmp_path: Path) -> None:
    """The reason one restores: what is in place cannot be read any more."""
    tree = build_tree(tmp_path / "data")
    state = tmp_path / "state"
    settings = Settings(state_dir=state)
    with serve(tree, state) as client:
        client.post(WATCHES, params={"path": "public"}, headers=ALICE)
        made = backup(settings, tmp_path / "backups")
    (state / INDEX_NAME).write_bytes(b"\0" * 4096 + b"what a failing disk leaves")
    (state / f"{INDEX_NAME}-wal").write_bytes(b"half a log")
    restore(settings, made, force=True)
    kept = [p for p in state.iterdir() if ".before-restore-" in p.name and not p.name.endswith("-wal")]
    assert len(kept) == 1 and kept[0].read_bytes().endswith(b"what a failing disk leaves")  # as found
    # the log of the damaged index is not left where the restored one would take it for its own
    assert sqlite3.connect(state / INDEX_NAME).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with serve(tree, state) as client:
        assert [w["path"] for w in client.get(WATCHES, headers=ALICE).json()["watches"]] == ["public"]


def test_what_is_not_a_usable_backup_is_refused(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "data")
    state = tmp_path / "state"
    settings = Settings(state_dir=state)
    nothing = backup(Settings(state_dir=tmp_path / "never_used"), tmp_path / "backups")
    assert json.loads((nothing / MANIFEST_NAME).read_text())["index"] is None  # the Live mode was never used
    with pytest.raises(ConfigError, match="holds no Live index"):
        restore(settings, nothing)
    with pytest.raises(ConfigError, match="no manifest"):
        restore(settings, tmp_path / "data")

    with serve(tree, state) as client:
        client.post(WATCHES, params={"path": "public"}, headers=ALICE)
    made = backup(settings, tmp_path / "more")
    newer = tmp_path / "newer"
    shutil.copytree(made, newer)
    with sqlite3.connect(newer / INDEX_NAME) as db:
        db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(ConfigError, match="newer imageBio"):
        restore(Settings(state_dir=tmp_path / "elsewhere"), newer)
    damaged = tmp_path / "damaged"
    shutil.copytree(made, damaged)
    (damaged / INDEX_NAME).write_bytes(b"not a database at all" * 100)
    with pytest.raises(ConfigError, match="does not hold a Live index that can be read"):
        restore(Settings(state_dir=tmp_path / "elsewhere"), damaged)
    assert not (tmp_path / "elsewhere" / INDEX_NAME).exists()  # nothing was put in place


def test_backup_and_restore_from_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree = build_tree(tmp_path / "data")
    state = tmp_path / "state"
    config = tmp_path / "imagebio.toml"
    config.write_text(f'[live]\nstate_dir = "{state}"\n')
    with serve(tree, state) as client:
        client.post(WATCHES, params={"path": "public"}, headers=ALICE)
    assert cli.main(["backup", str(tmp_path / "backups"), "--config", str(config)]) == 0
    out = capsys.readouterr().out
    made = Path(out.strip().removeprefix("backup written to "))
    assert made.parent == tmp_path / "backups" and (made / INDEX_NAME).is_file()
    assert cli.main(["restore", str(made), "--config", str(config)]) == 2
    assert "already a Live index here" in capsys.readouterr().err
    assert cli.main(["restore", str(made), "--config", str(config), "--force"]) == 0
    assert "restored, 1 watch(es): start the server" in capsys.readouterr().out
    assert cli.main(["restore", str(tmp_path / "data"), "--config", str(config)]) == 2
    assert "no manifest" in capsys.readouterr().err
