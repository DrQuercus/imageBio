"""Deterministic TIFF fixture builders (generated at test time, never committed).

Valid files are written with ``tifffile.imwrite`` in the tests themselves; this module holds what
tifffile cannot (or should not) produce: seeded pixel data, a minimal hand-written TIFF encoder
for hostile or unusual structures, and helpers that patch the IFDs of an existing file.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# TIFF field types
BYTE, ASCII, SHORT, LONG, RATIONAL, UNDEFINED, LONG8 = 1, 2, 3, 4, 5, 7, 16
_FORMATS = {BYTE: "B", SHORT: "H", LONG: "I", UNDEFINED: "B", LONG8: "Q"}

# tag codes used by the tests
IMAGE_WIDTH, IMAGE_LENGTH, BITS_PER_SAMPLE, COMPRESSION, PHOTOMETRIC = 256, 257, 258, 259, 262
STRIP_OFFSETS, SAMPLES_PER_PIXEL, ROWS_PER_STRIP, STRIP_BYTE_COUNTS, COLORMAP = 273, 277, 278, 279, 320


def pattern(shape: Sequence[int], dtype: Any, seed: int = 0) -> np.ndarray:
    """Seeded pseudo-random pixels that use the whole range of ``dtype``."""
    rng = np.random.default_rng(seed)
    dt = np.dtype(dtype)
    if dt.kind == "b":
        return rng.integers(0, 2, shape).astype(bool)
    if dt.kind == "f":
        return ((rng.random(shape) - 0.5) * 2000).astype(dt)
    if dt.kind == "c":
        return (rng.random(shape) + 1j * rng.random(shape)).astype(dt)
    info = np.iinfo(dt)
    return rng.integers(info.min, info.max, shape, dtype=dt, endpoint=True)


def coded(shape: Sequence[int], dtype: Any = np.uint16) -> np.ndarray:
    """An N-D stack whose every plane is unique: plane ``i`` (C order over the leading axes)
    holds ``i * 7 + (y * 3 + x) % 5``, so a wrong (t, c, z) -> page mapping cannot go unnoticed."""
    *lead, ny, nx = shape
    n = int(np.prod(lead, dtype=np.int64))
    yy, xx = np.mgrid[:ny, :nx]
    planes = np.arange(n).reshape(n, 1, 1) * 7 + (yy * 3 + xx) % 5
    return planes.reshape(*lead, ny, nx).astype(dtype)


# ─── Minimal TIFF encoder ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class RawPage:
    """One IFD: ``tags`` maps a tag code to ``(type, values)``; ``data`` is its single strip.

    ``values`` is a sequence of ints, a sequence of ``(numerator, denominator)`` pairs for
    RATIONAL, or bytes for BYTE/ASCII/UNDEFINED. StripOffsets and StripByteCounts are added
    automatically unless given.
    """

    tags: Mapping[int, tuple[int, Any]]
    data: bytes = b""


def gray_page(width: int, height: int, data: bytes, *, bits: int = 8, compression: int = 1) -> RawPage:
    """A single-strip grayscale page."""
    return RawPage(
        {
            IMAGE_WIDTH: (LONG, [width]),
            IMAGE_LENGTH: (LONG, [height]),
            BITS_PER_SAMPLE: (SHORT, [bits]),
            COMPRESSION: (SHORT, [compression]),
            PHOTOMETRIC: (SHORT, [1]),
            SAMPLES_PER_PIXEL: (SHORT, [1]),
            ROWS_PER_STRIP: (LONG, [height]),
        },
        data,
    )


def build_tiff(pages: Sequence[RawPage], *, byteorder: str = "<", bigtiff: bool = False) -> bytes:
    """Encode ``pages`` as a classic TIFF or a BigTIFF (data and tag values before each IFD)."""
    bo = byteorder
    magic = b"II" if bo == "<" else b"MM"
    if bigtiff:
        buf = bytearray(magic + struct.pack(bo + "HHHQ", 43, 8, 0, 0))
        pointer, off_fmt, count_fmt, entry_count_fmt, inline = 8, "Q", "Q", "Q", 8
    else:
        buf = bytearray(magic + struct.pack(bo + "HI", 42, 0))
        pointer, off_fmt, count_fmt, entry_count_fmt, inline = 4, "I", "H", "I", 4

    def align() -> None:
        if len(buf) % 2:
            buf.append(0)

    for page in pages:
        align()
        data_offset = len(buf)
        buf += page.data
        tags = dict(page.tags)
        tags.setdefault(STRIP_OFFSETS, (LONG8 if bigtiff else LONG, [data_offset]))
        tags.setdefault(STRIP_BYTE_COUNTS, (LONG, [len(page.data)]))
        entries = []
        for code in sorted(tags):
            typ, values = tags[code]
            if isinstance(values, bytes | bytearray):
                raw, count = bytes(values), len(values)
            elif typ == RATIONAL:
                raw, count = b"".join(struct.pack(bo + "II", n, d) for n, d in values), len(values)
            else:
                raw, count = struct.pack(f"{bo}{len(values)}{_FORMATS[typ]}", *values), len(values)
            if len(raw) <= inline:
                value_field = raw.ljust(inline, b"\0")
            else:
                align()
                value_field = struct.pack(bo + off_fmt, len(buf))
                buf += raw
            entries.append(struct.pack(bo + "HH" + entry_count_fmt, code, typ, count) + value_field)
        align()
        struct.pack_into(bo + off_fmt, buf, pointer, len(buf))  # link from the header / previous IFD
        buf += struct.pack(bo + count_fmt, len(entries)) + b"".join(entries)
        pointer = len(buf)
        buf += struct.pack(bo + off_fmt, 0)
    return bytes(buf)


def write_fake_eer(path: Path, *, bigtiff: bool = False, pages: int = 2) -> None:
    """A tiny file that looks like EER to a TIFF parser: pages with compression 65001 and the
    acquisition XML in tag 65001 (same structure as legacy/cryoviewer/tests/eer_fixture.py; the
    strips are not a valid event stream, the TIFF reader must refuse before decoding)."""
    xml = b'<metadata><item name="numberOfFrames">2</item></metadata>'
    out = []
    for i in range(pages):
        page = gray_page(8, 8, b"\x7f\x7f\x7f\x00", bits=1, compression=65001)
        if i == 0:
            page = RawPage({**page.tags, 65001: (UNDEFINED, xml)}, page.data)
        out.append(page)
    path.write_bytes(build_tiff(out, bigtiff=bigtiff))


# ─── Inspecting and patching existing files ───────────────────────────────────


@dataclass(frozen=True)
class IfdInfo:
    offset: int  # position of the IFD
    next_pointer: int  # position of its "next IFD" field
    entries: dict[int, int]  # tag code -> position of the 12/20-byte entry


def _layout(data: bytes) -> tuple[str, bool]:
    bo = "<" if data[:2] == b"II" else ">"
    return bo, struct.unpack_from(bo + "H", data, 2)[0] == 43


def walk_ifds(data: bytes) -> list[IfdInfo]:
    """The IFD chain of a well-formed file (reference walker for the tests)."""
    bo, big = _layout(data)
    off_fmt, count_fmt, entry_size = (bo + "Q", bo + "Q", 20) if big else (bo + "I", bo + "H", 12)
    (offset,) = struct.unpack_from(off_fmt, data, 8 if big else 4)
    out: list[IfdInfo] = []
    while offset and len(out) < 100_000:
        (n,) = struct.unpack_from(count_fmt, data, offset)
        first = offset + struct.calcsize(count_fmt)
        entries = {
            struct.unpack_from(bo + "H", data, first + i * entry_size)[0]: first + i * entry_size
            for i in range(n)
        }
        next_pointer = first + n * entry_size
        out.append(IfdInfo(offset, next_pointer, entries))
        (offset,) = struct.unpack_from(off_fmt, data, next_pointer)
    return out


def set_next_ifd(data: bytes, page: int, target: int) -> bytes:
    """Make the "next IFD" pointer of ``page`` point to ``target`` (0 ends the chain)."""
    bo, big = _layout(data)
    buf = bytearray(data)
    struct.pack_into(bo + ("Q" if big else "I"), buf, walk_ifds(data)[page].next_pointer, target)
    return bytes(buf)


def patch_tag(data: bytes, code: int, value: int, *, page: int = 0) -> bytes:
    """Overwrite the (inline, single) value of tag ``code`` in IFD ``page``."""
    bo, big = _layout(data)
    entry = walk_ifds(data)[page].entries[code]
    (typ,) = struct.unpack_from(bo + "H", data, entry + 2)
    buf = bytearray(data)
    struct.pack_into(bo + _FORMATS[typ], buf, entry + (12 if big else 8), value)
    return bytes(buf)


def set_tag_offset(data: bytes, code: int, offset: int, *, page: int = 0) -> bytes:
    """Make the out-of-line value of tag ``code`` in IFD ``page`` start at ``offset``."""
    bo, big = _layout(data)
    entry = walk_ifds(data)[page].entries[code]
    buf = bytearray(data)
    struct.pack_into(bo + ("Q" if big else "I"), buf, entry + (12 if big else 8), offset)
    return bytes(buf)
