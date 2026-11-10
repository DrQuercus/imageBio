"""MRC reader: bit-exact against mrcfile, hand-built variants, robustness (AUDIT CV-11..15, CV-28, LIVE-1)."""

from __future__ import annotations

import json
import os
import struct
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mrcfile
import numpy as np
import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from fixtures.mrc import (
    HEADER_SIZE,
    IMOD_4BIT_BYTES,
    IMOD_BYTES_SIGNED,
    IMOD_STAMP,
    fei_records,
    pack_4bit,
    raw_header,
    sample,
    write_raw,
    write_with_mrcfile,
)
from imagebio.core.errors import (
    CorruptFileError,
    ImageBioError,
    InvalidRequestError,
    NotFoundError,
    UnsupportedFormatError,
)
from imagebio.core.model import PLANE_DTYPES, CalibrationSource, Dataset, StackKind
from imagebio.io.readers import mrc as mrc_module
from imagebio.io.readers.mrc import READER, MrcDataset, MrcHeader, parse_header, read_header
from imagebio.io.registry import SNIFF_BYTES, Reader, Registry

# mrcfile (the reference) still assigns to ndarray.dtype, which NumPy 2.5 deprecates
pytestmark = pytest.mark.filterwarnings("ignore:Setting the dtype on a NumPy array:DeprecationWarning")

HEADER = CalibrationSource.HEADER
EXTENDED = CalibrationSource.EXTENDED_HEADER
DEFAULT = CalibrationSource.DEFAULT


def reference_sections(path: Path) -> np.ndarray:
    """All sections as mrcfile reads them: ``(n, ny, nx)``, native byte order."""
    with mrcfile.open(str(path)) as mrc:
        data = np.array(mrc.data)
    data = data.reshape(-1, *data.shape[-2:])
    return data.astype(data.dtype.newbyteorder("="))


def all_sections(ds: Dataset, c: int = 0) -> np.ndarray:
    i = ds.info
    return np.stack([ds.read_plane(t, c, z) for t in range(i.size_t) for z in range(i.size_z)])


def assert_bit_exact(got: np.ndarray, expected: np.ndarray) -> None:
    assert got.dtype == expected.dtype and got.shape == expected.shape
    assert got.tobytes() == expected.tobytes()


def open_error(path: Path) -> CorruptFileError:
    with pytest.raises(CorruptFileError) as e:
        READER.open(path)
    return e.value


# ─── Reader identity ──────────────────────────────────────────────────────────


def test_reader_contract(tmp_path: Path):
    assert isinstance(READER, Reader) and READER.name == "mrc"
    assert set(READER.extensions) == {".mrc", ".mrcs", ".map", ".rec", ".st", ".ali"}
    path = write_with_mrcfile(tmp_path / "x.MRC", sample("int16", (6, 8)))
    with Registry([READER]).open(path) as ds:
        assert isinstance(ds, MrcDataset)
        assert (ds.info.format, ds.info.reader) == ("mrc", "mrc")
        assert ds.info.shape == (1, 1, 1, 6, 8) and ds.info.stack_kind is StackKind.IMAGE


def test_hand_built_headers_are_read_back_by_mrcfile(tmp_path: Path):
    """Anchors the hand-built fixtures (and so the tests using them) to the reference parser."""
    for bo in "<>":
        head = raw_header(
            5, 4, 3, 1, byte_order=bo, cell=(7.5, 6.0, 4.5), ispg=1, origin=(1, 2, 3), labels=["hand built"]
        )
        path = write_raw(tmp_path / f"h{ord(bo)}.mrc", head, bytes(5 * 4 * 3 * 2))
        with mrcfile.open(str(path)) as mrc:
            h = mrc.header
            assert (int(h.nx), int(h.ny), int(h.nz), int(h.mode), int(h.ispg)) == (5, 4, 3, 1, 1)
            assert (int(h.mx), int(h.my), int(h.mz)) == (5, 4, 3)
            assert h.cella.tolist() == (7.5, 6.0, 4.5) and h.origin.tolist() == (1.0, 2.0, 3.0)
            assert (int(h.mapc), int(h.mapr), int(h.maps)) == (1, 2, 3) and int(h.nversion) == 20140
            assert [label.strip() for label in mrc.get_labels()] == ["hand built"]
            assert mrc.data.shape == (3, 4, 5)


# ─── Standard modes, bit-exact against mrcfile ────────────────────────────────


@pytest.mark.parametrize("byte_order", ["<", ">"])
@pytest.mark.parametrize(
    ("dtype", "mode", "plane_dtype"),
    [
        ("int8", 0, "int8"),
        ("int16", 1, "int16"),
        ("float32", 2, "float32"),
        ("uint16", 6, "uint16"),
        ("float16", 12, "float32"),
    ],
)
@pytest.mark.parametrize("shape", [(7, 9), (4, 7, 9)])
def test_standard_modes_bit_exact(tmp_path: Path, dtype, mode, plane_dtype, byte_order, shape):
    path = write_with_mrcfile(tmp_path / "a.mrc", sample(dtype, shape, seed=mode), byte_order=byte_order)
    reference = reference_sections(path)
    with READER.open(path) as ds:
        info = ds.info
        assert info.metadata["mode"] == mode
        # single bytes have no byte order: mrcfile writes a little-endian header for int8 data
        written_order = "big" if byte_order == ">" and dtype != "int8" else "little"
        assert path.read_bytes()[212] == (0x11 if written_order == "big" else 0x44)
        assert info.metadata["byte_order"] == written_order
        assert info.metadata["byte_order_source"] == "machine_stamp"
        assert info.dtype == np.dtype(plane_dtype) and info.dtype in PLANE_DTYPES
        assert (info.size_y, info.size_x) == shape[-2:] and info.plane_count == len(reference)
        got = all_sections(ds)
        # float16 is widened to float32: exact, every float16 value (NaN and Inf included) fits
        assert_bit_exact(got, reference.astype(plane_dtype))
        plane = ds.read_plane(0, 0, 0)
        assert plane.flags.c_contiguous and plane.dtype.isnative
        base = plane  # a real in-memory array, not a view of a memory-mapped file
        while base is not None:
            assert isinstance(base, np.ndarray) and not isinstance(base, np.memmap)
            base = base.base


def test_nan_inf_survive(tmp_path: Path):
    data = sample("float32", (5, 6))
    assert np.isnan(data).any() and np.isinf(data).any()
    with READER.open(write_with_mrcfile(tmp_path / "nan.mrc", data)) as ds:
        assert_bit_exact(ds.read_plane(), data)


def test_extended_header_of_unknown_type_is_skipped(tmp_path: Path):
    """NSYMBT bytes are skipped whatever EXTTYP says (legacy bug: offset ignored by the JS parser)."""
    data = sample("int16", (3, 5, 6))
    ext = np.frombuffer(bytes(range(256)) * 3, dtype="V1")
    path = write_with_mrcfile(tmp_path / "ext.mrc", data, extended_header=ext, exttyp=b"SERI")
    with READER.open(path) as ds:
        assert ds.info.metadata["nsymbt"] == 768 and ds.info.metadata["exttyp"] == "SERI"
        assert ds.info.metadata["extended_header"] == {"type": "SERI", "nbytes": 768}
        assert_bit_exact(all_sections(ds), data)


# ─── Modes mrcfile cannot read: 101, 16, IMOD bytes ───────────────────────────


