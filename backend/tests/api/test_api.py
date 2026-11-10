from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import mrcfile
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fixtures.api import ALICE, BOB, Tree, build_tree, decode, make_client
from imagebio.core.model import Dataset
from imagebio.io.registry import Reader, Registry, default_registry
from imagebio.proc.binning import bin_mean

PLANE = "/api/datasets/plane"
INFO = "/api/datasets/info"
STATS = "/api/datasets/stats"


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Tree:
    return build_tree(tmp_path_factory.mktemp("api"))


@pytest.fixture(scope="module")
def local(tree: Tree):  # standalone mode: one local user who sees every root
    with make_client(tree) as client:
        yield client


@pytest.fixture(scope="module")
def proxied(tree: Tree):
    with make_client(tree, auth_mode="proxy") as client:
        yield client


# ── planes ───────────────────────────────────────────────────────────────────


def test_health_and_identity(local, proxied):
    assert local.get("/healthz").json()["status"] == "ok"
    assert local.get("/api/me").json() == {"user": "local", "groups": [], "auth_mode": "none"}
    me = proxied.get("/api/me", headers=ALICE).json()
    assert me == {"user": "alice", "groups": ["cryoem", "staff"], "auth_mode": "proxy"}


@pytest.mark.parametrize("encoding", ["identity", "gzip", "zstd", "gzip, zstd;q=0.5", "br"])
def test_planes_are_bit_exact_for_every_encoding(local, tree: Tree, encoding: str):
    for t in (0, 4):
        r = local.get(PLANE, params={"path": "cryo/movie.mrc", "t": t}, headers={"Accept-Encoding": encoding})
        assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
        assert (r.headers["x-dtype"], r.headers["x-shape"], r.headers["x-bin"]) == ("float32", "48,64", "1")
        assert r.headers["x-region"] == "0,0,64,48" and r.headers["vary"] == "Accept-Encoding"
        np.testing.assert_array_equal(decode(r), tree.movie[t])
    # integer data compresses: the encoding is really used when the client accepts it
    r = local.get(
        PLANE, params={"path": "cryo/movie.eer", "opt.group": 12}, headers={"Accept-Encoding": encoding}
    )
    expected = {"identity": None, "br": None, "gzip": "gzip", "zstd": "zstd", "gzip, zstd;q=0.5": "zstd"}[
        encoding
    ]
    assert r.headers.get("content-encoding") == expected
    np.testing.assert_array_equal(decode(r), tree.eer.truth())


def test_every_reader_end_to_end(local, tree: Tree):
    r = local.get(PLANE, params={"path": "public/gray16.tif"})
    assert r.headers["x-dtype"] == "uint16"
    np.testing.assert_array_equal(decode(r), tree.gray16)
    for t, z, c in ((0, 0, 0), (1, 2, 1), (0, 1, 1)):  # hyperstack indices
        r = local.get(PLANE, params={"path": "light/hyper.tif", "t": t, "z": z, "c": c})
        np.testing.assert_array_equal(decode(r), tree.hyper[t, z, c])
    for c in range(3):  # RGB channels
        r = local.get(PLANE, params={"path": "light/rgb.png", "c": c})
        np.testing.assert_array_equal(decode(r), tree.rgb[..., c])
    info = local.get(INFO, params={"path": "light/hyper.tif"}).json()
    assert info["info"]["shape"] == {"t": 2, "c": 2, "z": 3, "y": 20, "x": 30}
    assert info["path"] == "light/hyper.tif" and info["file"]["name"] == "hyper.tif"


def test_eer_options_through_the_query(local, tree: Tree):
    info = local.get(INFO, params={"path": "cryo/movie.eer", "opt.group": 4}).json()["info"]
    assert info["shape"]["t"] == 3 and info["metadata"]["group"] == 4
    r = local.get(PLANE, params={"path": "cryo/movie.eer", "opt.group": 4, "t": 2})
    np.testing.assert_array_equal(decode(r), tree.eer.truth(8, 12))
    r = local.get(PLANE, params={"path": "cryo/movie.eer", "opt.group": 4, "opt.superres": 1})
    assert r.headers["x-shape"] == "64,96"
    assert local.get(PLANE, params={"path": "cryo/movie.eer", "opt.bogus": 1}).status_code == 400
    assert local.get(PLANE, params={"path": "cryo/movie.eer", "opt.group": 0}).status_code == 400
    assert local.get(PLANE, params={"path": "cryo/movie.mrc", "opt.group": 2}).status_code == 400


