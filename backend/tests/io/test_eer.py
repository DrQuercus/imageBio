"""EER reader: group sums bit-exact against the synthetic encoder's ground truth, options, metadata,
robustness (AUDIT CV-2: the legacy app showed seeded Poisson noise at a hard-coded 0.5 Å/px)."""

from __future__ import annotations

import importlib
import json
import math
import struct
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
import tifffile
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from fixtures.eer import (
    COMPRESSION,
    IMAGE_LENGTH,
    IMAGE_WIDTH,
    STRIP_BYTE_COUNTS,
    STRIP_OFFSETS,
    V0,
    V1,
    EerFile,
    Encoding,
    decode_fraction_naive,
    encode_fraction,
    encode_fraction_naive,
    even_length,
    pack_codes,
    random_events,
    render,
    v2,
    write_eer,
)
from imagebio.core.errors import (
    CorruptFileError,
    ImageBioError,
    InvalidRequestError,
    NotFoundError,
    UnsupportedFormatError,
)
from imagebio.core.model import PLANE_DTYPES, UNKNOWN_PIXEL, CalibrationSource, StackKind
from imagebio.io.readers import eer as eer_module
from imagebio.io.readers.eer import READER, EerDataset, default_group
from imagebio.io.registry import BUILTIN_READER_MODULES, SNIFF_BYTES, Reader, Registry

ENCODINGS = [V0, V1, v2(), v2(9, 2, 2), v2(7, 1, 1)]
CONTAINERS = [(True, "<"), (True, ">"), (False, "<"), (False, ">")]


def encoding_id(e: Encoding) -> str:
    return f"{e.compression}-{e.skip_bits}{e.horz_bits}{e.vert_bits}"


def open_eer(path: Path, **options: object) -> EerDataset:
    ds = READER.open(path, options or None)
    assert isinstance(ds, EerDataset)
    return ds


def rejected(path: Path, match: str | None = None, **options: object) -> str:
    """The public message of the CorruptFileError raised when opening ``path``."""
    with pytest.raises(CorruptFileError, match=match) as e:
        READER.open(path, options or None)
    message = e.value.public_message
    assert path.name in message and str(path.parent) not in message  # never the server path
    return message


def patched(source: EerFile, patches: dict[int, bytes], name: str = "patched.eer") -> Path:
    """A copy of ``source`` with ``patches`` (offset -> bytes) applied."""
    data = bytearray(source.path.read_bytes())
    for offset, raw in patches.items():
        data[offset : offset + len(raw)] = raw
    path = source.path.with_name(name)
    path.write_bytes(data)
    return path