@pytest.mark.parametrize("nx", [33, 32, 1, 2])
def test_mode_101_rows_are_padded_to_whole_bytes(tmp_path: Path, nx):
    """CV-11: with an odd nx every row ends with a padding nibble."""
    ny, nz = 10, 3
    values = np.random.default_rng(nx).integers(0, 16, size=(nz, ny, nx), dtype=np.uint8)
    packed = pack_4bit(values)
    assert len(packed) == nz * ny * ((nx + 1) // 2)
    path = write_raw(tmp_path / "packed.mrc", raw_header(nx, ny, nz, 101), packed)
    with READER.open(path) as ds:
        assert ds.info.dtype == np.uint8 and ds.info.shape == (nz, 1, 1, ny, nx)
        assert_bit_exact(all_sections(ds), values)


@pytest.mark.parametrize("byte_order", ["<", ">"])
def test_mode_16_is_rgb(tmp_path: Path, byte_order):
    """CV-12: 3 bytes per pixel, exposed as C = 3."""
    ny, nx, nz = 30, 40, 2
    rgb = sample("uint8", (nz, ny, nx, 3), seed=16)
    path = write_raw(tmp_path / "rgb.mrc", raw_header(nx, ny, nz, 16, byte_order=byte_order), rgb.tobytes())
    with READER.open(path) as ds:
        info = ds.info
        assert info.rgb and info.size_c == 3 and info.dtype == np.uint8
        assert info.shape == (nz, 3, 1, ny, nx) and info.stack_kind is StackKind.HYPERSTACK
        assert [c.name for c in info.channels] == ["R", "G", "B"]
        for c in range(3):
            got = all_sections(ds, c)
            assert_bit_exact(got, np.ascontiguousarray(rgb[..., c]))
            assert got[0].flags.c_contiguous
    single = write_raw(tmp_path / "rgb1.mrc", raw_header(nx, ny, 1, 16), rgb[0].tobytes())
    with READER.open(single) as ds:
        assert ds.info.stack_kind is StackKind.IMAGE and ds.info.shape == (1, 3, 1, ny, nx)


def test_mode_0_signedness_follows_mrc2014_and_imod(tmp_path: Path):
    """CV-13. MRC2014: signed. IMOD stamp: flag bit 0 says signed, clear means unsigned."""
    raw = sample("uint8", (2, 6, 7), seed=13)
    raw[0, 0, :3] = [100, 200, 255]
    body = raw.tobytes()

    def opened(name: str, **header):
        head = raw_header(7, 6, 2, 0, cell=(7.0, 6.0, 2.0), **header)
        return READER.open(write_raw(tmp_path / name, head, body))

    with opened("plain.mrc") as ds:  # no stamp: the standard, exactly what mrcfile reads
        assert ds.info.dtype == np.int8 and not ds.info.warnings
        assert_bit_exact(all_sections(ds), reference_sections(ds.path))
        assert ds.info.metadata["bytes_signed"] is True
    with opened("imod_unsigned.rec", imod_stamp=IMOD_STAMP, imod_flags=0) as ds:
        assert ds.info.dtype == np.uint8
        assert_bit_exact(all_sections(ds), raw)
        assert ds.read_plane(0, 0, 0)[0, :3].tolist() == [100, 200, 255]  # not wrapped to negatives
        assert ds.info.metadata["bytes_signed"] is False and ds.info.metadata["imod_flags"] == 0
    with opened("imod_signed.rec", imod_stamp=IMOD_STAMP, imod_flags=IMOD_BYTES_SIGNED) as ds:
        assert ds.info.dtype == np.int8
        assert_bit_exact(all_sections(ds), raw.view(np.int8))
    with opened("imod_other_flags.rec", imod_stamp=IMOD_STAMP, imod_flags=2 | 4 | 8) as ds:
        assert ds.info.dtype == np.uint8 and not ds.info.warnings  # only bit 0 matters
    with opened("be.rec", byte_order=">", imod_stamp=IMOD_STAMP, imod_flags=0) as ds:
        assert ds.info.dtype == np.uint8
    with opened("flags_without_stamp.mrc", imod_stamp=12345, imod_flags=0) as ds:
        assert ds.info.dtype == np.int8 and "imod_flags" not in ds.info.metadata


def test_mode_0_without_stamp_uses_header_statistics_like_imod(tmp_path: Path):
    """A header maximum above 127 cannot describe signed bytes: IMOD reads such files unsigned."""
    raw = sample("uint8", (5, 4), seed=3)
    head = raw_header(4, 5, 1, 0, stats=(0.0, 255.0, 127.3))
    with READER.open(write_raw(tmp_path / "old.mrc", head, raw.tobytes())) as ds:
        assert ds.info.dtype == np.uint8
        assert_bit_exact(ds.read_plane(), raw)
        assert any("unsigned" in w for w in ds.info.warnings)
    for name, stats in {
        "neg": (-128.0, 127.0, 0.0),
        "low": (0.0, 127.0, 5.0),
        "nan": (0.0, float("nan"), 0.0),
    }.items():
        head = raw_header(4, 5, 1, 0, stats=stats)
        with READER.open(write_raw(tmp_path / f"{name}.mrc", head, raw.tobytes())) as ds:
            assert ds.info.dtype == np.int8 and not any("unsigned" in w for w in ds.info.warnings)


def test_imod_legacy_4bit_flag_in_mode_0(tmp_path: Path):
    """IMOD flag 16: each mode-0 byte holds two 4-bit pixels (low nibble first)."""
    values = np.random.default_rng(4).integers(0, 16, size=(2, 5, 8), dtype=np.uint8)
    head = raw_header(4, 5, 2, 0, imod_stamp=IMOD_STAMP, imod_flags=IMOD_4BIT_BYTES)
    with READER.open(write_raw(tmp_path / "k2.mrc", head, pack_4bit(values))) as ds:
        assert ds.info.shape == (2, 1, 1, 5, 8) and ds.info.dtype == np.uint8
        assert_bit_exact(all_sections(ds), values)
        assert any("4-bit" in w for w in ds.info.warnings)


@pytest.mark.parametrize("mode", [3, 4])
def test_complex_modes_are_rejected(tmp_path: Path, mode):
    """CV-14: never decoded as float32."""
    path = write_raw(tmp_path / "fft.mrc", raw_header(4, 4, 1, mode), bytes(4 * 4 * 8))
    message = str(open_error(path))
    assert f"mode {mode}" in message and "complex" in message and "fft.mrc" in message
    assert not READER.sniff(path, path.read_bytes()[:SNIFF_BYTES])


def test_complex_file_written_by_mrcfile_is_rejected(tmp_path: Path):
    path = write_with_mrcfile(tmp_path / "c.mrc", sample("complex64", (4, 4)))
    assert "mode 4" in str(open_error(path))


@pytest.mark.parametrize("mode", [5, 7, 13, 100, 102, -1, 2**24])
def test_unknown_modes_are_rejected(tmp_path: Path, mode):
    path = write_raw(tmp_path / "odd.mrc", raw_header(4, 4, 1, mode), bytes(4 * 4 * 16))
    assert f"mode {mode}" in str(open_error(path))


# ─── Byte order ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("byte_order", ["<", ">"])
@pytest.mark.parametrize(
    ("stamp", "announced"),
    [
        (b"\x44\x44\0\0", "<"),
        (b"\x44\x41\0\0", "<"),
        (b"\x11\x11\0\0", ">"),
        (b"\0\0\0\0", None),
        (b"\xff\x00\x12\x34", None),
        (b"\x11\x44\0\0", None),
    ],
)
def test_byte_order_falls_back_to_header_sanity(tmp_path: Path, byte_order, stamp, announced):
    """A missing, garbage or wrong machine stamp does not prevent reading a sane header."""
    data = sample("int16", (2, 5, 6), seed=7)
    head = raw_header(6, 5, 2, 1, byte_order=byte_order, machine_stamp=stamp, cell=(6.0, 5.0, 2.0))
    body = data.astype(data.dtype.newbyteorder(byte_order)).tobytes()
    with READER.open(write_raw(tmp_path / "s.mrc", head, body)) as ds:
        assert_bit_exact(all_sections(ds), data)
        meta, warnings = ds.info.metadata, ds.info.warnings
        assert meta["byte_order"] == {"<": "little", ">": "big"}[byte_order]
        assert ds.info.calibration.x.value == pytest.approx(1.0)  # floats decoded in the right order too
        if announced == byte_order:
            assert meta["byte_order_source"] == "machine_stamp" and not warnings
        elif announced is None:  # nothing to contradict: not worth a warning
            assert meta["byte_order_source"] == "header" and not warnings
        else:
            assert meta["byte_order_source"] == "header"
            assert len(warnings) == 1 and "machine stamp" in warnings[0]


def test_file_without_map_signature(tmp_path: Path):
    """Pre-2000 files have no 'MAP ': accepted only when the header is sane."""
    data = sample("uint16", (4, 5), seed=2)
    old = raw_header(5, 4, 1, 6, byte_order=">", map_id=b"\0\0\0\0", machine_stamp=b"\0\0\0\0")
    path = write_raw(tmp_path / "old.mrc", old, data.astype(">u2").tobytes())
    assert not READER.sniff(path, path.read_bytes()[:SNIFF_BYTES])
    with READER.open(path) as ds:
        assert_bit_exact(ds.read_plane(), data)
        assert any("MAP" in w for w in ds.info.warnings)
        assert ds.info.metadata["origin"] is None  # those bytes mean something else in old files
    junk = write_raw(tmp_path / "junk.mrc", np.random.default_rng(0).bytes(5000))
    assert "not an MRC file" in str(open_error(junk))
    text = write_raw(tmp_path / "text.mrc", b"hello world\n" * 200)
    assert "not an MRC file" in str(open_error(text))


# ─── Pixel size and its provenance ────────────────────────────────────────────


def test_pixel_size_from_header(tmp_path: Path):
    path = write_with_mrcfile(tmp_path / "v.mrc", sample("int8", (3, 4, 5)), voxel_size=(1.5, 2.5, 3.5))
    with READER.open(path) as ds, mrcfile.open(str(path)) as ref:
        cal = ds.info.calibration
        assert cal.known and not ds.info.warnings
        assert (cal.x.unit, cal.x.source, cal.y.source) == ("Å", HEADER, HEADER)
        assert cal.z is not None and cal.z.source is HEADER and cal.t is None
        voxel = (cal.x.value, cal.y.value, cal.z.value)
        assert voxel == pytest.approx((1.5, 2.5, 3.5))
        assert voxel == pytest.approx([float(ref.voxel_size[axis]) for axis in "xyz"])


def test_pixel_size_uses_sampling_not_dimensions(tmp_path: Path):
    """Å/px = CELLA / MX (MX may differ from NX for a sub-volume)."""
    head = raw_header(8, 8, 1, 0, sampling=(16, 16, 1), cell=(32.0, 48.0, 5.0))
    with READER.open(write_raw(tmp_path / "sub.mrc", head, bytes(64))) as ds:
        cal = ds.info.calibration
        assert (cal.x.value, cal.y.value, cal.z) == (pytest.approx(2.0), pytest.approx(3.0), None)


def test_unknown_pixel_size_is_flagged_not_invented(tmp_path: Path):
    """CV-15: no silent 1.0 Å/px."""
    cases = [
        ((0.0, 0.0, 0.0), (8, 8, 1)),
        ((8.0, 8.0, 1.0), (0, 0, 0)),
        ((float("nan"), -3.0, 1.0), (8, 8, 1)),
        ((float("inf"), 1e-30, 1.0), (8, 8, 1)),
    ]
    for i, (cell, sampling) in enumerate(cases):
        head = raw_header(8, 8, 1, 0, sampling=sampling, cell=cell)
        with READER.open(write_raw(tmp_path / f"u{i}.mrc", head, bytes(64))) as ds:
            cal = ds.info.calibration
            assert not cal.known and cal.x.source is DEFAULT and cal.y.source is DEFAULT
            assert cal.x.to_angstrom() is None and cal.x.unit == "px"
            assert sum("pixel size unknown" in w for w in ds.info.warnings) == 1
            assert ds.info.to_json()["calibration"]["x"]["known"] is False


def test_volume_without_z_spacing(tmp_path: Path):
    head = raw_header(4, 4, 3, 0, cell=(4.0, 4.0, 0.0), ispg=1)
    with READER.open(write_raw(tmp_path / "noz.mrc", head, bytes(48))) as ds:
        cal = ds.info.calibration
        assert cal.known and cal.z is not None and not cal.z.known
        assert any("Z spacing unknown" in w for w in ds.info.warnings)


def test_non_standard_axis_order(tmp_path: Path):
    """MAPC/MAPR/MAPS say which cell axis runs along columns, rows and sections."""
    head = raw_header(4, 6, 2, 0, cell=(4.0, 12.0, 6.0), sampling=(4, 6, 2), axis_order=(2, 1, 3), ispg=1)
    with READER.open(write_raw(tmp_path / "yxz.map", head, bytes(48))) as ds:
        cal = ds.info.calibration  # cell spacing: X 1 Å, Y 2 Å, Z 3 Å; columns run along Y
        assert (cal.x.value, cal.y.value) == (pytest.approx(2.0), pytest.approx(1.0))
        assert cal.z is not None and cal.z.value == pytest.approx(3.0)
        assert any("axis order" in w for w in ds.info.warnings)
        assert (ds.info.metadata["mapc"], ds.info.metadata["mapr"], ds.info.metadata["maps"]) == (2, 1, 3)


@pytest.mark.parametrize("byte_order", ["<", ">"])
@pytest.mark.parametrize("exttyp", [b"FEI1", b"FEI2"])
def test_fei_extended_header(tmp_path: Path, exttyp, byte_order):
    """CV-15: EPU files carry Å/px (in metres) and per-section metadata in the extended header."""
    data = sample("int16", (3, 6, 8), seed=5)
    tilts = [-3.0, 0.0, 3.0]
    ext = fei_records(
        exttyp,
        3,
        byte_order,
        Pixel_size_X=0.83e-10,
        Pixel_size_Y=0.85e-10,
        Alpha_tilt=tilts,
        Dose=[1.5e20, 1.5e20, 1.5e20],
        Defocus=[-1.0e-6, -1.2e-6, -1.4e-6],
        Integration_time=0.25,
        Timestamp=[45000.5, 45000.6, 45000.7],
        Microscope_type=b"TITAN52336320",
        Application=b"Tomography",
        HT=300000.0,
        Magnification=81000.0,
    )
    path = write_with_mrcfile(
        tmp_path / "epu.mrc",
        data,
        byte_order=byte_order,
        image_stack=True,
        extended_header=ext,
        exttyp=exttyp,
    )
    with mrcfile.open(str(path)) as ref:
        reference = ref.indexed_extended_header
        assert float(ref.header.cella.x) == 0.0  # the main header has no pixel size
        ref_tilts = reference["Alpha tilt"].tolist()
        ref_px = float(reference["Pixel size X"][0]) * 1e10
    with READER.open(path) as ds:
        assert_bit_exact(all_sections(ds), data)
        cal, meta = ds.info.calibration, ds.info.metadata
        assert cal.known and cal.x.source is EXTENDED and cal.y.source is EXTENDED and cal.x.unit == "Å"
        assert cal.x.value == pytest.approx(0.83) and ref_px == pytest.approx(0.83)
        assert cal.y.value == pytest.approx(0.85)
        assert not ds.info.warnings
        ext_meta = meta["extended_header"]
        assert ext_meta["type"] == exttyp.decode() and ext_meta["sections"] == 3
        assert ext_meta["nbytes"] == ext.nbytes and ext_meta["record_nbytes"] == ext.dtype.itemsize
        first = ext_meta["first_section"]
        assert first["Microscope type"] == "TITAN52336320" and first["Application"] == "Tomography"
        assert first["HT"] == 300000.0 and first["Magnification"] == 81000.0
        assert first["Integration time"] == 0.25 and first["Dose"] == 1.5e20
        assert first["Pixel size X"] == pytest.approx(0.83e-10)
        assert not any(k.startswith(("Bitmask", "Unused")) or "reserved" in k for k in first)
        per_section = ext_meta["per_section"]  # only what changes from one section to the next
        assert per_section["Alpha tilt"] == ref_tilts == tilts
        assert per_section["Defocus"] == [-1.0e-6, -1.2e-6, -1.4e-6]
        assert per_section["Timestamp"] == [45000.5, 45000.6, 45000.7]
        assert "Dose" not in per_section and "Integration time" not in per_section
        assert ds.info.stack_kind is StackKind.TILT_SERIES  # the tilt angle changes between sections
        json.dumps(ds.info.to_json(), allow_nan=False)


def test_fei_header_agreeing_or_disagreeing_with_main_header(tmp_path: Path):
    data = sample("int16", (6, 8))
    ext = fei_records(b"FEI1", 1, Pixel_size_X=0.83e-10, Pixel_size_Y=0.83e-10)

    def opened(name: str, voxel: float):
        return READER.open(
            write_with_mrcfile(tmp_path / name, data, voxel_size=voxel, extended_header=ext, exttyp=b"FEI1")
        )

    with opened("agree.mrc", 0.835) as ds:  # within 1 %
        assert ds.info.calibration.x.source is HEADER and not ds.info.warnings
        assert ds.info.calibration.x.value == pytest.approx(0.835)
    with opened("disagree.mrc", 1.2) as ds:  # the header wins, and the user is told
        cal = ds.info.calibration
        assert cal.x.source is HEADER and (cal.x.value, cal.y.value) == pytest.approx((1.2, 1.2))
        (warning,) = ds.info.warnings
        assert "1.2" in warning and "0.83" in warning and "extended header" in warning


def test_fei_header_that_cannot_be_interpreted(tmp_path: Path):
    data = sample("int16", (6, 8))
    cases = {
        "short": np.zeros(100, dtype="V1"),  # shorter than one record
        "badsize": fei_records(b"FEI1", 1, Pixel_size_X=1e-10, Metadata_size=12),
        "hugesize": fei_records(b"FEI1", 1, Pixel_size_X=1e-10, Metadata_size=2**30),
        "zero_px": fei_records(b"FEI1", 1),
        "absurd_px": fei_records(b"FEI2", 1, Pixel_size_X=1e200, Pixel_size_Y=-1.0),
        "nan_px": fei_records(b"FEI2", 1, Pixel_size_X=float("nan"), Pixel_size_Y=float("inf")),
    }
    for name, ext in cases.items():
        path = write_with_mrcfile(tmp_path / f"{name}.mrc", data, extended_header=ext, exttyp=b"FEI1")
        with READER.open(path) as ds:
            assert_bit_exact(ds.read_plane(), data)
            assert not ds.info.calibration.known and ds.info.calibration.x.source is DEFAULT
            assert any("pixel size unknown" in w for w in ds.info.warnings)
            if name in ("short", "badsize", "hugesize"):
                assert any("cannot be interpreted" in w for w in ds.info.warnings)
                assert "first_section" not in ds.info.metadata["extended_header"]
            json.dumps(ds.info.to_json(), allow_nan=False)


def test_fei_type_without_extended_header(tmp_path: Path):
    path = write_with_mrcfile(tmp_path / "noext.mrc", sample("int16", (4, 4)), voxel_size=1.0, exttyp=b"FEI1")
    with READER.open(path) as ds:
        assert not ds.info.warnings and "extended_header" not in ds.info.metadata


def test_fei_records_larger_than_the_known_layout(tmp_path: Path):
    """'Metadata size' is the record stride: a newer, longer record is still read."""
    ext = fei_records(b"FEI2", 2, Pixel_size_X=1.1e-10, Pixel_size_Y=1.1e-10, Alpha_tilt=[10.0, 20.0])
    stride = ext.dtype.itemsize + 40
    padded = np.zeros((2, stride), dtype=np.uint8)
    padded[:, : ext.dtype.itemsize] = ext.view(np.uint8).reshape(2, -1)
    padded[:, :4] = np.frombuffer(struct.pack("<i", stride), dtype=np.uint8)
    path = write_with_mrcfile(
        tmp_path / "new.mrc",
        sample("int16", (2, 4, 4)),
        extended_header=padded.reshape(-1).view("V1"),
        exttyp=b"FEI2",
    )
    with READER.open(path) as ds:
        assert ds.info.calibration.x.value == pytest.approx(1.1)
        assert ds.info.metadata["extended_header"]["per_section"]["Alpha tilt"] == [10.0, 20.0]
        assert ds.info.metadata["extended_header"]["record_nbytes"] == stride
        assert "Scan rotation" in ds.info.metadata["extended_header"]["first_section"]  # an FEI2 field
    # declared FEI2 but with 768-byte records: the FEI1 fields are read, nothing beyond the record
    short = fei_records(b"FEI1", 2, Pixel_size_X=1.3e-10, Pixel_size_Y=1.3e-10)
    path = write_with_mrcfile(
        tmp_path / "fei2_short.mrcs", sample("int16", (2, 4, 4)), extended_header=short, exttyp=b"FEI2"
    )
    with READER.open(path) as ds:
        assert ds.info.calibration.x.value == pytest.approx(1.3) and not ds.info.warnings
        assert "Scan rotation" not in ds.info.metadata["extended_header"]["first_section"]


def test_metadata_is_bounded_and_json_safe(tmp_path: Path):
    """A long (here: random, i.e. hostile) extended header cannot blow up the metadata."""
    nz = 3000
    ext = fei_records(b"FEI2", nz)
    noise = np.random.default_rng(1).integers(0, 256, size=ext.nbytes, dtype=np.uint8)
    noisy = noise.view(ext.dtype).copy()
    noisy["Metadata size"] = ext.dtype.itemsize
    path = write_with_mrcfile(
        tmp_path / "long.mrc",
        np.zeros((nz, 2, 2), np.int8),
        image_stack=True,
        extended_header=noisy,
        exttyp=b"FEI2",
    )
    tracemalloc.start()
    try:
        with READER.open(path) as ds:
            text = json.dumps(ds.info.to_json(), allow_nan=False)
            _, peak = tracemalloc.get_traced_memory()
            ext_meta = ds.info.metadata["extended_header"]
    finally:
        tracemalloc.stop()
    assert len(text) < 300_000 and peak < 8 * 2**20
    assert ext_meta["sections"] == nz and ext_meta["per_section_truncated"] is True
    assert all(len(v) <= 512 for v in ext_meta["per_section"].values())


def test_header_metadata_matches_mrcfile(tmp_path: Path):
    data = sample("float32", (2, 5, 6), seed=9)
    data[0, 0, :6] = 1.0  # keep the statistics finite
    path = write_with_mrcfile(tmp_path / "meta.mrc", data, voxel_size=1.25, header={"nxstart": 3})
    with mrcfile.open(str(path), mode="r+") as mrc:
        mrc.header.origin = (10.0, 20.0, 30.0)
        mrc.add_label("gain corrected")
    with READER.open(path) as ds, mrcfile.open(str(path)) as ref:
        meta, h = ds.info.metadata, ref.header
        assert (meta["nx"], meta["ny"], meta["nz"]) == (6, 5, 2) == (int(h.nx), int(h.ny), int(h.nz))
        assert (meta["mode"], meta["ispg"], meta["nsymbt"]) == (2, int(h.ispg), 0)
        assert meta["nversion"] == int(h.nversion) and meta["exttyp"] == ""
        assert (meta["mapc"], meta["mapr"], meta["maps"]) == (1, 2, 3)
        assert meta["origin"] == [10.0, 20.0, 30.0] and meta["nstart"] == [3, 0, 0]
        assert meta["sampling"] == [6, 5, 2] and meta["cell"] == pytest.approx(h.cella.tolist())
        hint = meta["statistics_hint"]  # header values, not recomputed: hints only
        assert hint == {k: pytest.approx(float(h[k])) for k in ("dmin", "dmax", "dmean", "rms")}
        assert meta["labels"] == [label.strip() for label in ref.get_labels()]
        assert meta["labels"][1] == "gain corrected"
        assert meta["sections_in_file"] == 2 and meta["complete"] is True
        assert "extended_header" not in meta and "bytes_signed" not in meta
        json.dumps(ds.info.to_json(), allow_nan=False)


def test_non_finite_header_values_stay_json_safe(tmp_path: Path):
    head = raw_header(4, 4, 1, 2, stats=(float("nan"), float("inf"), float("-inf")), rms=float("nan"))
    with READER.open(write_raw(tmp_path / "nanstats.mrc", head, bytes(64))) as ds:
        assert ds.info.metadata["statistics_hint"] == {"dmin": None, "dmax": None, "dmean": None, "rms": None}
        json.dumps(ds.info.to_json(), allow_nan=False)


# ─── Axis semantics ───────────────────────────────────────────────────────────

MOVIE, STACK, TILT, VOLUME, IMAGE = (
    StackKind.MOVIE,
    StackKind.STACK,
    StackKind.TILT_SERIES,
    StackKind.VOLUME,
    StackKind.IMAGE,
)


@pytest.mark.parametrize(
    ("name", "ispg", "nz", "kind", "axis"),
    [
        ("movie.mrc", 0, 5, MOVIE, "t"),
        ("volume.mrc", 1, 5, VOLUME, "z"),
        ("p1_crystal.mrc", 230, 5, VOLUME, "z"),
        ("odd_ispg.mrc", 300, 5, MOVIE, "t"),
        ("particles.mrcs", 0, 5, STACK, "t"),
        ("relion_particles.mrcs", 1, 5, STACK, "t"),  # RELION writes ISPG 1 in stacks
        ("UPPER.MRCS", 0, 5, STACK, "t"),
        ("tilt.st", 0, 5, TILT, "t"),
        ("aligned.ali", 1, 5, TILT, "t"),
        ("tomogram.rec", 0, 5, VOLUME, "z"),  # IMOD writes ISPG 0 in tomograms
        ("emd_1234.map", 1, 5, VOLUME, "z"),
        ("emd_0000.map", 0, 5, VOLUME, "z"),
        ("image.mrc", 0, 1, IMAGE, None),
        ("slab.mrc", 1, 1, IMAGE, None),
        ("one.mrcs", 0, 1, IMAGE, None),
        ("one.rec", 1, 1, IMAGE, None),
    ],
)
def test_axis_semantics(tmp_path: Path, name, ispg, nz, kind, axis):
    data = sample("int16", (nz, 4, 6), seed=nz)
    head = raw_header(6, 4, nz, 1, ispg=ispg, cell=(6.0, 4.0, 2.0 * nz))
    with READER.open(write_raw(tmp_path / name, head, data.tobytes())) as ds:
        info = ds.info
        assert info.stack_kind is kind
        assert (info.size_t, info.size_z) == {"t": (nz, 1), "z": (1, nz), None: (1, 1)}[axis]
        assert_bit_exact(all_sections(ds), data)
        assert (info.calibration.z is not None) == (axis == "z")
        if axis == "z":
            assert info.calibration.z.value == pytest.approx(2.0)


def test_volume_stack_goes_on_t_and_z(tmp_path: Path):
    data = sample("int16", (2, 3, 4, 5), seed=401)
    path = write_with_mrcfile(tmp_path / "vstack.mrc", data, voxel_size=2.0)
    with mrcfile.open(str(path)) as ref:
        assert ref.is_volume_stack() and int(ref.header.ispg) == 401 and int(ref.header.mz) == 3
        reference = np.array(ref.data)
    with READER.open(path) as ds:
        assert ds.info.shape == (2, 1, 3, 4, 5) and ds.info.stack_kind is StackKind.HYPERSTACK
        for t in range(2):
            for z in range(3):
                assert_bit_exact(ds.read_plane(t, 0, z), reference[t, z])
        assert ds.info.calibration.z is not None and ds.info.calibration.z.value == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("nz", "mz", "shape_tz", "kind", "warns"),
    [
        (6, 6, (1, 6), VOLUME, False),  # a stack of one volume
        (6, 1, (6, 1), STACK, False),  # a stack of one-section volumes
        (6, 4, (1, 6), VOLUME, True),  # NZ is not a multiple of MZ
        (6, 0, (1, 6), VOLUME, True),
    ],
)
def test_degenerate_volume_stacks(tmp_path: Path, nz, mz, shape_tz, kind, warns):
    head = raw_header(4, 4, nz, 0, ispg=401, sampling=(4, 4, mz))
    with READER.open(write_raw(tmp_path / "vs.mrc", head, bytes(16 * nz))) as ds:
        assert (ds.info.size_t, ds.info.size_z) == shape_tz and ds.info.stack_kind is kind
        assert any("volume stack" in w for w in ds.info.warnings) == warns


