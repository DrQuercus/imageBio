"""EER reader (Thermo Fisher Falcon 4/4i electron-event movies).

An EER file is a TIFF container (BigTIFF in practice) with one page per *fraction*: about a
millisecond of exposure, stored as the run-length coded list of the electrons detected. A movie
holds 1000-1600 fractions of 4096 x 4096 pixels with only ~250 000 events each, so single
fractions are useless to look at. The dataset therefore exposes *groups*: plane ``t`` is the sum
of ``group`` consecutive fractions (electron counts, ``uint16``) and ``T`` is the number of groups.
``imagecodecs.eer_decode`` is the only bitstream decoder; the container is parsed here.

Format decisions
----------------
Container
    The directory chain is walked by hand, classic TIFF or BigTIFF, either byte order. tifffile
    would do it, but it silently stops at a broken chain, and a movie that is still being written
    must be an error, not a shorter movie. Every offset and size is checked against the real file
    size before anything is read or allocated; a loop, a directory or a strip beyond the end of
    the file, or pages that disagree on size or encoding are a :class:`CorruptFileError`.
Compression (TIFF tag 259)
    65000: 8-bit run lengths; 65001: 7-bit (the usual one); 65002: code sizes in tags
    65007-65009 (default 7, 2, 2), limited to what the decoder accepts. Each event carries
    2 + 2 sub-pixel bits. Any other compression is not an EER file, with one exception: an
    uncompressed first page followed by EER pages is the integrated image some files carry; it
    is skipped and reported (``metadata["integrated_image"]``).
Strips
    A fraction is one bitstream. When a page has several strips their bytes are concatenated, as
    RELION and Thermo Fisher's EerReaderLib do (tifffile decodes each strip on its own; the two
    readings only agree for one strip per page, the only layout seen so far).
Grouping (option ``group``)
    Default ``ceil(n_fractions / 50)``: at most 50 planes. The last group may hold fewer
    fractions: it is kept (RELION drops it), with a warning and
    ``metadata["last_group_fractions"]``.
Super-resolution (option ``superres`` = 0, 1 or 2)
    Renders the events on a grid refined 2 or 4 times with the sub-pixel bits; every event still
    counts once. It needs 2-bit sub-pixel positions (1 bit allows ``superres=1``): the convention
    for wider fields is not established, so they are refused instead of guessed.
Pixel size
    An EER file does not store the specimen pixel size: ``sensorPixelSize`` is the physical
    detector pixel (14 µm). The calibration is therefore the ``DEFAULT`` placeholder (1 px, at
    any ``superres``) with a warning; the caller takes the real value from the EPU ``.xml``.
    Only the frame time is known (``exposureTime``, hence ``Calibration.t``).
Metadata
    The acquisition XML of tag 65001 (``<metadata><item name=.. unit=..>``) goes to
    ``metadata["acquisition"]`` (units in ``"acquisition_units"``). Values that are canonical
    numbers become numbers, ``Yes``/``No`` booleans, the rest stays text (a serial number keeps
    its leading zeros). ``dose_per_fraction`` is derived from ``totalDose`` when present. A file
    holding another number of fractions than ``numberOfFrames`` gets a warning and
    ``metadata["complete"] = False`` when fractions are missing. XML with a DTD is not parsed.
Truncated or corrupt fractions
    The decoder stops silently when a stream runs out of data, so a zero-filled or cut strip
    would give a plausible, wrong image. Each fraction is therefore decoded into a frame with a
    few guard rows, after two zero bytes: a stream that reaches the last pixel leaves an event
    on the first guard pixel. When that mark is missing (or the decoder fails) the stream is
    decoded twice more at the exact size, followed once by all-ones and once by zero bits, and
    is only accepted when both give the same image, which is the case exactly when the last
    pixel is reached inside the strip. Anything else is a :class:`CorruptFileError`.

Speed and memory (measured: 4096 x 4096, 0.015 e/px, 384 kB per fraction, 4 cores)
    ``eer_decode`` adds the events to a ``uint8`` ``out`` array without clearing it, which sums
    fractions with no decoded copy: 3.6 ms per fraction on one thread against 6.9 ms for
    decode + add into ``uint16``. This behaviour is not documented, so it is verified once per
    process (:func:`_decoder_accumulates`), with decode-then-add as fallback. The decoder
    releases the GIL: with 4 threads a fraction costs 1.4 ms and a 40-fraction group 55 ms
    (150 ms on one thread), hence the small shared thread pool. Each thread reads its strips with
    ``os.pread`` and owns one ``uint8`` buffer of one frame, flushed into the result every 255
    fractions: a read holds the result plus at most four such buffers and four compressed strips.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import math
import os
import re
import stat
import struct
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final
from xml.etree import ElementTree

import imagecodecs
import numpy as np
import numpy.typing as npt

from imagebio.core.errors import (
    AccessDeniedError,
    CorruptFileError,
    ImageBioError,
    InvalidRequestError,
    NotFoundError,
    UnsupportedFormatError,
)
from imagebio.core.model import (
    AxisCalibration,
    Calibration,
    CalibrationSource,
    Dataset,
    DatasetInfo,
    Plane,
    StackKind,
)
from imagebio.io.registry import SNIFF_BYTES, Reader

__all__ = ["DEFAULT_MAX_FRAMES", "MAX_SIDE", "READER", "EerDataset", "EerReader", "default_group"]

DEFAULT_MAX_FRAMES: Final = 50
#: Largest frame side accepted, in detector pixels (a Falcon 4i is 4096 x 4096).
MAX_SIDE: Final = 8192

_VARIANTS: Final[Mapping[int, str]] = {
    65000: "EER, 8-bit run lengths",
    65001: "EER, 7-bit run lengths",
    65002: "EER, code sizes in tags 65007-65009",
}
_FIXED_BITS: Final[Mapping[int, tuple[int, int, int]]] = {65000: (8, 2, 2), 65001: (7, 2, 2)}
_DEFAULT_BITS: Final = (7, 2, 2)
_WIDTH, _HEIGHT, _COMPRESSION, _STRIP_OFFSETS, _STRIP_COUNTS = 256, 257, 259, 273, 279
_ACQUISITION, _CODE_BITS = 65001, (65007, 65008, 65009)

_UINT_TYPES: Final[Mapping[int, str]] = {1: "B", 3: "H", 4: "I", 13: "I", 16: "Q", 18: "Q"}
_BYTE_TYPES: Final = (1, 2, 7)  # BYTE, ASCII, UNDEFINED
_IFD_READ: Final = 512  # one read holds a usual directory (BigTIFF, 15 tags: 316 bytes)
_MAX_TAGS: Final = 1024
_MAX_FRACTIONS: Final = 1 << 17
_MAX_XML_BYTES: Final = 256 * 1024
_MAX_ITEMS: Final = 1024
_MAX_PLANE_PIXELS: Final = 16384 * 16384
_MAX_GROUP: Final = 65535  # a pixel counts at most one event per fraction: uint16 cannot overflow
_STREAM_SLACK: Final = 4096  # bytes of padding tolerated after a stream

_GUARD_PIXELS: Final = 256
_BATCH: Final = 255  # fractions summed in uint8 before widening
_MIN_CHUNK: Final = 4  # fractions per thread below which threads do not pay
_SCRATCH_BYTES: Final = 256 << 20  # bound on the uint8 buffers of one read
_WORKERS: Final = min(
    4, len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
)
_DECODE_ERRORS: Final = (RuntimeError, ValueError)  # imagecodecs.ImcdError is a RuntimeError


class _Malformed(Exception):
    """Why the content is not a readable EER file (the public error adds the file name)."""


# ─── Positional I/O ───────────────────────────────────────────────────────────

_seek_lock = threading.Lock()


def _seek_read(fd: int, nbytes: int, offset: int) -> bytes:
    """``os.pread`` for platforms without it (Windows): seek + read under a lock."""
    with _seek_lock:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, nbytes)


_pread: Final[Callable[[int, int, int], bytes]] = getattr(os, "pread", _seek_read)
_ReadFn = Callable[[int, int], bytes]  # (offset, nbytes) -> bytes, fewer at end of file


def _io_error(name: str, error: OSError) -> ImageBioError:
    """A server-side failure (not a property of the file content), without the server path."""
    return ImageBioError(f"{name}: I/O error ({error.strerror or type(error).__name__})")


def _open_fd(path: Path) -> int:
    """Open a regular file for reading. Error messages only carry the file name."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)  # never block on a FIFO
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        raise NotFoundError(f"file not found: {path.name}") from None
    except IsADirectoryError:
        raise UnsupportedFormatError(f"{path.name} is a directory") from None
    except PermissionError:
        raise AccessDeniedError(f"permission denied: {path.name}") from None
    except ValueError:  # embedded NUL
        raise NotFoundError("file not found: invalid file name") from None
    except OSError as e:
        raise _io_error(path.name, e) from None
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        os.close(fd)
        what = "a directory" if stat.S_ISDIR(mode) else "not a regular file"
        raise UnsupportedFormatError(f"{path.name} is {what}")
    return fd