def test_bin_and_region(local, tree: Tree):
    frame = tree.movie[1]
    r = local.get(PLANE, params={"path": "cryo/movie.mrc", "t": 1, "bin": 5})
    assert (r.headers["x-dtype"], r.headers["x-shape"], r.headers["x-bin"]) == ("float32", "10,13", "5")
    np.testing.assert_array_equal(decode(r), bin_mean(frame, 5))
    pixel = json.loads(r.headers["x-pixel-size"])
    assert pixel["value"] == pytest.approx(6.25) and pixel["unit"] == "Å" and pixel["source"] == "header"
    r = local.get(PLANE, params={"path": "cryo/movie.mrc", "t": 1, "x": 10, "y": 7, "w": 21, "h": 30})
    assert r.headers["x-region"] == "10,7,21,30" and r.headers["x-dtype"] == "float32"
    np.testing.assert_array_equal(decode(r), frame[7:37, 10:31])
    r = local.get(
        PLANE, params={"path": "cryo/movie.mrc", "t": 1, "x": 10, "y": 7, "w": 21, "h": 30, "bin": 4}
    )
    np.testing.assert_array_equal(decode(r), bin_mean(frame[7:37, 10:31], 4))
    r = local.get(PLANE, params={"path": "public/gray16.tif", "bin": 2})  # integers are averaged exactly
    np.testing.assert_array_equal(decode(r), bin_mean(tree.gray16, 2))


@pytest.mark.parametrize("params", [
    {"t": 5}, {"c": 1}, {"z": 1}, {"t": -1}, {"t": "x"}, {"bin": 0}, {"bin": 65}, {"bin": 1.5},
    {"x": 0}, {"x": 0, "y": 0, "w": 65, "h": 1}, {"x": 60, "y": 0, "w": 5, "h": 1}, {"x": 0, "y": 0, "w": 0, "h": 1},
    {"bogus": 1}, {"opt.": 1}, {"opt.a b": 1},
])  # fmt: skip
def test_invalid_requests_are_400_json(local, params: dict[str, Any]):
    r = local.get(PLANE, params={"path": "cryo/movie.mrc", **params})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "invalid_request"


def test_repeated_parameters_and_missing_path(local):
    assert local.get(PLANE + "?path=cryo/movie.mrc&t=0&t=1").status_code == 400
    assert local.get(PLANE + "?path=cryo/movie.mrc&path=public/gray16.tif").status_code == 400
    assert local.get(PLANE).status_code == 400
    assert local.get(PLANE, params={"path": "cryo/session"}).status_code == 400  # a directory


def test_response_size_limit(tree: Tree):
    with make_client(tree, max_response_pixels=1000) as client:
        r = client.get(PLANE, params={"path": "cryo/movie.mrc"})
        assert r.status_code == 400 and "larger bin or request a region" in r.json()["error"]["message"]
        assert client.get(PLANE, params={"path": "cryo/movie.mrc", "bin": 2}).status_code == 200
        assert (
            client.get(PLANE, params={"path": "cryo/movie.mrc", "x": 0, "y": 0, "w": 30, "h": 30}).status_code
            == 200
        )


