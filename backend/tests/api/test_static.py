"""Serving the built web interface."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from imagebio.api.app import create_app
from imagebio.core.config import Settings


def client(static_dir: Path | None) -> TestClient:
    return TestClient(create_app(Settings(), static_dir=static_dir), base_url="http://localhost")


def test_serves_the_built_interface_with_its_own_csp(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>imageBio</title><script type=module src=/assets/app.js></script>"
    )
    (tmp_path / "assets/app.js").write_text("console.log('hi')")
    with client(tmp_path) as c:
        page = c.get("/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        csp = page.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
        assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-cache"
        script = c.get("/assets/app.js")
        assert script.status_code == 200 and "javascript" in script.headers["content-type"]
        assert c.get("/assets/../index.html").status_code in (200, 404)  # never escapes the directory
        assert c.get("/assets/%2e%2e/%2e%2e/pyproject.toml").status_code == 404
        # the API keeps its strict policy, and the Host check also protects the page
        assert c.get("/api/me").headers["content-security-policy"].startswith("default-src 'none'")
        assert c.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_without_a_build_the_root_says_so(tmp_path: Path):
    for static_dir in (None, tmp_path):
        with client(static_dir) as c:
            r = c.get("/")
            assert r.status_code == 404 and r.json()["error"]["code"] == "ui_not_built"
            assert c.get("/api/me").status_code == 200