def _file_reader(fd: int, size: int, name: str) -> _ReadFn:
    def read(offset: int, nbytes: int) -> bytes:
        if offset >= size:
            return b""
        try:
            return _pread(fd, min(nbytes, size - offset), offset)
        except OSError as e:
            raise _io_error(name, e) from None

    return read


class _SharedFd:
    """A descriptor shared by reader threads. Closing is deferred until the reads in flight are
    done, so a descriptor number is never reused under a running read."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._users = 0
        self._closed = False
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def borrow(self) -> Iterator[int]:
        with self._lock:
            if self._closed:
                raise ValueError("dataset is closed")
            self._users += 1
        try:
            yield self._fd
        finally:
            with self._lock:
                self._users -= 1
                if self._closed and self._users == 0:
                    os.close(self._fd)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._users == 0:
                    os.close(self._fd)


# ─── TIFF container ───────────────────────────────────────────────────────────

_Entry = tuple[int, int, bytes]  # field type, count, raw value-or-offset field


@dataclass(frozen=True, slots=True)
class _Container:
    order: str  # struct byte-order character
    big: bool  # BigTIFF
    first_ifd: int
    count: struct.Struct  # number of entries of a directory
    entry: struct.Struct  # tag, field type, count, value or offset
    pointer: struct.Struct  # file offset


def _parse_header(head: bytes) -> _Container | None:
    order = {b"II": "<", b"MM": ">"}.get(head[:2])
    if order is None or len(head) < 8:
        return None
    (version,) = struct.unpack_from(order + "H", head, 2)
    if version == 42:
        (first,) = struct.unpack_from(order + "I", head, 4)
        return _Container(order, False, first, *(struct.Struct(order + f) for f in ("H", "HHI4s", "I")))
    if version == 43 and len(head) >= 16:
        offset_size, zero, first = struct.unpack_from(order + "HHQ", head, 4)
        if (offset_size, zero) == (8, 0):
            return _Container(order, True, first, *(struct.Struct(order + f) for f in ("Q", "HHQ8s", "Q")))
    return None


def _read_ifd(read: _ReadFn, c: _Container, offset: int, page: int) -> tuple[dict[int, _Entry], int]:
    """The entries of the directory at ``offset`` and the offset of the next one (0: none)."""
    unfinished = f"image directory {page} lies beyond the end of the file (truncated or still being written)"
    block = read(offset, _IFD_READ)
    if len(block) < c.count.size:
        raise _Malformed(unfinished)
    (count,) = c.count.unpack_from(block)
    if not 1 <= count <= _MAX_TAGS:
        raise _Malformed(f"image directory {page} is invalid ({count} entries)")
    end = c.count.size + count * c.entry.size
    if len(block) < end + c.pointer.size:
        block += read(offset + len(block), end + c.pointer.size - len(block))
        if len(block) < end + c.pointer.size:
            raise _Malformed(unfinished)
    tags: dict[int, _Entry] = {}
    for tag, kind, n, raw in c.entry.iter_unpack(block[c.count.size : end]):
        tags.setdefault(tag, (kind, n, raw))
    return tags, c.pointer.unpack_from(block, end)[0]


def _uints(read: _ReadFn, c: _Container, tags: Mapping[int, _Entry], tag: int, limit: int) -> tuple[int, ...]:
    """The unsigned integers of ``tag`` (at most ``limit``); empty when the tag is absent."""
    if tag not in tags:
        return ()
    kind, count, raw = tags[tag]
    code = _UINT_TYPES.get(kind)
    if code is None or not 1 <= count <= limit:
        raise _Malformed(f"TIFF tag {tag} has an unexpected type or count")
    nbytes = count * struct.calcsize(code)
    if nbytes > len(raw):
        raw = read(c.pointer.unpack(raw)[0], nbytes)
        if len(raw) < nbytes:
            raise _Malformed(
                f"TIFF tag {tag} points beyond the end of the file (truncated or still being written)"
            )
    return struct.unpack_from(f"{c.order}{count}{code}", raw)


def _uint(
    read: _ReadFn, c: _Container, tags: Mapping[int, _Entry], tag: int, default: int | None = None
) -> int:
    values = _uints(read, c, tags, tag, 1)
    if values:
        return values[0]
    if default is None:
        raise _Malformed(f"TIFF tag {tag} is missing")
    return default


def _tag_bytes(read: _ReadFn, c: _Container, entry: _Entry, limit: int) -> bytes | None:
    kind, count, raw = entry
    if kind not in _BYTE_TYPES or count > limit:
        return None
    if count > len(raw):
        raw = read(c.pointer.unpack(raw)[0], count)
    return raw[:count] if len(raw) >= count else None


@dataclass(frozen=True, slots=True)
class _Layout:
    """Where the fractions of a file are, and how they are coded."""

    big: bool
    order: str
    width: int
    height: int
    compression: int
    bits: tuple[int, int, int]  # run length, horizontal and vertical sub-pixel bits
    strip_offsets: tuple[int, ...]  # the strips of every fraction, in file order
    strip_counts: tuple[int, ...]
    first_strip: tuple[int, ...]  # index of the first strip of each fraction, then the strip count
    xml: bytes | None  # acquisition metadata, tag 65001
    integrated: bool  # an uncompressed sum image precedes the fractions

    @property
    def n_fractions(self) -> int:
        return len(self.first_strip) - 1


def _check_coding(width: int, height: int, bits: tuple[int, int, int]) -> tuple[int, int]:
    """Validate the frame size and code sizes; return the least and most bytes of a valid stream."""
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
        raise _Malformed(f"implausible frame size {width} × {height}")
    skip, horz, vert = bits
    code = skip + horz + vert
    if not (4 <= skip <= 14 and 1 <= horz <= 4 and 1 <= vert <= 4 and 9 <= code <= 16):  # eer_decode's domain
        raise _Malformed(f"unsupported EER code sizes ({skip} + {horz} + {vert} bits)")
    pixels = width * height
    # every code moves on by 1 to 2**skip - 1 pixels: bounds on the size of a stream that ends on the last one
    least = max(0, pixels // ((1 << skip) - 1) - 1) * skip // 8
    most = ((pixels + 1) * code + 7) // 8 + _STREAM_SLACK
    return least, most


def _read_layout(read: _ReadFn, size: int) -> _Layout:
    """Walk the whole directory chain. Raises :class:`_Malformed`."""
    c = _parse_header(read(0, 16))
    if c is None:
        raise _Malformed("not an EER file (no TIFF signature)")
    coding: tuple[int, int, int, tuple[int, int, int]] | None = None
    least = most = total = 0
    offsets: list[int] = []
    counts: list[int] = []
    first = [0]
    xml: _Entry | None = None
    integrated = False
    seen: set[int] = set()
    offset = c.first_ifd
    for page in itertools.count():
        if not offset:
            break
        if offset in seen:
            raise _Malformed("the image directories form a loop")
        if page > _MAX_FRACTIONS:
            raise _Malformed(f"more than {_MAX_FRACTIONS} image directories")
        seen.add(offset)
        tags, offset = _read_ifd(read, c, offset, page)
        compression = _uint(read, c, tags, _COMPRESSION, 1)
        xml = xml or tags.get(_ACQUISITION)
        if compression not in _VARIANTS:
            if page == 0 and compression == 1 and offset:
                integrated = True  # sum image written by the camera: the fractions follow
                continue
            if coding is None:
                raise _Malformed(f"not an EER file (TIFF compression {compression})")
            raise _Malformed(f"page {page} is not an EER fraction (TIFF compression {compression})")
        index = len(first) - 1
        width, height = _uint(read, c, tags, _WIDTH), _uint(read, c, tags, _HEIGHT)
        bits = _FIXED_BITS.get(compression) or (
            _uint(read, c, tags, _CODE_BITS[0], _DEFAULT_BITS[0]),
            _uint(read, c, tags, _CODE_BITS[1], _DEFAULT_BITS[1]),
            _uint(read, c, tags, _CODE_BITS[2], _DEFAULT_BITS[2]),
        )
        if coding is None:
            least, most = _check_coding(width, height, bits)
            coding = (width, height, compression, bits)
        elif coding != (width, height, compression, bits):
            raise _Malformed(f"fraction {index} differs from the first one in size or encoding")
        strip_offsets = _uints(read, c, tags, _STRIP_OFFSETS, height)
        strip_counts = _uints(read, c, tags, _STRIP_COUNTS, height)
        if not strip_offsets or len(strip_offsets) != len(strip_counts):
            raise _Malformed(f"fraction {index} has no valid strip table")
        header = 16 if c.big else 8
        if any(n < 1 or o < header or o + n > size for o, n in zip(strip_offsets, strip_counts, strict=True)):
            raise _Malformed(
                f"fraction {index} lies beyond the end of the file (truncated or still being written)"
            )
        nbytes = sum(strip_counts)
        total += nbytes
        if not least <= nbytes <= most:
            raise _Malformed(f"fraction {index}: {nbytes} bytes cannot encode a {width} × {height} frame")
        if total > size:
            raise _Malformed("the fractions overlap each other")
        offsets += strip_offsets
        counts += strip_counts
        first.append(len(offsets))
    if coding is None:
        raise _Malformed("holds no EER fraction (empty or still being written)")
    width, height, compression, bits = coding
    return _Layout(
        big=c.big,
        order=c.order,
        width=width,
        height=height,
        compression=compression,
        bits=bits,
        strip_offsets=tuple(offsets),
        strip_counts=tuple(counts),
        first_strip=tuple(first),
        xml=_tag_bytes(read, c, xml, _MAX_XML_BYTES) if xml else None,
        integrated=integrated,
    )


# ─── Acquisition metadata ─────────────────────────────────────────────────────

_DTD: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_INT: Final = re.compile(r"[+-]?(0|[1-9]\d*)")
_FLOAT: Final = re.compile(r"[+-]?((0|[1-9]\d*)(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def _typed(text: str) -> Any:
    """``Yes``/``No`` and canonical numbers get their type; anything else stays text."""
    if text in ("Yes", "No"):
        return text == "Yes"
    if len(text) <= 32:
        if _INT.fullmatch(text):
            return int(text)
        if _FLOAT.fullmatch(text) and math.isfinite(value := float(text)):
            return value
    return text


def _parse_acquisition(xml: bytes) -> tuple[dict[str, Any], dict[str, str]] | None:
    """``(items, units)`` of ``<metadata><item name=".." unit="..">value</item>..``, or None."""
    text = xml.strip(b"\0 \t\r\n")
    if not text or _DTD.search(text):  # no DTD, hence no entity to expand
        return None
    try:
        root = ElementTree.fromstring(text)  # noqa: S314 - size bounded, DTD refused above
    except (ElementTree.ParseError, ValueError, LookupError):
        return None
    items: dict[str, Any] = {}
    units: dict[str, str] = {}
    for item in itertools.islice(root.iter("item"), _MAX_ITEMS):
        name = item.get("name")
        if name:
            items[name] = _typed((item.text or "").strip())
            if unit := item.get("unit"):
                units[name] = unit
    return items, units


def _positive(value: Any) -> float | None:
    """``value`` as a float when it is a finite number > 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _dose_per_fraction(
    items: Mapping[str, Any], units: Mapping[str, str], fractions: int
) -> dict[str, Any] | None:
    total = _positive(items.get("totalDose"))
    if total is not None:
        return {
            "value": total / fractions,
            "unit": units.get("totalDose"),
            "source": "totalDose / number of fractions",
        }
    rate, exposure = _positive(items.get("meanDoseRate")), _positive(items.get("exposureTime"))
    if rate and exposure and (units.get("meanDoseRate"), units.get("exposureTime")) == ("e/pixel/s", "s"):
        return {
            "value": rate * exposure / fractions,
            "unit": "e/pixel",
            "source": "meanDoseRate × exposureTime / number of fractions",
        }
    return None


