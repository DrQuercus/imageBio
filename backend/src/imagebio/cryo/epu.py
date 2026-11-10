"""EPU (Thermo Fisher) per-image metadata: the ``.xml`` written next to every exposure.

The file is a .NET DataContract dump (root ``MicroscopeImage``). Its XML namespaces and the
exact nesting have changed between EPU versions, so elements are matched on their *local* names
along the paths documented in ``docs/research/cryoem-live.md`` (section 2, taken from MDCatch),
and every field is optional: what is not found is ``None``, never a guess.

The XML also tells which files belong to one exposure. EPU names the integrated image
``FoilHole_<hole>_Data_<target>_<n>_<date>_<time>.mrc``, its metadata ``….xml``, its preview
``….jpg`` and the movie ``…_Fractions.mrc``, ``…_fractions.tiff`` or ``…_EER.eer``:
:func:`exposure_name` gives the common part.

Not verified on files from a real microscope yet: the fixtures of the tests are built from the
documented structure.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

from imagebio.core.errors import CorruptFileError

__all__ = ["MAX_XML_BYTES", "EpuImageMetadata", "exposure_name", "parse_epu_xml", "read_epu_xml"]

MAX_XML_BYTES: Final = 8 * 2**20
_DTD: Final = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
_MAX_ELEMENTS: Final = 200_000

#: Suffixes EPU (and K3/Falcon recording) add to the name of an exposure for its movie.
_MOVIE_SUFFIXES: Final = ("_fractions", "_eer")
_IMAGE_EXTENSIONS: Final = frozenset({".mrc", ".mrcs", ".st", ".tif", ".tiff", ".eer", ".dm4"})


def exposure_name(file_name: str) -> str:
    """The name shared by the files of one exposure: no extension, no movie suffix.

    ``FoilHole_1_Data_2_3_20261110_101500_EER.eer`` and ``FoilHole_1_Data_2_3_20261110_101500.xml``
    both give ``FoilHole_1_Data_2_3_20261110_101500``; SerialEM's ``stack.mrc.mdoc`` gives
    ``stack``. Only the last extension is removed otherwise: ``a.1.mrc`` and ``a.2.mrc`` are
    two exposures.
    """
    stem, dot, extension = file_name.rpartition(".")
    if not dot or not stem:
        return file_name
    if extension.lower() == "mdoc":
        inner, inner_dot, inner_extension = stem.rpartition(".")
        if inner_dot and inner and f".{inner_extension.lower()}" in _IMAGE_EXTENSIONS:
            stem = inner
    lowered = stem.lower()
    for suffix in _MOVIE_SUFFIXES:
        if lowered.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)]
    return stem


@dataclass(frozen=True, slots=True)
class EpuImageMetadata:
    """What a live monitor needs from an EPU image XML. Every field may be ``None``."""

    #: Å per pixel of the image the XML describes (the integrated image, at the camera binning).
    pixel_size: float | None = None
    #: Total dose on the specimen, e/Å².
    dose: float | None = None
    #: Defocus asked for, µm (negative: underfocus).
    applied_defocus: float | None = None
    exposure_time: float | None = None  # s
    detector: str | None = None
    binning: int | None = None
    fractions: int | None = None
    magnification: float | None = None
    voltage: float | None = None  # kV
    spot_size: int | None = None
    #: Stage position: x, y, z in µm, a and b (tilts) in degrees.
    stage: dict[str, float] | None = None
    beam_shift: tuple[float, float] | None = None
    image_shift: tuple[float, float] | None = None
    #: Acquisition time, seconds since the epoch (UTC), and as written in the file.
    acquired_at: float | None = None
    acquired_text: str | None = None
    #: File name of the gain reference of an EER movie (EPU does not copy it next to the data).
    eer_gain_reference: str | None = None
    software_version: str | None = None
    instrument: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items() if v is not None}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element: ElementTree.Element | None, *path: str) -> ElementTree.Element | None:
    """The descendant reached by following local names, first match at each step."""
    for name in path:
        if element is None:
            return None
        element = next((c for c in element if _local(c.tag) == name), None)
    return element


def _text(element: ElementTree.Element | None, *path: str) -> str | None:
    found = _child(element, *path)
    text = (found.text or "").strip() if found is not None else ""
    return text or None


def _number(element: ElementTree.Element | None, *path: str) -> float | None:
    text = _text(element, *path)
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _key_values(element: ElementTree.Element | None) -> dict[str, str]:
    """``{Key: Value}`` of the ``KeyValueOfstringanyType`` children of ``element``."""
    out: dict[str, str] = {}
    if element is None:
        return out
    for pair in element:
        key, value = _text(pair, "Key"), _child(pair, "Value")
        if key and value is not None and len(out) < 1000:
            out[key] = (value.text or "").strip()
    return out


def _float_of(values: dict[str, str], key: str) -> float | None:
    try:
        value = float(values[key])
    except (KeyError, ValueError):
        return None
    return value if math.isfinite(value) else None


_FRACTION_DIGITS: Final = re.compile(r"(\.\d{6})\d+")


def _timestamp(text: str | None) -> float | None:
    """Seconds since the epoch of an ISO 8601 time; .NET writes seven fractional digits."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(_FRACTION_DIGITS.sub(r"\1", text.replace("Z", "+00:00")))
    except ValueError:
        return None
    # without an offset the time is the microscope PC's local time: taken as this server's
    return when.timestamp()


