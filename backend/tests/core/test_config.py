from __future__ import annotations

from pathlib import Path

import pytest

from imagebio.core.config import ConfigError, RootConfig, Settings, is_loopback_host, parse_size


def test_parse_size():
    assert parse_size(123) == 123
    assert parse_size("2GiB") == 2 * 2**30 and parse_size("512 MiB") == 512 * 2**20
    assert parse_size("1.5GB") == 1_500_000_000 and parse_size("64") == 64
    for bad in ("", "2 parsecs", "-1", True, -5):
        with pytest.raises(ConfigError):
            parse_size(bad)


def test_root_config_validation(tmp_path: Path):
    assert RootConfig("glacios", tmp_path, frozenset({"cryoem"})).groups == {"cryoem"}
    for bad in ("", "..", "a/b", "-x", "x" * 65, "a b"):
        with pytest.raises(ConfigError):
            RootConfig(bad, tmp_path)
    with pytest.raises(ConfigError):
        RootConfig("rel", Path("relative/dir"))


def test_loopback_only_unless_private_proxy_network():
    assert is_loopback_host("127.0.0.1") and is_loopback_host("localhost") and is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0") and not is_loopback_host("127.evil.example")
    with pytest.raises(ConfigError, match="refusing to listen"):
        Settings(host="0.0.0.0")
    with pytest.raises(ConfigError, match="refusing to listen"):
        Settings(host="0.0.0.0", auth_mode="proxy")
    with pytest.raises(ConfigError):
        Settings(trust_network=True)  # needs proxy mode
    assert Settings(host="0.0.0.0", auth_mode="proxy", trust_network=True).host == "0.0.0.0"


def test_local_mode_names_roots_after_directories(tmp_path: Path):
    (tmp_path / "a" / "data").mkdir(parents=True)
    (tmp_path / "b" / "data").mkdir(parents=True)
    (tmp_path / "my films!").mkdir()
    s = Settings.local([tmp_path / "a" / "data", tmp_path / "b" / "data", tmp_path / "my films!"])
    assert [r.name for r in s.roots] == ["data", "data-2", "my_films_"]
    assert s.auth_mode == "none" and s.host == "127.0.0.1"


def test_load_toml(tmp_path: Path):
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(f"""
[server]
port = 9001
allowed_hosts = ["ImageBio.Lab.Example"]
[auth]
mode = "proxy"
[cache]
plane_bytes = "256MiB"
[[roots]]
name = "glacios"
path = "{tmp_path}"
groups = ["cryoem"]
[[roots]]
name = "shared"
path = "{tmp_path}"
""")
    s = Settings.load(cfg)
    assert s.port == 9001 and s.auth_mode == "proxy" and s.plane_cache_bytes == 256 * 2**20
    assert s.allowed_hosts == {"imagebio.lab.example"}
    assert [(r.name, sorted(r.groups)) for r in s.roots] == [("glacios", ["cryoem"]), ("shared", [])]
    # the live monitor has sensible defaults and a per-user place for its index
    assert (s.live_interval_s, s.live_min_stable_s, s.live_scan, s.state_dir) == (5.0, 10.0, True, None)
    assert s.live_state_dir.name == "imagebio" and s.live_state_dir.is_absolute()


def test_live_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(f"""
[cache]
thumb_bytes = "64MiB"
[live]
state_dir = "{tmp_path / "state"}"
interval = 2
min_stable = 6
scan = false
""")
    s = Settings.load(cfg)
    assert (s.live_interval_s, s.live_min_stable_s, s.live_scan) == (2.0, 6.0, False)
    assert s.live_state_dir == tmp_path / "state" and s.thumb_cache_bytes == 64 * 2**20
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert Settings().live_state_dir == tmp_path / "xdg" / "imagebio"
    for bad in ("[live]\ninterval = 0.1", "[live]\nmin_stable = 0", "[live]\npoll = 3"):
        cfg.write_text(bad)
        with pytest.raises(ConfigError):
            Settings.load(cfg)


@pytest.mark.parametrize(
    "text",
    [
        "[server]\nprot = 1",  # typo in a key
        "[sever]\nport = 1",  # typo in a section
        "[auth]\nmode = 'ldap'",
        "[[roots]]\nname = 'a'",  # path missing
        "[[roots]]\nname = 'a'\npath = '/x'\n[[roots]]\nname = 'a'\npath = '/y'",
        "not toml at all [",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str):
    cfg = tmp_path / "bad.toml"
    cfg.write_text(text)
    with pytest.raises(ConfigError):
        Settings.load(cfg)


def test_missing_config_file(tmp_path: Path):
    with pytest.raises(ConfigError):
        Settings.load(tmp_path / "nope.toml")
