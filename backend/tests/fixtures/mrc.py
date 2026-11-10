"""Deterministic MRC fixture builders.

Standard files are written with ``mrcfile`` (the reference implementation the reader is compared
to). Variants that ``mrcfile`` cannot write (modes 3, 16 and 101, IMOD flags, old-style or hostile
headers) are built by hand, field by field, from the offsets of the MRC2014 specification;
``tests/io/test_mrc.py`` checks that ``mrcfile`` reads these hand-built headers back.
"""

from __future__ import annotations

import struct
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import mrcfile
import numpy as np
from mrcfile.dtypes import get_ext_header_dtype

HEADER_SIZE = 1024
IMOD_STAMP = 1146047817  # bytes "IMOD" read as a little-endian int32
IMOD_BYTES_SIGNED = 1
IMOD_4BIT_BYTES = 16
MACHINE_STAMPS = {"<": b"\x44\x44\x00\x00", ">": b"\x11\x11\x00\x00"}


def sample(dtype: str, shape: Sequence[int], seed: int = 0) -> np.ndarray:
    """Seeded test data in native byte order: full value range, extremes, NaN/Inf for floats."""
    rng = np.random.default_rng(seed)
    dt = np.dtype(dtype)
    if dt.kind == "c":
        return (rng.normal(size=shape) + 1j * rng.normal(size=shape)).astype(dt)
    if dt.kind == "f":
        data = rng.normal(100.0, 25.0, size=shape).astype(dt)
        flat = data.reshape(-1)
        specials = [np.nan, np.inf, -np.inf, np.finfo(dt).max, np.finfo(dt).tiny, -0.0]
        flat[: len(specials)] = specials[: flat.size]
        return data
    lo, hi = np.iinfo(dt).min, np.iinfo(dt).max
    data = rng.integers(lo, hi, size=shape, dtype=dt, endpoint=True)
    flat = data.reshape(-1)
    flat[:2] = [lo, hi][: flat.size]
    return data


def write_with_mrcfile(
    path: Path,
    data: np.ndarray,
    *,
    byte_order: str = "<",
    image_stack: bool = False,
    voxel_size: float | tuple[float, float, float] | None = None,
    extended_header: np.ndarray | None = None,
    exttyp: bytes | None = None,
    header: dict[str, Any] | None = None,
) -> Path:
    """Write ``data`` with mrcfile. 2-D -> image, 3-D -> volume (or image stack), 4-D -> volume stack."""
    with warnings.catch_warnings(), mrcfile.new(str(path), overwrite=True) as mrc:
        warnings.filterwarnings("ignore", "Data array contains", RuntimeWarning)  # NaN and Inf are wanted
        mrc.set_data(data.astype(data.dtype.newbyteorder(byte_order)))
        if image_stack:
            mrc.set_image_stack()
        if voxel_size is not None:
            mrc.voxel_size = voxel_size
        if extended_header is not None:
            mrc.set_extended_header(extended_header)
        if exttyp is not None:
            mrc.header.exttyp = exttyp
        for key, value in (header or {}).items():
            mrc.header[key] = value
    return path


def raw_header(
    nx: int,
    ny: int,
    nz: int,
    mode: int,
    *,
    byte_order: str = "<",
    sampling: tuple[int, int, int] | None = None,
    cell: tuple[float, float, float] = (0.0, 0.0, 0.0),
    axis_order: tuple[int, int, int] = (1, 2, 3),
    stats: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rms: float = 0.0,
    ispg: int = 0,
    nsymbt: int = 0,
    exttyp: bytes = b"",
    nversion: int = 20140,
    imod_stamp: int = 0,
    imod_flags: int = 0,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    map_id: bytes = b"MAP ",
    machine_stamp: bytes | None = None,
    labels: Sequence[str] = (),
) -> bytes:
    """A 1024-byte MRC2014 header built by hand (offsets from the specification)."""
    b = byte_order
    h = bytearray(HEADER_SIZE)
    struct.pack_into(b + "4i", h, 0, nx, ny, nz, mode)
    struct.pack_into(b + "3i", h, 28, *(sampling or (nx, ny, nz)))  # MX, MY, MZ
    struct.pack_into(b + "3f", h, 40, *cell)  # CELLA
    struct.pack_into(b + "3f", h, 52, 90.0, 90.0, 90.0)  # CELLB
    struct.pack_into(b + "3i", h, 64, *axis_order)  # MAPC, MAPR, MAPS
    struct.pack_into(b + "3f", h, 76, *stats)  # DMIN, DMAX, DMEAN
    struct.pack_into(b + "2i", h, 88, ispg, nsymbt)
    h[104:108] = exttyp.ljust(4, b"\0")
    struct.pack_into(b + "i", h, 108, nversion)
    struct.pack_into(b + "2i", h, 152, imod_stamp, imod_flags)
    struct.pack_into(b + "3f", h, 196, *origin)
    h[208:212] = map_id
    h[212:216] = MACHINE_STAMPS[b] if machine_stamp is None else machine_stamp
    struct.pack_into(b + "f", h, 216, rms)
    struct.pack_into(b + "i", h, 220, len(labels))
    for i, label in enumerate(labels):
        h[224 + 80 * i : 304 + 80 * i] = label.encode("ascii").ljust(80)
    return bytes(h)


def write_raw(path: Path, *chunks: bytes) -> Path:
    path.write_bytes(b"".join(chunks))
    return path


def pack_4bit(sections: np.ndarray) -> bytes:
    """Mode 101 packing, written as a naive loop: two pixels per byte, the first one in the low
    nibble, every row padded to a whole byte. ``sections`` is ``(nz, ny, nx)`` with values 0-15."""
    out = bytearray()
    for section in sections.tolist():
        for row in section:
            for i in range(0, len(row), 2):
                high = row[i + 1] if i + 1 < len(row) else 0
                out.append(row[i] | (high << 4))
    return bytes(out)


def fei_records(exttyp: bytes, count: int, byte_order: str = "<", **columns: Any) -> np.ndarray:
    """``count`` FEI1/FEI2 extended-header records (mrcfile dtypes). Column keywords use ``_`` for
    the spaces and dashes of the field names (``Pixel_size_X=0.83e-10``, ``Alpha_tilt=[...]``)."""
    dtype = get_ext_header_dtype(exttyp, byte_order)
    records = np.zeros(count, dtype=dtype)
    records["Metadata size"] = dtype.itemsize
    records["Metadata version"] = 0 if exttyp == b"FEI1" else 2
    by_key = {name.replace(" ", "_").replace("-", "_"): name for name in dtype.names}
    for key, value in columns.items():
        records[by_key[key]] = value
    return records
