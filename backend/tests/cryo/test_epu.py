"""EPU image XML: a file written here from the documented structure (namespaces included)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from imagebio.core.errors import CorruptFileError
from imagebio.cryo.epu import EpuImageMetadata, exposure_name, parse_epu_xml, read_epu_xml

SO = "http://schemas.datacontract.org/2004/07/Fei.SharedObjects"
ARRAYS = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
DRAWING = "http://schemas.datacontract.org/2004/07/System.Drawing"
TYPES = "http://schemas.datacontract.org/2004/07/Fei.Types"


def pair(key: str, value: str, kind: str = "double") -> str:
    return (
        f'<a:KeyValueOfstringanyType><a:Key>{key}</a:Key><a:Value i:type="b:{kind}" '
        f'xmlns:b="http://www.w3.org/2001/XMLSchema">{value}</a:Value></a:KeyValueOfstringanyType>'
    )


def epu_xml(*, gain: str = r"Z:\TemScripting\EF-Falcon\20261110_090000_EER_GainReference.gain") -> bytes:
    custom = "".join(
        [
            pair("AppliedDefocus", "-1.6E-06"),
            pair("Dose", "4.25E+21"),  # electrons per square metre: 42.5 e/Å²
            pair("DoseOnCamera", "1.9E+21"),
            pair("Detectors[EF-Falcon].EerGainReference", gain, "string"),
            pair("Detectors[EF-Falcon].FrameRate", "248.0"),
            pair("PhasePlateUsed", "false", "boolean"),
        ]
    )
    specific = pair("NumberOffractions", "40", "int") + pair("ElectronCountingEnabled", "true", "boolean")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<MicroscopeImage xmlns="{SO}" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <CustomData xmlns:a="{ARRAYS}">{custom}</CustomData>
  <microscopeData>
    <acquisition>
      <acquisitionDateTime>2026-11-10T10:15:00.1234567+01:00</acquisitionDateTime>
      <beamTilt i:nil="true"/>
      <camera>
        <Binning xmlns:a="{DRAWING}"><a:x>1</a:x><a:y>1</a:y></Binning>
        <CameraSpecificInput xmlns:a="{ARRAYS}">{specific}</CameraSpecificInput>
        <ExposureTime>4.01</ExposureTime>
        <Name>EF-Falcon</Name>
      </camera>
    </acquisition>
    <core><ApplicationSoftware>EPU</ApplicationSoftware><ApplicationSoftwareVersion>3.6.0.6389</ApplicationSoftwareVersion></core>
    <gun><AccelerationVoltage>300000</AccelerationVoltage></gun>
    <instrument><ComputerName>TITAN</ComputerName><InstrumentID>3593</InstrumentID></instrument>
    <optics>
      <BeamShift xmlns:a="{TYPES}"><a:_x>0.0125</a:_x><a:_y>-0.004</a:_y></BeamShift>
      <ImageShift xmlns:a="{TYPES}"><a:_x>0</a:_x><a:_y>0</a:_y></ImageShift>
      <SpotIndex>5</SpotIndex>
      <TemMagnification><NominalMagnification>96000</NominalMagnification></TemMagnification>
    </optics>
    <stage><Position><A>0.0349066</A><B>0</B><X>0.000123</X><Y>-4.5E-05</Y><Z>1.2E-05</Z></Position></stage>
  </microscopeData>
  <SpatialScale><pixelSize>
    <x><numericValue>8.3E-11</numericValue><unit><_x003C_Name_x003E_k__BackingField>Meter</_x003C_Name_x003E_k__BackingField></unit></x>
    <y><numericValue>8.3E-11</numericValue></y>
  </pixelSize></SpatialScale>
  <uniqueID>abc</uniqueID>
</MicroscopeImage>""".encode()


def test_the_documented_fields_are_read_in_usable_units() -> None:
    m = parse_epu_xml(epu_xml())
    assert m.pixel_size == pytest.approx(0.83)  # Å
    assert m.dose == pytest.approx(42.5)  # e/Å²
    assert m.applied_defocus == pytest.approx(-1.6)  # µm
    assert m.exposure_time == pytest.approx(4.01)
    assert m.detector == "EF-Falcon" and m.binning == 1 and m.fractions == 40
    assert m.magnification == 96000 and m.voltage == pytest.approx(300.0) and m.spot_size == 5
    assert m.stage == pytest.approx({"x": 123.0, "y": -45.0, "z": 12.0, "a": 2.0, "b": 0.0}, rel=1e-5)
    assert m.beam_shift == (0.0125, -0.004) and m.image_shift == (0.0, 0.0)
    assert m.software_version == "3.6.0.6389" and m.instrument == "3593"
    assert m.eer_gain_reference == "20261110_090000_EER_GainReference.gain"  # the name, not the Windows path