def test_etag_304_and_reread_when_the_file_changes(tree: Tree, tmp_path: Path):
    path = tree.base / "public/changing.mrc"
    first = np.full((8, 8), 1, np.float32)
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(first)
    with make_client(tree) as client:
        for url in (PLANE, INFO, STATS):
            a = client.get(url, params={"path": "public/changing.mrc"})
            assert a.status_code == 200 and a.headers["cache-control"] == "private, no-cache"
            etag = a.headers["etag"]
            again = client.get(url, params={"path": "public/changing.mrc"}, headers={"If-None-Match": etag})
            assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
            other = client.get(url, params={"path": "public/gray16.tif"}, headers={"If-None-Match": etag})
            assert other.status_code == 200
        np.testing.assert_array_equal(
            decode(client.get(PLANE, params={"path": "public/changing.mrc"})), first
        )
        plane_etag = client.get(PLANE, params={"path": "public/changing.mrc"}).headers["etag"]
        assert (
            client.get(PLANE, params={"path": "public/changing.mrc", "bin": 2}).headers["etag"] != plane_etag
        )
        with mrcfile.new(path, overwrite=True) as m:  # same size, new content
            m.set_data(np.full((8, 8), 2, np.float32))
        st_ = path.stat()
        os.utime(path, ns=(st_.st_atime_ns, st_.st_mtime_ns + 2_000_000_000))
        changed = client.get(
            PLANE, params={"path": "public/changing.mrc"}, headers={"If-None-Match": plane_etag}
        )
        assert changed.status_code == 200 and changed.headers["etag"] != plane_etag
        assert float(decode(changed)[0, 0]) == 2.0
    path.unlink()


def test_stats_endpoint(local, tree: Tree):
    s = local.get(STATS, params={"path": "cryo/movie.mrc", "t": 2}).json()
    frame = tree.movie[2].astype(np.float64)
    assert s["min"] == float(frame.min()) and s["max"] == float(frame.max())
    assert s["mean"] == pytest.approx(frame.mean()) and s["std"] == pytest.approx(frame.std())
    assert s["n_finite"] == frame.size and sum(s["histogram"]["counts"]) == frame.size
    assert s["robust"][0] < s["auto"][0] < s["auto"][1] < s["robust"][1]


# ── files and listings ───────────────────────────────────────────────────────


def test_roots_and_listing(local, proxied):
    assert [r["name"] for r in local.get("/api/fs/roots").json()["roots"]] == ["cryo", "light", "public"]
    assert [r["name"] for r in proxied.get("/api/fs/roots", headers=ALICE).json()["roots"]] == [
        "cryo",
        "public",
    ]
    listing = local.get("/api/fs/list", params={"path": "cryo"}).json()
    by_name = {e["name"]: e for e in listing["entries"]}
    assert listing["parent"] is None and listing["entries"][0]["name"] == "session"
    assert by_name["movie.mrc"]["supported"] and by_name["movie.eer"]["supported"]
    assert by_name["movie.mrc"]["path"] == "cryo/movie.mrc" and by_name["movie.mrc"]["size"] > 1024
    sub = local.get("/api/fs/list", params={"path": "cryo/session", "limit": 1}).json()
    assert sub["parent"] == "cryo" and sub["entries"][0]["supported"] is False and sub["total"] == 1
    assert local.get("/api/fs/list", params={"path": "cryo/movie.mrc"}).status_code == 400
    assert local.get("/api/fs/list", params={"path": "cryo", "limit": 0}).status_code == 400


# ── security ─────────────────────────────────────────────────────────────────


def test_proxy_identity_is_required_and_unambiguous(proxied):
    assert proxied.get("/api/me").status_code == 401
    assert proxied.get(PLANE, params={"path": "cryo/movie.mrc"}).status_code == 401
    assert proxied.get("/api/me", headers={"X-Forwarded-User": "  "}).status_code == 401
    assert proxied.get("/api/me", headers={"X-Forwarded-User": "a,b"}).status_code == 401
    assert proxied.get("/healthz").status_code == 200  # the only open route
    # the same identity header twice is ambiguous (a client-supplied one next to the proxy's)
    twice = [("X-Forwarded-User", "mallory"), ("X-Forwarded-User", "alice"), ("X-Forwarded-Groups", "cryoem")]
    assert proxied.get("/api/me", headers=twice).status_code == 401
    groups_twice = [
        ("X-Forwarded-User", "bob"),
        ("X-Forwarded-Groups", "lm"),
        ("X-Forwarded-Groups", "cryoem"),
    ]
    assert proxied.get("/api/me", headers=groups_twice).status_code == 401
    assert proxied.get("/api/me", headers={"x-forwarded-user": "carol"}).json()["groups"] == []


