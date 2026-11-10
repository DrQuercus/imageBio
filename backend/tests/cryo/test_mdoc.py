"""SerialEM mdoc files, written here as SerialEM and Tomo5 write them."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from imagebio.core.errors import CorruptFileError
from imagebio.cryo.mdoc import parse_mdoc, read_mdoc

TILT_SERIES = b"""PixelSpacing = 2.14
Voltage = 300
ImageFile = Position_1.mrc
ImageSize = 4096 4096
DataMode = 1

[T = SerialEM: Digitized on Krios                 10-Nov-26  10:15:00    ]

[T =     Tilt axis angle = 84.8, binning = 1  spot = 5  camera = 0]

[ZValue = 0]
TiltAngle = -0.0100708
StagePosition = 123.456 -45.5
StageZ = -12.75
Magnification = 64000
Intensity = 0.105
ExposureDose = 3.05
PixelSpacing = 2.14
SpotSize = 5
Defocus = -3.96
TargetDefocus = -4
ImageShift = 0.12 -0.34
RotationAngle = 174.8
ExposureTime = 1.2
Binning = 1
MinMaxMean = 12 1890 412.5
PriorRecordDose = 0
SubFramePath = X:\\DoseFractions\\session\\Position_1_001_0.0.tif
NumSubFrames = 8
FrameDosesAndNumber = 0.38 8
DateTime = 10-Nov-26  10:15:07

[ZValue = 1]
TiltAngle = 3.00
StagePosition = 123.5 -45.4
ExposureDose = 3.05
PriorRecordDose = 3.05
DateTime = 10-Nov-2026 10:15:31
SubFramePath = /mnt/frames/Position_1_002_3.0.tif
"""


def test_global_items_titles_and_sections() -> None:
    doc = parse_mdoc(TILT_SERIES)
    assert doc.header == {
        "PixelSpacing": "2.14",
        "Voltage": "300",
        "ImageFile": "Position_1.mrc",
        "ImageSize": "4096 4096",
        "DataMode": "1",
    }
    assert len(doc.titles) == 2 and doc.titles[0].startswith("SerialEM: Digitized on Krios")
    assert [(s.kind, s.value) for s in doc.sections] == [("ZValue", "0"), ("ZValue", "1")]
    assert doc.images() == doc.sections
    assert doc.pixel_size() == 2.14


def test_values_convert_on_demand() -> None:
    first = parse_mdoc(TILT_SERIES).sections[0]
    assert first.number("TiltAngle") == pytest.approx(-0.0100708)
    assert first.numbers("MinMaxMean") == [12.0, 1890.0, 412.5]
    assert first.number("MinMaxMean") == 12.0
    assert first.text("SubFramePath") == r"X:\DoseFractions\session\Position_1_001_0.0.tif"
    assert (
        first.number("SubFramePath") is None
        and first.number("Absent") is None
        and first.text("Absent") is None
    )


def test_summary_gives_fixed_units_and_file_names_only() -> None:
    first, second = parse_mdoc(TILT_SERIES).sections
    assert first.summary() == {
        "tilt_angle": pytest.approx(-0.0100708),
        "pixel_size": 2.14,
        "defocus": -3.96,
        "target_defocus": -4.0,
        "dose": 3.05,
        "exposure_time": 1.2,
        "magnification": 64000.0,
        "prior_dose": 0.0,
        "frames": 8,
        "stage": {"x": 123.456, "y": -45.5, "z": -12.75},
        "acquired_at": datetime(2026, 11, 10, 10, 15, 7).timestamp(),
        "frames_file": "Position_1_001_0.0.tif",
    }
    assert second.summary()["frames_file"] == "Position_1_002_3.0.tif"
    assert second.summary()["acquired_at"] == datetime(2026, 11, 10, 10, 15, 31).timestamp()
    assert "defocus" not in second.summary() and second.summary()["stage"] == {"x": 123.5, "y": -45.4}


def test_frame_sets_comments_and_odd_lines() -> None:
    doc = parse_mdoc(
        b"# a comment\r\n\r\n[FrameSet = 0]\r\nSubFramePath = D:\\frames\\a.tif\r\nNumSubFrames = 40\r\n"
        b"garbage line without equal sign\r\nDateTime = someday\r\n[MontSection = 0]\r\nPieceCoordinates = 0 0 0\r\n"
    )
    assert [s.kind for s in doc.sections] == ["FrameSet", "MontSection"]
    frames = doc.images()
    assert len(frames) == 1 and frames[0].summary() == {"frames": 40, "frames_file": "a.tif"}
    assert frames[0].acquired_at() is None
    assert doc.pixel_size() is None


def test_a_file_still_being_written_is_read_up_to_its_last_line() -> None:
    cut = TILT_SERIES[: TILT_SERIES.index(b"[ZValue = 1]") + 30]  # in the middle of a line
    doc = parse_mdoc(cut)
    assert len(doc.sections) == 2
    assert doc.sections[0].number("NumSubFrames") == 8


@pytest.mark.parametrize("data", [b"", b"\n\n# only comments\n", b"MRC\0\0binary\0data = 1", b"no item here"])
def test_what_is_not_an_mdoc_is_refused(data: bytes) -> None:
    with pytest.raises(CorruptFileError):
        parse_mdoc(data, "x.mdoc")


def test_reading_from_a_file_never_leaks_its_path(tmp_path: Path) -> None:
    path = tmp_path / "Position_1.mrc.mdoc"
    path.write_bytes(TILT_SERIES)
    assert len(read_mdoc(path).sections) == 2
    with pytest.raises(CorruptFileError) as error:
        read_mdoc(tmp_path / "missing.mdoc")
    assert str(tmp_path) not in str(error.value)