def test_the_acquisition_time_keeps_its_zone() -> None:
    m = parse_epu_xml(epu_xml())
    assert m.acquired_text == "2026-11-10T10:15:00.1234567+01:00"
    expected = datetime(2026, 11, 10, 9, 15, 0, 123456, tzinfo=UTC).timestamp()
    assert m.acquired_at == pytest.approx(expected, abs=1e-6)


def test_other_namespaces_and_missing_fields_do_not_break_the_parser() -> None:
    renamed = epu_xml().replace(SO.encode(), b"http://example.org/another/version")
    assert parse_epu_xml(renamed).pixel_size == pytest.approx(0.83)
    bare = parse_epu_xml(b"<MicroscopeImage><microscopeData><gun/></microscopeData></MicroscopeImage>")
    assert bare == EpuImageMetadata()
    assert bare.to_json() == {}
    partial = parse_epu_xml(
        b"<MicroscopeImage><SpatialScale><pixelSize><x><numericValue>nan</numericValue></x></pixelSize>"
        b"</SpatialScale><microscopeData><acquisition><acquisitionDateTime>yesterday</acquisitionDateTime>"
        b"<camera><ExposureTime>fast</ExposureTime><Binning><x>0</x></Binning></camera></acquisition>"
        b"</microscopeData></MicroscopeImage>"
    )
    assert partial.pixel_size is None and partial.exposure_time is None and partial.binning is None
    assert partial.acquired_at is None and partial.acquired_text == "yesterday"


def test_to_json_holds_only_what_is_known() -> None:
    body = parse_epu_xml(epu_xml()).to_json()
    assert body["beam_shift"] == [0.0125, -0.004]
    assert "pixel_size" in body and all(v is not None for v in body.values())


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<MicroscopeImage><microscopeData>",  # still being written
        b"not xml at all",
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        b"<x>" + b"<y/>" * 250_000 + b"</x>",
    ],
)
def test_what_is_not_a_complete_and_plain_xml_is_refused(data: bytes) -> None:
    with pytest.raises(CorruptFileError):
        parse_epu_xml(data, "FoilHole_1.xml")


def test_reading_from_a_file_never_leaks_its_path(tmp_path: Path) -> None:
    path = tmp_path / "FoilHole_1_Data_2_3_20261110_101500.xml"
    path.write_bytes(epu_xml())
    assert read_epu_xml(path).dose == pytest.approx(42.5)
    path.write_bytes(b"<broken")
    with pytest.raises(CorruptFileError) as error:
        read_epu_xml(path)
    assert str(tmp_path) not in str(error.value) and path.name in str(error.value)
    with pytest.raises(CorruptFileError) as error:
        read_epu_xml(tmp_path / "missing.xml")
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("FoilHole_1_Data_2_3_20261110_101500.xml", "FoilHole_1_Data_2_3_20261110_101500"),
        ("FoilHole_1_Data_2_3_20261110_101500.mrc", "FoilHole_1_Data_2_3_20261110_101500"),
        ("FoilHole_1_Data_2_3_20261110_101500.jpg", "FoilHole_1_Data_2_3_20261110_101500"),
        ("FoilHole_1_Data_2_3_20261110_101500_EER.eer", "FoilHole_1_Data_2_3_20261110_101500"),
        ("FoilHole_1_Data_2_3_20261110_101500_Fractions.mrc", "FoilHole_1_Data_2_3_20261110_101500"),
        ("FoilHole_1_Data_2_3_20261110_101500_fractions.tiff", "FoilHole_1_Data_2_3_20261110_101500"),
        ("stack.mrc.mdoc", "stack"),
        ("stack.mdoc", "stack"),
        ("notes.v2.mdoc", "notes.v2"),
        ("a.1.mrc", "a.1"),
        ("a.2.mrc", "a.2"),
        ("_EER.eer", "_EER"),
        ("noextension", "noextension"),
        (".hidden", ".hidden"),
    ],
)
def test_exposure_name(name: str, expected: str) -> None:
    assert exposure_name(name) == expected