# ─── Options and description ──────────────────────────────────────────────────


def default_group(n_fractions: int) -> int:
    """Fractions per plane giving at most :data:`DEFAULT_MAX_FRAMES` planes."""
    return max(1, math.ceil(n_fractions / DEFAULT_MAX_FRAMES))


def _options(options: Mapping[str, Any] | None) -> tuple[int | None, int]:
    opts = dict(options or {})
    group, superres = opts.pop("group", None), opts.pop("superres", None)
    if opts:
        raise InvalidRequestError(f"unknown EER reader option(s): {', '.join(sorted(map(str, opts)))}")
    if group is not None and (not isinstance(group, int) or isinstance(group, bool) or group < 1):
        raise InvalidRequestError("group must be an integer >= 1 (fractions summed per frame)")
    if superres is None:
        superres = 0
    if not isinstance(superres, int) or isinstance(superres, bool) or superres not in (0, 1, 2):
        raise InvalidRequestError("superres must be 0, 1 or 2 (x1, x2 or x4 sampling)")
    return group, superres


def _describe(layout: _Layout, group: int | None, superres: int) -> tuple[DatasetInfo, int]:
    """The dataset description and the group size in effect."""
    n = layout.n_fractions
    width, height = layout.width << superres, layout.height << superres
    skip, horz, vert = layout.bits
    if superres:
        if not superres <= min(horz, vert) <= max(horz, vert) <= 2:
            raise InvalidRequestError(
                f"superres={superres} is not available: this file stores {horz} + {vert} sub-pixel bits"
            )
        if width % (1 << horz) or height % (1 << vert):  # a limit of the decoder
            raise InvalidRequestError(f"superres={superres} needs an even frame width and height")
        if width * height > _MAX_PLANE_PIXELS:
            raise InvalidRequestError(f"superres={superres} would give a {width} × {height} image: too large")
    group = default_group(n) if group is None else group
    if min(group, n) > _MAX_GROUP:
        raise InvalidRequestError(f"group must be at most {_MAX_GROUP}")
    size_t = math.ceil(n / group)
    last = n - (size_t - 1) * group
    warnings = [
        "pixel size unknown: an EER file does not store the specimen pixel size (the EPU .xml of the "
        "movie has it), distances are in pixels"
    ]
    if last != group:
        warnings.append(f"the last frame sums {last} fraction(s) instead of {group}")
    acquisition = _parse_acquisition(layout.xml) if layout.xml is not None else None
    if acquisition is None:
        missing = layout.xml is None
        warnings.append(
            "no acquisition metadata (TIFF tag 65001)"
            if missing
            else "the acquisition metadata cannot be parsed and are ignored"
        )
    items, units = acquisition or ({}, {})
    announced = items.get("numberOfFrames")
    if isinstance(announced, bool) or not isinstance(announced, int) or announced < 1:
        announced = None
    if announced not in (None, n):
        cause = ": truncated or still being written" if n < (announced or 0) else ""
        warnings.append(f"the acquisition metadata announce {announced} fractions, the file holds {n}{cause}")
    exposed = announced or n  # fractions the exposure and the dose are spread over
    exposure = _positive(items.get("exposureTime")) if units.get("exposureTime") == "s" else None
    metadata: dict[str, Any] = {
        "n_fractions": n,
        "group": group,
        "last_group_fractions": last,
        "superres": superres,
        "sampling": 1 << superres,
        "detector_size": {"x": layout.width, "y": layout.height},
        "compression": layout.compression,
        "compression_name": _VARIANTS[layout.compression],
        "skip_bits": skip,
        "horz_bits": horz,
        "vert_bits": vert,
        "container": "BigTIFF" if layout.big else "TIFF",
        "byte_order": "little" if layout.order == "<" else "big",
        "integrated_image": layout.integrated,
        "complete": announced is None or n >= announced,
        "dose_per_fraction": _dose_per_fraction(items, units, exposed),
        "acquisition": items,
        "acquisition_units": units,
    }
    info = DatasetInfo(
        format="eer",
        reader=EerReader.name,
        size_x=width,
        size_y=height,
        dtype=np.dtype(np.uint16),
        size_t=size_t,
        calibration=Calibration(
            t=AxisCalibration(exposure * group / exposed, "s", CalibrationSource.METADATA)
            if exposure
            else None
        ),
        stack_kind=StackKind.MOVIE,
        metadata=metadata,
        warnings=tuple(warnings),
    )
    return info, group


