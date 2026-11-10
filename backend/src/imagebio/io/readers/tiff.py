"""TIFF reader: TIFF, BigTIFF, ImageJ hyperstacks, OME-TIFF and compressed movie stacks.

Built on tifffile + imagecodecs, the only decoder (no silent PIL fallback, AUDIT CV-27), and
strictly lazy: opening parses the IFDs, ``read_plane`` reads and decodes one page (AUDIT CV-6).

Format decisions
----------------
Series
    A TIFF file may hold several images ("series" for tifffile: pages grouped by shape, OME
    images, thumbnails). The first one is opened: it is the main image in every format we target.
    The others, and the pyramid levels of the opened one, are listed in ``info.metadata``;
    ``options={"series": n}`` opens another one. Only the full-resolution level is read.

Axes
    The axes tifffile reports are mapped to ``T, C, Z``: ``Z`` and ``C`` keep their meaning, every
    other page axis (``T`` time, ``I``/``Q`` plain page sequence, ...) goes to ``T``; several of
    them are flattened in file order, with a warning. Samples of a page become channels: RGB(A)
    gives ``C = 3/4`` with ``rgb=True``, gray + alpha ``C = 2``, and a palette image is expanded
    to RGB through its colour map (8-bit when the map is an 8-bit one scaled to 16 bits, as
    libtiff, ImageJ and Pillow write it; else the 16-bit values).

Stack kind
    ``IMAGE``: one displayed plane. ``MOVIE``: a multi-page gray stack without ImageJ/OME
    metadata, i.e. what detectors and motion correction write (K3/Falcon LZW movies); this is a
    convention, TIFF cannot tell a movie from a particle stack. ``VOLUME``: only ``Z > 1``.
    ``HYPERSTACK``: more than one of T, C (non-RGB), Z. ``STACK``: everything else.

Pixel values
    Planes are returned as stored: MinIsWhite images are not inverted, CMYK/Lab samples are not
    converted (both are flagged in ``info.warnings``). Conversions are limited to the documented
    ones: bilevel -> uint8 (0/1), float16 -> float32, 64-bit integers -> float64 (warning),
    palette -> RGB. Complex samples are refused. Empty strips or tiles of a sparse page read as
    zero, the TIFF convention (warning); planes that OME metadata declare but the file lacks
    are errors, not zeros.

Pixel size (AUDIT CV-16)
    OME ``PhysicalSize*`` and ``TimeIncrement`` with their units; else ImageJ ``unit`` with
    ``X/YResolution`` (pixels per unit), ``spacing`` and ``finterval``; else ``X/YResolution`` with
    a cm or inch ``ResolutionUnit`` (how IMOD, MotionCor and SerialEM store it). The value is kept
    in the unit of the file. A resolution without unit, or one that is a screen/print default
    (72 or 96 dpi, 1 pixel per unit), is not a pixel size: the calibration is ``DEFAULT`` and a
    warning says why.

EER
    EER is a TIFF container (compression 65000-65002). Such files are refused with a
    ``CorruptFileError`` so that the registry hands them to the EER reader, whatever their name.

Robustness
    * The IFD chain is walked here first, with positional reads. A chain that is cut, circular
      or endless, or a standard tag whose value lies beyond the end of the file, is a
      ``CorruptFileError`` ("truncated or still being written"): tifffile would silently read
      what is left as something else (fewer pages, a plain stack, palette indices).
    * Every page is checked against the real file size before anything is allocated: its data
      must be in the file, in the expected number of strips or tiles, and large enough for the
      image given what the codec can expand to. A page decodes to :data:`MAX_PLANE_BYTES` at most.
    * tifffile is never allowed to open another file: multi-file OME-TIFF, Micro-Manager and
      NDTiff datasets name sibling files (even ``../x``) in their metadata. Such files are read
      as plain stacks, with a warning. OME-XML that declares plane counts out of proportion with
      the file is ignored the same way, because tifffile sizes its page lists from it.
    * Any exception of tifffile or imagecodecs becomes a ``CorruptFileError`` whose message
      holds the file name only; the details go to the log.

Thread safety
    Uncompressed contiguous planes are read with ``os.pread``. Everything else goes through
    tifffile with one re-entrant lock around its seek + read pairs; decoding runs outside the
    lock. Multi-sample pages (interleaved RGB, palette, compressed planar) are decoded as a whole
    and the last one is kept (up to 64 MiB, one per dataset) so that reading its channels one
    after the other decodes it once. ``close()`` takes effect when the reads in flight are done;
    reading a closed dataset raises ``RuntimeError``.
"""

from __future__ import annotations

import contextlib
import logging
import math
import operator
import os
import re
import struct
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, ClassVar, Final
from xml.etree import ElementTree

import numpy as np
import tifffile

from imagebio.core.errors import CorruptFileError, ImageBioError, InvalidRequestError, NotFoundError
from imagebio.core.model import (
    PLANE_DTYPES,
    UNKNOWN_PIXEL,
    AxisCalibration,
    Calibration,
    CalibrationSource,
    ChannelInfo,
    Dataset,
    DatasetInfo,
    Plane,
    StackKind,
)
from imagebio.io.registry import Reader

log = logging.getLogger(__name__)

