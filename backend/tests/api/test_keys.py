"""Personal keys through the API (ADR 015): made by a user of the proxy, shown once, reading
only with the rights of their owner, never administering, never mixed with another identity,
revoked by their owner or an administrator; and the files that plug an assistant in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures.api import ALICE, BOB, Tree, build_tree, make_client
from imagebio.api.keys import AI_FILES

ADMIN = {"X-Forwarded-User": "root", "X-Forwarded-Groups": "it"}
AI_DIR = Path(__file__).resolve().parents[2] / "src" / "imagebio" / "ai"


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path / "data")


@pytest.fixture
def client(tree: Tree, tmp_path: Path):
    with make_client(tree, auth_mode="proxy", state_dir=tmp_path / "state", admins=frozenset({"root"})) as c:
        yield c


def make_key(client, who: dict[str, str] = ALICE, name: str = "laptop", days: int = 30) -> dict:
    r = client.post(
        "/api/me/keys",
        content=json.dumps({"name": name, "days": days}),
        headers={**who, "Content-Type": "application/json"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_a_key_is_shown_once_and_reads_with_the_rights_of_its_owner(client, tmp_path: Path) -> None:
    made = make_key(client)
    key = made["key"]
    assert key.startswith("ibk_") and made["name"] == "laptop" and made["hint"] == key[:8]
    assert key not in (tmp_path / "state" / "keys.json").read_text()
    listed = client.get("/api/me/keys", headers=ALICE).json()
    assert listed["standalone"] is False
    assert [k["id"] for k in listed["keys"]] == [made["id"]]
    assert key not in json.dumps(listed)
    assert client.get("/api/me/keys", headers=BOB).json()["keys"] == []

    me = client.get("/api/me", headers=bearer(key)).json()  # no password, no proxy identity
    assert me["user"] == "alice" and me["admin"] is False
    assert client.get("/api/fs/list", params={"path": "cryo"}, headers=bearer(key)).status_code == 200
    assert (
        client.get("/api/datasets/info", params={"path": "cryo/movie.mrc"}, headers=bearer(key)).status_code
        == 200
    )
    # what alice cannot read, her key cannot either
    assert client.get("/api/fs/list", params={"path": "light"}, headers=bearer(key)).status_code == 403
    assert client.get("/api/fs/list", params={"path": "cryo"}).status_code == 401


def test_a_key_never_writes(client) -> None:
    key = make_key(client)["key"]
    ident = make_key(client, name="other")["id"]
    for method, url in (
        ("POST", "/api/me/keys"),  # no key made with a key
        ("DELETE", f"/api/me/keys/{ident}"),
        ("POST", "/api/live/watches"),
        ("PUT", "/api/admin/accounts"),
        ("PATCH", "/api/me/keys"),
        ("OPTIONS", "/api/me"),
    ):
        r = client.request(
            method, url, content=b"{}", headers={**bearer(key), "Content-Type": "application/json"}
        )
        assert r.status_code == 403, (method, url, r.status_code)
        assert r.json()["error"]["code"] == "read_only_key"
    # with the lowercase scheme and extra spaces, the same
    r = client.post("/api/me/keys", content=b"{}", headers={"Authorization": f"bearer   {key}"})
    assert r.status_code == 403
    assert len(client.get("/api/me/keys", headers=ALICE).json()["keys"]) == 2


def test_a_key_never_administers(client) -> None:
    key = make_key(client, ADMIN)["key"]
    assert client.get("/api/me", headers=ADMIN).json()["admin"] is True
    assert client.get("/api/me", headers=bearer(key)).json()["admin"] is False
    for url in ("/api/admin/settings", "/api/admin/log"):
        assert client.get(url, headers=bearer(key)).status_code == 403


def test_a_key_and_another_identity_together_are_no_identity(client) -> None:
    key = make_key(client)["key"]
    bob_key = make_key(client, BOB, name="bob's")["key"]
    # a key with a proxy identity: which one is meant? none
    assert client.get("/api/me", headers={**bearer(key), **BOB}).status_code == 401
    assert client.get("/api/me", headers={**bearer(key), "X-Forwarded-User": "alice"}).status_code == 401
    two = [("Authorization", f"Bearer {key}"), ("Authorization", f"Bearer {bob_key}")]
    assert client.get("/api/me", headers=two).status_code == 401
    with_basic = [("Authorization", f"Bearer {key}"), ("Authorization", "Basic YWxpY2U6eA==")]
    assert client.get("/api/me", headers=with_basic).status_code == 401
    # groups sent beside a key change nothing
    r = client.get(
        "/api/fs/list", params={"path": "light"}, headers={**bearer(key), "X-Forwarded-Groups": "lm"}
    )
    assert r.status_code == 403


def test_a_wrong_key_is_refused_and_another_bearer_is_not_a_key(client) -> None:
    key = make_key(client)["key"]
    for wrong in (key[:-1], key + "A", "ibk_", "ibk_" + "x" * 43):
        assert client.get("/api/me", headers=bearer(wrong)).status_code == 401
    # a token that is not a key leaves the proxy's identity to decide
    assert client.get("/api/me", headers={"Authorization": "Bearer abc", **BOB}).json()["user"] == "bob"
    assert client.get("/api/me", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_revoked_by_its_owner_or_by_an_administrator(client) -> None:
    first, second = make_key(client), make_key(client, name="second")
    bob = make_key(client, BOB, name="bob's")
    assert client.delete(f"/api/me/keys/{first['id']}", headers=BOB).status_code == 404  # not his
    assert client.get("/api/me", headers=bearer(first["key"])).status_code == 200
    r = client.delete(f"/api/me/keys/{first['id']}", headers=ALICE)
    assert r.status_code == 200 and [k["id"] for k in r.json()["keys"]] == [second["id"]]
    assert client.get("/api/me", headers=bearer(first["key"])).status_code == 401
    assert client.delete("/api/me/keys/not-a-key-id", headers=ALICE).status_code == 400

    accounts = {a["name"]: a for a in client.get("/api/admin/settings", headers=ADMIN).json()["accounts"]}
    assert accounts["alice"]["keys"] == 1 and accounts["bob"]["keys"] == 1
    assert client.delete("/api/admin/keys", params={"name": "alice"}, headers=BOB).status_code == 403
    r = client.delete("/api/admin/keys", params={"name": "alice"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert client.get("/api/me", headers=bearer(second["key"])).status_code == 401
    assert client.get("/api/me", headers=bearer(bob["key"])).status_code == 200


def test_the_groups_of_a_key(client) -> None:
    """Those the proxy gave its owner when it was made, and those given in the settings page."""
    key = make_key(client)["key"]  # alice: cryoem, staff
    assert client.get("/api/fs/list", params={"path": "light"}, headers=bearer(key)).status_code == 403
    version = client.get("/api/admin/settings", headers=ADMIN).json()["version"]
    r = client.put(
        "/api/admin/accounts",
        content=json.dumps({"version": version, "name": "alice", "groups": ["lm"], "admin": True}),
        headers={**ADMIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/fs/list", params={"path": "light"}, headers=bearer(key)).status_code == 200
    assert client.get("/api/fs/list", params={"path": "cryo"}, headers=bearer(key)).status_code == 200
    # an administrator by the page; her key still does not administer
    assert client.get("/api/me", headers=ALICE).json()["admin"] is True
    assert client.get("/api/admin/settings", headers=bearer(key)).status_code == 403


def test_making_a_key_checks_what_it_is_given(client) -> None:
    for body in (
        {"name": "", "days": 30},
        {"name": "k", "days": 0},
        {"name": "k", "days": 400},
        {"name": "k", "x": 1},
    ):
        r = client.post(
            "/api/me/keys", content=json.dumps(body), headers={**ALICE, "Content-Type": "application/json"}
        )
        assert r.status_code == 400, body
    assert (
        client.post(
            "/api/me/keys", content=b"[]", headers={**ALICE, "Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/me/keys", content=b"{}", headers={"Content-Type": "application/json"}).status_code
        == 401
    )
    assert make_key(client, days=366)["expires"] > 0
    assert client.get("/api/me/keys", headers=ALICE).json()["keys"][0]["name"] == "laptop"


def test_keys_need_the_proxy_secret_too(tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "p" * 40
    monkeypatch.setenv("IMAGEBIO_PROXY_SECRET", secret)
    with make_client(tree, auth_mode="proxy", state_dir=tmp_path / "state") as client:
        proxied = {"X-Imagebio-Proxy-Secret": secret}
        key = make_key(client, {**ALICE, **proxied})["key"]
        assert client.get("/api/me", headers={**bearer(key), **proxied}).status_code == 200
        assert client.get("/api/me", headers=bearer(key)).status_code == 401  # not through the proxy


def test_standalone_mode_has_no_keys(tree: Tree, tmp_path: Path) -> None:
    with make_client(tree, state_dir=tmp_path / "state") as client:
        assert client.get("/api/me/keys").json() == {"standalone": True, "keys": []}
        r = client.post(
            "/api/me/keys",
            content=json.dumps({"name": "k", "days": 3}),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert not (tmp_path / "state" / "keys.json").exists()


def test_the_description_of_the_api_is_for_users_only(client) -> None:
    assert client.get("/api/openapi.json").status_code == 401
    key = make_key(client)["key"]
    for who in (ALICE, bearer(key)):
        r = client.get("/api/openapi.json", headers=who)
        assert r.status_code == 200, r.text
        spec = r.json()
        assert spec["openapi"].startswith("3.") and "/api/fs/list" in spec["paths"]
        assert "/api/live/watches/{watch_id}/items" in spec["paths"]
    assert client.get("/openapi.json", headers=ALICE).status_code in (401, 404)
    assert client.get("/docs", headers=ALICE).status_code in (401, 404)


@pytest.mark.parametrize("name", sorted(AI_FILES))
def test_the_files_that_plug_an_assistant_in(client, name: str) -> None:
    assert client.get(f"/api/ai/{name}").status_code == 401
    r = client.get(f"/api/ai/{name}", headers=ALICE)
    assert r.status_code == 200
    assert r.content == (AI_DIR / name).read_bytes()
    assert r.headers["content-type"].startswith(AI_FILES[name])
    assert "attachment" in r.headers["content-disposition"]


@pytest.mark.parametrize(
    "name", ["__init__.py", "..%2Fkeys.py", "../api/keys.py", "imagebio_ai.pyc", "skill.md"]
)
def test_nothing_else_is_downloaded_from_there(client, name: str) -> None:
    assert client.get(f"/api/ai/{name}", headers=ALICE).status_code == 404
