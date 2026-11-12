"""The microscope of a session: what its CTF is estimated with, set where it is known."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import numpy as np
import pytest

from fixtures.api import ALICE, BOB, build_tree
from fixtures.ctf import micrograph
from fixtures.live_world import WATCHES, World
from imagebio.core.config import ConfigError, RootConfig, Settings
from imagebio.live.simulator import _epu_xml, mrc_bytes

MICROSCOPE = "microscope"


def a_world(tmp_path: Path, **root_microscope: float) -> World:
    """A server whose data root ``public`` holds the sessions of a 200 kV microscope (or of
    the one given), the others those of ``[microscope]`` (300 kV)."""
    tree = build_tree(tmp_path / "data")
    roots = tuple(
        dataclasses.replace(r, **root_microscope) if r.name == "public" else r for r in tree.roots()
    )
    tree.roots = lambda: roots  # type: ignore[method-assign]
    for directory in ("public/glacios", "public/other", "cryo/krios"):
        (tree.base / directory).mkdir()
    return World(tree, tmp_path / "state")


def follow(world: World, session: str, who: dict[str, str]) -> int:
    watch = world.watch(session, who)
    world.run([], 5)
    return watch


def arrive(world: World, session: str, images: dict[str, np.ndarray], xml_kv: float | None = None) -> None:
    directory = world.tree.base / session
    for name, pixels in images.items():
        (directory / f"{name}.mrc").write_bytes(mrc_bytes(pixels.astype(np.float32), 1.0))
        if xml_kv is not None:  # what EPU writes next to it, the voltage of the acquisition in it
            xml = _epu_xml(world.now, 1.0e0, 40.0, 1.5, (0.0, 0.0), 1).replace(
                b"300000", str(int(xml_kv * 1000)).encode()
            )
            assert str(int(xml_kv * 1000)).encode() in xml
            (directory / f"{name}.xml").write_bytes(xml)
        for p in directory.iterdir():
            os.utime(p, (world.now, world.now))
    os.utime(directory, (world.now, world.now))
    world.run([], 40)


def ctf_of(world: World, watch: int, who: dict[str, str]) -> dict[str, dict]:
    return {item["name"]: item["metrics"] for item in world.items(watch, who)["items"]}


def put(world: World, watch: int, who: dict[str, str], **params: float) -> dict:
    r = world.client.put(f"{WATCHES}/{watch}/{MICROSCOPE}", params=params, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_values_come_from_the_session_its_data_root_or_the_server(tmp_path: Path) -> None:
    world = a_world(tmp_path, kv=200.0, cs_mm=1.4)
    try:
        glacios = follow(world, "public/glacios", ALICE)
        krios = follow(world, "cryo/krios", ALICE)
        here = world.summary(glacios, ALICE)["microscope"]
        assert (here["kv"], here["cs_mm"], here["amplitude"]) == (
            {"value": 200.0, "source": "root"},
            {"value": 1.4, "source": "root"},
            {"value": 0.07, "source": "server"},
        )
        there = world.summary(krios, ALICE)["microscope"]
        assert there["kv"] == {"value": 300.0, "source": "server"} and there["set_at"] is None

        set_here = put(world, glacios, ALICE, kv=120, amp=0.1)
        assert (
            set_here["kv"] == {"value": 120.0, "source": "session"} and set_here["cs_mm"]["source"] == "root"
        )
        assert (
            set_here["own"] == {"kv": 120.0, "cs_mm": None, "amplitude": 0.1}
            and set_here["set_at"] == world.now
        )
        # it belongs to the directory: everybody who watches it sees it
        theirs = follow(world, "public/glacios", BOB)
        assert world.summary(theirs, BOB)["microscope"]["kv"] == {"value": 120.0, "source": "session"}
        # what is left out is forgotten
        forgot = put(world, glacios, ALICE)
        assert forgot["kv"]["source"] == "root" and forgot["own"] == {
            "kv": None,
            "cs_mm": None,
            "amplitude": None,
        }
        assert forgot["set_at"] is None
        for bad in ({"kv": 5}, {"kv": 3000}, {"cs": -1}, {"amp": 0.9}, {"kv": "nan"}, {"voltage": 300}):
            r = world.client.put(f"{WATCHES}/{glacios}/{MICROSCOPE}", params=bad, headers=ALICE)
            assert r.status_code in (400, 422), bad
        assert (
            world.client.put(f"{WATCHES}/{glacios}/{MICROSCOPE}", params={"kv": 200}, headers=BOB).status_code
            == 404
        )
    finally:
        world.client.__exit__(None, None, None)


def test_the_ctf_is_estimated_with_the_voltage_of_the_session(tmp_path: Path) -> None:
    """A 200 kV micrograph read as if it came from a 300 kV microscope gets a defocus 27 % too
    large (the wavelength enters the phase): the data root says 200 kV, and it is right."""
    rng = np.random.default_rng(5)
    image = micrograph((1024, 1024), 1.0, 2.0, rng, kv=200.0)
    world = a_world(tmp_path, kv=200.0)
    try:
        glacios = follow(world, "public/glacios", ALICE)
        other = follow(world, "cryo/krios", ALICE)
        arrive(world, "public/glacios", {"a": image})
        (world.tree.base / "cryo/krios").joinpath("a.mrc").write_bytes(
            mrc_bytes(image.astype(np.float32), 1.0)
        )
        os.utime(world.tree.base / "cryo/krios", (world.now, world.now))
        world.run([], 40)
        right = ctf_of(world, glacios, ALICE)["a"]
        assert right["ctf"]["defocus"] == pytest.approx(2.0, rel=0.02)
        assert right["microscope"] == {"kv": 200.0, "kv_from": "root", "cs_mm": 2.7, "amplitude": 0.07}
        wrong = ctf_of(world, other, ALICE)["a"]  # the same image, said to be from the 300 kV one
        assert wrong["microscope"]["kv"] == 300.0 and wrong["ctf"]["defocus"] > 2.4
    finally:
        world.client.__exit__(None, None, None)


def test_changing_the_microscope_measures_again_what_used_other_values(tmp_path: Path) -> None:
    rng = np.random.default_rng(6)
    world = a_world(tmp_path)  # no microscope declared for the data root: the server's 300 kV
    try:
        watch = follow(world, "public/glacios", ALICE)
        arrive(world, "public/glacios", {"a": micrograph((1024, 1024), 1.0, 2.0, rng, kv=200.0)})
        arrive(
            world, "public/glacios", {"b": micrograph((1024, 1024), 1.0, 1.5, rng, kv=200.0)}, xml_kv=200.0
        )
        before = ctf_of(world, watch, ALICE)
        assert before["a"]["microscope"]["kv"] == 300.0 and before["a"]["ctf"]["defocus"] > 2.4  # wrong
        assert before["b"]["microscope"]["kv_from"] == "xml" and before["b"]["ctf"][
            "defocus"
        ] == pytest.approx(1.5, rel=0.02)
        seen = world.items(watch, ALICE)["seq"]

        changed = put(world, watch, ALICE, kv=200)
        assert changed["remeasure"] == 1  # "b" said its voltage itself: nothing to redo for it
        world.run([], 20)
        after = ctf_of(world, watch, ALICE)
        assert after["a"]["microscope"] == {
            "kv": 200.0,
            "kv_from": "session",
            "cs_mm": 2.7,
            "amplitude": 0.07,
        }
        assert after["a"]["ctf"]["defocus"] == pytest.approx(2.0, rel=0.02)
        assert after["b"] == before["b"]
        assert [i["name"] for i in world.items(watch, ALICE, since=seen)["items"]] == [
            "a"
        ]  # told as a change

        # a value every image used changes: all of them are measured again
        assert put(world, watch, ALICE, kv=200, cs=1.4)["remeasure"] == 2
        assert put(world, watch, ALICE, kv=200, cs=1.4)["remeasure"] == 0  # the same again: nothing
    finally:
        world.client.__exit__(None, None, None)


def test_a_data_root_declares_its_microscope_in_the_configuration(tmp_path: Path) -> None:
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text(
        f'[[roots]]\nname = "glacios"\npath = "{tmp_path}"\nkv = 200\ncs_mm = 2.7\namplitude_contrast = 0.1\n'
        f'[[roots]]\nname = "krios"\npath = "{tmp_path}"\n'
    )
    glacios, krios = Settings.load(cfg).roots
    assert (glacios.kv, glacios.cs_mm, glacios.amplitude_contrast) == (200.0, 2.7, 0.1)
    assert (krios.kv, krios.cs_mm, krios.amplitude_contrast) == (None, None, None)
    for bad in (
        "kv = 5",
        "kv = true",
        "cs_mm = 50",
        "amplitude_contrast = 0.8",
        'kv = "200"',
        "voltage = 200",
    ):
        cfg.write_text(f'[[roots]]\nname = "g"\npath = "{tmp_path}"\n{bad}\n')
        with pytest.raises(ConfigError):
            Settings.load(cfg)
    with pytest.raises(ConfigError, match="kv must be between"):
        RootConfig("g", tmp_path, kv=10000)
