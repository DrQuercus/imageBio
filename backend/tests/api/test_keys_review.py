"""Regressions found by the independent review of lot E (ADR 015), on the server: each test
fails on the code before its fix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures.api import ALICE, Tree, build_tree, make_client
from imagebio.admin import store as store_module

ADMIN = {"X-Forwarded-User": "root", "X-Forwarded-Groups": "it"}


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path / "data")


def make_key(client) -> str:
    r = client.post(
        "/api/me/keys",
        content=json.dumps({"name": "k", "days": 30}),
        headers={**ALICE, "Content-Type": "application/json"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["key"])


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_a_group_taken_away_on_one_server_stops_the_keys_on_the_other(
    tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two servers on one state directory: before, the one that only saw keys (an assistant
    polling at night) kept the groups of before, until a person happened to come by."""
    monkeypatch.setattr(store_module, "_REFRESH_EVERY_S", 0.0)
    state = tmp_path / "state"
    with make_client(tree, auth_mode="proxy", state_dir=state, admins=frozenset({"root"})) as a:
        version = a.get("/api/admin/settings", headers=ADMIN).json()["version"]
        r = a.put(
            "/api/admin/accounts",
            content=json.dumps({"version": version, "name": "alice", "groups": ["lm"], "admin": False}),
            headers={**ADMIN, "Content-Type": "application/json"},
        )
        assert r.status_code == 200, r.text
        key = make_key(a)
        with make_client(tree, auth_mode="proxy", state_dir=state, admins=frozenset({"root"})) as b:
            light = {"path": "light"}
            assert b.get("/api/fs/list", params=light, headers=bearer(key)).status_code == 200
            version = a.get("/api/admin/settings", headers=ADMIN).json()["version"]
            r = a.delete("/api/admin/accounts", params={"name": "alice", "version": version}, headers=ADMIN)
            assert r.status_code == 200, r.text
            assert b.get("/api/fs/list", params=light, headers=bearer(key)).status_code == 403


def test_a_key_opens_nothing_but_the_api(tree: Tree, tmp_path: Path) -> None:
    """The proxy lets a key through without a password: before, the pages and their files were
    served to anybody who wrote "Bearer ibk_" and anything after it."""
    with make_client(tree, auth_mode="proxy", state_dir=tmp_path / "state") as client:
        key = make_key(client)
        for path in ("/", "/index.html", "/assets/app.js", "/healthz"):
            for who in (bearer(key), bearer("ibk_" + "x" * 43)):
                assert client.get(path, headers=who).status_code == 401, path
        assert client.get("/api/me", headers=bearer(key)).status_code == 200
    with make_client(tree, state_dir=tmp_path / "local") as client:  # standalone: no keys, no rule
        assert client.get("/healthz", headers=bearer("ibk_" + "x" * 43)).status_code == 200


def test_a_key_that_polls_a_watch_does_not_keep_its_alerts_going(tree: Tree, tmp_path: Path) -> None:
    """Alerts stop after a week without anybody looking at the watch: an assistant polling
    with a key is not somebody looking."""
    (tree.base / "cryo" / "session").mkdir(exist_ok=True)
    with make_client(tree, auth_mode="proxy", state_dir=tmp_path / "state", live_scan=False) as client:
        key = make_key(client)
        r = client.post("/api/live/watches", params={"path": "cryo/session"}, headers=ALICE)
        assert r.status_code == 200, r.text
        watch = int(r.json()["id"])
        live = client.app.state.live
        live._seen.clear()
        assert client.get(f"/api/live/watches/{watch}", headers=bearer(key)).status_code == 200
        assert watch not in live._seen
        assert client.get(f"/api/live/watches/{watch}", headers=ALICE).status_code == 200
        assert watch in live._seen
