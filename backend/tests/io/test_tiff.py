from __future__ import annotations

import itertools
import json
import mmap
import os
import random
import tracemalloc
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import imagecodecs
import numpy as np
import psutil
import pytest
import tifffile
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from fixtures.tiff import (
    BITS_PER_SAMPLE,
    BYTE,
    COLORMAP,
    COMPRESSION,
    IMAGE_LENGTH,
    IMAGE_WIDTH,
    PHOTOMETRIC,
    SHORT,
    RawPage,
    build_tiff,
    coded,
    gray_page,
    patch_tag,
    pattern,
    set_next_ifd,
    set_tag_offset,
    walk_ifds,
    write_fake_eer,
)
from imagebio.core.errors import CorruptFileError, ImageBioError, InvalidRequestError, NotFoundError
from imagebio.core.model import PLANE_DTYPES, CalibrationSource, Dataset, DatasetInfo, Plane, StackKind
from imagebio.io.readers import tiff as tiff_module
from imagebio.io.readers.tiff import MAX_PLANE_BYTES, READER, TiffDataset, TiffReader
from imagebio.io.registry import Reader, Registry

# ─── Helpers ──────────────────────────────────────────────────────────────────


def open_tiff(path: Path, **options: Any) -> Dataset:
    return READER.open(path, options or None)


def is_mmap_backed(a: np.ndarray) -> bool:
    base: Any = a
    while base is not None:
        if isinstance(base, np.memmap | mmap.mmap):
            return True
        base = getattr(base, "base", None) if isinstance(base, np.ndarray) else None
    return False


def read(ds: Dataset, t: int = 0, c: int = 0, z: int = 0) -> Plane:
    """read_plane + the invariants every plane must satisfy."""
    plane = ds.read_plane(t, c, z)
    info = ds.info
    assert type(plane) is np.ndarray and not is_mmap_backed(plane)
    assert plane.shape == (info.size_y, info.size_x)
    assert plane.dtype == info.dtype and plane.dtype.isnative and plane.dtype in PLANE_DTYPES
    assert plane.flags.c_contiguous
    return plane


def all_planes(ds: Dataset) -> np.ndarray:
    """Every plane, as a (T, C, Z, Y, X) array."""
    info = ds.info
    out = np.empty(info.shape, info.dtype)
    for t, c, z in np.ndindex(info.size_t, info.size_c, info.size_z):
        out[t, c, z] = read(ds, t, c, z)
    return out


def plane_of(a: np.ndarray, axes: str, t: int, c: int, z: int) -> np.ndarray:
    """Plane (t, c, z) of an array whose leading axes are named by ``axes`` (e.g. 'TZC')."""
    return a[tuple({"T": t, "C": c, "Z": z}[ax] for ax in axes)]


def write_pages(path: Path, pages: np.ndarray, **kw: Any) -> None:
    """A plain multi-page TIFF, one IFD per plane and no descriptive metadata (like K3/Falcon
    movies written by EPU, SerialEM or MotionCor)."""
    with tifffile.TiffWriter(
        path, bigtiff=kw.pop("bigtiff", False), byteorder=kw.pop("byteorder", None)
    ) as tw:
        for page in pages:
            tw.write(page, contiguous=False, metadata=None, **kw)


# ─── Identity ─────────────────────────────────────────────────────────────────


def test_reader_identity_and_sniff():
    assert isinstance(READER, Reader) and isinstance(READER, TiffReader)
    assert READER.name == "tiff"
    assert set(READER.extensions) == {".tif", ".tiff", ".btf", ".gain"}
    for magic in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        assert READER.sniff(Path("x.tif"), magic + bytes(60))
    for other in (b"", b"II", b"II\x00*", b"\x89PNG\r\n\x1a\n", bytes(64), b"MAP " * 4):
        assert not READER.sniff(Path("x.tif"), other)


def test_registry_opens_tiff_by_extension_and_by_content(tmp_path: Path):
    a = pattern((9, 11), np.uint16)
    tifffile.imwrite(tmp_path / "a.ome.tif", a, ome=True)
    tifffile.imwrite(tmp_path / "b.btf", a, bigtiff=True)
    tifffile.imwrite(tmp_path / "misnamed.dat", a)
    reg = Registry([READER])
    for name in ("a.ome.tif", "b.btf", "misnamed.dat"):
        with reg.open(tmp_path / name) as ds:
            assert isinstance(ds, TiffDataset) and ds.info.reader == "tiff" and ds.info.format == "tiff"
            assert np.array_equal(read(ds), a)
    assert reg.supports("movie_fractions.TIFF") and reg.supports("x.ome.tif")


# ─── Bit-exactness against tifffile ───────────────────────────────────────────

GRAY_DTYPES = ("uint8", "int8", "uint16", "int16", "uint32", "int32", "float32", "float64")
STORAGE: dict[str, dict[str, Any]] = {
    "raw": {},
    "strips": {"rowsperstrip": 5},
    "lzw": {"compression": "lzw"},
    "deflate": {"compression": "zlib"},
    "zstd": {"compression": "zstd"},
    "packbits": {"compression": "packbits"},
    "lzw-predictor": {"compression": "lzw", "predictor": True},
    "tiled": {"tile": (16, 32)},
    "tiled-deflate": {"tile": (32, 16), "compression": "zlib"},
    "bigtiff": {"bigtiff": True},
    "bigtiff-zstd": {"bigtiff": True, "compression": "zstd"},
    "big-endian": {"byteorder": ">"},
    "big-endian-lzw": {"byteorder": ">", "compression": "lzw"},
    "big-endian-bigtiff-tiled": {"byteorder": ">", "bigtiff": True, "tile": (16, 16)},
}


@pytest.mark.parametrize("storage", STORAGE)
@pytest.mark.parametrize("dtype", GRAY_DTYPES)
def test_gray_pages_are_bit_exact(tmp_path: Path, dtype: str, storage: str):
    a = pattern((3, 37, 53), dtype, seed=len(storage))
    path = tmp_path / "stack.tif"
    write_pages(path, a, **dict(STORAGE[storage]))
    ref = tifffile.imread(path)
    assert np.array_equal(ref, a)  # the fixture is what we think it is
    with open_tiff(path) as ds, tifffile.TiffFile(path) as tif:
        info = ds.info
        assert info.shape == (3, 1, 1, 37, 53) and info.dtype == np.dtype(dtype)
        assert not info.rgb and info.stack_kind is StackKind.MOVIE
        for t in range(3):
            plane = read(ds, t)
            assert np.array_equal(plane, ref[t])
            assert np.array_equal(plane, tif.pages[t].asarray())
        assert info.metadata["page_count"] == 3
        assert info.metadata["is_bigtiff"] is bool(STORAGE[storage].get("bigtiff"))
        assert info.metadata["byte_order"] == (
            "big" if STORAGE[storage].get("byteorder") == ">" else "little"
        )


def test_float16_bool_and_64bit_integers_are_converted_as_documented(tmp_path: Path):
    f16 = pattern((2, 9, 13), np.float16)
    tifffile.imwrite(tmp_path / "f16.tif", f16, photometric="minisblack")
    with open_tiff(tmp_path / "f16.tif") as ds:
        assert ds.info.dtype == np.float32 and not ds.info.warnings[1:]
        assert np.array_equal(
            all_planes(ds)[:, 0, 0], tifffile.imread(tmp_path / "f16.tif").astype(np.float32)
        )

    bits = pattern((33, 47), bool)  # 47 px wide: rows are padded to whole bytes in the file
    for photometric in ("minisblack", "miniswhite"):
        path = tmp_path / f"bool_{photometric}.tif"
        tifffile.imwrite(path, bits, photometric=photometric)
        with open_tiff(path) as ds:
            assert ds.info.dtype == np.uint8 and ds.info.metadata["bits_per_sample"] == 1
            plane = read(ds)
            assert np.array_equal(plane, tifffile.imread(path).astype(np.uint8))
            assert np.array_equal(plane, bits.astype(np.uint8))  # stored values, never inverted
            assert any("MinIsWhite" in w for w in ds.info.warnings) is (photometric == "miniswhite")

    for dtype in (np.int64, np.uint64):
        path = tmp_path / f"{np.dtype(dtype).name}.tif"
        a = pattern((7, 5), dtype) >> 12  # below 2**53: the conversion is exact
        tifffile.imwrite(path, a)
        with open_tiff(path) as ds:
            assert ds.info.dtype == np.float64
            assert any("64-bit" in w and "float64" in w for w in ds.info.warnings)
            assert np.array_equal(read(ds), tifffile.imread(path).astype(np.float64))

    tifffile.imwrite(tmp_path / "complex.tif", pattern((7, 5), np.complex64))
    with pytest.raises(CorruptFileError, match="complex"):
        open_tiff(tmp_path / "complex.tif")


# ─── Hyperstacks: (t, c, z) -> page ───────────────────────────────────────────