def test_stack_kind_option_overrides_the_heuristic(tmp_path: Path):
    data = sample("int16", (5, 4, 6))
    path = write_raw(tmp_path / "tomo.mrc", raw_header(6, 4, 5, 1, ispg=0), data.tobytes())
    with READER.open(path) as ds:
        assert ds.info.stack_kind is MOVIE
    with READER.open(path, {"stack_kind": "volume"}) as ds:
        assert ds.info.stack_kind is VOLUME and (ds.info.size_t, ds.info.size_z) == (1, 5)
        assert_bit_exact(all_sections(ds), data)
    for value, kind in {"movie": MOVIE, "stack": STACK, "tilt_series": TILT}.items():
        with READER.open(path, {"stack_kind": value}) as ds:
            assert ds.info.stack_kind is kind and (ds.info.size_t, ds.info.size_z) == (5, 1)
    for bad in ({"stack_kind": "image"}, {"stack_kind": 3}, {"frames": 2}, {"stack_kind": "volume", "x": 1}):
        with pytest.raises(InvalidRequestError):
            READER.open(path, bad)
    assert READER.open(path, {}).info.stack_kind is MOVIE


# ─── Truncated, growing and hostile files ─────────────────────────────────────


def test_truncated_file_exposes_only_complete_sections(tmp_path: Path):
    """CV-28: 10 sections announced, 4.02 on disk."""
    data = sample("int16", (10, 12, 16), seed=28)
    full = write_with_mrcfile(tmp_path / "full.mrcs", data, image_stack=True, voxel_size=1.0)
    section = 12 * 16 * 2
    cut = write_raw(tmp_path / "cut.mrcs", full.read_bytes()[: HEADER_SIZE + int(4.02 * section)])
    with READER.open(cut) as ds:
        assert ds.info.size_t == 4 and ds.info.plane_count == 4
        assert ds.info.warnings == (
            "header says 10 sections, file holds 4: truncated or still being written",
        )
        assert ds.info.metadata["nz"] == 10 and ds.info.metadata["sections_in_file"] == 4
        assert ds.info.metadata["complete"] is False
        assert_bit_exact(all_sections(ds), data[:4])
        with pytest.raises(InvalidRequestError):
            ds.read_plane(4, 0, 0)
    one = write_raw(tmp_path / "one.mrcs", full.read_bytes()[: HEADER_SIZE + section])
    with READER.open(one) as ds:
        assert ds.info.stack_kind is StackKind.IMAGE and ds.info.plane_count == 1