#: Largest decoded page (all samples) this reader accepts. 2 GiB covers every detector frame
#: (K3 super-resolution: 94 MB; a 16k x 16k float32 montage: 1 GiB). Larger images (whole-slide
#: scans) need tiled access, which this reader does not offer yet.
MAX_PLANE_BYTES: Final = 2 * 2**30
#: Longest IFD chain accepted (bounds the time and memory a crafted file can cost).
MAX_PAGES: Final = 2**20
#: Largest believable ratio of decoded to stored bytes of a compressed page, for the codecs that
#: have no tighter bound in ``_MAX_EXPANSION``. Blank images do reach 32 768:1 with zstd (its
#: maximum); a page that claims more is taken as a false header and is not allocated.
MAX_COMPRESSION_RATIO: Final = 2**16

_MEMO_MAX_BYTES: Final = 64 * 2**20  # largest multi-sample page kept between channel reads
_MAX_DECODE_THREADS: Final = 8  # per read; tifffile alone would use half the cores for each read
_MAX_OME_XML_CHARS: Final = 16 * 2**20
_MAX_IFD_ENTRIES: Final = 4096  # same limit as tifffile
_MAX_LISTED_SERIES: Final = 32
_MAGIC: Final = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_EER_COMPRESSIONS: Final = frozenset({65000, 65001, 65002})
_JPEG_COMPRESSIONS: Final = frozenset({6, 7, 33007, 34892})  # decoded to RGB by imagecodecs
_MINISWHITE, _RGB, _PALETTE, _YCBCR = 0, 2, 3, 6  # PhotometricInterpretation
_RESOLUTION_UNITS: Final[Mapping[int, str]] = {2: "inch", 3: "cm", 4: "mm", 5: "µm"}
_UNITS_PER_INCH: Final[Mapping[str, float]] = {"inch": 1.0, "cm": 2.54, "mm": 25.4, "µm": 25400.0}
_PLACEHOLDER_DPI: Final = (72.0, 96.0)
_PROSE_LIMIT: Final = 500  # characters of free-text metadata kept in ``info.metadata``
#: What one stored byte can expand to at most, for the codecs whose limit is certain: LZW (a
#: full 12-bit table cycle yields about 1360:1), deflate and PNG (1032:1), PackBits (64:1).
_MAX_EXPANSION: Final[Mapping[int, int]] = {5: 2048, 8: 1100, 32946: 1100, 34933: 1100, 32773: 64}
#: Bytes per value of each TIFF field type.
_TYPE_SIZES: Final[Mapping[int, int]] = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
}  # fmt: skip


# ─── File structure ───────────────────────────────────────────────────────────


def _incomplete(name: str, what: str) -> CorruptFileError:
    return CorruptFileError(f"{name}: {what}: the file is truncated or still being written")


def _count_ifds(fd: int, size: int, name: str) -> int:
    """Walk the main IFD chain with positional reads and return its length.

    tifffile stops silently at a broken link, detects only some loops and drops the tags whose
    value lies beyond the end of the file. A file in that state must not be opened: it is being
    written, or its copy was interrupted, and what is left would be read as something else.
    """
    head = os.pread(fd, 16, 0)
    if head[:4] not in _MAGIC:
        raise CorruptFileError(f"{name}: not a TIFF file")
    order = "<" if head[:2] == b"II" else ">"
    if head[2:4] in (b"+\x00", b"\x00+"):  # BigTIFF: 8-byte counts and offsets
        if len(head) < 16 or struct.unpack(order + "HH", head[4:8]) != (8, 0):
            raise CorruptFileError(f"{name}: invalid BigTIFF header")
        count_format, offset_format, entry_format, first = (
            order + "Q",
            order + "Q",
            order + "HHQQ",
            head[8:16],
        )
    else:
        count_format, offset_format, entry_format, first = order + "H", order + "I", order + "HHII", head[4:8]
    count_size, offset_size = struct.calcsize(count_format), struct.calcsize(offset_format)
    entry_size = struct.calcsize(entry_format)
    if len(first) < offset_size:
        raise _incomplete(name, "the TIFF header is incomplete")
    (position,) = struct.unpack(offset_format, first)
    seen: set[int] = set()
    while position:
        page = len(seen)
        if position in seen:
            raise CorruptFileError(f"{name}: circular IFD chain (corrupt TIFF structure)")
        if page >= MAX_PAGES:
            raise CorruptFileError(f"{name}: more than {MAX_PAGES} pages")
        seen.add(position)
        raw = os.pread(fd, 1024, position)  # a typical IFD and its "next" pointer in one read
        if len(raw) < count_size:
            raise _incomplete(name, f"page {page} is missing")
        (entries,) = struct.unpack_from(count_format, raw)
        if entries > _MAX_IFD_ENTRIES:
            raise CorruptFileError(f"{name}: corrupt TIFF structure (page {page} has {entries} tags)")
        end = count_size + entries * entry_size
        if len(raw) < end + offset_size:
            raw = os.pread(fd, end + offset_size, position)
        if len(raw) < end + offset_size:
            raise _incomplete(name, f"page {page} is incomplete")
        for code, kind, count, value in struct.iter_unpack(entry_format, raw[count_size:end]):
            # standard tags and ImageJ metadata: everything the interpretation of a page relies on
            # (private tags of some writers are sloppy; tifffile ignores them and so do we)
            nbytes = count * _TYPE_SIZES.get(kind, 0)
            if nbytes > offset_size and value + nbytes > size and (code < 32768 or code in (50838, 50839)):
                raise _incomplete(name, f"tag {code} of page {page} lies beyond the end of the file")
        (position,) = struct.unpack_from(offset_format, raw, end)
    if not seen:
        raise CorruptFileError(f"{name}: the TIFF file contains no image")
    return len(seen)


