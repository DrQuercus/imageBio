"""``/metrics``: who may read it, and what it says (never a path or a name)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from fixtures.api import ALICE, build_tree
from fixtures.live import BASE
from imagebio.api.app import create_app
from imagebio.api.metrics import TOKEN_VARIABLE
from imagebio.core.config import Settings

SAMPLE = re.compile(r'^imagebio_[a-z_]+(\{[a-z_]+="[^"]*"(,[a-z_]+="[^"]*")*\})? -?\d+(\.\d+)?(e[+-]?\d+)?$')


def client_of(settings: Settings, *, address: str = "127.0.0.1") -> TestClient:
    return TestClient(
        create_app(settings),
        base_url="http://localhost",
        raise_server_exceptions=False,
        client=(address, 50000),
    )


def samples(text: str) -> dict[str, float]:
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    for line in lines:
        assert SAMPLE.match(line), line
    return {line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1]) for line in lines}


def test_metrics_are_off_unless_configured(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "data")
    with client_of(Settings(roots=tree.roots())) as client:
        r = client.get("/metrics")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_what_a_monitoring_system_reads(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "data")
    (tree.base / "public/session").mkdir()
    os.utime(tree.base / "public/session", (BASE, BASE))
    settings = Settings(roots=tree.roots(), metrics=True, state_dir=tmp_path / "state", live_scan=False)
    with client_of(settings) as client:
        first = client.get("/metrics")
        assert first.status_code == 200
        assert first.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        values = samples(first.text)
        assert values['imagebio_info{version="0.1.0"}'] == 1
        assert values["imagebio_plane_cache_bytes"] == 0 and values["imagebio_open_datasets"] == 0
        assert not any(name.startswith("imagebio_live") for name in values)  # the Live mode was never used
        assert not (tmp_path / "state").exists()  # and asking did not create its index

        # reading a plane twice: a miss, then a hit; one file open
        for _ in range(2):
            assert client.get("/api/datasets/plane", params={"path": "cryo/movie.mrc"}).status_code == 200
        values = samples(client.get("/metrics").text)
        assert values['imagebio_plane_cache_requests_total{outcome="miss"}'] == 1
        assert values['imagebio_plane_cache_requests_total{outcome="hit"}'] == 1
        assert values["imagebio_plane_cache_bytes"] == 48 * 64 * 4 and values["imagebio_open_datasets"] == 1

        # a watch and a scan
        live = client.app.state.live  # type: ignore[attr-defined]
        live.clock = lambda: BASE + 100.0
        assert client.post("/api/live/watches", params={"path": "public/session"}).status_code == 200
        live.scan_once(BASE + 40.0)
        text = client.get("/metrics").text
        values = samples(text)
        assert values['imagebio_live_watches{state="active"}'] == 1
        assert values['imagebio_live_watches{state="stopped"}'] == 0
        assert values['imagebio_live_scan_age_seconds{root="1"}'] == 60  # scanned at +40, asked at +100
        assert values['imagebio_live_scan_failing{root="1"}'] == 0
        assert values['imagebio_live_exposures{root="1"}'] == 0 and values["imagebio_live_index_bytes"] > 0
        # numbers only: no path of the server, no directory, no user
        assert str(tmp_path) not in text and "session" not in text and "public" not in text
        assert "# HELP imagebio_live_scan_age_seconds" in text and "# TYPE imagebio_info gauge" in text
        assert client.get("/metrics", params={"format": "json"}).status_code == 400


def test_metrics_are_for_this_computer_or_for_the_bearer_of_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = build_tree(tmp_path / "data")
    monkeypatch.delenv(TOKEN_VARIABLE, raising=False)
    settings = Settings(roots=tree.roots(), metrics=True)
    with client_of(settings, address="192.168.1.20") as elsewhere:
        r = elsewhere.get("/metrics")
        assert r.status_code == 403 and "this computer only" in r.json()["error"]["message"]
    with client_of(settings, address="::1") as local:
        assert local.get("/metrics").status_code == 200
        # the name the server is asked under still counts (DNS rebinding)
        assert local.get("/metrics", headers={"Host": "evil.example"}).status_code == 403

    # with a token: required from everybody, and enough from anywhere, under any name
    monkeypatch.setenv(TOKEN_VARIABLE, "s3cret-token")
    proxied = Settings(
        roots=tree.roots(),
        metrics=True,
        host="0.0.0.0",  # what a container listens on, behind its proxy
        auth_mode="proxy",
        trust_network=True,
        allowed_hosts=frozenset({"imagebio.lab.example"}),
    )
    with client_of(proxied, address="172.18.0.5") as scraper:
        assert scraper.get("/metrics").status_code == 401
        assert scraper.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert scraper.get("/metrics", headers={"Authorization": "s3cret-token"}).status_code == 401
        ok = scraper.get(
            "/metrics", headers={"Authorization": "Bearer s3cret-token", "Host": "imagebio:8000"}
        )
        assert ok.status_code == 200 and "imagebio_info" in ok.text
        assert "s3cret-token" not in ok.text
        # the token opens the metrics, nothing else: the rest still wants its Host and its user
        api = scraper.get(
            "/api/me", headers={"Authorization": "Bearer s3cret-token", "Host": "imagebio:8000"}
        )
        assert api.status_code == 403
        assert scraper.get("/api/me", headers={**ALICE, "Host": "imagebio.lab.example"}).status_code == 200
    with client_of(settings) as local:  # this computer too must show the token once there is one
        assert local.get("/metrics").status_code == 401
