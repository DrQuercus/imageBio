from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mrcfile
import numpy as np
import pytest

from imagebio.api import cli


def test_info_prints_the_dataset_description(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    with mrcfile.new(tmp_path / "a.mrc") as m:
        m.set_data(np.zeros((6, 8), np.float32))
        m.voxel_size = 1.5
    assert cli.main(["info", str(tmp_path / "a.mrc")]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["format"] == "mrc" and info["shape"]["x"] == 8 and info["calibration"]["x"]["value"] == 1.5


def test_info_errors_are_friendly(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["info", str(tmp_path / "missing.mrc")]) == 2
    err = capsys.readouterr().err
    assert "file not found: missing.mrc" in err and "Traceback" not in err
    (tmp_path / "notes.txt").write_text("x")
    assert cli.main(["info", str(tmp_path / "notes.txt")]) == 2


def test_serve_builds_settings_and_starts_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append({"app": app, **kw}))
    (tmp_path / "data").mkdir()
    assert cli.main(["serve", "--local", str(tmp_path / "data"), "--port", "9123"]) == 0
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 9123
    settings = calls[0]["app"].state.settings
    assert settings.auth_mode == "none" and [r.name for r in settings.roots] == ["data"]
    assert "http://127.0.0.1:9123" in capsys.readouterr().out

    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(f'[auth]\nmode = "proxy"\n[[roots]]\nname = "lab"\npath = "{tmp_path / "data"}"\n')
    assert cli.main(["serve", "--config", str(cfg)]) == 0
    assert calls[1]["port"] == 8000 and calls[1]["app"].state.settings.auth_mode == "proxy"
    assert cli.main(["serve", "--config", str(cfg), "--port", "8100"]) == 0 and calls[2]["port"] == 8100


def test_serve_local_can_name_alert_channels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append({"app": app, **kw}))
    (tmp_path / "data").mkdir()
    args = [
        "serve",
        "--local",
        str(tmp_path / "data"),
        "--ntfy",
        "https://ntfy.sh/my-topic",
        "--webhook",
        "https://x.example/hook",
    ]
    assert cli.main(args) == 0
    channels = calls[0]["app"].state.settings.alert_channels
    assert [(c.name, c.kind, c.url) for c in channels] == [
        ("ntfy", "ntfy", "https://ntfy.sh/my-topic"),
        ("webhook", "webhook", "https://x.example/hook"),
    ]
    assert "my-topic" not in capsys.readouterr().out  # an address is a secret: not echoed
    assert cli.main(["serve", "--local", str(tmp_path / "data"), "--ntfy", "ftp://x/y"]) == 2
    assert "http(s) address" in capsys.readouterr().err
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(f'[[roots]]\nname = "lab"\npath = "{tmp_path / "data"}"\n')
    assert cli.main(["serve", "--config", str(cfg), "--ntfy", "https://ntfy.sh/my-topic"]) == 2
    assert "declare [[alerts.channels]]" in capsys.readouterr().err
    assert len(calls) == 1


def test_serve_configuration_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("the server must not start"))
    assert cli.main(["serve", "--local", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text('[server]\nhost = "0.0.0.0"\n')
    assert cli.main(["serve", "--config", str(bad)]) == 2
    assert "refusing to listen on 0.0.0.0" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["serve"])  # neither --config nor --local


def test_simulate_writes_a_session_a_scanner_understands(tmp_path, capsys):
    """`imagebio simulate` against the real clock: a short run, then the files are checked."""
    from imagebio.live.layout import category_of, classify

    session = tmp_path / "session"
    assert cli.main(["simulate", str(session), "--rate", "36000", "--count", "3", "--size", "32"]) == 0
    out = capsys.readouterr().out
    assert "3 exposures started, 0 file(s) left unfinished" in out
    data = session / "Images-Disc1/GridSquare_1000000/Data"
    names = sorted(p.name for p in data.iterdir())
    assert len(names) == 12  # image, movie, preview and metadata for each exposure
    exposures = {classify(n).exposure for n in names}  # type: ignore[union-attr]
    assert len(exposures) == 3 and all(
        category_of("Images-Disc1/GridSquare_1000000/Data", e) == "data" for e in exposures
    )
    with mrcfile.open(data / next(n for n in names if n.endswith("_Fractions.mrc"))) as m:
        assert m.data.shape == (6, 32, 32)
    for bad in (["--rate", "0"], ["--size", "8"], ["--count", "-1"]):
        assert cli.main(["simulate", str(tmp_path / "x"), *bad]) == 2
