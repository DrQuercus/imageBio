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
metrics = false
ctf = false
drift = true
mean_free_path_nm = 322
[microscope]
kv = 200
cs_mm = 1.4
amplitude_contrast = 0.1
""")
    s = Settings.load(cfg)
    assert (s.live_interval_s, s.live_min_stable_s, s.live_scan) == (2.0, 6.0, False)
    assert (
        s.live_metrics is False and Settings().live_metrics is True
    )  # images are measured unless told not to
    assert s.live_state_dir == tmp_path / "state" and s.thumb_cache_bytes == 64 * 2**20
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert Settings().live_state_dir == tmp_path / "xdg" / "imagebio"
    assert s.live_mfp_nm == 322.0 and Settings().live_mfp_nm is None
    assert s.live_drift is True and Settings().live_drift is False  # reading frames is asked for
    assert s.live_ctf is False and Settings().live_ctf is True
    assert (s.kv, s.cs_mm, s.amplitude_contrast) == (200.0, 1.4, 0.1)
    assert (Settings().kv, Settings().cs_mm, Settings().amplitude_contrast) == (300.0, 2.7, 0.07)
    for bad in (
        "[live]\ninterval = 0.1",
        "[live]\nmin_stable = 0",
        "[live]\npoll = 3",
        "[live]\nmean_free_path_nm = 3",
        "[microscope]\nkv = 3",
        "[microscope]\ncs_mm = -1",
        "[microscope]\namplitude_contrast = 0.9",
        "[microscope]\nvoltage = 300",
    ):
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


PROGRAMS = """
[processing]
slots = 2
work_dir = "/scratch/imagebio"

[processing.motion]
argv = ["/opt/mc3/MotionCor3", "-InMrc", "{movie}", "-OutMrc", "{out}/sum.mrc", "-PixSize", "{apix}", "-FmDose", "{dose}"]
result = "{out}/*-Patch-Full.log"
parser = "motion-log"
image = "{out}/sum.mrc"
timeout = 900

[processing.motion.formats]
EER = ["/opt/mc3/MotionCor3", "-InEer", "{movie}", "-OutMrc", "{out}/sum.mrc"]

[processing.ctf]
argv = ["/opt/ctffind"]
stdin = ["{image}", "{out}/diag.mrc", "{apix}", "{kv}", "{cs}", "{amp}", "no"]
result = "{out}/diag.txt"
parser = "ctffind"
"""


def test_external_programs_are_declared_as_argument_lists(tmp_path: Path):
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(PROGRAMS)
    declared = Settings.load(cfg).processing
    assert declared is not None and Settings().processing is None  # nothing runs unless declared
    assert (declared.slots, declared.work_dir) == (2, Path("/scratch/imagebio"))
    assert declared.motion is not None and declared.ctf is not None
    assert declared.motion.argv[:3] == ("/opt/mc3/MotionCor3", "-InMrc", "{movie}")
    assert declared.motion.formats == {
        "eer": ("/opt/mc3/MotionCor3", "-InEer", "{movie}", "-OutMrc", "{out}/sum.mrc")
    }
    assert (declared.motion.timeout_s, declared.ctf.timeout_s) == (900.0, 300.0)
    assert declared.ctf.stdin[-1] == "no" and declared.ctf.parser == "ctffind"

    def broken(old: str, new: str) -> str:
        assert old in PROGRAMS
        return PROGRAMS.replace(old, new)

    for bad, why in (
        (broken('argv = ["/opt/ctffind"]', 'argv = "/opt/ctffind {image}"'), "a list"),  # a command line
        (broken('argv = ["/opt/ctffind"]', 'argv = ["ctffind"]'), "absolute path"),  # found through PATH
        (broken('argv = ["/opt/ctffind"]', 'argv = ["{out}/ctffind"]'), "absolute path"),
        (broken('argv = ["/opt/ctffind"]', "argv = []"), "list of 1 to"),
        (broken('"{kv}", "{cs}"', '"{kV}", "{cs}"'), "brace that is not a placeholder"),
        (broken('"{kv}", "{cs}"', '"{voltage}", "{cs}"'), "unknown placeholder"),
        (broken('result = "{out}/diag.txt"', 'result = "/tmp/diag.txt"'), "files of {out}/"),
        (broken('image = "{out}/sum.mrc"', 'image = "{movie}"'), "files of {out}/"),
        (broken('parser = "ctffind"', 'parser = "gctf"'), "parser must be 'ctffind'"),
        (broken('parser = "motion-log"', 'parser = "ctffind"'), "'motion-star' or 'motion-log'"),
        (broken('"-InMrc", "{movie}", ', ""), "every argv must name {movie}"),
        (broken('"-InEer", "{movie}", ', ""), "every argv must name {movie}"),
        (broken("EER = ", "DM4 = "), "unknown movie format"),
        (broken('stdin = ["{image}", ', 'stdin = ["{movie}", '), "it must name {image}"),
        (broken('"{amp}", "no"]', '"{amp}", "{dose}"]'), "are not its"),
        (broken("timeout = 900", "timeout = 0"), "timeout must be between"),
        (broken("timeout = 900", 'timeout = "long"'), "a number of seconds"),
        (broken("slots = 2", "slots = 0"), "slots must be between"),
        (broken("slots = 2", "slots = 1.5"), "whole number"),
        (broken('work_dir = "/scratch/imagebio"', 'work_dir = "scratch"'), "absolute path"),
        (broken('parser = "ctffind"', 'parser = "ctffind"\nshell = true'), "expected argv"),
        ("[processing]\nslots = 1\n", "declares neither"),
        (PROGRAMS + '\n[processing.pick]\nargv = ["/opt/cryolo"]\n', "unknown key"),
        (broken('"{amp}", "no"]', '"{amp}", "no\\nyes"]'), "control characters"),
    ):
        cfg.write_text(bad)
        with pytest.raises(ConfigError) as refused:
            Settings.load(cfg)
        assert why in str(refused.value), (why, str(refused.value))


def test_missing_config_file(tmp_path: Path):
    with pytest.raises(ConfigError):
        Settings.load(tmp_path / "nope.toml")