@pytest.mark.parametrize("kw", [{}, {"compression": "lzw"}, {"truncate": True}, {"byteorder": ">"}], ids=str)
def test_imagej_hyperstack_tzcyx_mapping(tmp_path: Path, kw: dict[str, Any]):
    a = coded((2, 3, 4, 17, 23))  # T Z C Y X: ImageJ order
    path = tmp_path / "hyper.tif"
    tifffile.imwrite(path, a, imagej=True, metadata={"axes": "TZCYX"}, **kw)
    with open_tiff(path) as ds, tifffile.TiffFile(path) as tif:
        info = ds.info
        assert info.shape == (2, 4, 3, 17, 23)  # T C Z Y X
        assert info.stack_kind is StackKind.HYPERSTACK and not info.rgb
        assert info.metadata["kind"] == "imagej" and info.metadata["axes"] == "TZCYX"
        assert info.metadata["imagej"]["frames"] == 2 and info.metadata["imagej"]["slices"] == 3
        truncated = len(tif.pages) == 1  # ImageJ's "one IFD, contiguous planes" layout for big stacks
        assert truncated is bool(kw.get("truncate")) and info.metadata["page_count"] == len(tif.pages)
        for t, c, z in np.ndindex(2, 4, 3):
            plane = read(ds, t, c, z)
            assert np.array_equal(plane, a[t, z, c]), (t, c, z)
            if not truncated:
                assert np.array_equal(plane, tif.pages[(t * 3 + z) * 4 + c].asarray())
    assert np.array_equal(tifffile.imread(path), a)


def test_imagej_zcyx_and_single_axis_stacks(tmp_path: Path):
    a = coded((3, 4, 17, 23), np.int16)
    tifffile.imwrite(tmp_path / "zc.tif", a, imagej=True, metadata={"axes": "ZCYX"})
    with open_tiff(tmp_path / "zc.tif") as ds:
        assert ds.info.shape == (1, 4, 3, 17, 23) and ds.info.stack_kind is StackKind.HYPERSTACK
        for c, z in np.ndindex(4, 3):
            assert np.array_equal(read(ds, 0, c, z), a[z, c])
    for axes, kind, shape in (
        ("ZYX", StackKind.VOLUME, (1, 1, 3)),
        ("TYX", StackKind.STACK, (3, 1, 1)),
        ("CYX", StackKind.STACK, (1, 3, 1)),
    ):
        path = tmp_path / f"{axes}.tif"
        tifffile.imwrite(path, a[:, 0], imagej=True, metadata={"axes": axes}, compression="zlib")
        with open_tiff(path) as ds:
            assert ds.info.shape[:3] == shape and ds.info.stack_kind is kind
            got = all_planes(ds).reshape(3, 17, 23)
            assert np.array_equal(got, a[:, 0]) and np.array_equal(got, tifffile.imread(path))


@pytest.mark.parametrize("axes", ["TCZYX", "TZCYX", "ZTCYX", "CZTYX", "ZCYX", "TZYX", "CYX"])
@pytest.mark.parametrize("compression", [None, "zlib"])
def test_ome_tiff_mapping_follows_the_declared_axes(tmp_path: Path, axes: str, compression: str | None):
    sizes = {"T": 2, "C": 3, "Z": 4}
    lead = axes[:-2]
    a = coded((*(sizes[ax] for ax in lead), 15, 19), np.uint16)
    path = tmp_path / "img.ome.tif"
    tifffile.imwrite(path, a, ome=True, metadata={"axes": axes}, compression=compression)
    with open_tiff(path) as ds:
        info = ds.info
        nt, nc, nz = (sizes[ax] if ax in lead else 1 for ax in "TCZ")
        assert info.shape == (nt, nc, nz, 15, 19) and info.metadata["kind"] == "ome"
        for t, c, z in np.ndindex(nt, nc, nz):
            assert np.array_equal(read(ds, t, c, z), plane_of(a, lead, t, c, z)), (t, c, z)
    assert np.array_equal(tifffile.imread(path), a)


def as_tczyx(a: np.ndarray, axes: str) -> np.ndarray:
    """What the reader must return for an array written with ``axes``: samples (S) are channels,
    unnamed axes (Q) are flattened into T, and stored types are converted as documented."""
    axes = axes.replace("S", "C")
    unnamed = axes.count("Q")
    if unnamed:
        a, axes = a.reshape(-1, *a.shape[unnamed:]), "T" + axes[unnamed:]
    for missing in (ax for ax in "TCZ" if ax not in axes):
        a, axes = a[None], missing + axes
    a = a.transpose([axes.index(ax) for ax in "TCZYX"])
    if a.dtype.kind == "b":
        return a.astype(np.uint8)
    if a.dtype == np.float16:
        return a.astype(np.float32)
    return a.astype(np.float64) if a.dtype.kind in "iu" and a.dtype.itemsize == 8 else a


def test_random_layouts_match_tifffile(tmp_path: Path):
    """Differential test: seeded random combinations of flavour, axes, pixel type and storage,
    every plane compared with what tifffile.imread returns for the whole file."""
    rnd = random.Random(20261110)
    gray_axes = {
        "imagej": ["TZCYX", "ZCYX", "TCYX", "TZYX", "ZYX", "TYX", "CYX", "YX"],
        "ome": ["TCZYX", "TZCYX", "ZTCYX", "CZTYX", "CTZYX", "ZCTYX", "ZCYX", "TZYX", "CYX", "ZYX", "YX"],
        "shaped": ["QQYX", "QYX", "YX"],
    }
    rgb_axes = {
        "imagej": ["TZYXS", "ZYXS", "TYXS", "YXS"],
        "ome": ["TZYXS", "ZYXS", "YXS", "ZSYX", "SYX"],
        "shaped": ["QYXS", "YXS", "QSYX", "SYX"],
    }
    dtypes = {
        "imagej": ["uint8", "uint16", "float32"],
        "ome": list(GRAY_DTYPES),
        "shaped": [*GRAY_DTYPES, "float16", "bool"],
    }
    path = tmp_path / "random.tif"
    kinds: set[str] = set()
    for case in range(150):
        flavour = rnd.choice(sorted(gray_axes))
        rgb = rnd.random() < 0.3
        axes = rnd.choice((rgb_axes if rgb else gray_axes)[flavour])
        dtype = rnd.choice(
            ["uint8"] if rgb and flavour == "imagej" else ["uint8", "uint16"] if rgb else dtypes[flavour]
        )
        sizes = {
            "T": rnd.randint(1, 3),
            "Z": rnd.randint(1, 4),
            "C": rnd.randint(1, 3),
            "Q": rnd.randint(1, 3),
        }
        sizes |= {
            "Y": rnd.randint(2, 40),
            "X": rnd.randint(2, 40),
            "S": 3 if flavour == "imagej" else rnd.choice([3, 4]),
        }
        a = pattern([sizes[ax] for ax in axes], dtype, seed=case)
        kw: dict[str, Any] = {"photometric": "rgb" if rgb else "minisblack"}
        if rgb and axes.index("S") < axes.index("Y"):
            kw["planarconfig"] = "separate"
        if sizes["S"] == 4 and rgb:
            kw["extrasamples"] = ["unassalpha"]
        compression = rnd.choice([None, None, "lzw", "zlib", "zstd", "packbits"])
        if compression:
            kw["compression"] = compression
            if dtype not in ("bool", "float16") and compression != "packbits" and rnd.random() < 0.3:
                kw["predictor"] = True
        if flavour != "imagej":  # ImageJ reads neither tiles nor BigTIFF
            if rnd.random() < 0.3:
                kw["tile"] = (16 * rnd.randint(1, 2), 16 * rnd.randint(1, 2))
            kw["bigtiff"] = rnd.random() < 0.2
        elif compression is None and rnd.random() < 0.3:
            kw["truncate"] = True  # single IFD, like ImageJ beyond 4 GB
        if rnd.random() < 0.3:
            kw["byteorder"] = ">"
        if flavour == "shaped":
            tifffile.imwrite(path, a, **kw)
        else:
            tifffile.imwrite(path, a, metadata={"axes": axes}, **{flavour: True}, **kw)
        expected = as_tczyx(tifffile.imread(path).reshape(a.shape), axes)
        with open_tiff(path) as ds:
            context = (case, flavour, axes, a.shape, dtype, kw)
            assert ds.info.shape == expected.shape and ds.info.dtype == expected.dtype, context
            assert ds.info.rgb is rgb, context
            assert np.array_equal(all_planes(ds), expected), context
            kinds.add(ds.info.metadata["kind"])
    assert kinds == {"imagej", "ome", "shaped"}


def test_plain_multipage_stacks_are_movies_on_t(tmp_path: Path):
    frames = pattern((12, 40, 56), np.uint8, seed=3)  # K3-like: uint8, LZW, one IFD per frame
    write_pages(tmp_path / "k3_fractions.tiff", frames, compression="lzw")
    tifffile.imwrite(tmp_path / "shaped.tif", frames, photometric="minisblack")  # tifffile's own layout
    for name, kind in (("k3_fractions.tiff", "uniform"), ("shaped.tif", "shaped")):
        with open_tiff(tmp_path / name) as ds:
            info = ds.info
            assert info.shape == (12, 1, 1, 40, 56) and info.stack_kind is StackKind.MOVIE
            assert info.metadata["kind"] == kind and info.metadata["page_count"] == 12
            assert np.array_equal(all_planes(ds)[:, 0, 0], frames)
    with open_tiff(tmp_path / "k3_fractions.tiff") as ds:
        md = ds.info.metadata
        assert md["compression"] == "LZW" and md["photometric"] == "MINISBLACK"
        assert md["bits_per_sample"] == 8 and md["samples_per_pixel"] == 1
        assert md["software"] == "tifffile.py" and md["tile"] is None and md["rows_per_strip"] >= 1


