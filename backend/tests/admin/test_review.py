"""Regressions found by the independent review of the settings page (ADR 014). Each test fails
on the code before its fix."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from fixtures.api import BOB, Tree, build_tree, make_client
from imagebio.admin.store import FILE_NAME, SettingsStore, valid_user_name
from imagebio.core.config import ConfigError, ProcessingConfig, RootConfig, Settings
from imagebio.core.errors import AccessDeniedError, ConflictError, InvalidRequestError
from imagebio.fs.roots import RootSet, User
from imagebio.live import backup as backups
from imagebio.live.index import Index

ADMIN = {"X-Forwarded-User": "root"}
SETTINGS = "/api/admin/settings"


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path / "data")


def put(client, url: str, body: object, who: dict[str, str] = ADMIN):
    return client.put(url, content=json.dumps(body), headers={**who, "Content-Type": "application/json"})


def version(client) -> int:
    return int(client.get(SETTINGS, headers=ADMIN).json()["version"])


def bob_lists(client, path: str):
    return client.get("/api/fs/list", params={"path": path}, headers=BOB)


def test_a_root_of_the_page_is_checked_again_each_time_it_is_served(tree: Tree, tmp_path: Path) -> None:
    # Regression (high): checked when saved, then resolved again with no check. A directory of
    # the page replaced by a link to the server's state showed the settings to everybody.
    state = tmp_path / "state"
    shared = tree.base / "public" / "shared"
    shared.mkdir()
    options = {"auth_mode": "proxy", "state_dir": state, "admins": frozenset({"root"})}
    with make_client(tree, **options) as client:
        roots = [
            {"name": "public", "path": str(tree.base / "public")},
            {"name": "shared", "path": str(shared)},
        ]
        assert (
            put(client, f"{SETTINGS}/roots", {"version": version(client), "value": roots}).status_code == 200
        )
        assert bob_lists(client, "shared").status_code == 200
        shutil.rmtree(shared)
        shared.symlink_to(state)  # whoever writes in the data directory
        # any change sets the roots again: this one is not served any more
        r = put(client, f"{SETTINGS}/microscope", {"version": version(client), "value": {"kv": 200}})
        assert r.status_code == 200
        assert bob_lists(client, "shared").status_code == 404
        assert not next(x for x in r.json()["sections"]["roots"]["value"] if x["name"] == "shared")[
            "available"
        ]
    # nor after a restart
    with make_client(tree, **options) as again:
        assert bob_lists(again, "shared").status_code == 404
        assert bob_lists(again, "public").status_code == 200


def test_a_root_written_by_hand_in_the_file_is_not_served(tree: Tree, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    sections = {
        "roots": [{"name": "etc", "path": "/etc"}, {"name": "public", "path": str(tree.base / "public")}]
    }
    (state / FILE_NAME).write_text(
        json.dumps({"schema": "imagebio.settings", "version": 3, "sections": sections})
    )
    with make_client(tree, auth_mode="proxy", state_dir=state) as client:
        assert bob_lists(client, "etc").status_code == 404  # outside the roots of the file
        assert bob_lists(client, "public").status_code == 200


def test_roots_may_not_be_added_next_to_those_of_the_file_or_around_the_work(tmp_path: Path) -> None:
    # Regression: the parents of the roots of the file were allowed: a root at /data opened /.
    for d in ("data/krios", "etc", "work"):
        (tmp_path / d).mkdir(parents=True)
    base = Settings(roots=(RootConfig("data", tmp_path / "data"),), state_dir=tmp_path / "state")
    with pytest.raises(InvalidRequestError, match="inside a data root"):
        SettingsStore(base).set_section(
            "roots", [{"name": "x", "path": str(tmp_path / "etc")}], by="a", version=0
        )
    from fixtures.programs import ctf_step

    work = tmp_path / "data" / "krios" / "work"
    work.mkdir()
    with_work = Settings(
        roots=base.roots,
        state_dir=tmp_path / "state",
        processing=ProcessingConfig(ctf=ctf_step(tmp_path), work_dir=work),
    )
    with pytest.raises(InvalidRequestError, match="state or work"):
        SettingsStore(with_work).set_section(
            "roots", [{"name": "krios", "path": str(tmp_path / "data/krios")}], by="a", version=0
        )


def test_the_roots_are_swapped_as_a_whole(tmp_path: Path) -> None:
    # Regression: the roots and their real paths were two tables, assigned one after the other:
    # a request between the two saw the open root with the path of the restricted one.
    pub, secret = tmp_path / "pub", tmp_path / "secret"
    for d in (pub, secret):
        d.mkdir()
        (d / "s.mrc").write_bytes(b"x")
    rs = RootSet([RootConfig("a", pub), RootConfig("b", secret, frozenset({"staff"}))])
    nobody = User("eve")
    parse = rs._parse

    def swap_meanwhile(logical, user, table):  # another request changes the roots right then
        out = parse(logical, user, table)
        rs.replace([RootConfig("a", secret, frozenset({"staff"})), RootConfig("b", pub)])
        return out

    rs._parse = swap_meanwhile  # type: ignore[method-assign]
    assert rs.resolve("a/s.mrc", nobody).real == pub / "s.mrc"  # all of the old set, or nothing
    rs._parse = parse  # type: ignore[method-assign]
    with pytest.raises(AccessDeniedError):
        rs.resolve("a/s.mrc", nobody)  # the new set: a is the restricted one now


def test_a_restore_checks_the_settings_before_it_moves_anything(tmp_path: Path) -> None:
    # Regression: a backup said to hold settings but without the file left the server with
    # none at all (a root narrowed in the page open again), the index already replaced.
    state = tmp_path / "state"
    settings = Settings(state_dir=state)
    store = SettingsStore(settings)
    store.set_account("bob", ["cryoem"], False, by="alice", version=0)
    Index(state / "live.sqlite").close()
    made = backups.backup(settings, tmp_path / "backups")
    (made / FILE_NAME).unlink()  # an incomplete copy
    before = (state / FILE_NAME).read_text()
    index_before = (state / "live.sqlite").stat().st_mtime_ns
    with pytest.raises(ConfigError, match="nothing was restored"):
        backups.restore(settings, made, force=True, with_settings=True)
    assert (state / FILE_NAME).read_text() == before
    assert (state / "live.sqlite").stat().st_mtime_ns == index_before


def test_repairing_the_index_does_not_bring_back_old_roles(tmp_path: Path) -> None:
    # Regression: one --force rolled back the settings too, silently; the command line tells
    # operators to restore with --force when the index is damaged.
    state = tmp_path / "state"
    settings = Settings(state_dir=state)
    SettingsStore(settings).set_account("bob", ["cryoem"], True, by="alice", version=0)
    Index(state / "live.sqlite").close()
    made = backups.backup(settings, tmp_path / "backups")
    SettingsStore(settings).remove_account("bob", by="alice", version=1)  # bob no longer admin
    done = backups.restore(settings, made, force=True)
    assert done["restored"] == {"index": True, "settings": False}
    assert not SettingsStore(settings).is_admin("bob", frozenset())


def test_backups_with_settings_are_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: a backup holding the settings was taken for "something else" and never pruned.
    settings = Settings(state_dir=tmp_path / "state")
    SettingsStore(settings).set_account("bob", ["x"], False, by="a", version=0)
    for stamp in ("2026-01-01T000000Z", "2026-01-02T000000Z", "2026-01-03T000000Z"):
        monkeypatch.setattr(backups, "_now", lambda stamp=stamp: stamp)
        backups.backup(settings, tmp_path / "backups")
    assert backups.prune(tmp_path / "backups", 1) == ["2026-01-01T000000Z", "2026-01-02T000000Z"]


def test_the_command_line_restores_settings_only_when_asked(tmp_path: Path, capsys) -> None:
    # Regression: a backup with settings and no index made the command crash after restoring.
    from imagebio.api import cli

    state = tmp_path / "state"
    config = tmp_path / "imagebio.toml"
    config.write_text(f'[live]\nstate_dir = "{state}"\n')
    SettingsStore(Settings(state_dir=state)).set_account("bob", ["x"], False, by="a", version=0)
    assert cli.main(["backup", str(tmp_path / "backups"), "--config", str(config)]) == 0
    made = next((tmp_path / "backups").iterdir())
    capsys.readouterr()
    assert cli.main(["restore", str(made), "--config", str(config), "--force"]) == 2
    assert "--settings" in capsys.readouterr().err
    assert cli.main(["restore", str(made), "--config", str(config), "--force", "--settings"]) == 0
    assert "restored the settings of the settings page" in capsys.readouterr().out


def test_a_server_holds_its_state_directory_so_no_restore_runs_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a server that never opened the Live index did not hold the state directory,
    # and a restore of the settings ran under it.
    from imagebio.api import cli

    state = tmp_path / "state"
    config = tmp_path / "imagebio.toml"
    (tmp_path / "d").mkdir()
    config.write_text(f'[live]\nstate_dir = "{state}"\n[[roots]]\nname = "d"\npath = "{tmp_path / "d"}"\n')
    SettingsStore(Settings(state_dir=state)).set_account("bob", ["x"], False, by="a", version=0)
    made = backups.backup(Settings(state_dir=state), tmp_path / "backups")
    seen: list[str] = []

    def run(app, **kw):  # while the server runs
        try:
            backups.restore(Settings(state_dir=state), made, force=True, with_settings=True)
            seen.append("restored")
        except ConfigError as e:
            seen.append(str(e))

    monkeypatch.setattr("uvicorn.run", run)
    assert cli.main(["serve", "--config", str(config)]) == 0
    assert seen == ["the state directory is in use: stop the server before restoring"]


def test_with_local_directories_the_page_does_not_replace_them(tmp_path: Path) -> None:
    # Regression: roots saved in the page while serving --local data_a replaced the directories
    # of a later serve --local data_b.
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
    state = tmp_path / "state"
    state.mkdir()
    saved = {
        "schema": "imagebio.settings",
        "version": 1,
        "sections": {"roots": [{"name": "a", "path": str(tmp_path / "a")}]},
    }
    (state / FILE_NAME).write_text(json.dumps(saved))
    store = SettingsStore(Settings.local([tmp_path / "b"], state_dir=state))
    assert [r.name for r in store.effective.roots] == ["b"]
    assert store.sections()["roots"]["source"] == "file"
    with pytest.raises(InvalidRequestError, match="command line"):
        store.set_section("roots", [{"name": "b", "path": str(tmp_path / "b")}], by="local", version=1)


@pytest.mark.parametrize(
    "body",
    [
        {"version": 0, "value": 5},
        {"version": 0, "value": [{"name": ["x"], "kind": "ntfy", "url": "https://n.example/t"}]},
        {"version": 0, "value": {"kv": 10**400}},
    ],
)
def test_malformed_bodies_are_refused_not_failed(tree: Tree, tmp_path: Path, body: dict) -> None:
    # Regression: these were described before they were checked, and answered 500.
    with make_client(
        tree, auth_mode="proxy", state_dir=tmp_path / "state", admins=frozenset({"root"})
    ) as client:
        for section in ("alerts", "live", "microscope"):
            r = put(client, f"{SETTINGS}/{section}", body)
            assert r.status_code == 400, (section, body, r.text)
        bad_groups = {"version": 0, "name": "bob", "groups": [{}], "admin": False}
        assert put(client, "/api/admin/accounts", bad_groups).status_code == 400
        deep = "[" * 100_000 + "]" * 100_000
        r = client.put(
            f"{SETTINGS}/alerts", content=deep.encode(), headers={**ADMIN, "Content-Type": "application/json"}
        )
        assert r.status_code in (400, 413)
        surrogate = b'{"version": 0, "name": "\\ud800", "groups": [], "admin": false}'
        r = client.put(
            "/api/admin/accounts", content=surrogate, headers={**ADMIN, "Content-Type": "application/json"}
        )
        assert r.status_code == 400
        assert version(client) == 0


def test_names_that_could_pass_for_another_are_refused() -> None:
    # Regression: invisible characters and C1 controls were accepted in the names given groups
    for name in ("bob\xad", "bob\x85", "​bob", "bo‮b", "bob ", "a,b", ""):
        assert not valid_user_name(name), repr(name)
    assert valid_user_name("bob@lab") and valid_user_name("Émilie Dupont")


def test_another_server_on_the_same_state_is_not_overwritten(tmp_path: Path) -> None:
    # Regression: the version was checked in memory only: two servers on one state directory
    # overwrote each other, and a role taken away stayed on the other one until it restarted.
    settings = Settings(state_dir=tmp_path / "state", admins=frozenset({"alice"}))
    first, second = SettingsStore(settings), SettingsStore(settings)
    second.set_account("bob", ["x"], True, by="alice", version=0)
    with pytest.raises(ConflictError):
        first.set_section("microscope", {"kv": 200}, by="alice", version=0)
    assert first.is_admin("bob", frozenset())  # it read the other's change on the way
    second.remove_account("bob", by="alice", version=1)
    first._looked_at = 0.0  # a second has passed
    first.refresh()
    assert not first.is_admin("bob", frozenset())


def test_a_change_whose_log_cannot_be_written_is_still_applied(tmp_path: Path, monkeypatch) -> None:
    # Regression: the log was written before the change was applied: a full disk gave an error
    # for a change saved on disk and only applied at the next restart.
    store = SettingsStore(Settings(state_dir=tmp_path / "state"))

    def full(entry: dict) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_log", full)
    store.set_account("bob", ["x"], False, by="a", version=0)
    assert (
        store.groups_of("bob") == {"x"} and SettingsStore(Settings(state_dir=tmp_path / "state")).version == 1
    )


def test_a_trailing_no_break_space_is_another_user() -> None:
    # Regression: strip() made "alice\xa0" into "alice", and the name now carries a role
    from types import SimpleNamespace

    from starlette.requests import Request

    from imagebio.api.security import current_user

    settings = Settings(auth_mode="proxy", admins=frozenset({"alice"}))
    app = SimpleNamespace(state=SimpleNamespace())  # no settings page: the file alone
    for raw, name, admin in ((b"alice\xa0", "alice\xa0", False), (b" alice\t", "alice", True)):
        request = Request({"type": "http", "headers": [(b"x-forwarded-user", raw)], "app": app})
        user = current_user(request, settings)
        assert (user.name, user.admin) == (name, admin)
