"""The settings changed in the settings page: checked like the file, saved whole, versioned,
logged without addresses, and refused when they could not be read back."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from imagebio.admin.store import (
    FILE_NAME,
    HISTORY_DIR,
    LOG_NAME,
    SettingsFileError,
    SettingsStore,
    effective_settings,
)
from imagebio.core.config import AlertChannel, ConfigError, RootConfig, Settings, SmtpConfig
from imagebio.core.errors import ConflictError, InvalidRequestError

SECRET_URL = "https://ntfy.example.org/lab-secret-topic-8f3a"


@pytest.fixture
def base(tmp_path: Path) -> Settings:
    for d in ("data/krios/project", "data/glacios", "data/new", "elsewhere"):
        (tmp_path / d).mkdir(parents=True)
    return Settings(
        roots=(
            RootConfig("krios", tmp_path / "data/krios", frozenset({"cryoem"})),
            RootConfig("glacios", tmp_path / "data/glacios"),
        ),
        state_dir=tmp_path / "state",
        admins=frozenset({"alice"}),
        admin_groups=frozenset({"imagebio-admin"}),
        alert_channels=(AlertChannel("phone", "ntfy", "https://ntfy.example.org/old"),),
        smtp=SmtpConfig("smtp.example.org"),
    )


def roots_of(settings: Settings) -> dict[str, tuple[str, list[str]]]:
    return {r.name: (str(r.path), sorted(r.groups)) for r in settings.roots}


def test_sections_lie_over_the_file_and_are_checked_like_it(base: Settings, tmp_path: Path) -> None:
    eff = effective_settings(
        base,
        {
            "microscope": {"kv": 200},
            "live": {"report_after_idle_min": 30, "mean_free_path_nm": None, "drift": True},
            "alerts": [{"name": "mail", "kind": "email", "to": ["a@example.org"]}],
        },
    )
    assert (eff.kv, eff.cs_mm) == (200.0, base.cs_mm)  # only what was set
    assert (eff.live_report_idle_s, eff.live_mfp_nm, eff.live_drift, eff.live_ctf) == (
        1800.0,
        None,
        True,
        True,
    )
    assert [c.name for c in eff.alert_channels] == ["mail"]
    assert eff.roots == base.roots and eff.admins == base.admins  # the rest is the file's
    for bad in (
        {"microscope": {"kv": 5000}},  # what the file may not hold either
        {"microscope": {"kv": True}},  # a boolean is not a voltage
        {"microscope": {"voltage": 300}},
        {"live": {"report_after_idle_min": 3}},
        {"live": {"metrics": "yes"}},
        {"live": {"interval": 1}},  # not one the page may change
        {"alerts": [{"name": "x", "kind": "ntfy", "url": "ftp://a/b"}]},
        {"roots": [{"name": "a", "path": "relative/path"}]},
        {"roots": [{"name": "a", "path": str(tmp_path), "groups": ["bad group"]}]},
        {"roots": [{"name": "a", "path": str(tmp_path)}, {"name": "a", "path": str(tmp_path)}]},
        {"server": {"host": "0.0.0.0"}},  # never the address, the proxy or the authentication
        {"processing": {}},
    ):
        with pytest.raises(ConfigError):
            effective_settings(base, bad)
    # an e-mail channel needs the mail relay of the file
    with pytest.raises(ConfigError):
        effective_settings(
            Settings(state_dir=base.state_dir), {"alerts": [{"name": "m", "kind": "email", "to": ["a@b.c"]}]}
        )


def test_a_change_is_saved_whole_versioned_logged_and_applied(base: Settings, tmp_path: Path) -> None:
    store = SettingsStore(base, clock=lambda: 1000.0)
    seen: list[Settings] = []
    store.on_change(seen.append)
    assert store.version == 0 and not (tmp_path / "state").exists()  # nothing written until a change
    value = [
        {"name": "krios", "path": str(tmp_path / "data/krios"), "groups": ["cryoem", "krios"]},
        # inside a root of the file: a project of the Krios, for another group
        {"name": "project", "path": str(tmp_path / "data/krios/../krios/project"), "groups": []},
    ]
    store.set_section("roots", value, by="alice", version=0)
    assert store.version == 1 and roots_of(seen[-1]) == {
        "krios": (str(tmp_path / "data/krios"), ["cryoem", "krios"]),
        "project": (str(tmp_path / "data/krios/project"), []),  # saved as its real path
    }
    saved = json.loads((tmp_path / "state" / FILE_NAME).read_text())
    assert saved["version"] == 1 and saved["updated_by"] == "alice"
    assert [r["path"] for r in saved["sections"]["roots"]] == [
        str(tmp_path / "data/krios"),
        str(tmp_path / "data/krios/project"),
    ]
    assert stat.S_IMODE(os.stat(tmp_path / "state" / FILE_NAME).st_mode) == 0o600
    # a change made on the version an other administrator already changed is refused
    with pytest.raises(ConflictError):
        store.set_section("microscope", {"kv": 200}, by="bob", version=0)
    store.set_section("alerts", [{"name": "phone", "kind": "ntfy", "url": SECRET_URL}], by="alice", version=1)
    store.reset_section("roots", by="alice", version=2)
    assert roots_of(store.effective) == roots_of(base)
    assert sorted(p.name for p in (tmp_path / "state" / HISTORY_DIR).iterdir()) == [
        "settings-v000001.json",
        "settings-v000002.json",
    ]
    log = store.log()
    assert [e["version"] for e in log] == [3, 2, 1] and all(e["by"] == "alice" for e in log)
    assert log[2]["what"] == "dossiers de données : +project, -glacios, ~krios"
    assert log[1]["what"] == "canaux d'alerte : ~phone"
    log_text = (tmp_path / "state" / LOG_NAME).read_text()
    assert "ntfy.example.org" not in log_text and str(tmp_path) not in log_text  # no address, no path
    # read again by the next server, with the same result
    again = SettingsStore(base)
    assert again.version == 3 and [c.url for c in again.effective.alert_channels] == [SECRET_URL]


def test_what_cannot_be_applied_is_refused_and_nothing_changes(base: Settings, tmp_path: Path) -> None:
    store = SettingsStore(base)
    root = lambda path: [{"name": "x", "path": str(path)}]  # noqa: E731
    for value, said in (
        (root(tmp_path / "elsewhere"), "inside a data root"),
        # next to the roots of the file is not inside them: a root at /data would open /
        (root(tmp_path / "data/new"), "inside a data root"),
        (root(tmp_path / "data/missing"), "not a directory"),
        (root(tmp_path / "data/krios/../../elsewhere"), "inside a data root"),
        (root(tmp_path), "inside a data root"),
    ):
        with pytest.raises(InvalidRequestError, match=said) as refused:
            store.set_section("roots", value, by="alice", version=0)
        assert str(tmp_path) not in str(refused.value)  # no server path in the message
    # a link that leads out of the roots is followed before it is judged
    (tmp_path / "data/krios/link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InvalidRequestError, match="inside a data root"):
        store.set_section("roots", root(tmp_path / "data/krios/link"), by="alice", version=0)
    # a root that would show the server's own state (the settings, the index, the reports)
    state_parent = Settings(
        roots=(RootConfig("all", tmp_path),), state_dir=tmp_path / "data/state", admins=frozenset({"alice"})
    )
    with pytest.raises(InvalidRequestError, match="state or work"):
        SettingsStore(state_parent).set_section("roots", root(tmp_path / "data"), by="alice", version=0)
    assert store.version == 0 and not (tmp_path / "state" / FILE_NAME).exists()
    # parents named in the file open more places
    explicit = Settings(roots=base.roots, state_dir=base.state_dir, root_parents=(tmp_path / "data",))
    SettingsStore(explicit).set_section("roots", root(tmp_path / "data/new"), by="alice", version=0)


def test_accounts_add_groups_and_the_role(base: Settings) -> None:
    store = SettingsStore(base)
    assert store.is_admin("alice", frozenset())  # named by the file
    assert store.is_admin("carol", frozenset({"imagebio-admin"}))  # a group of the file
    assert not store.is_admin("bob", frozenset({"cryoem"}))
    store.set_account("bob", ["cryoem", "cryoem", "krios"], True, by="alice", version=0)
    assert store.groups_of("bob") == {"cryoem", "krios"} and store.is_admin("bob", frozenset())
    # nobody takes the role away from themselves; another administrator can
    with pytest.raises(InvalidRequestError, match="themselves"):
        store.set_account("bob", ["cryoem"], False, by="bob", version=1)
    with pytest.raises(InvalidRequestError, match="themselves"):
        store.remove_account("bob", by="bob", version=1)
    store.remove_account("bob", by="alice", version=1)
    assert store.groups_of("bob") == frozenset() and not store.is_admin("bob", frozenset())
    for name, groups in (("bad,name", []), ("\x07bell", []), (" padded", []), ("eve", ["no space"])):
        with pytest.raises(InvalidRequestError):
            store.set_account(name, groups, False, by="alice", version=2)
    # the users an administrator is shown: given something, named by the file, or seen
    store.saw("dan", frozenset({"imagebio-admin"}))
    rows = {r["name"]: r for r in store.accounts(also=["erin"])}
    assert set(rows) == {"alice", "dan", "erin"}
    assert (rows["alice"]["admin"], rows["dan"]["admin"], rows["erin"]["admin"]) == ("file", "group", None)


def test_a_settings_file_that_cannot_be_used_stops_the_server(base: Settings, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for content in (
        "{not json",
        json.dumps({"schema": "something else", "version": 1}),
        json.dumps({"schema": "imagebio.settings", "version": 1, "sections": {"microscope": {"kv": 9999}}}),
        json.dumps({"schema": "imagebio.settings", "version": 1, "accounts": {"x": {"groups": "cryoem"}}}),
    ):
        (state / FILE_NAME).write_text(content)
        with pytest.raises(SettingsFileError, match="settings"):
            SettingsStore(base)