def _pair(element: ElementTree.Element | None) -> tuple[float, float] | None:
    x, y = _number(element, "_x"), _number(element, "_y")
    return (x, y) if x is not None and y is not None else None


def parse_epu_xml(data: bytes, name: str = "metadata.xml") -> EpuImageMetadata:
    """Parse the content of an EPU image XML. Raises :class:`CorruptFileError` when it is not
    XML at all (a file still being written, usually); unknown layouts give empty fields."""
    if len(data) > MAX_XML_BYTES:
        raise CorruptFileError(f"{name}: too large to be an EPU metadata file")
    text = data.strip(b"\0 \t\r\n")
    if _DTD.search(text):  # no DTD, hence no entity to expand
        raise CorruptFileError(f"{name}: XML with a document type definition is not read")
    try:
        root = ElementTree.fromstring(text)  # noqa: S314 - size bounded, DTD refused above
    except (ElementTree.ParseError, ValueError, LookupError):
        raise CorruptFileError(f"{name}: not a complete XML document") from None
    if sum(1 for _ in zip(root.iter(), range(_MAX_ELEMENTS + 1), strict=False)) > _MAX_ELEMENTS:
        raise CorruptFileError(f"{name}: too many elements for an EPU metadata file")

    scope = _child(root, "microscopeData")
    camera = _child(scope, "acquisition", "camera")
    optics = _child(scope, "optics")
    custom = _key_values(_child(root, "CustomData"))
    specific = _key_values(_child(camera, "CameraSpecificInput"))

    pixel = _number(root, "SpatialScale", "pixelSize", "x", "numericValue")  # metres
    dose = _float_of(custom, "Dose")  # electrons per square metre
    defocus = _float_of(custom, "AppliedDefocus")  # metres
    voltage = _number(scope, "gun", "AccelerationVoltage")  # volts
    binning = _number(camera, "Binning", "x")
    spot = _number(optics, "SpotIndex")
    fractions = next(
        (
            _float_of(specific, key)
            for key in ("NumberOffractions", "NumberOfFractions", "DoseFractions")
            if _float_of(specific, key) is not None
        ),
        None,
    )
    position = _child(scope, "stage", "Position")
    stage: dict[str, float] = {}
    for axis, scale in (("X", 1e6), ("Y", 1e6), ("Z", 1e6), ("A", 180 / math.pi), ("B", 180 / math.pi)):
        value = _number(position, axis)
        if value is not None:
            stage[axis.lower()] = value * scale
    acquired_text = _text(scope, "acquisition", "acquisitionDateTime")
    gain = next((v for k, v in custom.items() if k.endswith("EerGainReference") and v), None)

    return EpuImageMetadata(
        pixel_size=pixel * 1e10 if pixel and pixel > 0 else None,
        dose=dose * 1e-20 if dose is not None and dose >= 0 else None,
        applied_defocus=defocus * 1e6 if defocus is not None else None,
        exposure_time=_number(camera, "ExposureTime"),
        detector=_text(camera, "Name"),
        binning=int(binning) if binning and binning >= 1 else None,
        fractions=int(fractions) if fractions and fractions >= 1 else None,
        magnification=_number(optics, "TemMagnification", "NominalMagnification"),
        voltage=voltage / 1000 if voltage and voltage > 0 else None,
        spot_size=int(spot) if spot is not None else None,
        stage=stage or None,
        beam_shift=_pair(_child(optics, "BeamShift")),
        image_shift=_pair(_child(optics, "ImageShift")),
        acquired_at=_timestamp(acquired_text),
        acquired_text=acquired_text,
        # a Windows path on the microscope PC: only the file name means something here
        eer_gain_reference=re.split(r"[\\/]", gain)[-1] if gain else None,
        software_version=_text(scope, "core", "ApplicationSoftwareVersion"),
        instrument=_text(scope, "instrument", "InstrumentID"),
    )


def read_epu_xml(path: Path) -> EpuImageMetadata:
    """Read and parse an EPU image XML. Raises :class:`CorruptFileError` (file name only)."""
    try:
        with path.open("rb") as f:
            data = f.read(MAX_XML_BYTES + 1)
    except OSError:
        raise CorruptFileError(f"{path.name}: cannot be read") from None
    return parse_epu_xml(data, path.name)