@pytest.mark.parametrize("keep", [0, 1, 500, 1023, 1024, 1024 + 383])
def test_file_without_one_complete_section_is_an_error(tmp_path: Path, keep):
    full = write_with_mrcfile(tmp_path / "full.mrc", sample("int16", (3, 12, 16)))
    cut = write_raw(tmp_path / "cut.mrc", full.read_bytes()[:keep])
    message = str(open_error(cut))
    assert "truncated or still being written" in message and "cut.mrc" in message


def test_truncated_extended_header_is_an_error(tmp_path: Path):
    path = write_raw(tmp_path / "ext.mrc", raw_header(4, 4, 1, 0, nsymbt=5000), bytes(100))
    assert "extended header" in str(open_error(path))
    huge = write_raw(tmp_path / "huge.mrc", raw_header(4, 4, 1, 0, nsymbt=2**31 - 1), bytes(100))
    assert "extended header" in str(open_error(huge))


def test_truncated_volume_stack_keeps_complete_volumes(tmp_path: Path):
    data = sample("int8", (3, 2, 4, 4))
    full = write_with_mrcfile(tmp_path / "full.mrc", data)
    cut = write_raw(tmp_path / "cut.mrc", full.read_bytes()[: HEADER_SIZE + 5 * 16])
    with READER.open(cut) as ds:  # 5 of 6 sections: 2 whole volumes
        assert (ds.info.size_t, ds.info.size_z) == (2, 2)
        assert_bit_exact(all_sections(ds), data.reshape(-1, 4, 4)[:4])
        assert any("header says 6 sections, file holds 5" in w for w in ds.info.warnings)
    cut = write_raw(tmp_path / "cut1.mrc", full.read_bytes()[: HEADER_SIZE + 16])
    with READER.open(cut) as ds:
        assert ds.info.plane_count == 1