def test_unnamed_extra_axes_are_flattened_into_t_with_a_warning(tmp_path: Path):
    a = coded((2, 3, 9, 11))
    tifffile.imwrite(tmp_path / "qq.tif", a, photometric="minisblack")  # shaped series, axes 'QQYX'
    with open_tiff(tmp_path / "qq.tif") as ds:
        assert ds.info.shape == (6, 1, 1, 9, 11) and ds.info.metadata["axes"] == "QQYX"
        assert any("flattened into T" in w for w in ds.info.warnings)
        assert np.array_equal(all_planes(ds)[:, 0, 0], a.reshape(6, 9, 11))


def test_single_plane_kinds(tmp_path: Path):
    tifffile.imwrite(tmp_path / "one.tif", pattern((8, 9), np.float32))
    tifffile.imwrite(tmp_path / "rgb.tif", pattern((8, 9, 3), np.uint8), photometric="rgb")
    tifffile.imwrite(tmp_path / "rgbs.tif", pattern((4, 8, 9, 3), np.uint8), photometric="rgb")
    for name, kind in (
        ("one.tif", StackKind.IMAGE),
        ("rgb.tif", StackKind.IMAGE),
        ("rgbs.tif", StackKind.STACK),
    ):
        with open_tiff(tmp_path / name) as ds:
            assert ds.info.stack_kind is kind, name


# ─── Samples: RGB, alpha, palette ─────────────────────────────────────────────

RGB_CASES: dict[str, tuple[tuple[int, ...], str, dict[str, Any]]] = {
    "contig-u8": ((21, 30, 3), "uint8", {}),
    "contig-u16-lzw": ((21, 30, 3), "uint16", {"compression": "lzw"}),
    "contig-tiled-deflate": ((40, 50, 3), "uint8", {"tile": (16, 16), "compression": "zlib"}),
    "contig-jpeg": ((32, 48, 3), "uint8", {"compression": "jpeg"}),
    "planar-u8": ((3, 21, 30), "uint8", {"planarconfig": "separate"}),
    "planar-u16-be": ((3, 21, 30), "uint16", {"planarconfig": "separate", "byteorder": ">"}),
    "planar-lzw": ((3, 21, 30), "uint8", {"planarconfig": "separate", "compression": "lzw"}),
    "planar-strips": ((3, 21, 30), "uint16", {"planarconfig": "separate", "rowsperstrip": 4}),
    "rgba": ((21, 30, 4), "uint8", {"extrasamples": ["unassalpha"]}),
    "rgba-planar-zstd": (
        (4, 21, 30),
        "uint8",
        {"planarconfig": "separate", "extrasamples": ["unassalpha"], "compression": "zstd"},
    ),
    "float-rgb": ((21, 30, 3), "float32", {}),
}  # fmt: skip


@pytest.mark.parametrize("case", RGB_CASES)
def test_rgb_pages_become_channels(tmp_path: Path, case: str):
    shape, dtype, kw = RGB_CASES[case]
    planar = kw.get("planarconfig") == "separate"
    a = pattern(shape, dtype, seed=5)
    path = tmp_path / "rgb.tif"
    tifffile.imwrite(path, a, photometric="rgb", **kw)
    ref = tifffile.imread(path)  # JPEG is lossy: the reference is what tifffile decodes
    if "jpeg" not in case:
        assert np.array_equal(ref, a)
    n = shape[0] if planar else shape[-1]
    with open_tiff(path) as ds:
        info = ds.info
        assert info.rgb and info.size_c == n and info.shape[:3] == (1, n, 1)
        assert info.stack_kind is StackKind.IMAGE and info.dtype == np.dtype(dtype)
        assert info.metadata["planar_config"] == ("SEPARATE" if planar else "CONTIG")
        for c in (*range(n), 0, n - 1):  # also re-read: exercises the decoded-page memo
            assert np.array_equal(read(ds, 0, c), ref[c] if planar else ref[..., c]), c


def test_rgb_time_series_and_ome_rgb(tmp_path: Path):
    a = pattern((5, 16, 20, 3), np.uint8)
    tifffile.imwrite(tmp_path / "ij.tif", a, imagej=True, metadata={"axes": "TYXS"})
    with open_tiff(tmp_path / "ij.tif") as ds:
        assert ds.info.shape == (5, 3, 1, 16, 20) and ds.info.rgb and ds.info.stack_kind is StackKind.STACK
        assert np.array_equal(all_planes(ds)[:, :, 0], a.transpose(0, 3, 1, 2))
    tifffile.imwrite(tmp_path / "c.ome.tif", a, ome=True, photometric="rgb", metadata={"axes": "ZYXS"})
    planar = np.ascontiguousarray(a.transpose(0, 3, 1, 2))
    tifffile.imwrite(
        tmp_path / "p.ome.tif", planar, ome=True, photometric="rgb", planarconfig="separate",
        metadata={"axes": "ZSYX"}, compression="zlib",
    )  # fmt: skip
    for name in ("c.ome.tif", "p.ome.tif"):
        with open_tiff(tmp_path / name) as ds:
            assert ds.info.shape == (1, 3, 5, 16, 20) and ds.info.rgb, name
            assert ds.info.stack_kind is StackKind.VOLUME
            assert np.array_equal(all_planes(ds)[0], planar.transpose(1, 0, 2, 3))


def test_gray_alpha_is_two_plain_channels(tmp_path: Path):
    a = pattern((21, 30, 2), np.uint8)
    for kw in ({}, {"compression": "lzw"}):
        tifffile.imwrite(tmp_path / "la.tif", a, photometric="minisblack", extrasamples=["unassalpha"], **kw)
        with open_tiff(tmp_path / "la.tif") as ds:
            assert ds.info.shape[:3] == (1, 2, 1) and not ds.info.rgb
            assert ds.info.metadata["extra_samples"] == ["UNASSALPHA"]
            ref = tifffile.imread(tmp_path / "la.tif")
            assert np.array_equal(read(ds, 0, 0), ref[..., 0]) and np.array_equal(read(ds, 0, 1), ref[..., 1])


def test_other_colour_models_stay_stored_channels(tmp_path: Path):
    cmyk = pattern((12, 14, 4), np.uint8)
    tifffile.imwrite(tmp_path / "cmyk.tif", cmyk, photometric="separated")
    with open_tiff(tmp_path / "cmyk.tif") as ds:
        assert ds.info.shape[:3] == (1, 4, 1) and not ds.info.rgb and ds.info.stack_kind is StackKind.STACK
        assert any("SEPARATED" in w and "without colour conversion" in w for w in ds.info.warnings)
        assert np.array_equal(all_planes(ds)[0, :, 0], cmyk.transpose(2, 0, 1))
    # five samples cannot be RGB(A)
    five = pattern((12, 14, 5), np.uint16)
    tifffile.imwrite(tmp_path / "five.tif", five, photometric="minisblack", planarconfig="contig")
    with open_tiff(tmp_path / "five.tif") as ds:
        assert ds.info.size_c == 5 and not ds.info.rgb
        assert np.array_equal(
            all_planes(ds)[0, :, 0], tifffile.imread(tmp_path / "five.tif").transpose(2, 0, 1)
        )


def test_degenerate_sample_layouts(tmp_path: Path):
    # found by fuzzing: "RGB" or planar pages with a single sample have shape (1, Y, X) in tifffile
    data = bytes(pattern((6, 10), np.uint8))
    page = gray_page(10, 6, data)
    for tags in (
        {PHOTOMETRIC: (SHORT, [2])},
        {PHOTOMETRIC: (SHORT, [2]), 284: (SHORT, [2])},
        {284: (SHORT, [2])},
    ):
        path = tmp_path / "one_sample.tif"
        path.write_bytes(build_tiff([RawPage({**page.tags, **tags}, data)]))
        for compression in (1, 32773):  # raw bytes, then through the decoder (PackBits literal runs)
            if compression != 1:
                packed = b"".join(b"\x09" + data[i : i + 10] for i in range(0, 60, 10))
                tagged = {**page.tags, **tags, COMPRESSION: (SHORT, [compression])}
                path.write_bytes(build_tiff([RawPage(tagged, packed)]))
            with open_tiff(path) as ds:
                assert ds.info.shape == (1, 1, 1, 6, 10) and not ds.info.rgb
                assert np.array_equal(read(ds), np.frombuffer(data, np.uint8).reshape(6, 10))
                assert np.array_equal(read(ds), tifffile.imread(path).reshape(6, 10))
    # 1 x 1 pixel, and a 1-pixel-wide RGB strip
    tifffile.imwrite(tmp_path / "dot.tif", np.array([[7]], np.uint16))
    with open_tiff(tmp_path / "dot.tif") as ds:
        assert ds.info.shape == (1, 1, 1, 1, 1) and read(ds)[0, 0] == 7
    thin = pattern((3, 9, 1, 3), np.uint8)
    tifffile.imwrite(tmp_path / "thin.tif", thin, photometric="rgb")
    with open_tiff(tmp_path / "thin.tif") as ds:
        assert ds.info.shape == (3, 3, 1, 9, 1) and ds.info.rgb
        assert np.array_equal(all_planes(ds)[:, :, 0], thin.transpose(0, 3, 1, 2))


