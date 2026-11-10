from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from imagebio.core.errors import InvalidRequestError, NotFoundError
from imagebio.core.model import (
    UNKNOWN_PIXEL,
    AxisCalibration,
    Calibration,
    CalibrationSource,
    ChannelInfo,
    Dataset,
    DatasetInfo,
    FileId,
    Plane,
    StackKind,
    json_safe,
    normalize_unit,
)


def test_units_are_normalised():
    assert normalize_unit("A") == normalize_unit("angstrom") == normalize_unit("Å") == "Å"
    assert normalize_unit("um") == normalize_unit("μm") == normalize_unit("micron") == "µm"
    assert normalize_unit("") == normalize_unit(None) == normalize_unit("pixels") == "px"
    assert normalize_unit("furlong") == "furlong"


def test_axis_calibration_conversion_and_provenance():
    a = AxisCalibration(0.83, "A", CalibrationSource.HEADER)
    assert a.unit == "Å" and a.known and a.to_angstrom() == pytest.approx(0.83)
    assert AxisCalibration(0.1, "um", CalibrationSource.METADATA).to_angstrom() == pytest.approx(1000)
    assert AxisCalibration(1 / 300, "inch", CalibrationSource.METADATA).to_angstrom() == pytest.approx(
        2.54e8 / 300
    )
    # unknown pixel size: never a physical value
    assert not UNKNOWN_PIXEL.known and UNKNOWN_PIXEL.to_angstrom() is None
    assert AxisCalibration(2.0, "Å", CalibrationSource.DEFAULT).to_angstrom() is None
    assert AxisCalibration(1.5, "s", CalibrationSource.METADATA).to_angstrom() is None
    for bad in (0, -1, math.nan, math.inf):
        with pytest.raises(ValueError):
            AxisCalibration(bad, "Å", CalibrationSource.HEADER)


def test_binned_calibration_scales_x_and_y_only():
    z = AxisCalibration(4.0, "Å", CalibrationSource.HEADER)
    cal = Calibration.isotropic(1.1, "Å", CalibrationSource.HEADER)
    cal = Calibration(cal.x, cal.y, z=z)
    b = cal.binned(4)
    assert b.x.value == pytest.approx(4.4) and b.y.value == pytest.approx(4.4) and b.z == z
    assert b.x.source is CalibrationSource.HEADER
    assert Calibration().known is False and cal.known is True
    with pytest.raises(ValueError):
        cal.binned(0)


def test_dataset_info_validation_and_json():
    info = DatasetInfo(
        format="mrc",
        reader="mrc",
        size_x=8,
        size_y=4,
        size_t=3,
        dtype=np.dtype(">f4"),
        stack_kind=StackKind.MOVIE,
        metadata={"n": np.int32(3), "f": np.float32("nan"), "b": b"MAP ", "t": (1, 2), "p": Path("x")},
        warnings=["pixel size unknown"],
    )
    assert info.dtype == np.dtype("float32") and info.dtype.isnative
    assert info.shape == (3, 1, 1, 4, 8) and info.plane_count == 3 and info.plane_nbytes == 128
    assert info.metadata == {"n": 3, "f": None, "b": "MAP ", "t": [1, 2], "p": "x"}
    js = json.loads(json.dumps(info.to_json(), allow_nan=False))
    assert js["shape"] == {"t": 3, "c": 1, "z": 1, "y": 4, "x": 8}
    assert js["dtype"] == "float32" and js["stack_kind"] == "movie"
    assert js["calibration"]["x"] == {
        "value": 1.0,
        "unit": "px",
        "source": "default",
        "known": False,
        "angstrom": None,
    }

    with pytest.raises(ValueError):
        DatasetInfo(format="x", reader="x", size_x=0, size_y=4, dtype=np.dtype("u1"))
    with pytest.raises(ValueError):
        DatasetInfo(format="x", reader="x", size_x=4, size_y=4, dtype=np.dtype("complex64"))
    with pytest.raises(ValueError):
        DatasetInfo(format="x", reader="x", size_x=4, size_y=4, dtype=np.dtype("u1"), rgb=True)
    with pytest.raises(ValueError):
        DatasetInfo(
            format="x",
            reader="x",
            size_x=4,
            size_y=4,
            size_c=2,
            dtype=np.dtype("u1"),
            channels=(ChannelInfo("a"),),
        )


class _Fake(Dataset):
    def __init__(self, plane: Plane, **kw: int) -> None:
        info = DatasetInfo(format="fake", reader="fake", size_x=3, size_y=2, dtype=np.dtype("uint16"), **kw)
        super().__init__(Path("/nowhere/fake"), info)
        self.plane = plane
        self.closed = 0

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        return self.plane

    def close(self) -> None:
        self.closed += 1


def test_read_plane_enforces_the_contract():
    native = np.arange(6, dtype="uint16").reshape(2, 3)
    with _Fake(native, size_t=2) as ds:
        assert ds.read_plane(1) is native  # no copy when already conforming
        for bad in ((2, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1), (True, 0, 0), (0.0, 0, 0)):
            with pytest.raises(InvalidRequestError):
                ds.read_plane(*bad)  # type: ignore[arg-type]
    assert ds.closed == 1

    swapped = native.astype(">u2")
    fortran = np.asfortranarray(native)
    for odd in (swapped, fortran, native.astype("float64")):
        out = _Fake(odd).read_plane()
        assert out.dtype == np.dtype("uint16") and out.dtype.isnative and out.flags.c_contiguous
        assert np.array_equal(out, native)
    with pytest.raises(RuntimeError):
        _Fake(native.T.copy()).read_plane()


def test_file_id_changes_with_content(tmp_path: Path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"1234")
    a = FileId.of(p)
    assert a == FileId.of(p) and a.size == 4
    p.write_bytes(b"12345")
    b = FileId.of(p)
    assert a != b and a.etag != b.etag
    with pytest.raises(NotFoundError) as e:
        FileId.of(tmp_path / "missing.bin")
    assert str(tmp_path) not in str(e.value)  # no server path in public messages


def test_json_safe():
    assert json_safe(
        {
            "a": {
                1,
            },
            2: np.arange(2),
            "e": StackKind.MOVIE,
        }
    ) == {"a": [1], "2": [0, 1], "e": "movie"}
    assert json_safe(float("inf")) is None and json_safe(np.float64(1.5)) == 1.5