def test_trailing_bytes_are_tolerated(tmp_path: Path):
    data = sample("int16", (2, 4, 6))
    full = write_with_mrcfile(tmp_path / "full.mrc", data, voxel_size=1.0)
    padded = write_raw(tmp_path / "padded.mrc", full.read_bytes(), bytes(777))
    with READER.open(padded) as ds:
        assert not ds.info.warnings and ds.info.metadata["complete"] is True
        assert_bit_exact(all_sections(ds), data)


def test_growing_file(tmp_path: Path):
    """LIVE-1: a movie being written is never reported as complete; reopening sees the new frames."""
    data = sample("float32", (5, 8, 8), seed=1)
    path = tmp_path / "live.mrc"
    head = raw_header(8, 8, 5, 2, cell=(8.0, 8.0, 5.0))
    path.write_bytes(head[:600])
    assert "still being written" in str(open_error(path))
    path.write_bytes(head)
    assert "still being written" in str(open_error(path))
    early: Dataset | None = None
    for n in range(1, 6):
        with path.open("ab") as f:
            f.write(data[n - 1].tobytes()[:100])  # a partial section never counts
            f.flush()
            if n == 1:
                assert "still being written" in str(open_error(path))
            else:
                with READER.open(path) as partial:
                    assert partial.info.plane_count == n - 1
            f.write(data[n - 1].tobytes()[100:])
        ds = READER.open(path)
        assert ds.info.plane_count == n and ds.info.metadata["complete"] is (n == 5)
        assert bool(ds.info.warnings) is (n < 5)
        assert_bit_exact(all_sections(ds), data[:n])
        if n == 2:
            early = ds
        else:
            ds.close()
    assert early is not None  # a dataset opened earlier keeps its own, consistent view
    assert early.info.plane_count == 2
    assert_bit_exact(all_sections(early), data[:2])
    early.close()