def test_fill_order_and_predictor_without_compression(tmp_path: Path):
    # uncompressed but not "final": the bytes read from the file are post-processed
    stored = pattern((6, 10), np.uint16)
    page = gray_page(10, 6, stored.astype(">u2").tobytes(), bits=16)
    references = {
        266: imagecodecs.bitorder_decode(
            stored.astype(">u2").tobytes()
        ),  # FillOrder 2: bits reversed in each byte
        317: np.cumsum(stored, axis=1, dtype=np.uint16).astype(">u2").tobytes(),  # horizontal differencing
    }
    for code, expected in references.items():
        path = tmp_path / f"tag{code}.tif"
        path.write_bytes(build_tiff([RawPage({**page.tags, code: (SHORT, [2])}, page.data)], byteorder=">"))
        with tifffile.TiffFile(path) as tif:
            assert tif.pages.first.is_contiguous and not tif.pages.first.is_final
        with open_tiff(path) as ds:
            assert np.array_equal(read(ds), np.frombuffer(expected, ">u2").reshape(6, 10)), code
    with open_tiff(tmp_path / "tag266.tif") as ds:
        assert np.array_equal(read(ds), tifffile.imread(tmp_path / "tag266.tif"))


def test_multi_sample_pages_are_decoded_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    a = pattern((2, 32, 32, 3), np.uint8)
    write_pages(tmp_path / "rgb.tif", a, photometric="rgb", compression="zlib")
    calls: list[int] = []
    real = tifffile.TiffPage.asarray

    def counting(self: Any, **kwargs: Any) -> Any:
        calls.append(self.index)
        return real(self, **kwargs)

    monkeypatch.setattr(tifffile.TiffPage, "asarray", counting)
    with open_tiff(tmp_path / "rgb.tif") as ds:
        for t, c in ((0, 0), (0, 1), (0, 2), (1, 2), (1, 0), (0, 1)):
            assert np.array_equal(read(ds, t, c), a[t, :, :, c])
        assert calls == [0, 1, 0]
        calls.clear()
        monkeypatch.setattr(tiff_module, "_MEMO_MAX_BYTES", 0)  # pages too large to keep: no memo
        assert all(np.array_equal(read(ds, 1, c), a[1, :, :, c]) for c in range(3))
        assert calls == [1, 1, 1]


def test_palette_images_are_expanded_to_rgb(tmp_path: Path):
    # AUDIT CRYOVIEWER-MISSED-1: the legacy app displayed palette indices as if they were intensities
    rng = np.random.default_rng(11)
    idx = pattern((21, 30), np.uint8)
    colours = rng.integers(0, 256, (3, 256)).astype(np.uint16)
    for name, cmap in (("x257", colours * 257), ("x256", colours * 256)):  # libtiff and ImageJ scalings
        path = tmp_path / f"pal_{name}.tif"
        tifffile.imwrite(path, idx, photometric="palette", colormap=cmap, compression="lzw")
        expected = np.asarray(Image.open(path).convert("RGB"))  # independent decoder
        assert np.array_equal(expected, colours.astype(np.uint8)[:, idx].transpose(1, 2, 0))
        with open_tiff(path) as ds:
            info = ds.info
            assert info.rgb and info.shape[:3] == (1, 3, 1) and info.dtype == np.uint8
            assert info.stack_kind is StackKind.IMAGE and info.metadata["photometric"] == "PALETTE"
            assert info.metadata["palette"] == {"entries": 256, "bits": 8}
            for c in (0, 1, 2, 1):
                assert np.array_equal(read(ds, 0, c), expected[..., c])

    # a genuinely 16-bit palette is kept exact
    idx16 = pattern((9, 14), np.uint16)
    cmap16 = rng.integers(0, 65536, (3, 65536)).astype(np.uint16)
    tifffile.imwrite(
        tmp_path / "pal16.tif", np.stack([idx16, idx16[::-1]]), photometric="palette", colormap=cmap16
    )
    with open_tiff(tmp_path / "pal16.tif") as ds, tifffile.TiffFile(tmp_path / "pal16.tif") as tif:
        assert ds.info.dtype == np.uint16 and ds.info.rgb and ds.info.shape[:3] == (2, 3, 1)
        assert ds.info.metadata["palette"] == {"entries": 65536, "bits": 16}
        for t, c in np.ndindex(2, 3):
            assert np.array_equal(read(ds, t, c), tif.pages[t].colormap[c][tif.pages[t].asarray()])

    # 4-bit indices packed two per byte, written by hand; Pillow is the reference
    packed = bytes(rng.integers(0, 256, 6 * 5, dtype=np.uint8))  # 10 px wide = 5 bytes per row
    page = gray_page(10, 6, packed, bits=4)
    cmap4 = [int(v) * 257 for v in rng.integers(0, 256, 48)]
    page = RawPage({**page.tags, PHOTOMETRIC: (SHORT, [3]), COLORMAP: (SHORT, cmap4)}, packed)
    (tmp_path / "pal4.tif").write_bytes(build_tiff([page]))
    expected = np.asarray(Image.open(tmp_path / "pal4.tif").convert("RGB"))
    with open_tiff(tmp_path / "pal4.tif") as ds:
        assert ds.info.rgb and ds.info.dtype == np.uint8 and ds.info.metadata["palette"]["entries"] == 16
        assert np.array_equal(all_planes(ds)[0, :, 0].transpose(1, 2, 0), expected)


def test_palette_that_cannot_be_applied_is_reported(tmp_path: Path):
    page = gray_page(8, 8, bytes(range(64)))
    short_map = RawPage({**page.tags, PHOTOMETRIC: (SHORT, [3]), COLORMAP: (SHORT, [0] * 30)}, page.data)
    (tmp_path / "bad.tif").write_bytes(build_tiff([short_map]))
    with open_tiff(tmp_path / "bad.tif") as ds:
        assert not ds.info.rgb and ds.info.size_c == 1
        assert any("palette" in w and "indices" in w for w in ds.info.warnings)
        assert np.array_equal(read(ds), np.arange(64, dtype=np.uint8).reshape(8, 8))


# ─── Pixel size and its provenance (AUDIT CV-16) ──────────────────────────────


def unknown(info: DatasetInfo, needle: str) -> bool:
    cal = info.calibration
    return (
        not cal.known
        and cal.x.source is CalibrationSource.DEFAULT
        and cal.x.unit == "px"
        and cal.x.to_angstrom() is None
        and any("pixel size unknown" in w and needle in w for w in info.warnings)
    )


def test_pixel_size_from_imagej_metadata(tmp_path: Path):
    a = coded((2, 3, 9, 11))
    path = tmp_path / "ij.tif"
    tifffile.imwrite(
        path, a, imagej=True, resolution=(1 / 0.25, 1 / 0.5),
        metadata={"axes": "TZYX", "unit": "um", "spacing": 2.5, "finterval": 0.75},
    )  # fmt: skip
    with open_tiff(path) as ds:
        cal = ds.info.calibration
        assert cal.known and not any("pixel size" in w for w in ds.info.warnings)
        assert (cal.x.value, cal.x.unit, cal.x.source) == (0.25, "µm", CalibrationSource.METADATA)
        assert (cal.y.value, cal.y.unit) == (0.5, "µm")
        assert cal.x.to_angstrom() == pytest.approx(2500)  # the legacy reader answered 0.001 Å
        assert cal.z is not None and (cal.z.value, cal.z.unit, cal.z.source) == (
            2.5,
            "µm",
            CalibrationSource.METADATA,
        )
        assert cal.t is not None and (cal.t.value, cal.t.unit) == (0.75, "s")

    # ImageJ spellings of the unit; 0.83 Å/px expressed in each of them
    for unit, per_angstrom in (("\\u00B5m", 1e4), ("micron", 1e4), ("nm", 10.0), ("A", 1.0), ("mm", 1e7)):
        tifffile.imwrite(
            path, a[0, 0], imagej=True, resolution=(per_angstrom / 0.83,) * 2, metadata={"unit": unit}
        )
        with open_tiff(path) as ds:
            cal = ds.info.calibration
            assert cal.x.source is CalibrationSource.METADATA, unit
            assert cal.x.to_angstrom() == pytest.approx(0.83, rel=1e-6), unit
            assert cal.z is None and cal.t is None  # single plane: not applicable

    # an explicit unit makes 1 pixel per unit a real calibration
    tifffile.imwrite(path, a[0, 0], imagej=True, metadata={"unit": "um"})
    with open_tiff(path) as ds:
        assert ds.info.calibration.x.to_angstrom() == pytest.approx(1e4)

    # no unit, or "pixel": unknown
    for meta in ({}, {"unit": "pixel"}):
        tifffile.imwrite(path, a[0, 0], imagej=True, resolution=(4, 4), metadata=meta)
        with open_tiff(path) as ds:
            assert unknown(ds.info, "no unit")


def test_pixel_size_from_ome_metadata(tmp_path: Path):
    a = coded((2, 3, 9, 11))
    path = tmp_path / "img.ome.tif"
    meta = {
        "axes": "TZYX", "PhysicalSizeX": 0.5, "PhysicalSizeXUnit": "nm", "PhysicalSizeY": 0.25,
        "PhysicalSizeYUnit": "nm", "PhysicalSizeZ": 2.0, "PhysicalSizeZUnit": "µm", "TimeIncrement": 3.0,
    }  # fmt: skip
    tifffile.imwrite(path, a, ome=True, metadata=meta)
    with open_tiff(path) as ds:
        cal = ds.info.calibration
        assert (cal.x.value, cal.x.unit, cal.x.source) == (0.5, "nm", CalibrationSource.METADATA)
        assert (cal.y.value, cal.y.unit) == (0.25, "nm") and cal.x.to_angstrom() == pytest.approx(5.0)
        assert cal.z is not None and (cal.z.value, cal.z.unit) == (2.0, "µm")
        assert cal.t is not None and (cal.t.value, cal.t.unit) == (3.0, "s")
        assert ds.info.metadata["ome"]["pixels"]["PhysicalSizeXUnit"] == "nm"

    # OME's default unit is the micrometre; Å is spelled out
    tifffile.imwrite(path, a[0, 0], ome=True, metadata={"PhysicalSizeX": 0.1, "PhysicalSizeY": 0.1})
    with open_tiff(path) as ds:
        assert ds.info.calibration.x.unit == "µm" and ds.info.calibration.x.to_angstrom() == pytest.approx(
            1000
        )
    angstrom = {
        "PhysicalSizeX": 0.83,
        "PhysicalSizeXUnit": "Å",
        "PhysicalSizeY": 0.83,
        "PhysicalSizeYUnit": "Å",
    }
    tifffile.imwrite(path, a[0, 0], ome=True, metadata=angstrom)
    with open_tiff(path) as ds:
        assert ds.info.calibration.x.unit == "Å" and ds.info.calibration.x.to_angstrom() == pytest.approx(
            0.83
        )

    tifffile.imwrite(path, a[0, 0], ome=True)
    with open_tiff(path) as ds:
        assert unknown(ds.info, "OME")


