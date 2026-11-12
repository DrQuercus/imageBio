"""The settings page through the API: for administrators only, every change applied at once,
nothing secret sent, nothing read before the sender is known, and kept across a restart."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures.api import ALICE, BOB, Tree, build_tree, make_client

ADMIN = {"X-Forwarded-User": "root", "X-Forwarded-Groups": "it"}  # named in the file
CAROL = {"X-Forwarded-User": "carol", "X-Forwarded-Groups": "imagebio-admin"}  # by a group of the file
SECRET = "s3cret-password-of-the-relay"
TOPIC = "https://ntfy.example.org/lab-topic-0b7c"
SETTINGS = "/api/admin/settings"


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path / "data")


@pytest.fixture
def client(tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RELAY_PASSWORD", SECRET)
    from imagebio.core.config import AlertChannel, SmtpConfig

    with make_client(
        tree,
        auth_mode="proxy",
        state_dir=tmp_path / "state",
        admins=frozenset({"root"}),
        admin_groups=frozenset({"imagebio-admin"}),
        alert_channels=(AlertChannel("phone", "ntfy", TOPIC),),
        smtp=SmtpConfig("smtp.example.org", password_env="RELAY_PASSWORD"),
    ) as c:
        yield c


def put(client, url: str, body: object, who: dict[str, str] = ADMIN, **headers: str):
    return client.put(
        url, content=json.dumps(body), headers={**who, "Content-Type": "application/json", **headers}
    )


def settings(client, who: dict[str, str] = ADMIN) -> dict:
    r = client.get(SETTINGS, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def test_only_administrators_see_or_change_the_settings(client) -> None:
    me = client.get("/api/me", headers=ALICE).json()
    assert me["admin"] is False and me["groups_given"] == []
    assert client.get("/api/me", headers=ADMIN).json()["admin"] is True
    assert client.get("/api/me", headers=CAROL).json()["admin"] is True
    version = settings(client)["version"]
    for who in (ALICE, BOB, {}):
        assert client.get(SETTINGS, headers=who).status_code in (401, 403)
        assert client.get("/api/admin/log", headers=who).status_code in (401, 403)
        assert put(
            client, f"{SETTINGS}/microscope", {"version": version, "value": {"kv": 200}}, who
        ).status_code in (401, 403)
        r = put(
            client,
            "/api/admin/accounts",
            {"version": version, "name": "bob", "groups": ["cryoem"], "admin": True},
            who,
        )
        assert r.status_code in (401, 403)
        assert client.delete(f"{SETTINGS}/roots", params={"version": version}, headers=who).status_code in (
            401,
            403,
        )
    assert settings(client)["version"] == version  # nothing changed


def test_a_large_body_is_not_read_for_someone_who_may_not_send_it(client) -> None:
    huge = "x" * (2 * 1024 * 1024)
    assert put(client, f"{SETTINGS}/roots", {"version": 0, "value": huge}, ALICE).status_code == 403
    assert put(client, f"{SETTINGS}/roots", {"version": 0, "value": huge}).status_code == 413
    r = client.put(
        f"{SETTINGS}/roots", content=b"[1, 2]", headers={**ADMIN, "Content-Type": "application/json"}
    )
    assert r.status_code == 400
    r = client.put(
        f"{SETTINGS}/roots", content=b'{"version": 0}', headers={**ADMIN, "Content-Type": "text/plain"}
    )
    assert r.status_code == 400
    assert client.put(f"{SETTINGS}/nothing", content=b"{}", headers=ADMIN).status_code in (400, 404)


def test_nothing_secret_is_sent_and_users_never_see_addresses(client) -> None:
    page = client.get(SETTINGS, headers=ADMIN).text
    assert SECRET not in page
    data = json.loads(page)
    assert data["configuration"]["smtp"]["password_env"] == "RELAY_PASSWORD"
    assert data["configuration"]["smtp"]["password_set"] is True
    assert data["sections"]["alerts"]["value"][0]["url"] == TOPIC  # an administrator configures it
    channels = client.get("/api/live/alerts/channels", headers=ALICE).text
    assert "ntfy.example.org" not in channels and "phone" in channels  # a user picks by name


def test_groups_given_here_open_the_roots_at_once_and_are_taken_back(client) -> None:
    bob = lambda: {r["name"] for r in client.get("/api/fs/roots", headers=BOB).json()["roots"]}  # noqa: E731
    assert bob() == {"light", "public"}
    assert client.get("/api/fs/list", params={"path": "cryo"}, headers=BOB).status_code == 403
    version = settings(client)["version"]
    r = put(
        client,
        "/api/admin/accounts",
        {"version": version, "name": "bob", "groups": ["cryoem"], "admin": False},
    )
    assert r.status_code == 200, r.text
    assert bob() == {"cryo", "light", "public"}
    assert client.get("/api/fs/list", params={"path": "cryo"}, headers=BOB).status_code == 200
    me = client.get("/api/me", headers=BOB).json()
    assert me["groups_given"] == ["cryoem"] and set(me["groups"]) == {"cryoem", "lm"}
    row = next(a for a in r.json()["accounts"] if a["name"] == "bob")
    assert row["groups"] == ["cryoem"] and row["proxy_groups"] == ["lm"]
    # the role, given and taken back by another administrator; never by oneself
    version = r.json()["version"]
    r = put(
        client,
        "/api/admin/accounts",
        {"version": version, "name": "bob", "groups": ["cryoem"], "admin": True},
    )
    assert client.get(SETTINGS, headers=BOB).status_code == 200
    r = put(
        client,
        "/api/admin/accounts",
        {"version": r.json()["version"], "name": "bob", "groups": [], "admin": False},
        BOB,
    )
    assert r.status_code == 400 and "themselves" in r.json()["error"]["message"]
    r = client.delete(
        "/api/admin/accounts", params={"name": "bob", "version": settings(client)["version"]}, headers=ADMIN
    )
    assert r.status_code == 200
    assert bob() == {"light", "public"} and client.get(SETTINGS, headers=BOB).status_code == 403


def test_roots_and_the_rest_apply_at_once_and_survive_a_restart(client, tree: Tree, tmp_path: Path) -> None:
    (tree.base / "extra").mkdir()
    (tree.base / "extra" / "note.txt").write_text("x")
    version = settings(client)["version"]
    roots = [
        {"name": "cryo", "path": str(tree.base / "cryo"), "groups": ["cryoem"]},
        {"name": "extra", "path": str(tree.base / "extra"), "groups": ["lm"], "kv": 200},
    ]
    r = put(client, f"{SETTINGS}/roots", {"version": version, "value": roots})
    assert r.status_code == 200, r.text
    assert r.json()["sections"]["roots"]["source"] == "interface"
    assert client.get("/api/fs/list", params={"path": "extra"}, headers=BOB).status_code == 200
    assert client.get("/api/fs/list", params={"path": "public"}, headers=BOB).status_code == 403  # gone
    # the microscope, the Live settings and the alert channels
    version = r.json()["version"]
    r = put(client, f"{SETTINGS}/microscope", {"version": version, "value": {"kv": 200, "cs_mm": 1.4}})
    assert r.json()["sections"]["microscope"]["value"] == {
        "kv": 200.0,
        "cs_mm": 1.4,
        "amplitude_contrast": 0.07,
    }
    r = put(
        client, f"{SETTINGS}/live", {"version": r.json()["version"], "value": {"report_after_idle_min": 30}}
    )
    channels = [{"name": "mail", "kind": "email", "to": ["lab@example.org"]}]
    r = put(client, f"{SETTINGS}/alerts", {"version": r.json()["version"], "value": channels})
    assert r.status_code == 200
    assert [c["name"] for c in client.get("/api/live/alerts/channels", headers=ALICE).json()["channels"]] == [
        "mail"
    ]
    # a stale version is refused; so is what the file could not hold, with no path in the answer
    stale = put(client, f"{SETTINGS}/microscope", {"version": version, "value": {"kv": 120}})
    assert stale.status_code == 409
    bad = put(
        client,
        f"{SETTINGS}/roots",
        {"version": r.json()["version"], "value": [{"name": "x", "path": "/etc"}]},
    )
    assert bad.status_code == 400 and "/etc" not in bad.text and str(tmp_path) not in bad.text
    log = client.get("/api/admin/log", headers=ADMIN).json()["entries"]
    assert [e["by"] for e in log] == ["root"] * 4 and "lab@example.org" not in json.dumps(log)

    # another server on the same state: the same settings
    from imagebio.admin.store import SettingsFileError
    from imagebio.core.config import SmtpConfig

    # a server whose file lost what these settings need (the mail relay) does not start on half
    with pytest.raises(SettingsFileError, match="smtp"):
        make_client(tree, auth_mode="proxy", state_dir=tmp_path / "state", admins=frozenset({"root"}))
    with make_client(
        tree,
        auth_mode="proxy",
        state_dir=tmp_path / "state",
        admins=frozenset({"root"}),
        smtp=SmtpConfig("smtp.example.org"),
    ) as restarted:
        assert {r["name"] for r in restarted.get("/api/fs/roots", headers=BOB).json()["roots"]} == {"extra"}
        data = restarted.get(SETTINGS, headers=ADMIN).json()
        assert data["sections"]["live"]["value"]["report_after_idle_min"] == 30
        # back to the file, section by section
        r = restarted.delete(f"{SETTINGS}/roots", params={"version": data["version"]}, headers=ADMIN)
        assert r.json()["sections"]["roots"]["source"] == "file"
        assert {x["name"] for x in restarted.get("/api/fs/roots", headers=BOB).json()["roots"]} == {
            "light",
            "public",
        }


def test_a_change_sent_from_another_site_is_refused(client) -> None:
    version = settings(client)["version"]
    r = put(
        client,
        f"{SETTINGS}/microscope",
        {"version": version, "value": {"kv": 200}},
        Origin="https://evil.example",
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site_request"
    assert settings(client)["version"] == version