def test_groups_decide_which_roots_are_readable(proxied, tree: Tree):
    ok = proxied.get(PLANE, params={"path": "cryo/movie.mrc"}, headers=ALICE)
    np.testing.assert_array_equal(decode(ok), tree.movie[0])
    # the plane is now cached: bob must still be refused (authorisation comes before any cache)
    for url in (PLANE, INFO, STATS):
        assert proxied.get(url, params={"path": "cryo/movie.mrc"}, headers=BOB).status_code == 403
    assert proxied.get("/api/fs/list", params={"path": "cryo"}, headers=BOB).status_code == 403
    assert proxied.get(PLANE, params={"path": "light/rgb.png"}, headers=BOB).status_code == 200
    assert proxied.get(PLANE, params={"path": "light/rgb.png"}, headers=ALICE).status_code == 403
    for who in (ALICE, BOB):
        assert proxied.get(PLANE, params={"path": "public/gray16.tif"}, headers=who).status_code == 200
    # an etag learnt by alice is useless to bob
    etag = proxied.get(PLANE, params={"path": "cryo/movie.mrc"}, headers=ALICE).headers["etag"]
    r = proxied.get(PLANE, params={"path": "cryo/movie.mrc"}, headers={**BOB, "If-None-Match": etag})
    assert r.status_code == 403


HOSTILE = [
    "cryo/../outside/secret.mrc", "../outside/secret.mrc", "/etc/passwd", "cryo/..", "cryo/./movie.mrc",
    "outside/secret.mrc", "cryo\\movie.mrc", "cryo/escape/secret.mrc", "cryo/escape_file.mrc", "cryo/etc/passwd",
    "unknown/x", "cryo/a\x00b", "cryo/a\nb", "~/x", "cryo/" + "A" * 300, "file:///etc/passwd", "//etc/passwd",
]  # fmt: skip


@pytest.fixture(scope="module")
def with_escapes(tree: Tree) -> Tree:
    os.symlink(tree.base / "outside", tree.base / "cryo/escape")
    os.symlink(tree.base / "outside/secret.mrc", tree.base / "cryo/escape_file.mrc")
    os.symlink("/etc", tree.base / "cryo/etc")
    return tree


@pytest.mark.parametrize("path", HOSTILE)
def test_hostile_paths_are_denied_without_leaking(local, with_escapes: Tree, path: str):
    for url in (PLANE, INFO, STATS, "/api/fs/list"):
        r = local.get(url, params={"path": path})
        assert r.status_code == 403, (url, path, r.text)
        assert r.json() == {"error": {"code": "access_denied", "message": "access denied"}}
        assert str(with_escapes.base) not in r.text


def test_encoded_traversal_and_listing_of_escapes(local, with_escapes: Tree):
    base = str(with_escapes.base)
    for raw in ("cryo/%2e%2e/outside/secret.mrc", "cryo%2f..%2foutside%2fsecret.mrc", "%2e%2e/%2e%2e/etc/passwd",
                "cryo/..%2foutside/secret.mrc", "%2fetc%2fpasswd"):  # fmt: skip
        r = local.get(f"{INFO}?path={raw}")
        assert r.status_code == 403 and base not in r.text, raw
    # double encoding decodes once: an ordinary, missing name inside the root
    r = local.get(f"{INFO}?path=cryo/%252e%252e/outside/secret.mrc")
    assert r.status_code == 404 and base not in r.text
    names = [e["name"] for e in local.get("/api/fs/list", params={"path": "cryo"}).json()["entries"]]
    assert not {"escape", "escape_file.mrc", "etc"} & set(names)