# ─── Decoding ─────────────────────────────────────────────────────────────────

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    """The decoding threads, shared by every dataset (created on first use)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="imagebio-eer")
        return _pool


def _forget_pool() -> None:
    global _pool, _pool_lock
    _pool, _pool_lock = None, threading.Lock()


if hasattr(os, "register_at_fork"):  # the threads of the pool do not exist in a forked child
    os.register_at_fork(after_in_child=_forget_pool)


@functools.cache
def _decoder_accumulates() -> bool:
    """Whether ``eer_decode`` adds the events to a ``uint8`` ``out`` array without clearing it.

    imagecodecs 2026.10 does, which sums fractions without a decoded copy of each, but nothing
    documents it: it is checked once, on a 1 x 4 frame with one event decoded twice.
    """
    stream = b"\x01\x10\x00\x00"  # 7-bit codes: skip 1, event, skip 2
    out = np.zeros((1, 4), np.uint8)
    try:
        for _ in range(2):
            imagecodecs.eer_decode(stream, out.shape, 7, 2, 2, out=out)
    except Exception:
        return False
    return bool(out.tolist() == [[0, 2, 0, 0]])


class EerDataset(Dataset):
    """An open EER movie. Plane ``t`` is the sum of the fractions ``fraction_range(t)``."""

    def __init__(
        self, path: Path, info: DatasetInfo, layout: _Layout, fd: int, group: int, superres: int
    ) -> None:
        super().__init__(path, info)
        self.n_fractions: Final = layout.n_fractions
        self.group: Final = group
        self.superres: Final = superres
        self._layout = layout
        self._fd = _SharedFd(fd)
        guard = -(-_GUARD_PIXELS // layout.width)
        guard += -guard % 4  # keeps the padded frame a valid super-resolution size
        self._shape = (info.size_y, info.size_x)
        self._padded = ((layout.height + guard) << superres, info.size_x)
        self._mark = (slice(info.size_y, info.size_y + (1 << superres)), slice(0, 1 << superres))
        max_skip = (1 << layout.bits[0]) - 1  # all-ones bits: "skip, no event"
        ones = (layout.width * layout.height // max_skip + 2) * layout.bits[0] // 8 + 2
        self._ones_nbytes = ones + ones % 2

    def fraction_range(self, t: int) -> range:
        """The fractions summed in plane ``t``."""
        self.check_index(t, 0, 0)
        return range(t * self.group, min((t + 1) * self.group, self.n_fractions))

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        fractions = self.fraction_range(t)
        return self._sum(fractions.start, fractions.stop, np.uint16)

    def read_sum(self, start_fraction: int = 0, stop_fraction: int | None = None) -> npt.NDArray[np.uint32]:
        """Sum of the fractions ``start_fraction..stop_fraction`` (all by default), ``uint32``.

        The fast path for previews: one pass, whatever the group size. Same sampling as the planes.
        """
        stop = self.n_fractions if stop_fraction is None else stop_fraction
        for value in (start_fraction, stop):
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidRequestError("fraction indices must be integers")
        if not 0 <= start_fraction < stop <= self.n_fractions:
            raise InvalidRequestError(
                f"fraction range {start_fraction}..{stop} is empty or out of range (0..{self.n_fractions})"
            )
        return self._sum(start_fraction, stop, np.uint32)

    def _sum(self, start: int, stop: int, dtype: type[np.unsignedinteger[Any]]) -> Plane:
        total = np.zeros(self._shape, dtype)
        count = stop - start
        scratch = self._padded[0] * self._padded[1]
        workers = max(1, min(_WORKERS, count // _MIN_CHUNK, _SCRATCH_BYTES // scratch))
        edges = [start + count * k // workers for k in range(workers + 1)]
        lock, abort = threading.Lock(), threading.Event()
        with self._fd.borrow() as fd:

            def run(first: int, last: int) -> None:
                try:
                    self._accumulate(fd, first, last, total, lock, abort)
                except BaseException:
                    abort.set()  # the other threads stop at their next fraction
                    raise

            futures = [_executor().submit(run, a, b) for a, b in itertools.pairwise(edges[1:])]
            try:
                run(edges[0], edges[1])  # the calling thread takes its share
            finally:
                wait(futures)  # the descriptor and ``total`` must outlive every thread
            for future in futures:
                future.result()
        return total

    def _accumulate(
        self, fd: int, first: int, last: int, total: Plane, lock: threading.Lock, abort: threading.Event
    ) -> None:
        """Add the fractions ``first..last`` to ``total``, at most 255 at a time through ``uint8``."""
        direct = _decoder_accumulates()
        batch = np.zeros(self._padded, np.uint8)
        image = batch[: self._shape[0]]
        for start in range(first, last, _BATCH):
            fractions = range(start, min(start + _BATCH, last))
            if not (direct and self._add_directly(fd, fractions, batch, abort)):
                image[...] = 0
                self._add_checked(fd, fractions, image, abort)
            if abort.is_set():
                return
            with lock:
                np.add(total, image, out=total)
            image[...] = 0

    def _add_directly(self, fd: int, fractions: range, batch: Plane, abort: threading.Event) -> bool:
        """Fast path: the decoder adds each fraction to ``batch``. False as soon as a stream does
        not visibly end on the last pixel: ``batch`` is then discarded and the fractions redone."""
        rows = self._shape[0]
        for index in fractions:
            if abort.is_set():
                break
            data, _ = self._read_stream(fd, index)
            batch[rows:] = 0
            try:
                imagecodecs.eer_decode(
                    data, batch.shape, *self._layout.bits, superres=self.superres, out=batch
                )
            except _DECODE_ERRORS:
                return False
            if not batch[self._mark].any():
                return False
        return True

    def _add_checked(self, fd: int, fractions: range, image: Plane, abort: threading.Event) -> None:
        """Careful path: each fraction decoded on its own, doubtful endings settled exactly."""
        for index in fractions:
            if abort.is_set():
                break
            data, nbytes = self._read_stream(fd, index)
            try:
                frame = imagecodecs.eer_decode(data, self._padded, *self._layout.bits, superres=self.superres)
                complete = bool(frame[self._mark].any())
            except _DECODE_ERRORS:
                complete = False
            frame = frame[: self._shape[0]] if complete else self._decode_exactly(bytes(data[:nbytes]), index)
            np.add(image, frame.view(np.uint8), out=image)

    def _decode_exactly(self, stream: bytes, index: int) -> Plane:
        """Decode at the exact frame size, accepting only a stream that reaches the last pixel
        within its own bytes.

        The decoder ignores what follows the last pixel and stops silently when the data run
        out. Followed by all-ones bits ("skip, no event") a short stream overshoots, or lands by
        chance; followed by zero bits ("event on the next pixel") it gains an event. Only a
        complete stream decodes to the same image both ways.
        """
        odd = len(stream) % 2  # the decoder wants an even byte count
        try:
            frames = [
                imagecodecs.eer_decode(stream + tail, self._shape, *self._layout.bits, superres=self.superres)
                for tail in (b"\xff" * (self._ones_nbytes + odd), bytes(2 + odd))
            ]
        except _DECODE_ERRORS:
            frames = []
        if not frames or not np.array_equal(frames[0], frames[1]):
            raise CorruptFileError(
                f"{self._path.name}: fraction {index} is corrupt "
                "(its event stream does not end on the last pixel)"
            )
        return np.asarray(frames[0])

    def _read_stream(self, fd: int, index: int) -> tuple[bytearray, int]:
        """The bitstream of a fraction followed by 2 or 3 zero bytes (even total), and its length."""
        layout = self._layout
        strips = range(layout.first_strip[index], layout.first_strip[index + 1])
        nbytes = sum(layout.strip_counts[k] for k in strips)
        data = bytearray(nbytes + nbytes % 2 + 2)
        view = memoryview(data)
        done = 0
        for k in strips:
            offset, left = layout.strip_offsets[k], layout.strip_counts[k]
            while left:
                try:
                    chunk = _pread(fd, left, offset)
                except OSError as e:
                    raise _io_error(self._path.name, e) from None
                if not chunk:
                    raise CorruptFileError(
                        f"{self._path.name}: fraction {index} is incomplete (the file was truncated)"
                    )
                view[done : done + len(chunk)] = chunk
                done, offset, left = done + len(chunk), offset + len(chunk), left - len(chunk)
        return data, nbytes

    def close(self) -> None:
        self._fd.close()

    def __del__(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            fd.close()


# ─── Reader ───────────────────────────────────────────────────────────────────


def _sniff(path: Path, head: bytes) -> bool:
    c = _parse_header(head)
    if c is None or not c.first_ifd:
        return False
    fd: int | None = None

    def read(offset: int, nbytes: int) -> bytes:
        nonlocal fd
        if (
            offset + nbytes <= len(head) or len(head) < SNIFF_BYTES
        ):  # in the head, or the head is the whole file
            return head[offset : offset + nbytes]
        if fd is None:  # libtiff writes the first directory after the first strip, far from the head
            fd = _open_fd(path)
        return _pread(fd, nbytes, offset)

    try:
        tags, following = _read_ifd(read, c, c.first_ifd, 0)
        compression = _uint(read, c, tags, _COMPRESSION, 1)
    finally:
        if fd is not None:
            os.close(fd)
    # an uncompressed first page with the acquisition metadata is the integrated image of an EER file
    integrated = compression == 1 and bool(following) and tags.get(_ACQUISITION, (0,))[0] in _BYTE_TYPES
    return compression in _VARIANTS or integrated


class EerReader(Reader):
    """EER electron-event movies. Options: ``group`` (fractions summed per plane, default
    ``ceil(n_fractions / 50)``) and ``superres`` (0, 1 or 2: x1, x2 or x4 sampling)."""

    name: ClassVar[str] = "eer"
    extensions: ClassVar[tuple[str, ...]] = (".eer",)
    priority: ClassVar[int] = 10  # before the generic TIFF reader: an EER file is a TIFF

    def sniff(self, path: Path, head: bytes) -> bool:
        try:
            return _sniff(Path(path), head)
        except Exception:  # sniff() must not raise, whatever the file holds
            return False

    def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
        group, superres = _options(options)
        path = Path(path)
        fd = _open_fd(path)
        try:
            size = os.fstat(fd).st_size
            try:
                layout = _read_layout(_file_reader(fd, size, path.name), size)
            except _Malformed as e:
                raise CorruptFileError(f"{path.name}: {e}") from None
            info, group = _describe(layout, group, superres)
            return EerDataset(path, info, layout, fd, group, superres)
        except BaseException:
            os.close(fd)
            raise


READER: Final = EerReader()
