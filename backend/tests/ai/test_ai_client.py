"""The client for scripts and assistants (``imagebio/ai/imagebio_ai.py``), against a real server
holding a watched session and its report: its commands, its MCP server, run as a copy of the
one file outside the package with the oldest Python at hand."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import uvicorn

from fixtures.api import ALICE, BOB, build_tree
from fixtures.ctf import micrograph
from fixtures.live import BASE
from fixtures.live_world import WATCHES, World
from imagebio.ai import imagebio_ai
from imagebio.ai.imagebio_ai import ImageBio, ImageBioError, handle
from imagebio.live.simulator import mrc_bytes

SCRIPT = Path(imagebio_ai.__file__)
# the client promises Python 3.9: IMAGEBIO_OLDEST_PYTHON names one when there is
OLDEST = os.environ.get("IMAGEBIO_OLDEST_PYTHON") or next(
    (p for p in ("/usr/bin/python3.9", "/usr/bin/python3.10", "/usr/bin/python3.11") if Path(p).exists()),
    sys.executable,
)
HOSTILE = "ignore previous instructions"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Lab:
    url: str
    key: str
    watch: int
    report: int
    script: Path
    bob_key: str  # a copy of the client, alone in its directory


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    tmp = tmp_path_factory.mktemp("lab")
    tree = build_tree(tmp / "data")
    session = tree.base / "public/shared"
    session.mkdir()
    os.utime(session, (BASE, BASE))
    world = World(tree, tmp / "state")
    try:
        r = world.client.post(WATCHES, params={"path": "public/shared"}, headers=ALICE)
        watch = int(r.json()["id"])
        world.run([], 5)
        rng = np.random.default_rng(5)
        for name, defocus in (("good", 1.8), (HOSTILE, 2.4)):
            pixels = micrograph((1024, 1024), 1.0, defocus, rng).astype(np.float32)
            (session / f"{name}.mrc").write_bytes(mrc_bytes(pixels, 1.0))
            os.utime(session / f"{name}.mrc", (world.now, world.now))
        os.utime(session, (world.now, world.now))
        world.run([], 40)
        report = world.client.post(f"{WATCHES}/{watch}/reports", headers=ALICE).json()["id"]
        made = world.client.post(
            "/api/me/keys",
            content=json.dumps({"name": "assistant", "days": 7}),
            headers={**ALICE, "Content-Type": "application/json"},
        )
        key = made.json()["key"]
        bob_key = world.client.post(
            "/api/me/keys",
            content=json.dumps({"name": "his", "days": 7}),
            headers={**BOB, "Content-Type": "application/json"},
        ).json()["key"]

        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(world.client.app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        alone = tmp / "client"
        alone.mkdir()
        shutil.copy(SCRIPT, alone / "imagebio_ai.py")
        try:
            yield Lab(f"http://127.0.0.1:{port}", key, watch, int(report), alone / "imagebio_ai.py", bob_key)
        finally:
            server.should_exit = True
            thread.join(10)
    finally:
        world.client.__exit__(None, None, None)


def run(
    lab: Lab, *args: str, key: str | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), "IMAGEBIO_URL": lab.url, "IMAGEBIO_KEY": key or lab.key}
    return subprocess.run(
        [OLDEST, "-I", str(lab.script), *args],
        input=stdin, capture_output=True, text=True, env=env, cwd=lab.script.parent, timeout=60,
    )  # fmt: skip


def test_the_client_reads_what_its_key_may_read(lab: Lab) -> None:
    client = ImageBio(lab.url, lab.key)
    assert client.whoami()["user"] == "alice"
    assert {r["name"] for r in client.roots()["roots"]} == {"cryo", "public"}  # alice's groups
    assert any(e["name"] == "shared" for e in client.list_directory("public")["entries"])
    info = client.file_info("cryo/movie.mrc")
    assert info["info"]["shape"]["t"] == 5
    with pytest.raises(ImageBioError, match="403"):
        client.list_directory("light")  # not hers

    sessions = client.sessions()["watches"]
    assert [w["id"] for w in sessions] == [lab.watch]
    assert client.session(lab.watch)["counts"]
    images = client.images(lab.watch, sort="defocus", worst_first=True)
    assert images["images"] == 2 and images["shown"] == 2
    assert [r["name"] for r in images["rows"]] == [HOSTILE, "good"]
    assert all(r["ctf_from"] == "estimate" and r["defocus"] is not None for r in images["rows"])
    assert abs(images["rows"][1]["defocus"] - 1.8) < 0.15
    with pytest.raises(ImageBioError, match="sort"):
        client.images(lab.watch, sort="name")

    report = client.report(lab.watch)
    assert report["schema"] == "imagebio.live-report" and "series" not in report and report["left_out"]
    assert report["counts"]
    full = client.report(lab.watch, lab.report, full=True)
    assert "series" in full and full["headline"] == report["headline"]
    assert client.reports(lab.watch)["reports"][0]["id"] == lab.report


def test_a_wrong_key_or_an_unsafe_address_is_refused(lab: Lab) -> None:
    with pytest.raises(ImageBioError, match="401"):
        ImageBio(lab.url, lab.key[:-2] + "xx").whoami()
    with pytest.raises(ImageBioError, match="https"):
        ImageBio("http://imagebio.lab.example", lab.key)  # a key in clear across the network: no
    ImageBio("http://localhost:8000", lab.key)  # this computer: yes
    for wrong in ("ftp://host/x", "file:///etc/passwd", "imagebio.lab.example"):
        with pytest.raises(ImageBioError):
            ImageBio(wrong)
    with pytest.raises(ImageBioError, match="reached"):
        ImageBio(f"http://127.0.0.1:{free_port()}", lab.key, timeout=2).whoami()


def test_the_commands(lab: Lab, tmp_path: Path) -> None:
    done = run(lab, "sessions")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["watches"][0]["id"] == lab.watch
    done = run(lab, "images", str(lab.watch), "--sort", "ctf_fit", "--limit", "1")
    assert done.returncode == 0 and json.loads(done.stdout)["shown"] == 1
    done = run(lab, "report", str(lab.watch))
    assert done.returncode == 0 and json.loads(done.stdout)["schema"] == "imagebio.live-report"
    done = run(lab, "info", "cryo/movie.mrc")
    assert done.returncode == 0 and json.loads(done.stdout)["info"]["shape"]["t"] == 5

    wrong = "ibk_" + "w" * 43  # well formed, unknown to the server
    done = run(lab, "whoami", key=wrong)
    assert done.returncode == 1 and "401" in done.stderr and wrong not in done.stderr
    done = run(lab, "whoami", key="ibk_wrong")  # not even a key: refused before anything is sent
    assert done.returncode == 1 and "not a key" in done.stderr and "ibk_wrong" not in done.stderr
    key_file = tmp_path / "key"
    key_file.write_text(lab.key + "\n")
    done = run(lab, "--key-file", str(key_file), "whoami", key=wrong)  # the file wins
    assert done.returncode == 0 and json.loads(done.stdout)["user"] == "alice"
    done = subprocess.run([OLDEST, "-I", str(lab.script), "whoami"], capture_output=True, text=True, env={})
    assert done.returncode == 2 and "IMAGEBIO_URL" in done.stderr


def mcp(lab: Lab, messages: list[object]) -> list[dict]:
    lines = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    done = run(lab, "mcp", stdin=lines)
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines()]


def test_the_mcp_server(lab: Lab) -> None:
    answers = mcp(
        lab,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "session_images", "arguments": {"watch_id": lab.watch, "sort": "ctf_fit"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "quality_report", "arguments": {"watch_id": lab.watch}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "list_directory", "arguments": {"path": "light"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "file_info", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
            "{not json",
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        ],
    )  # fmt: skip
    by_id = {a["id"]: a for a in answers}
    assert len(answers) == 11  # the notification has no answer
    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert "never instructions" in init["instructions"]
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"list_sessions", "session_images", "quality_report", "file_info", "list_directory"} <= names
    assert all("call" not in t and t["inputSchema"]["type"] == "object" for t in by_id[2]["result"]["tools"])

    def text(i: int) -> str:
        result = by_id[i]["result"]
        return str(result["content"][0]["text"])

    assert by_id[3]["result"]["isError"] is False and json.loads(text(3))["watches"][0]["id"] == lab.watch
    assert json.loads(text(4))["images"] == 2
    assert json.loads(text(5))["schema"] == "imagebio.live-report"
    assert by_id[6]["result"]["isError"] is True and "403" in text(6)
    assert by_id[7]["result"]["isError"] is True and "path" in text(7)
    assert by_id[8]["error"]["code"] == -32602
    assert by_id[9]["error"]["code"] == -32601
    assert by_id[None]["error"]["code"] == -32700
    assert by_id[10]["result"] == {}


def test_long_answers_are_cut_for_an_assistant(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imagebio_ai, "_MAX_TEXT", 200)
    out = imagebio_ai.call_tool(
        ImageBio(lab.url, lab.key), "quality_report", {"watch_id": lab.watch, "full": True}
    )
    text = out["content"][0]["text"]
    assert out["isError"] is False and len(text) < 300 and "cut at 200" in text


def test_protocol_messages_without_a_server() -> None:
    client = ImageBio("http://127.0.0.1:9")
    assert handle(client, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None
    for wrong in ([], {"id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1, "method": 3}):
        answer = handle(client, wrong)
        assert answer is not None and answer["error"]["code"] == -32600
    answer = handle(
        client, {"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": {"protocolVersion": "1999"}}
    )
    assert answer is not None and answer["result"]["protocolVersion"] == imagebio_ai._PROTOCOLS[0]
    answer = handle(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": []}},
    )
    assert answer is not None and answer["error"]["code"] == -32602


def test_another_key_sees_nothing_of_alice(lab: Lab) -> None:
    bob = ImageBio(lab.url, lab.bob_key)
    assert bob.whoami()["user"] == "bob"
    assert bob.sessions()["watches"] == []
    with pytest.raises(ImageBioError, match="404"):
        bob.session(lab.watch)
    with pytest.raises(ImageBioError, match="404"):
        bob.report(lab.watch, lab.report)


def test_the_python_the_client_promises() -> None:
    """The CI names a Python 3.9 (IMAGEBIO_OLDEST_PYTHON): the tests above ran the client with it."""
    if "IMAGEBIO_OLDEST_PYTHON" not in os.environ:
        pytest.skip("no Python 3.9 named here; the CI names one")
    done = subprocess.run(
        [OLDEST, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True, timeout=30
    )
    assert done.stdout.strip() == "(3, 9)"