def test_pixel_size_from_resolution_tags(tmp_path: Path):
    a = pattern((9, 11), np.uint16)
    path = tmp_path / "plain.tif"
    # IMOD, MotionCor and SerialEM store the pixel size as pixels per cm or per inch
    for resolution, unit, name, angstrom in (
        (1e8 / 0.83, "CENTIMETER", "cm", 0.83),
        (2.54e8 / 1.1, "INCH", "inch", 1.1),
        (300, "INCH", "inch", 2.54e8 / 300),  # a real 300 dpi scan is a measurement
    ):
        tifffile.imwrite(path, a, resolution=(resolution, resolution), resolutionunit=unit, metadata=None)
        with open_tiff(path) as ds:
            cal = ds.info.calibration
            assert cal.known and cal.x.source is CalibrationSource.METADATA and cal.y == cal.x
            assert cal.x.unit == name and cal.x.value == pytest.approx(1 / resolution, rel=1e-6)
            assert cal.x.to_angstrom() == pytest.approx(angstrom, rel=1e-6)
            assert not any("pixel size" in w for w in ds.info.warnings)
            assert ds.info.metadata["resolution"]["unit"] == unit

    # anisotropic pixels are kept
    tifffile.imwrite(path, a, resolution=(1e4, 2e4), resolutionunit="CENTIMETER", metadata=None)
    with open_tiff(path) as ds:
        cal = ds.info.calibration
        assert cal.x.to_angstrom() == pytest.approx(1e4) and cal.y.to_angstrom() == pytest.approx(5e3)

    # screen/print placeholders are not pixel sizes
    for resolution, unit in (
        (72, "INCH"),
        (96, "INCH"),
        (72 / 2.54, "CENTIMETER"),
        (1, "INCH"),
        (1, "CENTIMETER"),
    ):
        tifffile.imwrite(path, a, resolution=(resolution, resolution), resolutionunit=unit, metadata=None)
        with open_tiff(path) as ds:
            assert unknown(ds.info, "screen/print default"), (resolution, unit)

    tifffile.imwrite(path, a, resolution=(1e8, 1e8), resolutionunit="NONE", metadata=None)
    with open_tiff(path) as ds:
        assert unknown(ds.info, "no unit")
    tifffile.imwrite(path, a, metadata=None)  # tifffile's default: 1/1, no unit
    with open_tiff(path) as ds:
        assert unknown(ds.info, "no unit")
    Image.fromarray(np.zeros((8, 8), np.uint8)).save(path)  # no resolution tags at all
    with open_tiff(path) as ds:
        assert unknown(ds.info, "no resolution")
    Image.fromarray(np.zeros((8, 8), np.uint8)).save(path, dpi=(96, 96))
    with open_tiff(path) as ds:
        assert unknown(ds.info, "96 dpi")


# ─── Channels, series, pyramids, metadata ─────────────────────────────────────


def test_channel_names_and_colours(tmp_path: Path):
    a = coded((3, 9, 11))
    channel = {"Name": ["DAPI", "GFP", "mCherry"], "Color": [0x0000FFFF, 0x00FF00FF, -16776961]}
    tifffile.imwrite(tmp_path / "c.ome.tif", a, ome=True, metadata={"axes": "CYX", "Channel": channel})
    with open_tiff(tmp_path / "c.ome.tif") as ds:
        assert [ch.name for ch in ds.info.channels] == ["DAPI", "GFP", "mCherry"]
        assert [ch.color for ch in ds.info.channels] == [(0, 0, 255), (0, 255, 0), (255, 0, 0)]
    luts = [np.zeros((3, 256), np.uint8) for _ in range(3)]
    for i, lut in enumerate(luts):
        lut[i] = np.arange(256)
    tifffile.imwrite(
        tmp_path / "c.tif", a, imagej=True, metadata={"axes": "CYX", "LUTs": luts, "mode": "composite"}
    )
    with open_tiff(tmp_path / "c.tif") as ds:
        assert [ch.color for ch in ds.info.channels] == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        assert ds.info.metadata["imagej"]["mode"] == "composite" and "LUTs" not in ds.info.metadata["imagej"]
    tifffile.imwrite(tmp_path / "plain.tif", a, photometric="minisblack")
    with open_tiff(tmp_path / "plain.tif") as ds:
        assert ds.info.channels == ()


def test_first_series_is_used_and_the_others_are_listed(tmp_path: Path):
    big, small = pattern((2, 24, 32), np.uint16), pattern((12, 16), np.uint8)
    path = tmp_path / "mixed.tif"
    with tifffile.TiffWriter(path) as tw:
        tw.write(big[0], metadata=None)
        tw.write(small, metadata=None)  # e.g. an embedded thumbnail
        tw.write(big[1], metadata=None)
    with open_tiff(path) as ds:
        info = ds.info
        assert info.shape == (2, 1, 1, 24, 32) and np.array_equal(all_planes(ds)[:, 0, 0], big)
        assert info.metadata["series_count"] == 2 and info.metadata["series_index"] == 0
        assert [s["shape"] for s in info.metadata["series"]] == [[2, 24, 32], [12, 16]]
        assert any("2 image series" in w for w in info.warnings)
    with open_tiff(path, series=1) as ds:
        assert ds.info.shape == (1, 1, 1, 12, 16) and np.array_equal(read(ds), small)
        assert ds.info.metadata["series_index"] == 1
    for bad in (2, -1, "1", True, 1.0):
        with pytest.raises(InvalidRequestError, match="series"):
            open_tiff(path, series=bad)
    with pytest.raises(InvalidRequestError, match="unknown option"):
        open_tiff(path, sereis=0)


def test_pyramid_levels_are_listed_and_the_full_resolution_is_read(tmp_path: Path):
    base = pattern((64, 96), np.uint8)
    path = tmp_path / "pyramid.ome.tif"
    with tifffile.TiffWriter(path) as tw:
        tw.write(base, subifds=2, metadata={"axes": "YX"}, tile=(32, 32), compression="zlib")
        tw.write(base[::2, ::2], subfiletype=1, tile=(16, 16), compression="zlib")
        tw.write(base[::4, ::4], subfiletype=1, tile=(16, 16), compression="zlib")
    with open_tiff(path) as ds:
        assert ds.info.metadata["levels"] == [[64, 96], [32, 48], [16, 24]]
        assert ds.info.metadata["tile"] == [32, 32] and ds.info.metadata["rows_per_strip"] is None
        assert ds.info.stack_kind is StackKind.IMAGE and np.array_equal(read(ds), base)


def test_info_is_json_serialisable_and_descriptive(tmp_path: Path):
    a = coded((2, 3, 9, 11))
    tifffile.imwrite(tmp_path / "a.tif", a, imagej=True, metadata={"axes": "TZYX", "Info": "x" * 5000})
    tifffile.imwrite(
        tmp_path / "b.ome.tif", a, ome=True, metadata={"axes": "TZYX"}, compression="zstd", bigtiff=True
    )
    tifffile.imwrite(tmp_path / "c.tif", pattern((16, 16, 3), np.uint8), photometric="rgb", tile=(16, 16))
    for name in ("a.tif", "b.ome.tif", "c.tif"):
        with open_tiff(tmp_path / name) as ds:
            js = json.loads(json.dumps(ds.info.to_json(), allow_nan=False))
            md = js["metadata"]
            for key in (
                "page_count", "is_bigtiff", "byte_order", "compression", "photometric", "bits_per_sample",
                "samples_per_pixel", "software", "kind", "axes", "shape", "series", "levels", "tile",
                "rows_per_strip", "segments_per_page", "flags",
            ):  # fmt: skip
                assert key in md, (name, key)
            assert len(json.dumps(md)) < 4000  # summaries, not megabytes of embedded metadata
            assert str(tmp_path) not in json.dumps(js)
    with open_tiff(tmp_path / "b.ome.tif") as ds:
        assert ds.info.metadata["compression"] == "ZSTD" and ds.info.metadata["is_bigtiff"] is True
        assert ds.info.metadata["ome"]["pixels"]["SizeZ"] == "3"


# ─── EER must go to the EER reader ────────────────────────────────────────────