@pytest.fixture(params=["accumulate", "copy"])
def decode_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test on both decoding paths: the decoder summing in place, and decode-then-add."""
    if request.param == "copy":
        monkeypatch.setattr(eer_module, "_decoder_accumulates", lambda: False)
    return str(request.param)


# ─── The references agree with each other ─────────────────────────────────────


@pytest.mark.parametrize("encoding", ENCODINGS, ids=encoding_id)
def test_fixture_encoder_matches_the_references(encoding: Encoding):
    rng = np.random.default_rng(1)
    for shape, count in (((16, 16), 40), ((7, 127), 0), ((13, 21), 273), ((1, 1), 1), ((6, 10), 31)):
        pixels = shape[0] * shape[1]
        events = random_events(rng, pixels, count, encoding)
        stream = encode_fraction(events, pixels, encoding)
        assert stream == encode_fraction_naive(events, pixels, encoding)
        for superres in range(min(encoding.horz_bits, encoding.vert_bits) + 1):
            expected = render([events], shape, encoding, superres)
            assert expected.sum() == count
            np.testing.assert_array_equal(decode_fraction_naive(stream, shape, encoding, superres), expected)
            if superres == 1 and encoding.horz_bits == 2 and (shape[0] % 2 or shape[1] % 2):
                continue  # imagecodecs wants even sizes there
            bits = (encoding.skip_bits, encoding.horz_bits, encoding.vert_bits)
            decoded = imagecodecs.eer_decode(stream, expected.shape, *bits, superres=superres)
            np.testing.assert_array_equal(decoded, expected)


def test_the_installed_decoder_sums_in_place():
    # if this fails the reader still works, on its slower path: update the timings of its docstring
    assert eer_module._decoder_accumulates()


# ─── Pixels ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("bigtiff", "byteorder"), CONTAINERS)
@pytest.mark.parametrize("encoding", ENCODINGS, ids=encoding_id)
def test_group_sums_are_bit_exact(
    tmp_path: Path, decode_mode: str, bigtiff: bool, byteorder: str, encoding: Encoding
):
    f = write_eer(
        tmp_path / "movie.eer",
        n_fractions=11,
        encoding=encoding,
        bigtiff=bigtiff,
        byteorder=byteorder,
        seed=3,
    )
    with open_eer(f.path, group=4) as ds:
        info = ds.info
        assert info.shape == (3, 1, 1, 32, 48)
        assert info.dtype == np.uint16 and info.dtype in PLANE_DTYPES
        assert (info.format, info.reader, info.stack_kind) == ("eer", "eer", StackKind.MOVIE)
        assert (ds.n_fractions, ds.group, ds.superres) == (11, 4, 0)
        for t in range(3):
            plane = ds.read_plane(t)
            assert plane.dtype == np.uint16 and plane.flags.c_contiguous and plane.flags.owndata
            assert not isinstance(plane, np.memmap)
            assert ds.fraction_range(t) == range(4 * t, min(4 * t + 4, 11))
            np.testing.assert_array_equal(plane, f.truth(4 * t, 4 * t + 4))
        total = ds.read_sum()
        assert total.dtype == np.uint32 and total.shape == (32, 48)
        np.testing.assert_array_equal(total, f.truth())
        np.testing.assert_array_equal(ds.read_sum(3, 8), f.truth(3, 8))
        np.testing.assert_array_equal(ds.read_sum(10), f.truth(10))
        meta = info.metadata
        assert (meta["compression"], meta["skip_bits"], meta["horz_bits"], meta["vert_bits"]) == (
            encoding.compression,
            encoding.skip_bits,
            encoding.horz_bits,
            encoding.vert_bits,
        )
        assert meta["container"] == ("BigTIFF" if bigtiff else "TIFF")
        assert meta["byte_order"] == ("little" if byteorder == "<" else "big")
    # the container, read by an independent parser: tifffile finds the same pages
    with tifffile.TiffFile(f.path, is_eer=True) as tif:
        np.testing.assert_array_equal(tif.series[0].asarray().sum(axis=0), total)


def test_content_is_read_not_invented(tmp_path: Path):
    """CV-2: two different movies give different frames, and the pixel size is not made up."""
    a = write_eer(tmp_path / "a.eer", seed=1)
    b = write_eer(tmp_path / "b.eer", seed=2)
    with open_eer(a.path) as da, open_eer(b.path) as db:
        assert not np.array_equal(da.read_plane(0), db.read_plane(0))
        np.testing.assert_array_equal(da.read_plane(0), a.truth(0, 1))
        calibration = da.info.calibration
        assert calibration.x is UNKNOWN_PIXEL and calibration.y is UNKNOWN_PIXEL and not calibration.known
        assert calibration.x.source is CalibrationSource.DEFAULT
        assert any("pixel size unknown" in w for w in da.info.warnings)


@pytest.mark.parametrize("workers", [1, 4])
def test_counts_above_255_are_exact(
    tmp_path: Path, decode_mode: str, monkeypatch: pytest.MonkeyPatch, workers: int
):
    """Fractions are summed in uint8 batches: a pixel hit in every fraction must not wrap."""
    monkeypatch.setattr(eer_module, "_WORKERS", workers)
    f = write_eer(tmp_path / "dense.eer", n_fractions=600, shape=(4, 6), events=3, hot=[5, 23])
    with open_eer(f.path, group=300) as ds:
        assert ds.info.size_t == 2
        for t in range(2):
            plane = ds.read_plane(t)
            assert plane[0, 5] == plane[3, 5] == 300
            np.testing.assert_array_equal(plane, f.truth(300 * t, 300 * t + 300))
        total = ds.read_sum()
        assert total[0, 5] == 600 and total.sum() == f.n_events() == 600 * 5
        np.testing.assert_array_equal(total, f.truth())
        np.testing.assert_array_equal(ds.read_sum(100, 511), f.truth(100, 511))


@pytest.mark.parametrize("strips", [2, 3, 7])
def test_strips_of_a_fraction_are_one_stream(tmp_path: Path, decode_mode: str, strips: int):
    f = write_eer(tmp_path / "strips.eer", n_fractions=5, strips=strips)
    assert all(len(page) == strips for page in f.layout.strips)
    with open_eer(f.path, group=2) as ds:
        np.testing.assert_array_equal(ds.read_plane(1), f.truth(2, 4))
        np.testing.assert_array_equal(ds.read_sum(), f.truth())


@pytest.mark.parametrize(("bigtiff", "byteorder"), [(True, "<"), (False, ">")])
def test_integrated_first_page_is_skipped(tmp_path: Path, bigtiff: bool, byteorder: str):
    f = write_eer(tmp_path / "final.eer", integrated=True, bigtiff=bigtiff, byteorder=byteorder)
    assert READER.sniff(f.path, f.path.read_bytes()[:SNIFF_BYTES])
    with open_eer(f.path, group=5) as ds:
        assert ds.n_fractions == 12 and ds.info.size_t == 3
        assert ds.info.metadata["integrated_image"] is True
        assert ds.info.metadata["acquisition"]["numberOfFrames"] == 12
        np.testing.assert_array_equal(ds.read_plane(2), f.truth(10, 12))
        np.testing.assert_array_equal(ds.read_sum(), f.truth())


@pytest.mark.parametrize(
    "tail", [b"\0", b"\0" * 7, b"\0" * 64, b"\xff" * 64, b"\x7f\x3f" * 300, b"\x55" * 33]
)
def test_bytes_after_a_complete_stream_are_ignored(tmp_path: Path, decode_mode: str, tail: bytes):
    """Padding or junk after the last pixel, odd strip sizes: the decoder stops at the last pixel."""
    f = write_eer(tmp_path / "padded.eer", n_fractions=6, tail=tail)
    with open_eer(f.path, group=3) as ds:
        np.testing.assert_array_equal(ds.read_plane(0), f.truth(0, 3))
        np.testing.assert_array_equal(ds.read_sum(), f.truth())


@pytest.mark.parametrize("tail", [b"", b"\0\0", b"\xff\xff", b"\xa5" * 20])
def test_unusual_but_valid_stream_endings(tmp_path: Path, decode_mode: str, tail: bytes):
    """A last run coded as a bare max-skip, and a last event with no closing code after it.

    The second ending is only well defined when nothing but zero padding follows the stream:
    after an event on the last pixel, junk bytes would read as one more code past the end.
    """
    ny, nx = 4, 127
    bare_skip = [(3, 7), (5, 4)] + [(127, 7)] * 2 + [(ny * nx - 128 - 4 - 2 * 127, 7), (9, 4), (127, 7)]
    last_pixel = [(3, 7), (5, 4)] + [(127, 7)] * 3 + [(ny * nx - 5 - 3 * 127, 7), (9, 4)]
    streams = {0: even_length(pack_codes(bare_skip))}
    if not tail.strip(b"\0"):
        streams[1] = even_length(pack_codes(last_pixel))
    f = write_eer(
        tmp_path / "endings.eer", n_fractions=3, shape=(ny, nx), events=20, streams=streams, tail=tail
    )
    expected = [f.truth(k, k + 1) for k in range(3)]
    expected[0] = np.zeros((ny, nx), np.int64)
    expected[0].flat[[3, ny * nx - 128]] = 1
    if 1 in streams:
        expected[1] = np.zeros((ny, nx), np.int64)
        expected[1].flat[[3, ny * nx - 1]] = 1
    for index in streams:  # what the strict reference says of these streams
        np.testing.assert_array_equal(
            decode_fraction_naive(streams[index] + b"\0\0", (ny, nx)), expected[index]
        )
    with open_eer(f.path, group=1) as ds:
        for t in range(3):
            np.testing.assert_array_equal(ds.read_plane(t), expected[t])
        np.testing.assert_array_equal(ds.read_sum(), sum(expected))


# ─── Options ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n_fractions", [1, 7, 50, 51, 100, 101, 149])
def test_default_group_gives_at_most_50_frames(tmp_path: Path, n_fractions: int):
    f = write_eer(tmp_path / "movie.eer", n_fractions=n_fractions, shape=(4, 6), events=3)
    with open_eer(f.path) as ds:
        group = math.ceil(n_fractions / 50)
        assert ds.group == group == default_group(n_fractions) == ds.info.metadata["group"]
        assert ds.info.size_t == math.ceil(n_fractions / group) <= 50
        planes = [ds.read_plane(t) for t in range(ds.info.size_t)]
        assert sum(int(p.sum()) for p in planes) == f.n_events()  # no fraction dropped, none counted twice
        np.testing.assert_array_equal(planes[-1], f.truth((ds.info.size_t - 1) * group))


def test_partial_last_group_is_kept_and_reported(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=10)
    with open_eer(f.path, group=4) as ds:
        assert ds.info.size_t == 3 and ds.fraction_range(2) == range(8, 10)
        assert ds.info.metadata["last_group_fractions"] == 2
        assert any("last frame sums 2 fraction(s) instead of 4" in w for w in ds.info.warnings)
        np.testing.assert_array_equal(ds.read_plane(2), f.truth(8, 10))
        assert sum(int(ds.read_plane(t).sum()) for t in range(3)) == f.n_events()
    with open_eer(f.path, group=5) as ds:
        assert ds.info.size_t == 2 and ds.info.metadata["last_group_fractions"] == 5
        assert not any("last frame" in w for w in ds.info.warnings)
    with open_eer(f.path, group=1000) as ds:  # more than the movie holds: one frame with everything
        assert ds.info.size_t == 1 and ds.info.metadata["last_group_fractions"] == 10
        np.testing.assert_array_equal(ds.read_plane(0), f.truth())


@pytest.mark.parametrize("encoding", [V0, V1, v2(9, 2, 2)], ids=encoding_id)
@pytest.mark.parametrize("superres", [1, 2])
def test_superres(tmp_path: Path, decode_mode: str, encoding: Encoding, superres: int):
    f = write_eer(tmp_path / "movie.eer", n_fractions=9, encoding=encoding, events=200)
    scale = 1 << superres
    with open_eer(f.path, group=4, superres=superres) as ds:
        info = ds.info
        assert (info.size_t, info.size_y, info.size_x) == (3, 32 * scale, 48 * scale)
        assert (info.metadata["superres"], info.metadata["sampling"]) == (superres, scale)
        assert info.metadata["detector_size"] == {"x": 48, "y": 32}
        assert info.calibration.x is UNKNOWN_PIXEL  # still a placeholder, still in px
        for t in range(3):
            plane = ds.read_plane(t)
            assert plane.shape == (32 * scale, 48 * scale) and plane.dtype == np.uint16
            np.testing.assert_array_equal(plane, f.truth(4 * t, 4 * t + 4, superres))
            assert plane.sum() == f.n_events(4 * t, 4 * t + 4)  # every event counts once at any sampling
            binned = plane.reshape(32, scale, 48, scale).sum(axis=(1, 3))
            np.testing.assert_array_equal(binned, f.truth(4 * t, 4 * t + 4))  # and stays in its pixel
        total = ds.read_sum()
        assert total.sum() == f.n_events()
        np.testing.assert_array_equal(total, f.truth(superres=superres))
    with tifffile.TiffFile(f.path, is_eer=True, superres=superres) as tif:
        np.testing.assert_array_equal(tif.series[0].asarray().sum(axis=0), total)


def test_superres_needs_the_sub_pixel_bits(tmp_path: Path):
    one_bit = write_eer(tmp_path / "one.eer", encoding=v2(7, 1, 1))
    with open_eer(one_bit.path, superres=1) as ds:
        np.testing.assert_array_equal(ds.read_sum(), one_bit.truth(superres=1))
    with pytest.raises(InvalidRequestError, match=r"superres=2 is not available.*1 \+ 1 sub-pixel bits"):
        open_eer(one_bit.path, superres=2)
    wide = write_eer(tmp_path / "wide.eer", encoding=v2(6, 3, 3))
    with open_eer(wide.path) as ds:  # the plain image does not depend on the sub-pixel bits
        np.testing.assert_array_equal(ds.read_sum(), wide.truth())
    with pytest.raises(InvalidRequestError, match="superres=1 is not available"):
        open_eer(wide.path, superres=1)  # convention unknown for 3 bits: refused, not guessed
    odd = write_eer(tmp_path / "odd.eer", shape=(9, 15), events=20)
    with pytest.raises(InvalidRequestError, match="even frame width and height"):
        open_eer(odd.path, superres=1)
    with open_eer(odd.path, superres=2) as ds:
        np.testing.assert_array_equal(ds.read_sum(), odd.truth(superres=2))


@pytest.mark.parametrize(
    "options",
    [
        {"grp": 2},
        {"group": 4, "series": 0},
        {"group": 0},
        {"group": -3},
        {"group": 2.0},
        {"group": "4"},
        {"group": True},
        {"superres": 3},
        {"superres": -1},
        {"superres": 1.0},
        {"superres": True},
        {"superres": "1"},
    ],
)
def test_invalid_options(tmp_path: Path, options: dict[str, object]):
    f = write_eer(tmp_path / "movie.eer", n_fractions=2)
    with pytest.raises(InvalidRequestError):
        READER.open(f.path, options)
    with pytest.raises(InvalidRequestError):  # options are checked before the file is touched
        READER.open(tmp_path / "missing.eer", options)


def test_none_options_mean_defaults(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer")
    with open_eer(f.path, group=None, superres=None) as ds:
        assert (ds.group, ds.superres) == (1, 0)


def test_group_is_bounded_by_the_plane_dtype(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = write_eer(tmp_path / "movie.eer", n_fractions=12)
    monkeypatch.setattr(eer_module, "_MAX_GROUP", 5)
    with pytest.raises(InvalidRequestError, match="group must be at most 5"):
        open_eer(f.path, group=6)
    open_eer(f.path, group=5).close()


def test_oversized_superres_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = write_eer(tmp_path / "movie.eer")
    monkeypatch.setattr(eer_module, "_MAX_PLANE_PIXELS", 32 * 48 * 4)
    open_eer(f.path, superres=1).close()
    with pytest.raises(InvalidRequestError, match=r"192 × 128 image: too large"):
        open_eer(f.path, superres=2)


def test_index_and_range_validation(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=10)
    with open_eer(f.path, group=4) as ds:
        for bad in (3, -1, True, 1.0):
            with pytest.raises(InvalidRequestError):
                ds.read_plane(bad)  # type: ignore[arg-type]
            with pytest.raises(InvalidRequestError):
                ds.fraction_range(bad)  # type: ignore[arg-type]
        with pytest.raises(InvalidRequestError):
            ds.read_plane(0, 1, 0)
        for start, stop in ((-1, 3), (0, 11), (4, 4), (5, 2), (10, None), (0.0, 3), (0, True), (False, 3)):
            with pytest.raises(InvalidRequestError):
                ds.read_sum(start, stop)  # type: ignore[arg-type]


# ─── Metadata ─────────────────────────────────────────────────────────────────


def test_acquisition_metadata(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=12, shape=(32, 48), events=60)
    with open_eer(f.path, group=3) as ds:
        info = ds.info
    meta = info.metadata
    acquisition = meta["acquisition"]
    assert acquisition == {
        "acquisitionID": "20261110_101500_synthetic",  # not a number, whatever float() thinks of "_"
        "cameraName": "BM-Falcon",
        "commercialName": "Falcon 4i",
        "eerGainReference": "20261110_090000_EER_GainReference.gain",
        "exposureTime": 4.0,
        "hasFinalImage": False,
        "meanDoseRate": 12 * 60 / (32 * 48) / 4.0,
        "numberOfFrames": 12,
        "sensorImageHeight": 32,
        "sensorImageWidth": 48,
        "sensorPixelSize.height": 1.4e-05,
        "sensorPixelSize.width": 1.4e-05,
        "serialNumber": "004217",  # keeps its leading zeros
        "timestamp": "2026-11-10T10:15:00.123+01:00",
        "totalDose": 12 * 60 / (32 * 48),
    }
    assert type(acquisition["numberOfFrames"]) is int and type(acquisition["exposureTime"]) is float
    assert meta["acquisition_units"]["totalDose"] == "e/pixel"
    assert meta["acquisition_units"]["sensorPixelSize.width"] == "m"
    assert "numberOfFrames" not in meta["acquisition_units"]
    assert (meta["n_fractions"], meta["group"], meta["last_group_fractions"], meta["complete"]) == (
        12,
        3,
        3,
        True,
    )
    assert meta["compression_name"] == "EER, 7-bit run lengths" and meta["integrated_image"] is False
    dose = meta["dose_per_fraction"]
    assert dose["unit"] == "e/pixel" and dose["value"] == pytest.approx(60 / (32 * 48))
    # the detector pixel (14 µm) is not the specimen pixel size: only the frame time is calibrated
    assert not info.calibration.known and info.calibration.z is None
    assert info.calibration.t is not None and info.calibration.t.source is CalibrationSource.METADATA
    assert (info.calibration.t.value, info.calibration.t.unit) == (pytest.approx(1.0), "s")
    assert info.warnings == (next(w for w in info.warnings if "pixel size unknown" in w),)
    assert json.loads(json.dumps(info.to_json()))["metadata"]["acquisition"]["serialNumber"] == "004217"


def test_dose_from_the_dose_rate_and_missing_units(tmp_path: Path):
    by_rate = write_eer(tmp_path / "rate.eer", n_fractions=10, acquisition={"totalDose": None})
    with open_eer(by_rate.path) as ds:
        dose = ds.info.metadata["dose_per_fraction"]
        assert dose["unit"] == "e/pixel" and "meanDoseRate" in dose["source"]
        assert dose["value"] == pytest.approx(by_rate.n_events() / (32 * 48) / 10)
    bare = write_eer(
        tmp_path / "bare.eer", acquisition={"totalDose": 6.0, "exposureTime": 4.0, "meanDoseRate": None}
    )
    with open_eer(bare.path) as ds:  # values without units: reported, never completed by a guess
        assert ds.info.metadata["dose_per_fraction"] == {
            "value": 0.5,
            "unit": None,
            "source": "totalDose / number of fractions",
        }
        assert ds.info.calibration.t is None
    none = write_eer(tmp_path / "none.eer", acquisition={"totalDose": "n/a", "meanDoseRate": None})
    with open_eer(none.path) as ds:
        assert ds.info.metadata["dose_per_fraction"] is None
        assert ds.info.metadata["acquisition"]["totalDose"] == "n/a"


def test_fraction_count_differs_from_the_metadata(tmp_path: Path):
    short = write_eer(tmp_path / "short.eer", n_fractions=12, acquisition={"numberOfFrames": 20})
    with open_eer(short.path, group=4) as ds:
        assert ds.n_fractions == 12 and ds.info.metadata["complete"] is False
        assert any(
            "announce 20 fractions, the file holds 12: truncated or still" in w for w in ds.info.warnings
        )
        # exposure and dose are spread over the 20 fractions of the acquisition
        assert ds.info.calibration.t is not None and ds.info.calibration.t.value == pytest.approx(
            4.0 * 4 / 20
        )
        assert ds.info.metadata["dose_per_fraction"]["value"] == pytest.approx(
            short.acquisition["totalDose"][0] / 20
        )
        np.testing.assert_array_equal(ds.read_sum(), short.truth())
    longer = write_eer(tmp_path / "long.eer", n_fractions=12, acquisition={"numberOfFrames": 10})
    with open_eer(longer.path) as ds:
        assert ds.info.metadata["complete"] is True
        assert any(w.endswith("announce 10 fractions, the file holds 12") for w in ds.info.warnings)


@pytest.mark.parametrize(
    ("xml", "warning"),
    [
        (None, "no acquisition metadata"),
        (b"", "cannot be parsed"),
        (b"<metadata><item name='a'>1</item>", "cannot be parsed"),
        (b"\xff\xfe\x00garbage", "cannot be parsed"),
        (b'<?xml version="1.0" encoding="no-such-codec"?><metadata/>', "cannot be parsed"),
        (
            b'<!DOCTYPE m [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            b'<metadata><item name="x">&b;&b;&b;&b;</item></metadata>',
            "cannot be parsed",
        ),
    ],
)
def test_missing_or_unusable_metadata(tmp_path: Path, xml: bytes | None, warning: str):
    f = write_eer(tmp_path / "movie.eer", xml=xml)
    with open_eer(f.path) as ds:
        meta = ds.info.metadata
        assert meta["acquisition"] == {} and meta["acquisition_units"] == {}
        assert meta["dose_per_fraction"] is None and meta["complete"] is True
        assert ds.info.calibration.t is None
        assert any(warning in w for w in ds.info.warnings)
        np.testing.assert_array_equal(ds.read_sum(), f.truth())


def test_metadata_values_are_not_reinterpreted(tmp_path: Path):
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<metadata>\n'
        b' <item name="a">007</item><item name="b">1_000</item><item name="c">1e999</item>'
        b'<item name="d">nan</item><item name="e"> -2.5e-3 </item><item name="f">Yes</item>'
        b'<item name="g"></item><item>orphan</item><item name="h">+12</item><item name="i">0x10</item>'
        b'<item name="j" unit="\xc3\x85">1 &lt; 2 &amp; 3</item><group><item name="k">.5</item></group>'
        b"</metadata>\0\0"
    )
    f = write_eer(tmp_path / "movie.eer", xml=xml)
    with open_eer(f.path) as ds:
        assert ds.info.metadata["acquisition"] == {
            "a": "007", "b": "1_000", "c": "1e999", "d": "nan", "e": -0.0025, "f": True, "g": "", "h": 12,
            "i": "0x10", "j": "1 < 2 & 3", "k": 0.5,
        }  # fmt: skip
        assert ds.info.metadata["acquisition_units"] == {"j": "Å"}
        json.dumps(ds.info.to_json(), allow_nan=False)


def test_oversized_metadata_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(eer_module, "_MAX_XML_BYTES", 64)
    f = write_eer(tmp_path / "movie.eer")
    with open_eer(f.path) as ds:
        assert ds.info.metadata["acquisition"] == {}
        assert any("no acquisition metadata" in w for w in ds.info.warnings)


# ─── Reader and registry ──────────────────────────────────────────────────────


def test_reader_contract():
    assert isinstance(READER, Reader)
    assert (READER.name, READER.extensions) == ("eer", (".eer",))
    assert READER.priority > Reader.priority  # ahead of the generic TIFF reader
    assert "imagebio.io.readers.eer" in BUILTIN_READER_MODULES
    assert importlib.import_module("imagebio.io.readers.eer").READER is READER


def sniff(path: Path) -> bool:
    return READER.sniff(path, path.read_bytes()[:SNIFF_BYTES])


@pytest.mark.parametrize(("bigtiff", "byteorder"), CONTAINERS)
@pytest.mark.parametrize("encoding", [V0, V1, v2()], ids=encoding_id)
def test_sniff_reads_the_first_directory(tmp_path: Path, bigtiff: bool, byteorder: str, encoding: Encoding):
    kwargs = {"n_fractions": 2, "shape": (128, 128), "events": 3000, "encoding": encoding}
    in_head = write_eer(tmp_path / "head.eer", ifd_first=True, bigtiff=bigtiff, byteorder=byteorder, **kwargs)  # type: ignore[arg-type]
    assert in_head.layout.ifd_offsets[0] < 64 and sniff(in_head.path)
    assert READER.sniff(
        tmp_path / "gone.eer", in_head.path.read_bytes()[:SNIFF_BYTES]
    )  # no file access needed
    # libtiff order: the first directory comes after the first strip, beyond the sniffed bytes
    beyond = write_eer(tmp_path / "beyond.eer", bigtiff=bigtiff, byteorder=byteorder, **kwargs)  # type: ignore[arg-type]
    assert beyond.layout.ifd_offsets[0] > SNIFF_BYTES and sniff(beyond.path)
    assert not READER.sniff(tmp_path / "gone.eer", beyond.path.read_bytes()[:SNIFF_BYTES])


def test_sniff_refuses_everything_else(tmp_path: Path):
    image = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    for name, kwargs in (
        ("plain.tif", {}),
        ("big.tif", {"bigtiff": True}),
        ("lzw.tif", {"compression": "lzw"}),
        ("large.tif", {"bigtiff": True, "byteorder": ">"}),
    ):
        data = np.tile(image, (4, 4)) if name == "large.tif" else image
        tifffile.imwrite(tmp_path / name, data, **kwargs)  # type: ignore[arg-type]
        assert not sniff(tmp_path / name)
    # a private tag 65001 on an ordinary single-page TIFF does not make it an EER file
    tifffile.imwrite(tmp_path / "tagged.tif", image, extratags=[(65001, 7, 10, b"<metadata>", True)])
    assert not sniff(tmp_path / "tagged.tif")
    heads = (
        b"", b"II", b"II*\0", b"II*\0\0\0\0\0", b"MM\0+\0\x08\0\0", b"II+\0\x08\0\0\0" + b"\xff" * 8,
        b"II*\0\xff\xff\xff\xff", b"MM\0*" + bytes(60), b"\x89PNG\r\n\x1a\n" + bytes(64), bytes(4096),
    )  # fmt: skip
    for head in heads:
        path = tmp_path / "junk.eer"
        path.write_bytes(head)
        assert READER.sniff(path, head) is False
    assert READER.sniff(tmp_path, b"II*\0" + bytes(4092)) is False  # a directory: no exception either
    valid = write_eer(tmp_path / "movie.eer")
    assert sniff(valid.path)
    assert not sniff(patched(valid, {valid.layout.first_ifd_field: struct.pack("<Q", 1 << 63)}))
    assert not sniff(patched(valid, {valid.layout.ifd_offsets[0]: struct.pack("<Q", 1 << 40)}))


def test_registry_opens_eer_by_content(tmp_path: Path):
    registry = Registry([READER])
    f = write_eer(tmp_path / "movie.eer")
    misnamed = tmp_path / "movie.tiff"
    misnamed.write_bytes(f.path.read_bytes())
    for path in (f.path, misnamed):
        with registry.open(path, {"group": 6}) as ds:
            assert isinstance(ds, EerDataset) and ds.info.reader == "eer"
            np.testing.assert_array_equal(ds.read_plane(1), f.truth(6, 12))
    assert registry.supports("FoilHole_1_Data_2_3_20261110_101500_EER.eer")
    tifffile.imwrite(tmp_path / "plain.eer", np.zeros((8, 8), np.uint8))
    with pytest.raises(CorruptFileError, match="not an EER file"):
        registry.open(tmp_path / "plain.eer")


# ─── Files that must be refused ───────────────────────────────────────────────


def test_non_eer_files_are_refused(tmp_path: Path):
    image = np.arange(64, dtype=np.uint8).reshape(8, 8)
    tifffile.imwrite(tmp_path / "plain.eer", image)
    assert "TIFF compression 1" in rejected(tmp_path / "plain.eer", "not an EER file")
    tifffile.imwrite(tmp_path / "lzw.eer", image, compression="lzw", bigtiff=True)
    assert "TIFF compression 5" in rejected(tmp_path / "lzw.eer", "not an EER file")
    tifffile.imwrite(tmp_path / "stack.eer", np.stack([image] * 3), photometric="minisblack")
    rejected(tmp_path / "stack.eer", "not an EER file")  # an uncompressed first page is not enough
    for name, content in (
        ("empty.eer", b""),
        ("text.eer", b"hello world, not a TIFF at all"),
        ("short.eer", b"II*"),
    ):
        (tmp_path / name).write_bytes(content)
        rejected(tmp_path / name, "no TIFF signature")
    with pytest.raises(NotFoundError) as missing:
        READER.open(tmp_path / "missing.eer")
    assert str(tmp_path) not in missing.value.public_message
    (tmp_path / "dir.eer").mkdir()
    with pytest.raises(UnsupportedFormatError):
        READER.open(tmp_path / "dir.eer")


@pytest.mark.parametrize("ifd_first", [False, True])
@pytest.mark.parametrize("bigtiff", [True, False])
def test_truncated_files_never_open(tmp_path: Path, ifd_first: bool, bigtiff: bool):
    """A copy cut anywhere (a movie still being written or transferred) is an error, not a shorter movie."""
    f = write_eer(tmp_path / "movie.eer", n_fractions=6, ifd_first=ifd_first, bigtiff=bigtiff)
    data = f.path.read_bytes()
    cut_file = tmp_path / "cut.eer"
    cuts = sorted(
        {*range(0, len(data), 23), *f.layout.ifd_offsets, *(o for page in f.layout.strips for o, _ in page)}
    )
    cuts += [len(data) - 1, len(data) - 2]
    for cut in cuts:
        cut_file.write_bytes(data[:cut])
        message = rejected(cut_file)
        if cut > 16:
            assert "truncated or still being written" in message, (cut, message)
    cut_file.write_bytes(data)
    open_eer(cut_file).close()


def test_broken_directory_chain(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=6)
    layout = f.layout
    pointer = lambda value: struct.pack(layout.offset_format, value)  # noqa: E731
    size = f.path.stat().st_size
    rejected(
        patched(f, {layout.next_fields[2]: pointer(size + 100)}), "image directory 3 lies beyond the end"
    )
    rejected(patched(f, {layout.next_fields[2]: pointer(size - 3)}), "image directory 3 lies beyond the end")
    rejected(patched(f, {layout.next_fields[2]: pointer(2**64 - 1)}), "image directory 3 lies beyond the end")
    rejected(patched(f, {layout.next_fields[4]: pointer(layout.ifd_offsets[1])}), "form a loop")
    rejected(patched(f, {layout.next_fields[5]: pointer(layout.ifd_offsets[5])}), "form a loop")
    rejected(patched(f, {layout.next_fields[1]: pointer(f.strip(0)[0])}))  # a "directory" inside event data
    rejected(patched(f, {layout.first_ifd_field: pointer(0)}), "holds no EER fraction")
    rejected(
        patched(f, {layout.ifd_offsets[3]: struct.pack("<Q", 0)}),
        r"image directory 3 is invalid \(0 entries\)",
    )
    rejected(patched(f, {layout.ifd_offsets[3]: struct.pack("<Q", 5000)}), "image directory 3 is invalid")
    with open_eer(patched(f, {layout.next_fields[2]: pointer(0)})) as ds:
        # a chain that ends early is a valid, shorter file: only the metadata can tell
        assert ds.n_fractions == 3 and ds.info.metadata["complete"] is False
        assert any("announce 6 fractions, the file holds 3" in w for w in ds.info.warnings)


def test_inconsistent_or_absurd_pages(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=5)
    short, long_ = (lambda v: struct.pack("<H", v)), (lambda v: struct.pack("<I", v))
    rejected(
        patched(f, {f.value_field(3, COMPRESSION): short(5)}),
        r"page 3 is not an EER fraction \(TIFF compression 5\)",
    )
    rejected(
        patched(f, {f.value_field(3, COMPRESSION): short(65000)}), "fraction 3 differs from the first one"
    )
    rejected(patched(f, {f.value_field(2, IMAGE_WIDTH): long_(47)}), "fraction 2 differs from the first one")
    rejected(patched(f, {f.value_field(0, COMPRESSION): short(7)}), r"not an EER file \(TIFF compression 7\)")
    everywhere = lambda tag, raw: {f.value_field(k, tag): raw for k in range(5)}  # noqa: E731
    rejected(patched(f, everywhere(IMAGE_WIDTH, long_(0))), "implausible frame size 0 × 32")
    rejected(patched(f, everywhere(IMAGE_LENGTH, long_(2**31))), "implausible frame size")
    tracemalloc.start()
    try:  # a few kilobytes cannot hold 8192 × 8192 frames: refused before anything is allocated
        rejected(
            patched(f, everywhere(IMAGE_WIDTH, long_(8192)) | everywhere(IMAGE_LENGTH, long_(8192))),
            r"cannot encode a 8192 × 8192 frame",
        )
        rejected(patched(f, everywhere(IMAGE_WIDTH, long_(60000)) | everywhere(IMAGE_LENGTH, long_(60000))))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1 << 20
    # tags of the wrong type or count
    rejected(
        patched(f, {f.value_field(1, IMAGE_WIDTH) - 6: short(12)}), "TIFF tag 256 has an unexpected type"
    )
    rejected(
        patched(f, {f.value_field(1, STRIP_OFFSETS) - 8: struct.pack("<Q", 2)}),
        "fraction 1 lies beyond|strip table|TIFF tag 273",
    )
    bad_bits = write_eer(tmp_path / "bits.eer", encoding=v2(7, 2, 2))
    for tag, value in ((65007, 3), (65007, 15), (65008, 0), (65009, 5)):
        fields = {bad_bits.value_field(k, tag): long_(value) for k in range(12)}
        rejected(patched(bad_bits, fields, "badbits.eer"), "unsupported EER code sizes")


def test_strips_outside_the_file(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=4)
    size = f.path.stat().st_size
    q = lambda v: struct.pack("<Q", v)  # noqa: E731
    offset, count = f.value_field(2, STRIP_OFFSETS), f.value_field(2, STRIP_BYTE_COUNTS)
    rejected(patched(f, {offset: q(size - 10)}), "fraction 2 lies beyond the end of the file")
    rejected(patched(f, {offset: q(2**63)}), "fraction 2 lies beyond the end of the file")
    rejected(patched(f, {offset: q(3)}), "fraction 2 lies beyond the end of the file")  # inside the header
    rejected(patched(f, {count: q(0)}), "fraction 2 lies beyond the end of the file")
    rejected(patched(f, {count: q(2**62)}), "fraction 2 lies beyond the end of the file")
    rejected(
        patched(f, {offset: q(16), count: q(size - 16)}),
        "fraction 2: .* bytes cannot encode a 48 × 32 frame|overlap each other",
    )
    rejected(patched(f, {count: q(4)}), "fraction 2: 4 bytes cannot encode")
    # every fraction pointing at the same bytes: more event data than the file holds
    one = f.strip(0)
    aliased = write_eer(tmp_path / "aliased.eer", n_fractions=60, shape=(64, 64), events=2)
    fields = {}
    for k in range(60):
        fields[aliased.value_field(k, STRIP_OFFSETS)] = q(16)
        fields[aliased.value_field(k, STRIP_BYTE_COUNTS)] = q(aliased.path.stat().st_size // 30)
    assert one[1] > 0
    rejected(patched(aliased, fields, "alias.eer"), "overlap each other")


def test_file_truncated_after_opening(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=6)
    with open_eer(f.path, group=2) as ds:
        with f.path.open("r+b") as handle:
            handle.truncate(f.strip(4)[0] + 10)
        np.testing.assert_array_equal(ds.read_plane(0), f.truth(0, 2))
        with pytest.raises(
            CorruptFileError, match=r"movie\.eer: fraction 4 is incomplete \(the file was truncated\)"
        ):
            ds.read_plane(2)
        with pytest.raises(CorruptFileError, match=r"fraction [45] is incomplete"):
            ds.read_sum()


def damaged_stream(f: EerFile, fraction: int, kind: str) -> bytes:
    """The strip of ``fraction`` after a typical accident; same length, so the file stays well formed."""
    offset, count = f.strip(fraction)
    stream = f.path.read_bytes()[offset : offset + count]
    half = count // 2
    if kind == "zero-filled":  # a hole left by an interrupted copy
        return bytes(count)
    if kind == "zero-tail":
        return stream[:half] + bytes(count - half)
    if kind == "ones":
        return b"\xff" * count
    if kind == "ones-tail":
        return stream[:half] + b"\xff" * (count - half)
    if kind == "shifted":  # one byte lost early on
        return stream[:9] + stream[10:] + b"\0"
    if kind == "other-frame-size":
        bigger = random_events(np.random.default_rng(5), 40 * 48, 30)
        return encode_fraction(bigger, 40 * 48).ljust(count, b"\0")[:count]
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["zero-filled", "zero-tail", "ones", "ones-tail", "shifted", "other-frame-size"]
)
@pytest.mark.parametrize("superres", [0, 2])
def test_corrupt_fraction_is_an_error_not_an_image(
    tmp_path: Path, decode_mode: str, kind: str, superres: int
):
    f = write_eer(tmp_path / "movie.eer", n_fractions=12, events=120)
    damaged = damaged_stream(f, 7, kind)
    with pytest.raises(ValueError):  # the strict reference refuses the stream too
        decode_fraction_naive(damaged, f.shape)
    path = patched(f, {f.strip(7)[0]: damaged})
    with open_eer(path, group=3, superres=superres) as ds:  # the container is intact: it opens
        np.testing.assert_array_equal(ds.read_plane(1), f.truth(3, 6, superres))
        np.testing.assert_array_equal(ds.read_plane(3), f.truth(9, 12, superres))
        np.testing.assert_array_equal(ds.read_sum(8, 12), f.truth(8, 12, superres))
        for read in (lambda: ds.read_plane(2), ds.read_sum, lambda: ds.read_sum(7, 8)):
            with pytest.raises(CorruptFileError, match=r"patched\.eer: fraction 7 is corrupt") as e:
                read()
            assert str(tmp_path) not in e.value.public_message
        np.testing.assert_array_equal(ds.read_plane(0), f.truth(0, 3, superres))  # still usable afterwards


def test_undershoot_that_lands_by_chance_is_still_refused(tmp_path: Path, decode_mode: str):
    """A stream cut a multiple of 127 pixels short: all-ones padding alone would land on the last pixel."""
    ny, nx = 4, 127
    codes = [(3, 7), (5, 4), (127, 7), (10, 7), (2, 4)]  # ends on pixel 142: 366 = 2 × 127 + 112 short
    for missing in (0, 127 - 112, 2 * 127 - 112):  # pixels until a whole number of max-skips remains
        stream = even_length(pack_codes([*codes, (missing, 7), (1, 4)] if missing else codes))
        f = write_eer(tmp_path / "movie.eer", n_fractions=2, shape=(ny, nx), events=10, streams={1: stream})
        with open_eer(f.path, group=1) as ds:
            np.testing.assert_array_equal(ds.read_plane(0), f.truth(0, 1))
            with pytest.raises(CorruptFileError, match="fraction 1 is corrupt"):
                ds.read_plane(1)


@pytest.fixture(scope="module")
def fuzz_source(tmp_path_factory: pytest.TempPathFactory) -> EerFile:
    return write_eer(
        tmp_path_factory.mktemp("fuzz") / "source.eer", n_fractions=6, shape=(16, 24), events=40, seed=9
    )


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_damaged_strip_bytes(fuzz_source: EerFile, data: st.DataObject):
    """Whatever happens to the bytes of a strip, the result is an error or exactly the events the
    strict reference decoder finds in it: never an exception, never an image of something else."""
    f = fuzz_source
    fraction = data.draw(st.integers(0, 5), label="fraction")
    offset, count = f.strip(fraction)
    stream = bytearray(f.path.read_bytes()[offset : offset + count])
    kind = data.draw(st.sampled_from(["flip", "bytes", "fill", "random"]), label="kind")
    if kind == "flip":
        for position in data.draw(
            st.lists(st.integers(0, count * 8 - 1), min_size=1, max_size=3), label="bits"
        ):
            stream[position // 8] ^= 1 << (position % 8)
    elif kind == "bytes":
        for position, value in data.draw(
            st.lists(st.tuples(st.integers(0, count - 1), st.integers(0, 255)), min_size=1, max_size=4),
            label="bytes",
        ):
            stream[position] = value
    elif kind == "fill":
        start = data.draw(st.integers(0, count - 1), label="start")
        value = data.draw(st.sampled_from([0, 0xFF, 0x7F, 0x55]), label="value")
        stream[start:] = bytes([value]) * (count - start)
    else:
        stream[:] = data.draw(st.binary(min_size=count, max_size=count), label="stream")
    try:
        strict = decode_fraction_naive(bytes(stream), f.shape)
    except ValueError:
        strict = None
    path = patched(f, {offset: bytes(stream)}, "fuzzed.eer")
    with open_eer(path, group=1) as ds:
        try:
            plane = ds.read_plane(fraction)
        except CorruptFileError as e:
            assert strict is None, "a stream that is valid for the reference was refused"
            assert "fuzzed.eer: fraction" in e.public_message
            event("refused")
            return
        event("accepted, valid" if strict is not None else "accepted with zero padding")
        if strict is None:  # valid only if the strip is taken as zero-padded: then that is what is shown
            strict = decode_fraction_naive(bytes(stream) + bytes(4), f.shape)
        np.testing.assert_array_equal(plane, strict)
        other = (fraction + 1) % 6
        np.testing.assert_array_equal(ds.read_plane(other), f.truth(other, other + 1))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_damaged_container_bytes(fuzz_source: EerFile, data: st.DataObject):
    """Random damage to the header, the directories or the tag data: a clean error or a readable file."""
    f = fuzz_source
    raw = f.path.read_bytes()
    in_strips = {i for page in f.layout.strips for o, n in page for i in range(o, o + n)}
    structure = [i for i in range(len(raw)) if i not in in_strips]
    patches = {
        structure[index]: bytes([value])
        for index, value in data.draw(
            st.lists(
                st.tuples(
                    st.integers(0, len(structure) - 1), st.sampled_from([0, 1, 2, 7, 8, 16, 0x7F, 0x80, 0xFF])
                ),
                min_size=1,
                max_size=4,
            ),
            label="patches",
        )
    }
    path = patched(f, patches, "fuzzed.eer")
    assert isinstance(sniff(path), bool)
    try:
        with open_eer(path, group=2) as ds:
            assert ds.info.size_x <= eer_module.MAX_SIDE and ds.info.size_y <= eer_module.MAX_SIDE
            for t in range(min(ds.info.size_t, 4)):
                plane = ds.read_plane(t)
                assert plane.shape == (ds.info.size_y, ds.info.size_x) and plane.dtype == np.uint16
            json.dumps(ds.info.to_json(), allow_nan=False)
            event("opened")
    except ImageBioError as e:
        assert isinstance(e, CorruptFileError), e
        assert str(path.parent) not in e.public_message
        event("refused")


# ─── Concurrency and resources ────────────────────────────────────────────────


def test_concurrent_reads(tmp_path: Path, decode_mode: str):
    f = write_eer(tmp_path / "movie.eer", n_fractions=48, shape=(64, 64), events=300)
    expected = [f.truth(8 * t, 8 * t + 8) for t in range(6)]
    with open_eer(f.path, group=8) as ds:

        def work(seed: int) -> None:
            rng = np.random.default_rng(seed)
            for t in rng.permutation(6).tolist():
                np.testing.assert_array_equal(ds.read_plane(t), expected[t])
            np.testing.assert_array_equal(ds.read_sum(), f.truth())

        with ThreadPoolExecutor(8) as pool:
            for result in [pool.submit(work, seed) for seed in range(16)]:
                result.result()


def test_close_is_idempotent_and_safe_under_reads(tmp_path: Path):
    f = write_eer(tmp_path / "movie.eer", n_fractions=40, shape=(64, 64), events=300)
    expected = f.truth()
    ds = open_eer(f.path, group=8)
    started = threading.Barrier(5)
    outcomes: list[str] = []

    def work() -> None:
        started.wait()
        for _ in range(20):
            try:
                np.testing.assert_array_equal(ds.read_sum(), expected)  # complete and right, or refused
                outcomes.append("read")
            except ValueError as e:
                assert "closed" in str(e)
                outcomes.append("closed")

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    started.wait()
    time.sleep(0.01)
    ds.close()
    ds.close()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 80 and outcomes[-1] == "closed"
    with pytest.raises(ValueError, match="closed"):
        ds.read_plane(0)


def test_a_read_holds_a_few_frames_not_the_movie(tmp_path: Path):
    ny = nx = 256
    f = write_eer(tmp_path / "movie.eer", n_fractions=96, shape=(ny, nx), events=2000)
    with open_eer(f.path, group=48) as ds:
        ds.read_plane(0)  # thread pool and decoder check are set up
        tracemalloc.start()
        try:
            total = ds.read_sum()
            plane = ds.read_plane(1)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    np.testing.assert_array_equal(total, f.truth())
    np.testing.assert_array_equal(plane, f.truth(48, 96))
    frame = ny * nx
    # both results, one uint8 frame per thread (4 at most), strips: far from 96 decoded frames
    assert peak < total.nbytes + plane.nbytes + 6 * frame, f"peak {peak / frame:.1f} frames"


@pytest.mark.perf
@pytest.mark.timeout(300)
def test_perf_4k_group(tmp_path: Path):
    """Budget for a group of 4096 x 4096 fractions at 0.015 e/px (run with -m perf).

    Eight fractions only: the pure-Python fixture encoder needs about 3 s per 4K fraction.
    """
    n = 8
    f = write_eer(tmp_path / "4k.eer", n_fractions=n, shape=(4096, 4096), events=250_000)
    with open_eer(f.path, group=n) as ds:
        ds.read_plane(0)
        started = time.perf_counter()
        plane = ds.read_plane(0)
        elapsed = time.perf_counter() - started
    assert plane.sum() == f.n_events()
    print(f"\n4096 x 4096, {n} fractions: {elapsed * 1000:.0f} ms ({elapsed * 1000 / n:.1f} ms per fraction)")
    assert elapsed < 0.025 * n  # 25 ms per fraction: several times the measured cost