def test_missing_and_unsupported_files(local, tree: Tree):
    r = local.get(INFO, params={"path": "cryo/missing.mrc"})
    assert r.status_code == 404 and r.json()["error"] == {
        "code": "not_found",
        "message": "not found: cryo/missing.mrc",
    }
    r = local.get(INFO, params={"path": "cryo/session/notes.txt"})
    assert r.status_code == 415 and str(tree.base) not in r.text
    r = local.get(PLANE, params={"path": "cryo/corrupt.mrc"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "corrupt_file"
    assert "corrupt.mrc" in r.json()["error"]["message"] and str(tree.base) not in r.text
    assert local.get("/api/nope").json() == {"error": {"code": "not_found", "message": "not found"}}
    assert local.post(PLANE).status_code == 405


@settings(max_examples=150, deadline=None, derandomize=True)
@given(path=st.text(max_size=40))
def test_no_generated_path_leaks_or_crashes(local, tree: Tree, path: str):
    r = local.get(INFO, params={"path": path})
    assert r.status_code in (400, 403, 404, 415, 422), (path, r.status_code)
    assert str(tree.base) not in r.text and "Traceback" not in r.text


def test_host_allow_list(tree: Tree):
    with make_client(tree, allowed_hosts=frozenset({"imagebio.lab.example"})) as client:
        for host, status in (("localhost", 200), ("127.0.0.1:8000", 200), ("[::1]:8000", 200), ("LOCALHOST.", 200),
                             ("imagebio.lab.example", 200), ("imagebio.lab.example:443", 200),
                             ("evil.example", 403), ("localhost.evil.example", 403), ("", 403)):  # fmt: skip
            r = client.get("/api/me", headers={"Host": host})
            assert r.status_code == status, host
        r = client.get("/api/me", headers={"Host": "evil.example"})
        assert r.json()["error"]["code"] == "forbidden_host"
        assert (
            client.get("/healthz", headers={"Host": "10.0.0.5"}).status_code == 200
        )  # health checks by address


def test_security_headers_on_every_response(local):
    for r in (local.get("/api/me"), local.get("/api/nope"), local.get(PLANE, params={"path": "cryo/movie.mrc"}),
              local.get("/api/me", headers={"Host": "evil.example"})):  # fmt: skip
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in r.headers["content-security-policy"]


def test_unexpected_errors_are_500_without_details(tree: Tree):
    class Boom(Reader):
        name = "boom"
        extensions = (".boom",)

        def sniff(self, path: Path, head: bytes) -> bool:
            return True

        def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
            raise RuntimeError(f"secret detail about {path}")

    (tree.base / "public/x.boom").write_bytes(b"boom")
    with make_client(tree, registry=Registry([Boom(), *default_registry().readers])) as client:
        r = client.get(INFO, params={"path": "public/x.boom"})
        assert r.status_code == 500
        assert r.json() == {"error": {"code": "internal_error", "message": "internal error"}}
        assert "secret detail" not in r.text and str(tree.base) not in r.text


# ── concurrency ──────────────────────────────────────────────────────────────


def test_concurrent_clients_get_exact_data_and_the_cache_stays_bounded(tree: Tree):
    cap = 3 * tree.movie[0].nbytes
    with make_client(tree, plane_cache_bytes=cap) as client:
        errors: list[BaseException] = []

        def work(seed: int) -> None:
            rng = np.random.default_rng(seed)
            try:
                for _ in range(25):
                    if rng.random() < 0.6:
                        t = int(rng.integers(0, 5))
                        r = client.get(PLANE, params={"path": "cryo/movie.mrc", "t": t})
                        np.testing.assert_array_equal(decode(r), tree.movie[t])
                    else:
                        t, z, c = (int(rng.integers(0, n)) for n in (2, 3, 2))
                        r = client.get(PLANE, params={"path": "light/hyper.tif", "t": t, "z": z, "c": c})
                        np.testing.assert_array_equal(decode(r), tree.hyper[t, z, c])
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=work, args=(s,)) for s in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        assert not errors
        stats = client.app.state.service.cache_stats()["planes"]  # type: ignore[attr-defined]
        assert stats["bytes"] <= cap and stats["hits"] > 0 and stats["evictions"] > 0
