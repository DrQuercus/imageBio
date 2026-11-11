"""The files of deploy/ are examples people copy: they must stay in step with the program."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from imagebio.api.metrics import TOKEN_VARIABLE
from imagebio.api.security import PROXY_SECRET_HEADER, PROXY_SECRET_VARIABLE
from imagebio.core.config import Settings

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
pytestmark = pytest.mark.skipif(not DEPLOY.is_dir(), reason="deploy/ is not part of this checkout")


def test_the_configurations_shipped_are_accepted() -> None:
    in_container = Settings.load(DEPLOY / "compose/imagebio.toml")
    # the container listens on its own network: only possible behind the proxy, and said so
    assert (in_container.host, in_container.auth_mode, in_container.trust_network) == (
        "0.0.0.0",
        "proxy",
        True,
    )
    assert in_container.state_dir == Path("/var/lib/imagebio") and in_container.metrics
    assert [(r.name, str(r.path)) for r in in_container.roots] == [("microscope", "/data/microscope")]

    on_the_host = Settings.load(DEPLOY / "systemd/imagebio.toml")
    assert (on_the_host.host, on_the_host.auth_mode, on_the_host.trust_network) == (
        "127.0.0.1",
        "proxy",
        False,
    )
    assert on_the_host.allowed_hosts == {"imagebio.lab.example"} and on_the_host.state_dir == Path(
        "/var/lib/imagebio"
    )
    for settings in (in_container, on_the_host):
        assert settings.user_header == "X-Forwarded-User"  # what the Caddy files send


def test_every_variable_of_the_stack_is_explained_in_the_example() -> None:
    explained = set(
        re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", (DEPLOY / "compose/env.example").read_text(), re.M)
    )
    used: set[str] = set()
    for name in ("docker-compose.yml", "docker-compose.sso.yml", "caddy/Caddyfile", "caddy/Caddyfile.sso"):
        text = (DEPLOY / "compose" / name).read_text()
        used |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", text)) | set(
            re.findall(r"\{\$([A-Z][A-Z0-9_]+)", text)
        )
    assert used and used <= explained, sorted(used - explained)
    # the names the program reads are the names the files give
    assert {PROXY_SECRET_VARIABLE, TOKEN_VARIABLE} <= used


def test_every_proxy_presents_the_secret_and_replaces_the_identity() -> None:
    for name in ("compose/caddy/Caddyfile", "compose/caddy/Caddyfile.sso", "systemd/Caddyfile"):
        text = (DEPLOY / name).read_text()
        assert f"header_up {PROXY_SECRET_HEADER} {{${PROXY_SECRET_VARIABLE}}}" in text, name
        assert re.search(r"handle /metrics \{\s+respond 404", text), name  # never through the proxy
    for name in ("compose/caddy/Caddyfile", "systemd/Caddyfile"):  # where Caddy itself authenticates
        text = (DEPLOY / name).read_text()
        assert (
            "header_up X-Forwarded-User {http.auth.user.id}" in text
            and "header_up -X-Forwarded-Groups" in text
        )
    for unit in ("imagebio.service", "imagebio-backup.service"):
        text = (DEPLOY / "systemd" / unit).read_text()
        assert "ExecStart=/opt/imagebio/venv/bin/imagebio " in text and "User=imagebio" in text
        assert "--config /etc/imagebio/imagebio.toml" in text