def _pread(fd: int, count: int, offset: int, name: str) -> bytes:
    """Exactly ``count`` bytes at ``offset`` (thread-safe: does not use the file position)."""
    chunks: list[bytes] = []
    while count > 0:
        chunk = os.pread(fd, count, offset)
        if not chunk:
            raise _incomplete(name, "image data end before the end of the plane")
        chunks.append(chunk)
        offset += len(chunk)
        count -= len(chunk)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def _tifffile(file: IO[bytes], *, ome: bool = True) -> tifffile.TiffFile:
    """Parse ``file`` with tifffile, which must never open a sibling file named by metadata.
    ``ome=False`` makes it ignore OME-XML."""
    file.seek(0)
    return tifffile.TiffFile(
        file, _multifile=False, is_mmstack=False, is_ndtiff=False, is_ome=None if ome else False
    )


# ─── OME-XML ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _OmeImage:
    name: str | None
    pixels: Mapping[str, str]  # attributes of the Pixels element
    channels: tuple[Mapping[str, str], ...]  # attributes of its Channel elements


def _local(tag: Any) -> str:
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _parse_ome(xml: str, n_ifds: int) -> list[_OmeImage] | None:
    """Images declared by an OME-XML header, or ``None`` when it cannot be trusted.

    tifffile builds page lists and index arrays whose sizes come straight from ``SizeT/Z/C``,
    ``PlaneCount`` and Modulo annotations: values out of proportion with the file (crafted, or
    the header of a multi-file dataset) must never reach it.
    """
    if len(xml) > _MAX_OME_XML_CHARS:
        return None
    limit = 8 * n_ifds + 64
    images: list[_OmeImage] = []
    try:
        # ElementTree resolves no external entity and expat bounds entity expansion; tifffile
        # parses the same string with the same parser.
        root = ElementTree.fromstring(xml)  # noqa: S314
        for element in root.iter():
            tag, attrib = _local(element.tag), element.attrib
            if tag == "TiffData":
                if max(int(attrib.get("PlaneCount", 0)), int(attrib.get("NumPlanes", 0))) > limit:
                    return None
            elif tag.startswith("ModuloAlong") and "Start" in attrib:
                steps = (float(attrib["End"]) - float(attrib["Start"])) / float(attrib.get("Step", 1))
                if not steps <= limit:  # also catches NaN
                    return None
            elif tag == "Image":
                for pixels in element:
                    if _local(pixels.tag) != "Pixels":
                        continue
                    sizes = [int(pixels.attrib.get(f"Size{axis}", 1)) for axis in "TZC"]
                    if min(sizes) < 1 or math.prod(sizes) > limit:
                        return None
                    channels = tuple(dict(ch.attrib) for ch in pixels if _local(ch.tag) == "Channel")
                    images.append(_OmeImage(attrib.get("Name"), dict(pixels.attrib), channels))
    except (ElementTree.ParseError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return images


def _ome_color(value: str | None) -> tuple[int, int, int] | None:
    """RGB of an OME ``Color`` attribute (a signed 32-bit RGBA integer)."""
    try:
        rgba = int(value or "") & 0xFFFFFFFF
    except ValueError:
        return None
    return (rgba >> 24, (rgba >> 16) & 255, (rgba >> 8) & 255)


# ─── Layout: axes, samples, dtype ─────────────────────────────────────────────


def _page_axes(axes: str, shape: Sequence[int], page_items: int) -> list[tuple[str, int]] | None:
    """The series axes that index pages, outermost first (``None`` when inconsistent).

    They are what remains before the shortest suffix of ``shape`` that holds one page. Axes of
    length 1 are dropped: they do not change the page order.
    """
    k, items = len(shape), 1
    while k and items < page_items:
        k -= 1
        items *= shape[k]
    if items != page_items or len(axes) != len(shape) or min(shape, default=1) < 1:
        return None
    return [(axis, n) for axis, n in zip(axes[:k], shape[:k], strict=True) if n > 1]


def _roles(lead: Sequence[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    """Assign each page axis to ``z``, ``c`` or ``t``: the first Z and C keep their meaning."""
    out: list[tuple[str, int]] = []
    for axis, n in lead:
        role = {"Z": "z", "C": "c"}.get(axis, "t")
        if role != "t" and any(r == role for r, _ in out):
            role = "t"
        out.append((role, n))
    return tuple(out)


def _plane_dtype(dtype: np.dtype[Any]) -> tuple[np.dtype[Any], str | None]:
    """dtype of the planes for a stored ``dtype``, and the warning the conversion deserves."""
    if dtype.kind == "b":
        return np.dtype(np.uint8), None
    if dtype.kind == "f" and dtype.itemsize == 2:
        return np.dtype(np.float32), None
    if dtype.kind in "iu" and dtype.itemsize == 8:
        return np.dtype(np.float64), "64-bit integer pixels are converted to float64 (exact up to 2^53 only)"
    return dtype.newbyteorder("="), None


def _palette(keyframe: tifffile.TiffPage) -> np.ndarray[Any, Any] | None:
    """Colour look-up table ``(3, entries)`` of a palette page, or ``None`` when unusable.

    TIFF colour maps are 16-bit. An 8-bit palette is stored scaled by 257 (libtiff, Pillow) or
    256 (ImageJ), or sometimes unscaled: those are returned as the uint8 values they stand for.
    """
    cmap, bits, dtype = keyframe.colormap, keyframe.bitspersample, keyframe.dtype
    if not (
        isinstance(cmap, np.ndarray) and cmap.ndim == 2 and cmap.shape[0] == 3 and cmap.dtype.kind == "u"
    ):
        return None
    if not (isinstance(bits, int) and 1 <= bits <= 16 and cmap.shape[1] >= 2**bits):
        return None
    if dtype is None or dtype.kind != "u":
        return None
    if not (cmap % 257).any():
        return (cmap // 257).astype(np.uint8)
    if not (cmap % 256).any():
        return (cmap >> 8).astype(np.uint8)
    if (cmap < 256).all():
        return cmap.astype(np.uint8)
    return cmap.astype(np.uint16)


def _stack_kind(size_t: int, size_c: int, size_z: int, rgb: bool, plain: bool) -> StackKind:
    varying = (size_t > 1) + (size_z > 1) + (size_c > 1 and not rgb)
    if varying == 0:
        return StackKind.IMAGE
    if varying > 1:
        return StackKind.HYPERSTACK
    if size_z > 1:
        return StackKind.VOLUME
    if size_t > 1 and size_c == 1 and plain:
        return StackKind.MOVIE
    return StackKind.STACK


# ─── Calibration ──────────────────────────────────────────────────────────────


def _measure(value: Any, unit: str | None) -> AxisCalibration | None:
    """A calibration read from metadata, or ``None`` when the value or the unit is unusable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(number) and number > 0):
        return None
    axis = AxisCalibration(number, unit or "", CalibrationSource.METADATA)
    return axis if axis.known else None


def _rational(value: Any) -> float | None:
    """A strictly positive TIFF RATIONAL as a float."""
    try:
        numerator, denominator = value
        result = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _imagej_unit(value: Any) -> str | None:
    """The length unit of ImageJ metadata; ``None`` when absent or not a physical unit."""
    if not isinstance(value, str):
        return None
    unit = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)  # ImageJ escapes µ
    axis = AxisCalibration(1.0, unit, CalibrationSource.METADATA)
    return axis.unit if axis.known else None


def _placeholder(resolution: float, unit: str) -> str | None:
    """Describe ``resolution`` when it is a screen/print default rather than a measurement."""
    if resolution == 1:
        return f"1 pixel per {unit}"
    dpi = resolution * _UNITS_PER_INCH[unit]
    for known in _PLACEHOLDER_DPI:
        if math.isclose(dpi, known, rel_tol=1e-4):
            return f"{known:g} dpi"
    return None


def _resolution_tags(page: tifffile.TiffPage) -> tuple[float | None, float | None, Any]:
    """XResolution and YResolution (pixels per unit, ``None`` when absent or invalid) and the
    raw ResolutionUnit."""
    tags = page.tags
    return _rational(tags.valueof(282)), _rational(tags.valueof(283)), tags.valueof(296)


def _calibration(
    keyframe: tifffile.TiffPage,
    ome: _OmeImage | None,
    imagej: Mapping[str, Any] | None,
    size_t: int,
    size_z: int,
) -> tuple[Calibration, list[str]]:
    """Calibration with its provenance, and the warnings it deserves."""
    x = y = z = t = None
    if ome is not None:
        px = ome.pixels
        x = _measure(px.get("PhysicalSizeX"), px.get("PhysicalSizeXUnit", "µm"))
        y = _measure(px.get("PhysicalSizeY"), px.get("PhysicalSizeYUnit", "µm"))
        z = _measure(px.get("PhysicalSizeZ"), px.get("PhysicalSizeZUnit", "µm"))
        t = _measure(px.get("TimeIncrement"), px.get("TimeIncrementUnit", "s"))
        reason = "the OME metadata hold no physical pixel size"
    else:
        x_resolution, y_resolution, unit_tag = _resolution_tags(keyframe)
        resolution = x_resolution or y_resolution
        unit = None
        if imagej is not None:
            unit = _imagej_unit(imagej.get("unit"))
            z = _measure(imagej.get("spacing"), unit) if unit else None
            t = _measure(imagej.get("finterval"), "s")
        reason = "the file has no resolution tags"
        if resolution and unit is None:  # no ImageJ unit: the ResolutionUnit tag decides (default: inch)
            unit = _RESOLUTION_UNITS.get(2 if unit_tag is None else unit_tag)
            default = _placeholder(resolution, unit) if unit else None
            if unit is None:
                reason = "the TIFF resolution tags have no unit"
            elif default:
                reason = f"the TIFF resolution tags hold a screen/print default ({default}), not a pixel size"
                unit = None
        if resolution and unit:
            x = _measure(1 / x_resolution, unit) if x_resolution else None
            y = _measure(1 / y_resolution, unit) if y_resolution else None
    warnings: list[str] = []
    if x is None and y is None:
        warnings.append(f"pixel size unknown: {reason}")
    elif x is None or y is None:
        warnings.append("only one of the X and Y pixel sizes is stored: square pixels are assumed")
    calibration = Calibration(
        x=x or y or UNKNOWN_PIXEL,
        y=y or x or UNKNOWN_PIXEL,
        z=z if size_z > 1 else None,
        t=t if size_t > 1 else None,
    )
    return calibration, warnings


# ─── Metadata ─────────────────────────────────────────────────────────────────


def _name(value: Any) -> Any:
    """Name of a tifffile enum member; unknown codes stay numbers."""
    return getattr(value, "name", value)


def _clip(text: Any) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    return text if len(text) <= _PROSE_LIMIT else text[:_PROSE_LIMIT] + "…"


def _channels(
    ome: _OmeImage | None, imagej: Mapping[str, Any] | None, size_c: int, rgb: bool
) -> tuple[ChannelInfo, ...]:
    if rgb or size_c == 1:
        return ()
    if ome is not None and len(ome.channels) == size_c:
        return tuple(ChannelInfo(ch.get("Name") or None, _ome_color(ch.get("Color"))) for ch in ome.channels)
    luts = imagej.get("LUTs") if imagej is not None else None  # one (3, 256) colour ramp per channel
    if not (isinstance(luts, list) and len(luts) == size_c):
        return ()
    if not all(isinstance(lut, np.ndarray) and lut.shape == (3, 256) for lut in luts):
        return ()
    return tuple(ChannelInfo(None, (int(lut[0, -1]), int(lut[1, -1]), int(lut[2, -1]))) for lut in luts)


def _series_summary(series: tifffile.TiffPageSeries) -> dict[str, Any]:
    return {
        "kind": series.kind or "generic",
        "name": _clip(series.name),
        "axes": series.axes,
        "shape": list(series.shape),
        "dtype": str(series.dtype),
        "levels": len(series.levels),
    }


def _metadata(
    tif: tifffile.TiffFile,
    all_series: Sequence[tifffile.TiffPageSeries],
    series_index: int,
    n_ifds: int,
    ome: _OmeImage | None,
    imagej: Mapping[str, Any] | None,
) -> dict[str, Any]:
    series = all_series[series_index]
    page = series.keyframe
    x_resolution, y_resolution, resolution_unit = _resolution_tags(page)
    md: dict[str, Any] = {
        "byte_order": "little" if tif.byteorder == "<" else "big",
        "is_bigtiff": bool(tif.is_bigtiff),
        "page_count": n_ifds,
        "series_count": len(all_series),
        "series_index": series_index,
        "series": [_series_summary(s) for s in all_series[:_MAX_LISTED_SERIES]],
        "kind": series.kind or "generic",
        "axes": series.axes,
        "shape": list(series.shape),
        "levels": [list(level.shape) for level in series.levels],  # full resolution first
        "compression": _name(page.compression),
        "photometric": _name(page.photometric),
        "planar_config": _name(page.planarconfig),
        "bits_per_sample": page.bitspersample,
        "sample_format": _name(page.sampleformat),
        "samples_per_pixel": page.samplesperpixel,
        "extra_samples": [_name(e) for e in page.extrasamples],
        "predictor": _name(page.predictor),
        "tile": list(page.tile) if page.tile else None,
        "rows_per_strip": None if page.is_tiled else page.rowsperstrip,
        "segments_per_page": len(page.dataoffsets),
        "resolution": {"x": x_resolution, "y": y_resolution, "unit": _name(resolution_unit)},
        "software": _clip(page.software),
        "datetime": str(page.datetime) if page.datetime else None,
        "flags": sorted(tif.pages.first.flags),
    }
    if ome is not None:
        md["ome"] = {"name": _clip(ome.name), "pixels": {k: _clip(v) for k, v in ome.pixels.items()}}
    elif imagej is not None:
        md["imagej"] = {
            k: (_clip(v) if isinstance(v, str) else v)
            for k, v in imagej.items()
            if isinstance(v, bool | int | float | str)
        }
    else:
        md["description"] = _clip(page.description)
    return md


# ─── Dataset ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Layout:
    """Where the planes of the opened series are and how a page stores its samples."""

    lead: tuple[tuple[str, int], ...]  # (role, length) of the axes that index pages, outermost first
    per_page: int  # channels one page provides (3 for an expanded palette)
    planar: bool  # samples stored as separate planes (PlanarConfiguration = 2)
    page_shape: tuple[int, ...]  # (Y, X) for one sample, else (Y, X, S) or planar (S, Y, X)
    shaped: tuple[int, ...]  # the same page in tifffile's normalised 5-D shape
    page_nbytes: int
    stored_dtype: np.dtype[Any]  # as in the file, file byte order
    lut: np.ndarray[Any, Any] | None  # palette (3, entries), in the plane dtype
    block_offset: int | None  # start of the pixel data when the whole series is one contiguous block


class TiffDataset(Dataset):
    """One image series of a TIFF file. Created by :class:`TiffReader`."""

    def __init__(
        self,
        path: Path,
        info: DatasetInfo,
        *,
        file: IO[bytes],
        series: tifffile.TiffPageSeries,
        layout: _Layout,
    ) -> None:
        super().__init__(path, info)
        self._file = file
        self._fd = file.fileno()
        self._series = series
        self._layout = layout
        self._tiff_lock = threading.RLock()  # tifffile's file position and its page objects
        self._memo_lock = threading.Lock()
        self._memo: tuple[int, np.ndarray[Any, Any]] | None = None  # last multi-sample page read
        self._state = threading.Lock()
        self._readers = 0
        self._closed = False

    # lifetime ------------------------------------------------------------------

    @contextlib.contextmanager
    def _reading(self) -> Iterator[None]:
        """Keep the file open during a read: a concurrent ``close()`` takes effect afterwards
        (a descriptor closed under a positional read could already belong to another file)."""
        with self._state:
            if self._closed:
                raise RuntimeError("dataset is closed")
            self._readers += 1
        try:
            yield
        finally:
            with self._state:
                self._readers -= 1
                if self._closed and not self._readers:
                    self._release()

    def _release(self) -> None:
        self._memo = None
        self._file.close()

    def close(self) -> None:
        with self._state:
            if not self._closed:
                self._closed = True
                if not self._readers:
                    self._release()

    # reading -------------------------------------------------------------------

    def _page_index(self, t: int, c: int, z: int) -> int:
        """Index of a page in the series (C order over the page axes)."""
        lead = self._layout.lead
        digits: list[int] = []
        for role, n in reversed(lead):  # T may be several flattened axes: split it back
            if role == "t":
                t, digit = divmod(t, n)
                digits.append(digit)
        index = 0
        for role, n in lead:
            index = index * n + (digits.pop() if role == "t" else c if role == "c" else z)
        return index

    def _locate(self, index: int) -> tuple[int | None, Any]:
        """``(offset, None)`` when page ``index`` is stored as plain bytes, else ``(None, page)``."""
        layout = self._layout
        if layout.block_offset is not None:
            return layout.block_offset + index * layout.page_nbytes, None
        with self._tiff_lock:  # tifffile may have to parse the IFD
            page = self._series[index]
        if page is None:
            raise CorruptFileError(f"{self._path.name}: plane {index} is missing from the file")
        return (page.dataoffsets[0], None) if page.is_final else (None, page)

    def _raw(self, offset: int, shape: tuple[int, ...]) -> np.ndarray[Any, Any]:
        dtype = self._layout.stored_dtype
        data = _pread(self._fd, math.prod(shape) * dtype.itemsize, offset, self._path.name)
        return np.frombuffer(data, dtype).reshape(shape)

    def _decode(self, page: Any, index: int) -> np.ndarray[Any, Any]:
        layout = self._layout
        try:
            # tifffile decodes into an uninitialised buffer: hand it zeros, so that whatever a
            # malformed page leaves unwritten can never be stale memory served as pixels
            out = np.zeros(layout.shaped, layout.stored_dtype.newbyteorder("="))
            workers = max(1, min(page.keyframe.maxworkers, _MAX_DECODE_THREADS))  # strips decode in parallel
            decoded: np.ndarray[Any, Any] = page.asarray(
                lock=self._tiff_lock, out=out, squeeze=False, maxworkers=workers
            )
        except Exception as e:  # tifffile and the codecs raise many types on corrupt data
            log.warning("tiff: cannot decode page %d of %s: %r", index, self._path.name, e)
            raise CorruptFileError(
                f"{self._path.name}: cannot decode page {index} ({type(e).__name__})"
            ) from None
        if decoded.shape != layout.shaped:  # tifffile answers an empty array for unusable pages
            raise CorruptFileError(f"{self._path.name}: cannot decode page {index} (no image data)")
        return decoded.reshape(layout.page_shape)

    def _whole_page(self, index: int) -> np.ndarray[Any, Any]:
        """A multi-sample page; the last one is kept for the reads of its other channels."""
        with self._memo_lock:
            if self._memo is not None and self._memo[0] == index:
                return self._memo[1]
            offset, page = self._locate(index)
            full = self._decode(page, index) if offset is None else self._raw(offset, self._layout.page_shape)
            self._memo = (index, full) if full.nbytes <= _MEMO_MAX_BYTES else None
            return full

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        layout, dtype = self._layout, self._info.dtype
        plane_shape = (self._info.size_y, self._info.size_x)
        with self._reading():
            c, sample = divmod(c, layout.per_page)
            index = self._page_index(t, c, z)
            if layout.per_page == 1 or (layout.planar and layout.lut is None):
                offset, page = self._locate(index)
                if offset is not None:  # the plane is stored as such: read its bytes only
                    offset += sample * layout.page_nbytes // layout.per_page
                    return np.ascontiguousarray(self._raw(offset, plane_shape), dtype)
                if layout.per_page == 1:
                    return np.ascontiguousarray(self._decode(page, index), dtype)
            full = self._whole_page(index)
            if layout.lut is not None:
                colours: Plane = layout.lut[sample].take(full, mode="clip")  # indices cannot exceed the table
                return colours
            # a copy: the plane must not keep the other samples of the page alive
            return np.array(full[sample] if layout.planar else full[..., sample], dtype)


# ─── Opening ──────────────────────────────────────────────────────────────────


def _parse(
    file: IO[bytes], n_ifds: int
) -> tuple[tifffile.TiffFile, list[tifffile.TiffPageSeries], list[_OmeImage] | None]:
    """tifffile's view of the file: its series, and the OME images when OME-XML drives them."""
    tif = _tifffile(file)
    if tif.pages.first.compression in _EER_COMPRESSIONS:
        raise CorruptFileError("EER file: handled by the eer reader")
    if not tif.is_ome:
        return tif, list(tif.series), None
    images = _parse_ome(tif.ome_metadata or "", n_ifds)
    if images is not None:
        try:
            return tif, list(tif.series), images
        except Exception as e:  # OME-XML that tifffile cannot digest: the pixels are still there
            log.warning("tiff: unusable OME-XML in %s: %r", tif.filename, e)
    tif = _tifffile(file, ome=False)
    return tif, list(tif.series), None


def _check_page(page: Any, index: int, nbytes: int, size: int, name: str) -> bool:
    """Refuse a page whose stored data cannot be what its header claims, before anything is
    allocated for it. Return True when some of its strips or tiles are empty (sparse page)."""
    offsets, counts = page.dataoffsets, page.databytecounts
    if not offsets:
        raise CorruptFileError(f"{name}: page {index} has no image data")
    if page.is_contiguous:
        if offsets[0] + nbytes > size:
            raise _incomplete(name, f"the image data of page {index} are incomplete")
        return False
    keyframe = page.keyframe
    expected = math.prod(keyframe.chunked)
    if len(offsets) != expected or len(counts) != expected:  # tifffile would zero-fill the rest
        raise CorruptFileError(
            f"{name}: page {index} has {min(len(offsets), len(counts))} strips or tiles, {expected} expected"
        )
    stored, filled = sum(counts), sum(1 for count in counts if count)
    if not filled:
        raise CorruptFileError(f"{name}: page {index} has no image data")
    if max(map(operator.add, offsets, counts)) > size or stored > size:
        raise _incomplete(name, f"the image data of page {index} are incomplete")
    # the most the stored bytes can decode to
    if keyframe.compression != 1:
        possible = stored * _MAX_EXPANSION.get(int(keyframe.compression), MAX_COMPRESSION_RATIO)
    else:  # bit-packed samples (1 to 7, 12... bits) are unpacked to whole bytes
        possible = stored * 8 * keyframe.dtype.itemsize // max(1, int(keyframe.bitspersample))
        if filled < expected:  # sparse: assume the empty strips are as large as the others
            possible = 2 * possible * expected // filled
    if nbytes > possible:
        raise CorruptFileError(f"{name}: the {stored} bytes stored for page {index} cannot hold its image")
    return bool(filled < expected)


def _check_pages(
    series: tifffile.TiffPageSeries, n_pages: int, nbytes: int, size: int, name: str, warnings: list[str]
) -> int | None:
    """Check that the pixel data of every page are in the file. Return the offset of the data
    when the series is one contiguous uncompressed block (ImageJ and tifffile write those, with
    a single IFD beyond 4 GB), which is then read without tifffile."""
    block_offset: int | None = series.dataoffset
    if block_offset is not None:
        if block_offset + n_pages * nbytes > size:
            raise _incomplete(name, "the image data are incomplete")
        return block_offset
    if len(series) != n_pages:
        raise CorruptFileError(
            f"{name}: the metadata declare {n_pages} planes, the file holds {len(series)} pages"
        )
    missing = sparse = 0
    for index in range(n_pages):
        page = series[index]
        if page is None:
            missing += 1
        else:
            sparse += _check_page(page, index, nbytes, size, name)
    if missing:
        warnings.append(
            f"{missing} of the {n_pages} planes declared by the metadata are missing from the file"
        )
    if sparse:
        warnings.append(f"{sparse} pages have strips or tiles without data (sparse file): they read as zero")
    return None


def _open(file: IO[bytes], path: Path, series_index: int) -> TiffDataset:
    name = path.name
    size = os.fstat(file.fileno()).st_size
    n_ifds = _count_ifds(file.fileno(), size, name)
    tif, all_series, ome_images = _parse(file, n_ifds)
    if not all_series:
        raise CorruptFileError(f"{name}: the TIFF file contains no image")
    if series_index >= len(all_series):
        raise InvalidRequestError(f"series {series_index} does not exist: {name} has {len(all_series)}")
    series = all_series[series_index]
    keyframe = series.keyframe
    warnings: list[str] = []
    if len(all_series) > 1:
        warnings.append(
            f"the file holds {len(all_series)} image series: series {series_index} is shown "
            '(the "series" option opens another one)'
        )

    # embedded metadata that tifffile used to build the series
    ome = imagej = None
    if series.kind == "ome" and ome_images is not None and len(ome_images) == len(all_series):
        ome = ome_images[series_index]
    elif series.kind == "imagej":
        imagej = tif.imagej_metadata
    elif tif.pages.first.is_ome:
        warnings.append(
            "the OME metadata could not be used (multi-file dataset, or inconsistent with the "
            "pages): only the pages of this file are shown, as a plain stack"
        )
    elif series.kind in ("generic", "uniform") and tif.imagej_metadata is not None:
        # tifffile falls back to a plain stack when ImageJ metadata do not fit the file, e.g. a
        # single-IFD hyperstack (ImageJ's layout beyond 4 GB) whose pixel data are cut short
        raise _incomplete(name, "the ImageJ metadata declare more images than the file holds")

    # one page: shape, samples, pixel type
    separate, depth, size_y, size_x, contig = keyframe.shaped
    stored = keyframe.dtype
    if stored is None:
        raise CorruptFileError(
            f"{name}: unsupported pixel type ({_name(keyframe.sampleformat)}, {keyframe.bitspersample} bits)"
        )
    if stored.kind == "c":
        raise CorruptFileError(f"{name}: complex pixel values are not supported")
    if min(separate, size_y, size_x, contig) < 1:
        raise CorruptFileError(f"{name}: the image has no pixels")
    if depth != 1:
        raise CorruptFileError(f"{name}: volumetric pages (ImageDepth > 1) are not supported")
    samples = separate * contig  # one of the two is 1
    page_nbytes = samples * size_y * size_x * stored.itemsize
    if page_nbytes > MAX_PLANE_BYTES:
        raise CorruptFileError(
            f"{name}: image too large ({size_x} x {size_y} pixels, {page_nbytes} bytes per page; "
            f"the limit is {MAX_PLANE_BYTES})"
        )
    if keyframe.compression not in tifffile.TIFF.DECOMPRESSORS:
        raise CorruptFileError(f"{name}: unsupported compression ({_name(keyframe.compression)})")
    dtype, dtype_warning = _plane_dtype(stored)
    if dtype not in PLANE_DTYPES:
        raise CorruptFileError(f"{name}: unsupported pixel type ({stored})")
    if dtype_warning:
        warnings.append(dtype_warning)

    # the pages of the series: axes, presence of their data
    lead = _page_axes(series.axes, series.shape, samples * size_y * size_x)
    if lead is None or any(axis in "YXS" for axis, _ in lead):
        raise CorruptFileError(
            f"{name}: unsupported layout (axes {series.axes}, shape {tuple(series.shape)}, "
            f"pages of {tuple(keyframe.shape)})"
        )
    roles = _roles(lead)
    size_t, size_c, size_z = (math.prod(n for role, n in roles if role == r) for r in "tcz")
    flattened = [(axis, n) for (axis, n), (role, _) in zip(lead, roles, strict=True) if role == "t"]
    if len(flattened) > 1:
        warnings.append(
            f"axes {''.join(axis for axis, _ in flattened)} ({' x '.join(str(n) for _, n in flattened)}) "
            "are flattened into T, in file order"
        )
    block_offset = _check_pages(series, size_t * size_c * size_z, page_nbytes, size, name, warnings)

    # samples -> channels
    photometric = int(keyframe.photometric)
    lut = _palette(keyframe) if photometric == _PALETTE and samples == 1 and size_c == 1 else None
    if lut is not None:
        dtype = lut.dtype
    elif photometric == _PALETTE:
        warnings.append("the colour palette could not be applied: pixel values are palette indices")
    per_page = samples if lut is None else 3
    decoded_rgb = photometric == _RGB or (
        photometric == _YCBCR and keyframe.compression in _JPEG_COMPRESSIONS
    )
    rgb = lut is not None or (decoded_rgb and samples in (3, 4) and size_c == 1)
    if photometric == _MINISWHITE:
        warnings.append(
            "photometric interpretation is MinIsWhite: stored values are shown as they are (0 = white)"
        )
    elif samples > 1 and not rgb and photometric > 1:
        warnings.append(
            f"{_name(keyframe.photometric)} samples are shown as stored channels, without colour conversion"
        )
    if series.transform is not None:
        warnings.append("stored values are shown: the intensity transform of this format is not applied")
    size_c *= per_page

    calibration, calibration_warnings = _calibration(keyframe, ome, imagej, size_t, size_z)
    metadata = _metadata(tif, all_series, series_index, n_ifds, ome, imagej)
    if lut is not None:
        metadata["palette"] = {"entries": int(lut.shape[1]), "bits": 8 * lut.dtype.itemsize}
    info = DatasetInfo(
        format="tiff",
        reader=TiffReader.name,
        size_x=size_x,
        size_y=size_y,
        dtype=dtype,
        size_t=size_t,
        size_c=size_c,
        size_z=size_z,
        calibration=calibration,
        stack_kind=_stack_kind(
            size_t, size_c, size_z, rgb, plain=series.kind in ("generic", "uniform", "shaped")
        ),
        rgb=rgb,
        channels=_channels(ome, imagej, size_c, rgb),
        metadata=metadata,
        warnings=(*calibration_warnings, *warnings),
    )
    page_shape: tuple[int, ...] = (size_y, size_x)
    if samples > 1:
        page_shape = (separate, size_y, size_x) if separate > 1 else (size_y, size_x, contig)
    layout = _Layout(
        lead=roles,
        per_page=per_page,
        planar=separate > 1,
        page_shape=page_shape,
        shaped=tuple(keyframe.shaped),
        page_nbytes=page_nbytes,
        stored_dtype=stored.newbyteorder(tif.byteorder),
        lut=lut,
        block_offset=block_offset,
    )
    return TiffDataset(path, info, file=file, series=series, layout=layout)


class TiffReader(Reader):
    """TIFF, BigTIFF, ImageJ and OME-TIFF. Option: ``series``, the index of the image series
    to open (default 0; ``info.metadata["series"]`` lists them)."""

    name: ClassVar[str] = "tiff"
    # .ome.tif(f) end with these; .gain is the gain reference EPU writes for EER movies
    extensions: ClassVar[tuple[str, ...]] = (".tif", ".tiff", ".btf", ".gain")

    def sniff(self, path: Path, head: bytes) -> bool:
        return head[:4] in _MAGIC

    def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
        path = Path(path)
        opts = dict(options or {})
        series = opts.pop("series", 0)
        if opts:
            raise InvalidRequestError(
                f"unknown option(s) for the tiff reader: {', '.join(sorted(map(str, opts)))}"
            )
        if not isinstance(series, int) or isinstance(series, bool) or series < 0:
            raise InvalidRequestError("option 'series' must be a non-negative integer")
        try:
            file = path.open("rb")
        except (FileNotFoundError, NotADirectoryError):
            raise NotFoundError(f"file not found: {path.name}") from None
        except OSError as e:
            raise CorruptFileError(
                f"{path.name}: cannot be read ({e.strerror or type(e).__name__})"
            ) from None
        try:
            return _open(file, path, series)
        except BaseException as e:
            file.close()
            if isinstance(e, ImageBioError) or not isinstance(e, Exception):
                raise
            # tifffile raises many exception types on malformed files; their text goes to the log
            log.warning("tiff: cannot open %s: %r", path.name, e)
            raise CorruptFileError(
                f"{path.name}: invalid or unsupported TIFF structure ({type(e).__name__})"
            ) from None


READER: Final = TiffReader()