@pytest.mark.parametrize("bigtiff", [False, True])
def test_eer_files_are_refused_even_when_named_tiff(tmp_path: Path, bigtiff: bool):
    path = tmp_path / "FoilHole_1_Data_2_EER.tiff"
    write_fake_eer(path, bigtiff=bigtiff)
    with tifffile.TiffFile(path) as tif:
        assert tif.pages.first.compression == 65001  # tifffile would happily try to decode it
    with pytest.raises(CorruptFileError, match=r"^EER file: handled by the eer reader$"):
        open_tiff(path)

    class FakeEer(Reader):
        name = "eer"
        extensions = (".eer",)

        def sniff(self, path: Path, head: bytes) -> bool:
            return head[:2] in (b"II", b"MM")

        def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
            return _Marker(path)

    class _Marker(Dataset):
        def __init__(self, path: Path) -> None:
            super().__init__(
                path, DatasetInfo(format="eer", reader="eer", size_x=1, size_y=1, dtype=np.dtype("u1"))
            )

        def _read_plane(self, t: int, c: int, z: int) -> Plane:
            return np.zeros((1, 1), np.uint8)

    assert Registry([READER, FakeEer()]).open(path).info.reader == "eer"


# ─── Corrupt, truncated and hostile files ─────────────────────────────────────


def expect_corrupt(path: Path, match: str) -> None:
    with pytest.raises(CorruptFileError, match=match) as e, open_tiff(path) as ds:
        all_planes(ds)
    assert str(path.parent) not in str(e.value) and path.name in str(e.value)


def test_missing_and_non_tiff_files(tmp_path: Path):
    with pytest.raises(NotFoundError) as e:
        open_tiff(tmp_path / "nope.tif")
    assert str(tmp_path) not in str(e.value)
    for i, content in enumerate(
        (b"", b"II", b"II*\x00", b"hello world, not a tiff", b"MM\x00+\x00\x08", bytes(4096))
    ):
        path = tmp_path / f"junk{i}.tif"
        path.write_bytes(content)
        expect_corrupt(path, "not a TIFF|truncated|no image|BigTIFF")
    (tmp_path / "dir.tif").mkdir()
    with pytest.raises(ImageBioError):
        open_tiff(tmp_path / "dir.tif")


def test_broken_and_circular_ifd_chains(tmp_path: Path):
    frames = pattern((5, 16, 16), np.uint16)
    path = tmp_path / "movie.tif"
    write_pages(path, frames, compression="lzw")
    data = path.read_bytes()
    ifds = walk_ifds(data)
    assert len(ifds) == 5

    # a writer that died (or has not finished): the chain points past the end of the file
    path.write_bytes(set_next_ifd(data, 4, len(data) + 1000))
    with tifffile.TiffFile(path) as tif:
        assert len(tif.pages) == 5  # tifffile silently stops there: we must not
    expect_corrupt(path, "truncated or still being written")
    path.write_bytes(data[: ifds[4].offset + 6])  # last IFD cut in the middle
    expect_corrupt(path, "truncated or still being written")
    path.write_bytes(data[: ifds[3].offset - 40])  # data of a page cut
    expect_corrupt(path, "truncated or still being written")

    for target in (ifds[0].offset, ifds[4].offset, ifds[2].offset):
        path.write_bytes(set_next_ifd(data, 4, target))
        expect_corrupt(path, "circular")

    path.write_bytes(set_next_ifd(data, 4, 0))  # sanity: the helper itself does not break files
    with open_tiff(path) as ds:
        assert np.array_equal(all_planes(ds)[:, 0, 0], frames)


def test_tag_values_beyond_the_end_of_the_file(tmp_path: Path):
    # tifffile drops such tags and reads the rest as something else (here: palette indices)
    path = tmp_path / "pal.tif"
    tifffile.imwrite(
        path, pattern((8, 8), np.uint8), photometric="palette", colormap=np.zeros((3, 256), np.uint16)
    )
    data = path.read_bytes()
    path.write_bytes(set_tag_offset(data, COLORMAP, len(data) - 100))
    expect_corrupt(
        path, "tag 320 of page 0 lies beyond the end of the file.*truncated or still being written"
    )
    # private tags are another matter: some writers leave junk there, tifffile ignores it
    page = gray_page(8, 8, bytes(range(64)))
    data = build_tiff([RawPage({**page.tags, 65100: (BYTE, bytes(100))}, page.data)])
    path.write_bytes(set_tag_offset(data, 65100, len(data) + 5000))
    with open_tiff(path) as ds:
        assert np.array_equal(read(ds), np.arange(64, dtype=np.uint8).reshape(8, 8))


def test_page_count_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_pages(tmp_path / "x.tif", pattern((5, 8, 8), np.uint8))
    monkeypatch.setattr(tiff_module, "MAX_PAGES", 4)
    expect_corrupt(tmp_path / "x.tif", "more than 4 pages")
    monkeypatch.setattr(tiff_module, "MAX_PAGES", 5)
    with open_tiff(tmp_path / "x.tif") as ds:
        assert ds.info.size_t == 5


