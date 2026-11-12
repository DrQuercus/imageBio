"""Regressions found by the independent review of lot E (ADR 015), on the client: each test fails
on the code before its fix."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from collections.abc import Iterator

import pytest

from imagebio.ai import imagebio_ai
from imagebio.ai.imagebio_ai import ImageBio, ImageBioError

KEY = "ibk_" + "S3cr3t-key_" * 4


class Seen:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []


def serve(host: str, answer: str, seen: Seen) -> Iterator[int]:
    """A small server on ``host``: "json" answers {"user": "x"}, "redirect:<url>" redirects there."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.requests.append((self.path, self.headers.get("Authorization")))
            if answer.startswith("redirect:"):
                self.send_response(302)
                self.send_header("Location", answer[len("redirect:") :] + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"user": "x"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer((host, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()


@pytest.fixture
def elsewhere() -> Iterator[tuple[int, Seen]]:
    seen = Seen()
    for port in serve("127.0.0.2", "json", seen):
        yield port, seen


def test_a_redirection_is_not_followed_and_the_key_goes_nowhere_else(elsewhere: tuple[int, Seen]) -> None:
    sink, seen_there = elsewhere
    seen_here = Seen()
    for port in serve("127.0.0.1", f"redirect:http://127.0.0.2:{sink}", seen_here):
        with pytest.raises(ImageBioError, match="redirection") as caught:
            ImageBio(f"http://127.0.0.1:{port}", KEY).whoami()
        assert KEY not in str(caught.value)
    assert seen_here.requests == [("/api/me", f"Bearer {KEY}")]
    assert seen_there.requests == []  # before: the key followed the redirection, in clear


def test_a_key_to_this_computer_never_goes_through_a_proxy(
    elsewhere: tuple[int, Seen], monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy, seen_by_proxy = elsewhere
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.2:{proxy}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.2:{proxy}")
    # as in a fresh process: urllib builds its shared opener from the environment of its first use
    monkeypatch.setattr(urllib.request, "_opener", None)
    seen = Seen()
    for port in serve("127.0.0.1", "json", seen):
        assert ImageBio(f"http://localhost:{port}", KEY).whoami() == {"user": "x"}
    assert seen.requests == [("/api/me", f"Bearer {KEY}")]
    assert seen_by_proxy.requests == []  # before: the proxy of the environment got the key


def test_a_key_with_a_line_end_works_and_a_malformed_one_is_never_said_back(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = Seen()
    for port in serve("127.0.0.1", "json", seen):
        assert ImageBio(f"http://127.0.0.1:{port}", KEY + "\r\n").whoami() == {"user": "x"}
    assert seen.requests == [("/api/me", f"Bearer {KEY}")]
    for wrong in (
        KEY + "\r\nX-Other: 1",
        "ibk_short",
        "sk-ant-" + "x" * 40,
        KEY + " " + KEY,
        KEY.replace("_", "é"),
    ):
        with pytest.raises(ImageBioError) as caught:
            ImageBio("https://imagebio.lab.example", wrong)
        assert wrong.strip() not in str(caught.value) and KEY not in str(caught.value)
    # the commands and the MCP server: an exit and a line, never the key
    monkeypatch.setenv("IMAGEBIO_KEY", KEY + "\rmore")
    assert imagebio_ai.main(["--url", "https://imagebio.lab.example", "whoami"]) == 1
    out = capsys.readouterr()
    assert "not a key of imageBio" in out.err and KEY not in out.err + out.out