def test_file_truncated_after_open(tmp_path: Path):
    data = sample("int16", (4, 8, 8))
    path = write_with_mrcfile(tmp_path / "shrinks.mrc", data)
    with READER.open(path) as ds:
        os.truncate(path, HEADER_SIZE + 2 * 128 + 10)
        assert_bit_exact(ds.read_plane(0, 0, 1), data[1])
        for z in (2, 3):
            with pytest.raises(CorruptFileError) as e:
                ds.read_plane(0, 0, z)
            assert "shrinks.mrc" in str(e.value) and str(tmp_path) not in str(e.value)


def test_hostile_dimensions_fail_before_any_allocation(tmp_path: Path):
    big = 2**31 - 1
    cases = [(big, big, big, 2), (big, 1, 1, 2), (1, big, 1, 101), (1, 1, big, 16), (65536, 65536, 1, 0)]
    tracemalloc.start()
    try:
        for i, (nx, ny, nz, mode) in enumerate(cases):
            path = write_raw(tmp_path / f"bomb{i}.mrc", raw_header(nx, ny, nz, mode), bytes(4096))
            if (nx, ny) == (1, 1):  # 3-byte sections: the file really holds 1365 of them
                with READER.open(path) as ds:
                    assert ds.info.size_t == 4096 // 3
            else:
                assert "truncated" in str(open_error(path))
        for dims in [(0, 4, 4), (4, 0, 4), (4, 4, 0), (-4, 4, 4), (4, -(2**31), 1)]:
            path = write_raw(tmp_path / "neg.mrc", raw_header(*dims, 2), bytes(4096))
            assert "dimensions" in str(open_error(path))
        path = write_raw(tmp_path / "negext.mrc", raw_header(4, 4, 1, 2, nsymbt=-8), bytes(4096))
        assert "extended header" in str(open_error(path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2**20


def test_reading_a_plane_allocates_about_one_plane(tmp_path: Path):
    """Only the requested section is read: memory does not scale with the file."""
    data = np.zeros((64, 256, 256), np.float32)
    path = write_with_mrcfile(tmp_path / "big.mrc", data, byte_order=">")
    plane_nbytes = 256 * 256 * 4
    with READER.open(path) as ds:
        tracemalloc.start()
        try:
            plane = ds.read_plane(0, 0, 40)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert plane.nbytes == plane_nbytes and peak < 2.5 * plane_nbytes


@st.composite
def hostile_files(draw: st.DrawFn) -> tuple[str, bytes]:
    """A short file whose header fields are each sane most of the time and arbitrary otherwise, so
    that both the rejections and the files that open with odd fields are explored."""
    anything = st.one_of(st.integers(-(2**31), 2**31 - 1), st.integers(-2, 70000))

    def field(*usual: int) -> int:
        return draw(st.sampled_from(usual) if draw(st.integers(0, 9)) else anything)

    nx, ny, nz, mz = (field(1, 2, 3, 7, 12) for _ in range(4))
    imod_stamp, imod_flags = draw(
        st.sampled_from([(0, 0), (IMOD_STAMP, 0), (IMOD_STAMP, 1), (IMOD_STAMP, 31)])
    )
    head = raw_header(
        nx,
        ny,
        nz,
        field(0, 1, 2, 6, 12, 16, 101),
        byte_order=draw(st.sampled_from("<>")),
        sampling=(nx, ny, mz),
        cell=draw(st.tuples(*[st.floats(width=32)] * 3)),
        stats=draw(st.tuples(*[st.floats(width=32)] * 3)),
        ispg=field(0, 1, 401),
        nsymbt=field(0, 100, 768, 888, 1776),
        exttyp=draw(st.sampled_from([b"", b"FEI1", b"FEI2", b"SERI", b"\xff\xfe\x00\x01"])),
        imod_stamp=imod_stamp,
        imod_flags=imod_flags,
        machine_stamp=draw(st.sampled_from([None, None, b"\0\0\0\0", b"\x11\x11\0\0", b"\x44\x41\0\0"])),
        map_id=draw(st.sampled_from([b"MAP ", b"MAP ", b"MAP\0", b"\0\0\0\0"])),
    )
    suffix = draw(st.sampled_from([".mrc", ".mrcs", ".st", ".rec", ".map", ".ali"]))
    body = np.random.default_rng(draw(st.integers(0, 2**32))).bytes(
        draw(st.sampled_from([0, 500, 4000, 4000]))
    )
    return suffix, head + body


def exercise(path: Path) -> bool:
    """Open, read the first and last planes, serialise. True when the file opened; only
    CorruptFileError may come out."""
    try:
        ds = READER.open(path)
    except CorruptFileError as e:
        assert path.name in str(e) and os.sep not in str(e)
        return False
    with ds:
        info = ds.info
        assert info.dtype in PLANE_DTYPES
        for t, c, z in {(0, 0, 0), (info.size_t - 1, info.size_c - 1, info.size_z - 1)}:
            plane = ds.read_plane(t, c, z)
            assert plane.shape == (info.size_y, info.size_x) and plane.dtype == info.dtype
        json.dumps(info.to_json(), allow_nan=False)
    return True


@settings(max_examples=500, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
@given(case=hostile_files())
def test_hostile_headers_only_raise_corrupt_file_errors(tmp_path_factory, case):
    suffix, blob = case
    path = write_raw(tmp_path_factory.getbasetemp() / f"hostile{suffix}", blob)
    assert isinstance(READER.sniff(path, blob[:SNIFF_BYTES]), bool)
    tracemalloc.start()
    try:
        opened = exercise(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2**20  # whatever the header claims, memory follows the real file size
    event("opened" if opened else "rejected")


@settings(max_examples=200, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
@given(blob=st.binary(max_size=3000), keep_map=st.booleans())
def test_random_bytes_only_raise_corrupt_file_errors(tmp_path_factory, blob, keep_map):
    if keep_map and len(blob) >= 216:
        blob = blob[:208] + b"MAP \x44\x44\0\0" + blob[216:]
    path = write_raw(tmp_path_factory.getbasetemp() / "random.mrc", blob)
    assert isinstance(READER.sniff(path, blob), bool)
    exercise(path)


@settings(max_examples=200, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
@given(data=st.data())
def test_truncated_or_damaged_valid_file(tmp_path_factory, data):
    """Any prefix of a valid file, with any one byte of its headers changed, opens or fails cleanly."""
    base = tmp_path_factory.getbasetemp()
    source = base / "valid_source.mrc"
    if not source.exists():
        ext = fei_records(b"FEI2", 4, Pixel_size_X=1e-10, Pixel_size_Y=1e-10, Alpha_tilt=[0, 1, 2, 3])
        write_with_mrcfile(source, sample("float32", (4, 5, 6)), extended_header=ext, exttyp=b"FEI2")
    blob = bytearray(source.read_bytes())
    header_nbytes = len(blob) - 4 * 5 * 6 * 4
    blob[data.draw(st.integers(0, header_nbytes - 1))] = data.draw(st.integers(0, 255))
    cut = data.draw(st.one_of(st.integers(0, len(blob)), st.integers(header_nbytes, len(blob))))
    event("opened" if exercise(write_raw(base / "damaged.mrc", bytes(blob[:cut]))) else "rejected")


# ─── I/O behaviour ────────────────────────────────────────────────────────────


def test_concurrent_reads_from_many_threads(tmp_path: Path):
    data = sample("float32", (40, 48, 64), seed=11)
    path = write_with_mrcfile(tmp_path / "threads.mrc", data, byte_order=">", image_stack=True)
    reference = reference_sections(path)
    order = np.random.default_rng(0).integers(0, 40, size=(16, 60))
    start = threading.Barrier(16)

    with READER.open(path) as ds:

        def work(indices: np.ndarray) -> int:
            start.wait()
            return sum(ds.read_plane(int(t)).tobytes() == reference[t].tobytes() for t in indices)

        with ThreadPoolExecutor(16) as pool:
            assert list(pool.map(work, order)) == [60] * 16


def test_close_is_idempotent_and_safe_under_concurrent_reads(tmp_path: Path):
    data = sample("int16", (8, 32, 32))
    path = write_with_mrcfile(tmp_path / "close.mrc", data)
    ds = READER.open(path)
    assert_bit_exact(ds.read_plane(0, 0, 3), data[3])
    ds.close()
    ds.close()
    with pytest.raises(ValueError, match="closed"):
        ds.read_plane(0, 0, 0)

    outcomes: list[str] = []
    for _ in range(20):  # closing while other threads read: a read either succeeds or says "closed"
        ds = READER.open(path)
        go = threading.Barrier(5)

        def read(ds: Dataset = ds, go: threading.Barrier = go) -> None:
            go.wait()
            for z in range(8):
                try:
                    ok = ds.read_plane(0, 0, z).tobytes() == data[z].tobytes()
                    outcomes.append("ok" if ok else "WRONG")
                except ValueError:
                    outcomes.append("closed")

        threads = [threading.Thread(target=read) for _ in range(4)]
        for th in threads:
            th.start()
        go.wait()
        ds.close()
        for th in threads:
            th.join()
    assert set(outcomes) <= {"ok", "closed"} and len(outcomes) == 20 * 4 * 8


def test_file_descriptor_is_released(tmp_path: Path):
    psutil = pytest.importorskip("psutil")
    path = write_with_mrcfile(tmp_path / "fd.mrc", sample("int16", (4, 4)))
    bad = write_raw(tmp_path / "bad.mrc", raw_header(4, 4, 1, 4), bytes(200))
    process = psutil.Process()
    before = process.num_fds()
    for _ in range(50):
        with READER.open(path) as ds:
            ds.read_plane()
        READER.open(path)  # never closed: released with the object
        with pytest.raises(CorruptFileError):
            READER.open(bad)
    assert process.num_fds() == before


@pytest.mark.parametrize("reader_name", ["_preadv_into", "_seek_read_into"])
def test_large_reads_are_chunked_and_short_reads_resumed(tmp_path: Path, monkeypatch, reader_name):
    """A read() may return fewer bytes than asked (always, above 2 GiB): the reader loops."""
    data = sample("float32", (3, 50, 70), seed=8)
    path = write_with_mrcfile(tmp_path / "chunks.mrc", data, byte_order=">")
    monkeypatch.setattr(mrc_module, "_read_into", getattr(mrc_module, reader_name))
    monkeypatch.setattr(mrc_module, "_READ_CHUNK", 1000)
    calls = []
    if reader_name == "_preadv_into":
        real = os.preadv

        def short_preadv(fd, buffers, offset):
            (buffer,) = buffers
            assert len(buffer) <= 1000
            calls.append(len(buffer))
            return real(fd, [buffer[: max(1, len(buffer) - 300)]], offset)

        monkeypatch.setattr(mrc_module.os, "preadv", short_preadv)
    with READER.open(path) as ds:
        assert_bit_exact(all_sections(ds), data)
    assert reader_name != "_preadv_into" or len(calls) > 3 * (14000 // 1000)


# ─── sniff, errors, helpers ───────────────────────────────────────────────────


def test_sniff(tmp_path: Path):
    path = write_with_mrcfile(tmp_path / "ok.dat", sample("int16", (4, 4)))
    head = path.read_bytes()[:SNIFF_BYTES]
    assert READER.sniff(path, head)
    assert READER.sniff(path, raw_header(4, 4, 1, 2, byte_order=">"))
    assert READER.sniff(path, raw_header(4, 4, 1, 2, map_id=b"MAP\0"))  # some writers omit the space
    assert READER.sniff(path, raw_header(4, 4, 1, 101, machine_stamp=b"\0\0\0\0"))
    assert not READER.sniff(path, head[:1023])
    assert not READER.sniff(path, b"")
    assert not READER.sniff(path, raw_header(4, 4, 1, 2, map_id=b"\0\0\0\0"))
    assert not READER.sniff(path, raw_header(0, 4, 1, 2))
    assert not READER.sniff(path, raw_header(4, 4, 1, 77))
    assert not READER.sniff(path, b"II*\x00" + bytes(2000))  # a TIFF
    assert not READER.sniff(path, b"\x89PNG\r\n\x1a\n" + bytes(2000))
    registry = Registry([READER])
    with registry.open(path) as ds:  # recognised by content under a foreign extension
        assert ds.info.reader == "mrc"
    with pytest.raises(UnsupportedFormatError):
        registry.open(write_raw(tmp_path / "notes.txt", b"hello"))


def test_error_messages_never_contain_a_directory(tmp_path: Path):
    folder = tmp_path / "secret-project-dir"
    folder.mkdir()
    messages = []
    for name, blob in {
        "empty.mrc": b"",
        "junk.mrc": bytes(range(256)) * 8,
        "complex.mrc": raw_header(4, 4, 1, 4) + bytes(200),
        "mode.mrc": raw_header(4, 4, 1, 9) + bytes(200),
        "dims.mrc": raw_header(-1, 4, 1, 2) + bytes(200),
        "short.mrc": raw_header(400, 400, 4, 2) + bytes(200),
        "ext.mrc": raw_header(4, 4, 1, 2, nsymbt=10**6) + bytes(200),
    }.items():
        messages.append((name, str(open_error(write_raw(folder / name, blob)))))
    with pytest.raises(NotFoundError) as missing:
        READER.open(folder / "missing.mrc")
    messages.append(("missing.mrc", str(missing.value)))
    with pytest.raises(NotFoundError) as missing:
        read_header(folder / "nowhere" / "missing.mrc")
    messages.append(("missing.mrc", str(missing.value)))
    with pytest.raises(UnsupportedFormatError) as directory:
        READER.open(folder)
    messages.append((folder.name, str(directory.value)))
    for name, message in messages:
        assert name in message, message
        assert str(tmp_path) not in message and "secret-project-dir/" not in message and os.sep not in message


def test_special_files_and_io_errors(tmp_path: Path, monkeypatch):
    with pytest.raises(NotFoundError):
        READER.open(tmp_path / "nul\0byte.mrc")
    fifo = tmp_path / "pipe.mrc"
    os.mkfifo(fifo)
    with pytest.raises(UnsupportedFormatError, match="not a regular file"):  # and no hang
        READER.open(fifo)
    path = write_with_mrcfile(tmp_path / "disk.mrc", sample("int16", (4, 4)))
    if os.geteuid() != 0:  # root reads everything
        path.chmod(0)
        with pytest.raises(ImageBioError, match=r"permission denied: disk\.mrc"):
            READER.open(path)
        path.chmod(0o644)
    with READER.open(path) as ds:

        def failing_read(fd, buffers, offset):
            raise OSError(5, "Input/output error", str(path))

        monkeypatch.setattr(mrc_module, "_read_into", failing_read)
        with pytest.raises(ImageBioError) as e:  # a server-side failure, not a corrupt file
            ds.read_plane()
        assert not isinstance(e.value, CorruptFileError) and e.value.status == 500
        assert str(e.value) == "disk.mrc: I/O error (Input/output error)"
        with pytest.raises(ImageBioError, match=r"^disk\.mrc: I/O error"):
            READER.open(path)


def test_header_helpers_for_completeness_checks(tmp_path: Path):
    """Live mode decides that a file is complete from the header alone (PLAN §3, rule 9)."""
    data = sample("int16", (3, 6, 8))
    ext = fei_records(b"FEI1", 3)
    path = write_with_mrcfile(tmp_path / "h.mrc", data, extended_header=ext, exttyp=b"FEI1", voxel_size=1.0)
    header = read_header(path)
    assert isinstance(header, MrcHeader)
    assert (header.nx, header.ny, header.nz, header.mode, header.nsymbt) == (8, 6, 3, 1, 3 * 768)
    assert header.data_offset == 1024 + 3 * 768 and header.section_nbytes == 6 * 8 * 2
    assert header.expected_size == path.stat().st_size
    assert header == parse_header(path.read_bytes()[:HEADER_SIZE], "h.mrc")
    assert parse_header(raw_header(33, 10, 2, 101), "x").expected_size == 1024 + 2 * 10 * 17
    assert parse_header(raw_header(5, 4, 2, 16), "x").expected_size == 1024 + 2 * 4 * 5 * 3
    with pytest.raises(CorruptFileError, match=r"x\.mrc"):
        parse_header(b"too short", "x.mrc")
    with pytest.raises(CorruptFileError):
        read_header(write_raw(tmp_path / "tiny.mrc", b"MAP "))