def test_sparse_tiles_read_as_zero_and_are_reported(tmp_path: Path):
    a = pattern((64, 64), np.uint16) | 1  # no zero anywhere
    path = tmp_path / "sparse.tif"
    tifffile.imwrite(path, a, tile=(32, 32), compression="zlib", metadata=None)
    with tifffile.TiffFile(path) as tif:
        counts = tif.pages.first.tags[325]  # TileByteCounts
        assert counts.count == 4
        position, fmt = (
            counts.valueoffset + 2 * (counts.valuebytecount // 4),
            tif.byteorder + "IH"[counts.dtype == 3],
        )
    data = bytearray(path.read_bytes())
    data[position : position + (4 if fmt[1] == "I" else 2)] = bytes(
        4 if fmt[1] == "I" else 2
    )  # third tile: empty
    path.write_bytes(bytes(data))
    with open_tiff(path) as ds:
        plane = read(ds)
        assert any("sparse" in w and "zero" in w for w in ds.info.warnings)
        assert not plane[32:, :32].any() and np.array_equal(plane[:32], a[:32])
        assert np.array_equal(plane, tifffile.imread(path))


def test_pages_without_data_and_impossible_compression_ratios(tmp_path: Path):
    # found by fuzzing: a 300-byte file may declare a gigabyte of empty (or absurdly compressed) strips,
    # which tifffile fills with zeros
    path = tmp_path / "x.tif"
    empty = gray_page(30_000, 30_000, b"", compression=8)  # 900 MB declared, nothing stored
    path.write_bytes(build_tiff([empty]))
    tracemalloc.start()
    try:
        expect_corrupt(path, "page 0 has no image data")
        for compression, stored in ((5, 100_000), (8, 500_000), (32773, 10_000_000), (50000, 10_000)):
            page = gray_page(30_000, 30_000, bytes(stored), compression=compression)
            path.write_bytes(build_tiff([page]))
            expect_corrupt(path, f"the {stored} bytes stored for page 0 cannot hold its image")
        assert tracemalloc.get_traced_memory()[1] < 64 * 2**20
    finally:
        tracemalloc.stop()
    # ... while a blank image compresses extremely well and must still open
    blank = np.zeros((4096, 4096), np.uint8)
    for kw in (
        {"compression": "zstd", "rowsperstrip": 4096},
        {"compression": "zlib"},
        {"compression": "lzw"},
    ):
        tifffile.imwrite(path, blank, metadata=None, **kw)
        with open_tiff(path) as ds:
            assert not read(ds).any()


def test_volumetric_pages_are_refused(tmp_path: Path):
    tifffile.imwrite(
        tmp_path / "v.tif", pattern((4, 16, 16), np.uint8), volumetric=True, photometric="minisblack"
    )
    with tifffile.TiffFile(tmp_path / "v.tif") as tif:
        assert tif.pages.first.imagedepth == 4 and len(tif.pages) == 1
    expect_corrupt(tmp_path / "v.tif", "volumetric")


def test_image_data_beyond_the_end_of_the_file(tmp_path: Path):
    a = pattern((3, 64, 64), np.uint16)
    for name, kw in (
        ("raw", {}),
        ("lzw", {"compression": "lzw"}),
        ("ij", {"imagej": True}),
        ("ijt", {"imagej": True, "truncate": True}),
    ):
        path = tmp_path / f"{name}.tif"
        tifffile.imwrite(path, a, photometric="minisblack", **kw)
        data = path.read_bytes()
        path.write_bytes(data[:-64])  # tifffile writes the pixel data last
        expect_corrupt(path, "truncated or still being written")


def test_absurd_dimensions_are_rejected_before_allocating(tmp_path: Path):
    a = pattern((32, 32), np.uint16)
    path = tmp_path / "x.tif"
    cases = {
        "raw": ({}, "truncated or still being written"),  # 32 rows of data cannot be 40000 rows
        "lzw": ({"compression": "lzw"}, "1 strips or tiles, 1250 expected"),  # tifffile would zero-fill
        "tiled": ({"tile": (16, 16)}, "4 strips or tiles, 5000 expected"),
    }
    tracemalloc.start()
    try:
        for kw, match in cases.values():
            tifffile.imwrite(path, a, metadata=None, **kw)
            data = path.read_bytes()
            for code, value in ((IMAGE_LENGTH, 3_000_000_000), (IMAGE_WIDTH, 2_000_000_000)):
                path.write_bytes(patch_tag(data, code, value))
                expect_corrupt(path, "too large")
            # 40000 x 32 px: under the plane limit, but the stored data cannot be that image
            path.write_bytes(patch_tag(data, IMAGE_LENGTH, 40_000))
            expect_corrupt(path, match)
        assert tracemalloc.get_traced_memory()[1] < 32 * 2**20
    finally:
        tracemalloc.stop()

    # the limit itself: a (compressed) page larger than MAX_PLANE_BYTES is refused with a clear message
    assert MAX_PLANE_BYTES == 2 * 2**30
    side = 50_000  # 2.5e9 one-byte pixels
    page = gray_page(side, side, bytes(4096), compression=8)
    path.write_bytes(build_tiff([page]))
    expect_corrupt(path, "too large")


def test_unsupported_variants_are_clean_errors(tmp_path: Path):
    a = pattern((16, 16), np.uint8)
    path = tmp_path / "x.tif"
    tifffile.imwrite(path, a, metadata=None)
    data = path.read_bytes()
    path.write_bytes(patch_tag(data, COMPRESSION, 12345))
    expect_corrupt(path, "compression")
    path.write_bytes(patch_tag(data, BITS_PER_SAMPLE, 16))  # 256 bytes cannot be 16 x 16 uint16
    expect_corrupt(path, "truncated or still being written")
    path.write_bytes(patch_tag(data, BITS_PER_SAMPLE, 9))  # bit-packed: 288 bytes needed, 256 stored
    expect_corrupt(path, "256 bytes stored for page 0 cannot hold its image")
    path.write_bytes(patch_tag(data, BITS_PER_SAMPLE, 7))  # 7-bit samples fit: a valid, if odd, file
    with open_tiff(path) as ds:
        assert np.array_equal(read(ds), tifffile.imread(path)) and ds.info.metadata["bits_per_sample"] == 7
    path.write_bytes(patch_tag(data, IMAGE_WIDTH, 0))
    expect_corrupt(path, "no pixels|no image")
    # garbage inside a compressed strip: the decoder error is contained
    tifffile.imwrite(path, pattern((64, 64), np.uint8), metadata=None, compression="zlib")
    data = bytearray(path.read_bytes())
    with tifffile.TiffFile(path) as tif:
        offset, count = tif.pages.first.dataoffsets[0], tif.pages.first.databytecounts[0]
    data[offset : offset + count] = bytes(count)
    path.write_bytes(bytes(data))
    with open_tiff(path) as ds:
        with pytest.raises(CorruptFileError, match=r"x\.tif: cannot decode page 0") as e:
            ds.read_plane()
        assert str(tmp_path) not in str(e.value)


OME_UUID = "urn:uuid:11111111-1111-1111-1111-111111111111"


def ome_xml(pixels_attributes: str, tiff_data: str) -> str:
    """A hand-written OME-XML header for 16 x 16 uint16 planes."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<OME UUID="{OME_UUID}" xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0" Name="x">'
        '<Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16" SizeX="16" SizeY="16" SizeZ="1" '
        f'SizeC="1" PhysicalSizeX="0.5" PhysicalSizeY="0.5" {pixels_attributes}>'
        f'<Channel ID="Channel:0:0" SamplesPerPixel="1"/>{tiff_data}</Pixels></Image></OME>'
    )


def test_ome_references_to_other_files_are_never_followed(tmp_path: Path):
    # tifffile opens the files named in the OME-XML (also "../x"): a reader must only read the
    # file it was asked to open (no path from file content, cf. the confinement rule)
    here, outside = tmp_path / "root", tmp_path / "outside"
    here.mkdir()
    outside.mkdir()
    mine, secret = pattern((2, 16, 16), np.uint16, seed=1), pattern((2, 16, 16), np.uint16, seed=2)
    other_uuid = OME_UUID.replace("1", "2")
    xml = ome_xml(
        'SizeT="4"',
        f'<TiffData IFD="0" FirstT="0" PlaneCount="2"><UUID FileName="a.ome.tif">{OME_UUID}</UUID></TiffData>'
        '<TiffData IFD="0" FirstT="2" PlaneCount="2">'
        f'<UUID FileName="../outside/b.ome.tif">{other_uuid}</UUID></TiffData>',
    )
    tifffile.imwrite(here / "a.ome.tif", mine, description=xml, metadata=None, photometric="minisblack")
    xml_b = xml.replace(f'UUID="{OME_UUID}" xmlns', f'UUID="{other_uuid}" xmlns')
    tifffile.imwrite(
        outside / "b.ome.tif", secret, description=xml_b, metadata=None, photometric="minisblack"
    )
    assert tifffile.imread(here / "a.ome.tif").shape == (4, 16, 16)  # tifffile follows the reference
    with open_tiff(here / "a.ome.tif") as ds:
        assert ds.info.shape == (2, 1, 1, 16, 16)
        assert np.array_equal(all_planes(ds)[:, 0, 0], mine)
        assert any("OME" in w and "this file" in w for w in ds.info.warnings)
        opened = {Path(f.path).name for f in psutil.Process().open_files()}
        assert "b.ome.tif" not in opened


def test_hostile_ome_sizes_do_not_allocate(tmp_path: Path):
    a = pattern((2, 16, 16), np.uint16)
    for i, (attrs, data) in enumerate(
        (
            ('SizeT="1000000000000"', '<TiffData IFD="0" PlaneCount="2"/>'),
            ('SizeT="2"', '<TiffData IFD="0" PlaneCount="1000000000000"/>'),
            ('SizeT="2"', '<TiffData IFD="0" NumPlanes="99999999999"/>'),
            ('SizeT="3000000000"', "<TiffData/>"),
        )
    ):
        path = tmp_path / f"h{i}.ome.tif"
        tifffile.imwrite(path, a, description=ome_xml(attrs, data), metadata=None, photometric="minisblack")
        tracemalloc.start()
        try:
            with open_tiff(path) as ds:
                assert ds.info.shape == (2, 1, 1, 16, 16) and np.array_equal(all_planes(ds)[:, 0, 0], a)
                assert any("OME" in w for w in ds.info.warnings)
                assert not ds.info.calibration.known  # nothing is taken from metadata we do not trust
            assert tracemalloc.get_traced_memory()[1] < 32 * 2**20
        finally:
            tracemalloc.stop()


def test_unusable_ome_xml_falls_back_to_the_plain_pages(tmp_path: Path):
    a = pattern((2, 16, 16), np.uint16)
    good = ome_xml('SizeT="2"', '<TiffData IFD="0" PlaneCount="2"/>')
    modulo = (
        "<StructuredAnnotations>"
        '<XMLAnnotation ID="Annotation:0" Namespace="openmicroscopy.org/omero/dimension/modulo">'
        '<Value><Modulo><ModuloAlongT Type="other" Start="0" End="1e13" Step="1"/></Modulo></Value>'
        "</XMLAnnotation></StructuredAnnotations></OME>"
    )
    variants = {
        "no-order": good.replace('DimensionOrder="XYZCT" ', ""),  # tifffile raises KeyError
        "not-xml": good.replace("</Pixels>", "</Pixel>"),
        "modulo": good.replace("</OME>", modulo),  # tifffile would build a 1e13-long range
        "entities": good.replace("<OME ", '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><OME ', 1),
    }
    # XML attacks: an external entity is never resolved, an entity bomb is stopped by the parser
    variants["external-entity"] = variants["entities"].replace('Name="x"', 'Name="&e;"')
    bomb = '<!ENTITY a0 "ha">' + "".join(f'<!ENTITY a{i} "{f"&a{i - 1};" * 10}">' for i in range(1, 10))
    variants["entity-bomb"] = good.replace("<OME ", f"<!DOCTYPE x [{bomb}]><OME ", 1).replace(
        'Name="x"', 'Name="&a9;"'
    )
    for name, xml in variants.items():
        path = tmp_path / f"{name}.ome.tif"
        tifffile.imwrite(path, a, description=xml, metadata=None, photometric="minisblack")
        with open_tiff(path) as ds:
            assert ds.info.shape == (2, 1, 1, 16, 16), name
            assert np.array_equal(all_planes(ds)[:, 0, 0], a)
            if name != "entities":  # a DTD that is declared but not used is harmless: still OME
                assert ds.info.metadata["kind"] != "ome" and any("OME" in w for w in ds.info.warnings), name
            assert "root:" not in json.dumps(ds.info.to_json())
    path = tmp_path / "good.ome.tif"
    tifffile.imwrite(path, a, description=good, metadata=None, photometric="minisblack")
    with open_tiff(path) as ds:
        assert ds.info.metadata["kind"] == "ome" and ds.info.calibration.x.to_angstrom() == pytest.approx(
            5000
        )


def test_ome_planes_missing_from_the_file_are_errors_not_zeros(tmp_path: Path):
    a = pattern((3, 16, 16), np.uint16)
    path = tmp_path / "short.ome.tif"
    xml = ome_xml('SizeT="4"', '<TiffData IFD="0" PlaneCount="3"/>')  # declares 4 planes, 3 pages exist
    tifffile.imwrite(path, a, description=xml, metadata=None, photometric="minisblack")
    assert not tifffile.imread(path)[3].any()  # tifffile fills the missing plane with zeros
    with open_tiff(path) as ds:
        assert ds.info.size_t == 4 and any("missing" in w for w in ds.info.warnings)
        assert np.array_equal(read(ds, 2), a[2])
        with pytest.raises(CorruptFileError, match="missing"):
            ds.read_plane(3)


# property-based robustness ----------------------------------------------------


@pytest.fixture(scope="module")
def victims(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[bytes, np.ndarray]]:
    """Small valid files of each main layout, with their planes as (T, C, Z, Y, X)."""
    d = tmp_path_factory.mktemp("victims")
    a = coded((2, 3, 12, 10))
    rgb = a[0].astype(np.uint8)  # (3, 12, 10)
    ome = {"axes": "TCYX"}
    writers: list[Any] = [
        lambda p: write_pages(p, a[0], compression="lzw"),
        lambda p: write_pages(p, a[0]),
        lambda p: tifffile.imwrite(p, a, imagej=True, metadata={"axes": "TZYX"}),
        lambda p: tifffile.imwrite(p, a, imagej=True, metadata={"axes": "TZYX"}, truncate=True),
        lambda p: tifffile.imwrite(p, a, ome=True, metadata=ome, compression="zlib", bigtiff=True),
        lambda p: tifffile.imwrite(p, rgb.transpose(1, 2, 0).copy(), photometric="rgb", tile=(16, 16)),
        lambda p: tifffile.imwrite(p, rgb, photometric="rgb", planarconfig="separate", compression="lzw"),
        lambda p: tifffile.imwrite(p, rgb[0], photometric="palette", colormap=np.zeros((3, 256), np.uint16)),
        lambda p: tifffile.imwrite(p, a, photometric="minisblack", truncate=True),
    ]
    out = []
    for i, write in enumerate(writers):
        path = d / f"v{i}.tif"
        write(path)
        with open_tiff(path) as ds:
            out.append((path.read_bytes(), all_planes(ds)))
    return out


def open_and_read_everything(path: Path) -> np.ndarray | None:
    """All planes, or None when the reader refuses the file with a clean error. The plane limit
    is lowered so that whatever a mutated header may legitimately declare stays unit-test sized."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tiff_module, "MAX_PLANE_BYTES", 8 * 2**20)
        try:
            with open_tiff(path) as ds:
                assert ds.info.plane_nbytes <= 8 * 2**20
                return all_planes(ds)
        except CorruptFileError as e:
            assert str(path.parent) not in str(e)
            return None


@settings(max_examples=400, deadline=None, derandomize=True, database=None)
@given(data=st.data())
def test_truncated_files_never_escape_or_return_wrong_pixels(
    victims: list[tuple[bytes, np.ndarray]], tmp_path_factory: pytest.TempPathFactory, data: st.DataObject
):
    content, planes = data.draw(st.sampled_from(victims))
    cut = data.draw(st.integers(0, len(content) - 1))
    path = tmp_path_factory.getbasetemp() / "truncated.tif"
    path.write_bytes(content[:cut])
    got = open_and_read_everything(path)
    # a shorter file may still be complete (trailing padding), but then it must read the same
    assert got is None or np.array_equal(got, planes)


@settings(max_examples=400, deadline=None, derandomize=True, database=None)
@given(data=st.data())
def test_mutated_files_never_escape(
    victims: list[tuple[bytes, np.ndarray]],
    tmp_path_factory: pytest.TempPathFactory,
    data: st.DataObject,
):
    content, _ = data.draw(st.sampled_from(victims))
    buf = bytearray(content)
    for _ in range(data.draw(st.integers(1, 4))):
        buf[data.draw(st.integers(0, len(buf) - 1))] = data.draw(st.integers(0, 255))
    path = tmp_path_factory.getbasetemp() / "mutated.tif"
    path.write_bytes(bytes(buf))
    got = open_and_read_everything(path)  # only CorruptFileError may come out
    assert got is None or got.ndim == 5


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(
    magic=st.sampled_from([b"II*\x00", b"MM\x00*", b"II+\x00\x08\x00\x00\x00", b"MM\x00+\x00\x08\x00\x00"]),
    body=st.binary(max_size=600),
)
def test_garbage_after_a_tiff_magic_never_escapes(
    tmp_path_factory: pytest.TempPathFactory,
    magic: bytes,
    body: bytes,
):
    path = tmp_path_factory.getbasetemp() / "garbage.tif"
    path.write_bytes(magic + body)
    open_and_read_everything(path)


# ─── Laziness, concurrency, lifetime ──────────────────────────────────────────


def test_only_the_requested_plane_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the whole file or series must never be loaded")

    monkeypatch.setattr(tifffile.TiffFile, "asarray", forbidden)
    monkeypatch.setattr(tifffile.TiffPageSeries, "asarray", forbidden)
    monkeypatch.setattr(tifffile, "imread", forbidden)
    monkeypatch.setattr(tifffile, "memmap", forbidden)
    requested: list[int] = []
    real_pread = os.pread

    def counting_pread(fd: int, n: int, offset: int) -> bytes:
        requested.append(n)
        return real_pread(fd, n, offset)

    a = coded((40, 64, 64))
    rgb = pattern((6, 3, 64, 64), np.uint16)
    tifffile.imwrite(tmp_path / "block.tif", a, imagej=True, metadata={"axes": "ZYX"})  # contiguous series
    write_pages(tmp_path / "pages.tif", a)  # one raw strip per page
    tifffile.imwrite(tmp_path / "planar.tif", rgb, photometric="rgb", planarconfig="separate", metadata=None)
    write_pages(tmp_path / "lzw.tif", a, compression="lzw")
    for name, plane_bytes in (("block.tif", 8192), ("pages.tif", 8192), ("planar.tif", 8192), ("lzw.tif", 0)):
        with open_tiff(tmp_path / name) as ds:
            monkeypatch.setattr(os, "pread", counting_pread)
            requested.clear()
            index = (3, 1, 0) if name == "planar.tif" else (0, 0, 17) if name == "block.tif" else (17, 0, 0)
            plane = read(ds, *index)
            monkeypatch.setattr(os, "pread", real_pread)
            assert np.array_equal(plane, rgb[3, 1] if name == "planar.tif" else a[17])
            assert sum(requested) == plane_bytes, name  # raw planes: exactly their own bytes
    # compressed pages go through tifffile, one page at a time: the file is 40 pages long
    with open_tiff(tmp_path / "lzw.tif") as ds:
        tracemalloc.start()
        read(ds, 5)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak < 8 * a[0].nbytes


@pytest.mark.parametrize(
    "layout", ["lzw-pages", "raw-pages", "imagej-block", "tiled-zstd", "rgb-jpeg", "rgb-planar-lzw"]
)
def test_concurrent_reads_are_consistent(tmp_path: Path, layout: str):
    path = tmp_path / "c.tif"
    if layout.startswith("rgb"):
        a = pattern((6, 64, 64, 3), np.uint8, seed=9)
        if layout == "rgb-jpeg":
            write_pages(path, a, photometric="rgb", compression="jpeg")
        else:
            write_pages(
                path, a.transpose(0, 3, 1, 2), photometric="rgb", planarconfig="separate", compression="lzw"
            )
    else:
        a = pattern((24, 96, 80), np.uint16, seed=9)
        if layout == "imagej-block":
            tifffile.imwrite(path, a, imagej=True, metadata={"axes": "ZYX"})
        else:
            kw = {
                "lzw-pages": {"compression": "lzw"},
                "raw-pages": {},
                "tiled-zstd": {"tile": (32, 32), "compression": "zstd"},
            }
            write_pages(path, a, **kw[layout])
    with open_tiff(path) as ds:
        info = ds.info
        expected = all_planes(ds)  # sequential reference (itself checked against tifffile elsewhere)
        if not layout.startswith("rgb"):
            assert np.array_equal(expected.reshape(a.shape), a)
        indices = list(np.ndindex(info.size_t, info.size_c, info.size_z))
        rng = np.random.default_rng(1)
        jobs = [indices[i] for i in rng.integers(0, len(indices), 400)]

        def work(index: tuple[int, int, int]) -> bool:
            return bool(np.array_equal(ds.read_plane(*index), expected[index]))

        with ThreadPoolExecutor(8) as pool:
            assert all(pool.map(work, jobs))


def test_close_releases_the_file_and_is_idempotent(tmp_path: Path):
    path = tmp_path / "x.tif"
    write_pages(path, pattern((3, 16, 16), np.uint16), compression="lzw")

    def is_open() -> bool:
        return any(Path(f.path) == path for f in psutil.Process().open_files())

    ds = open_tiff(path)
    assert is_open() and ds.path == path
    read(ds, 1)
    ds.close()
    ds.close()
    assert not is_open()
    with pytest.raises(RuntimeError, match="closed"):
        ds.read_plane(0)
    with pytest.raises(InvalidRequestError):
        ds.read_plane(3)  # index errors still come first
    # a failed open does not leak the handle either
    path.write_bytes(path.read_bytes()[:200])
    with pytest.raises(CorruptFileError):
        open_tiff(path)
    assert not is_open()


def test_close_waits_for_reads_in_flight(tmp_path: Path):
    # closing while another thread decodes must not pull the file from under it (or let the
    # descriptor number be reused by another file)
    path = tmp_path / "x.tif"
    a = pattern((16, 128, 128), np.uint16)
    write_pages(path, a, compression="zlib")
    for _ in range(20):
        ds = open_tiff(path)

        def work(t: int, ds: Dataset = ds) -> bool:
            try:
                return bool(np.array_equal(ds.read_plane(t), a[t]))
            except RuntimeError:  # started after close(): refused, never garbage
                return True

        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(work, t) for t in range(16)]
            ds.close()
            assert all(f.result() for f in futures)


def test_unknown_options_are_rejected(tmp_path: Path):
    path = tmp_path / "x.tif"
    tifffile.imwrite(path, pattern((4, 4), np.uint8))
    for key, value in itertools.product(("level", "page", "superres"), (0, None)):
        with pytest.raises(InvalidRequestError, match="unknown option"):
            READER.open(path, {key: value})
    with READER.open(path, {}) as ds:
        assert ds.info.size_x == 4
