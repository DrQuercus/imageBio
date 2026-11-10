from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from imagebio.core.errors import CorruptFileError, NotFoundError, UnsupportedFormatError
from imagebio.core.model import Dataset, DatasetInfo, Plane
from imagebio.io.registry import Reader, Registry


class _DS(Dataset):
    def __init__(self, path: Path, reader: str) -> None:
        super().__init__(
            path, DatasetInfo(format=reader, reader=reader, size_x=1, size_y=1, dtype=np.dtype("u1"))
        )

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        return np.zeros((1, 1), "u1")


def make_reader(name_: str, exts: tuple[str, ...], magic: bytes, prio: int = 0, fail: bool = False) -> Reader:
    class R(Reader):
        name = name_
        extensions = exts
        priority = prio

        def sniff(self, path: Path, head: bytes) -> bool:
            if magic == b"RAISE":
                raise RuntimeError("boom")
            return head.startswith(magic)

        def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
            if fail:
                raise CorruptFileError(f"{name_}: cannot read {path.name}")
            return _DS(path, name_)

    return R()


def test_selection_order(tmp_path: Path):
    reg = Registry(
        [
            make_reader("aaa", (".aaa",), b"AAA"),
            make_reader("bbb", (".bbb", ".aaa"), b"BBB", prio=5),
            make_reader("weak", (".old",), b"NEVER"),
        ]
    )
    (tmp_path / "x.aaa").write_bytes(b"AAA...")
    (tmp_path / "y.aaa").write_bytes(b"BBB...")
    (tmp_path / "misnamed.bbb").write_bytes(b"AAA...")
    (tmp_path / "nomagic.old").write_bytes(b"????")
    (tmp_path / "X.AAA").write_bytes(b"AAA...")
    assert reg.open(tmp_path / "x.aaa").info.reader == "aaa"  # extension + content
    assert reg.open(tmp_path / "y.aaa").info.reader == "bbb"  # content decides among claimants
    assert reg.open(tmp_path / "misnamed.bbb").info.reader == "aaa"  # content beats a wrong extension
    assert reg.open(tmp_path / "nomagic.old").info.reader == "weak"  # no magic: extension still gets a chance
    assert reg.open(tmp_path / "X.AAA").info.reader == "aaa"  # case-insensitive
    assert reg.supports("movie.AAA") and not reg.supports("notes.txt")
    assert reg.extensions == {".aaa", ".bbb", ".old"}


def test_errors(tmp_path: Path):
    reg = Registry(
        [make_reader("aaa", (".aaa",), b"AAA", fail=True), make_reader("crash", (".zzz",), b"RAISE")]
    )
    (tmp_path / "x.aaa").write_bytes(b"AAA")
    (tmp_path / "x.txt").write_bytes(b"hello")
    (tmp_path / "dir.aaa").mkdir()
    with pytest.raises(CorruptFileError, match=r"aaa: cannot read x\.aaa"):
        reg.open(tmp_path / "x.aaa")
    with pytest.raises(UnsupportedFormatError):
        reg.open(tmp_path / "x.txt")  # sniff() raising is contained
    with pytest.raises(UnsupportedFormatError):
        reg.open(tmp_path / "dir.aaa")
    with pytest.raises(NotFoundError) as e:
        reg.open(tmp_path / "missing.aaa")
    assert str(tmp_path) not in str(e.value)


def test_fallback_to_next_reader_and_first_error_wins(tmp_path: Path):
    reg = Registry(
        [
            make_reader("strict", (".img",), b"IMG", prio=9, fail=True),
            make_reader("lenient", (".img",), b"IMG"),
        ]
    )
    (tmp_path / "a.img").write_bytes(b"IMG")
    assert reg.open(tmp_path / "a.img").info.reader == "lenient"
    reg2 = Registry(
        [
            make_reader("one", (".img",), b"IMG", prio=9, fail=True),
            make_reader("two", (".img",), b"IMG", fail=True),
        ]
    )
    with pytest.raises(CorruptFileError, match="one:"):
        reg2.open(tmp_path / "a.img")


def test_registration_rules():
    reg = Registry([make_reader("aaa", (".aaa",), b"A")])
    with pytest.raises(ValueError):
        reg.register(make_reader("aaa", (".x",), b"A"))
    with pytest.raises(ValueError):
        reg.register(make_reader("bad", ("AAA",), b"A"))
