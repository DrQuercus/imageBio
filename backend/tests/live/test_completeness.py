"""The integrity half of the completeness test, on files cut at every interesting place."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

from fixtures.eer import write_eer
from imagebio.io.registry import default_registry
from imagebio.live.completeness import check_integrity, is_stable
from imagebio.live.simulator import mrc_bytes

REGISTRY = default_registry()


def test_stable_means_two_polls_far_enough_apart() -> None:
    assert not is_stable(100.0, 1, 200.0, 10.0)  # seen once: nothing to compare with
    assert not is_stable(100.0, 2, 105.0, 10.0)  # twice, but 5 s apart
    assert is_stable(100.0, 2, 110.0, 10.0)
    assert is_stable(100.0, 5, 150.0, 10.0)


def test_an_mrc_is_complete_when_it_holds_what_its_header_announces(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    data = mrc_bytes(rng.integers(0, 100, (6, 32, 48)).astype(np.uint16), 0.9, stack=True)
    path = tmp_path / "movie_Fractions.mrc"
    frame = 32 * 48 * 2
    for size in (0, 500, 1024, 1024 + frame - 1):  # not even one frame
        path.write_bytes(data[:size])
        result = check_integrity(path, "movie", REGISTRY)
        assert not result.ok and "movie_Fractions.mrc" in str(result.reason), size
    for frames in (1, 3, 5):  # some frames, valid so far, but not all
        path.write_bytes(data[: 1024 + frames * frame])
        result = check_integrity(path, "movie", REGISTRY)
        assert not result.ok and result.info is not None and result.info.size_t == frames
    path.write_bytes(data[:-1])
    assert not check_integrity(path, "movie", REGISTRY).ok
    path.write_bytes(data)
    result = check_integrity(path, "movie", REGISTRY)
    assert result.ok and result.reason is None
    assert result.info is not None and (result.info.size_t, result.info.size_y, result.info.size_x) == (
        6,
        32,
        48,
    )


def test_a_tiff_cut_anywhere_is_not_complete(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    whole = tmp_path / "whole.tif"
    tifffile.imwrite(whole, rng.integers(0, 4000, (5, 64, 64)).astype(np.uint16), compression="lzw")
    data = whole.read_bytes()
    assert check_integrity(whole, "image", REGISTRY).ok
    cut = tmp_path / "cut.tif"
    for size in (4, 100, len(data) // 3, len(data) // 2, len(data) - 10):
        cut.write_bytes(data[:size])
        result = check_integrity(cut, "image", REGISTRY)
        assert not result.ok, size
        assert str(tmp_path) not in str(result.reason)


def test_an_eer_missing_fractions_or_cut_is_not_complete(tmp_path: Path) -> None:
    eer = write_eer(tmp_path / "movie.eer", n_fractions=12)
    assert check_integrity(eer.path, "movie", REGISTRY).ok
    data = eer.path.read_bytes()
    cut = tmp_path / "cut.eer"
    for size in (len(data) // 4, len(data) // 2, len(data) - 3):
        cut.write_bytes(data[:size])
        assert not check_integrity(cut, "movie", REGISTRY).ok, size
    # a valid file that holds fewer fractions than its acquisition metadata announce
    short = write_eer(tmp_path / "short.eer", n_fractions=5, acquisition={"numberOfFrames": 12})
    result = check_integrity(short.path, "movie", REGISTRY)
    assert not result.ok and "12" in str(result.reason)


def test_previews_must_end_with_their_end_marker(tmp_path: Path) -> None:
    image = Image.fromarray(np.random.default_rng(2).integers(0, 255, (64, 64), dtype=np.uint8))
    for suffix in (".jpg", ".png"):
        path = tmp_path / f"preview{suffix}"
        image.save(path)
        assert check_integrity(path, "preview", REGISTRY).ok
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        result = check_integrity(path, "preview", REGISTRY)
        assert not result.ok and "truncated or still being written" in str(result.reason)
    assert not check_integrity(tmp_path / "missing.jpg", "preview", REGISTRY).ok


def test_an_xml_must_parse_and_an_mdoc_is_never_refused(tmp_path: Path) -> None:
    xml = tmp_path / "FoilHole_1.xml"
    xml.write_bytes(
        b"<MicroscopeImage><microscopeData><gun><AccelerationVoltage>300000</AccelerationVoltage>"
    )
    assert not check_integrity(xml, "xml", REGISTRY).ok
    xml.write_bytes(
        b"<MicroscopeImage><microscopeData><gun><AccelerationVoltage>300000</AccelerationVoltage></gun></microscopeData></MicroscopeImage>"
    )
    result = check_integrity(xml, "xml", REGISTRY)
    assert result.ok and result.epu is not None and result.epu.voltage == 300.0
    mdoc = tmp_path / "tilt.mrc.mdoc"
    mdoc.write_bytes(b"[ZValue = 0]\nTiltAngle = 1.0\n[ZValue = 1]\nTiltAn")
    assert check_integrity(mdoc, "mdoc", REGISTRY).ok


def test_missing_and_unknown_files(tmp_path: Path) -> None:
    gone = check_integrity(tmp_path / "gone.mrc", "image", REGISTRY)
    assert not gone.ok and "disappeared" in str(gone.reason) and str(tmp_path) not in str(gone.reason)
    odd = tmp_path / "odd.tif"
    odd.write_bytes(b"this is not an image at all, whatever its name says" * 10)
    assert not check_integrity(odd, "image", REGISTRY).ok


@pytest.mark.parametrize("role", ["image", "movie"])
def test_the_reason_never_holds_a_server_path(tmp_path: Path, role: str) -> None:
    path = tmp_path / "deep" / "tree" / "cut.mrc"
    path.parent.mkdir(parents=True)
    path.write_bytes(mrc_bytes(np.zeros((4, 8, 8), np.float32), 1.0, stack=True)[:1500])
    reason = str(check_integrity(path, role, REGISTRY).reason)
    assert "cut.mrc" in reason and "deep" not in reason and str(tmp_path) not in reason
