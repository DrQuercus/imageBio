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
