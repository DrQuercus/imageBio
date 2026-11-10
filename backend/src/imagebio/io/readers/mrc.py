"""MRC reader (MRC2014, with the IMOD/SerialEM and FEI/Thermo Fisher variants).

An MRC file is a 1024-byte header, an optional extended header of ``NSYMBT`` bytes, then ``NZ``
sections of ``NY`` rows. The header parser and the plane reader are written here: ``mrcfile`` loads
or maps whole files and knows neither mode 16, mode 101 nor the IMOD byte flags. It stays the
bit-exact reference of the tests and, at runtime, only supplies the FEI extended-header layouts.

Format decisions
----------------
Byte order
    From the machine stamp (bytes 212-213: ``44 44`` or ``44 41`` little-endian, ``11 11``
    big-endian). The stamp is only trusted if the header then makes sense (positive dimensions, a
    known mode, ``NSYMBT >= 0``, MAPC/MAPR/MAPS in 0..3); otherwise the other order is tried (IMOD
    relies on the same sanity test). A stamp that contradicts the header gives a warning.
Signature
    ``MAP`` at byte 208. Files without it (written before MRC2000) are accepted when the header
    passes the sanity test, with a warning; their origin and RMS fields are not interpreted.
Modes
    0 int8, 1 int16, 2 float32, 6 uint16, 12 float16 (returned as float32, which is exact),
    16 RGB (3 x uint8 per pixel: ``C = 3``, ``rgb=True``), 101 4-bit (uint8 values 0-15; two pixels
    per byte, the first one in the low nibble, and every row padded to a whole byte when NX is
    odd). Complex modes 3 and 4 and unknown modes are rejected: nothing is ever decoded as a guess.
Mode 0 signedness
    MRC2014 says signed, and that is what ``mrcfile`` returns. IMOD wrote unsigned bytes before
    4.2.23 and says which in its header: when ``imodStamp`` (int32 at byte 152) is 1146047817,
    bit 0 of ``imodFlags`` (byte 156) set means signed and clear means unsigned. Without the stamp
    the bytes are signed, unless the header statistics rule it out (``0 <= DMIN`` and
    ``127 < DMAX <= 255``, the unambiguous part of IMOD's rule for files without its stamp); that
    case is reported as a warning. ``imodFlags & 16`` marks old SerialEM files whose mode-0 bytes
    each hold two 4-bit pixels: they are unpacked like mode 101, twice as wide as NX says.
Pixel size
    ``CELLA / MX`` per axis (HEADER) when finite, positive and plausible; else "Pixel size X/Y"
    of the first FEI1/FEI2 record, in metres (EXTENDED_HEADER); else unknown (DEFAULT, 1 px, and
    a warning). When both exist and differ by more than 1 % the header wins and a warning says so.
    MAPC/MAPR/MAPS select which cell axis runs along columns, rows and sections; sections are
    always returned as stored (row 0 first), never transposed.
Extended header
    Always skipped by ``NSYMBT``. For EXTTYP FEI1/FEI2 the records are parsed with the
    "Metadata size" field as stride (newer, longer records keep working). Field names are those of
    the FEI specification, values are untouched (SI units: metres, volts, seconds; degrees).
    ``metadata["extended_header"]`` holds the first record and, for a few useful fields that change
    between sections (tilt, dose, defocus, timestamp...), one list per field. At most
    512 records are read. SerialEM ("SERI") records are not parsed yet.
Axes (heuristic; the ``stack_kind`` option overrides it)
    1. one section: IMAGE;
    2. by extension, because writers disagree on ISPG (RELION stacks say 1, IMOD tomograms 0):
       ``.mrcs`` STACK (T), ``.st``/``.ali`` TILT_SERIES (T), ``.map``/``.rec`` VOLUME (Z);
    3. ISPG 401-630: volume stack, ``T = NZ / MZ`` volumes of ``Z = MZ`` sections (HYPERSTACK);
    4. ISPG 1-230: VOLUME (Z);
    5. anything else is an image stack on T: TILT_SERIES when the FEI records show the tilt
       angle changing, else MOVIE.
Truncated or growing files
    The number of sections comes from the real file size. A file holding fewer complete sections
    than NZ exposes those it has, with a warning and ``metadata["complete"] = False``; without one
    complete section it is an error. The view is fixed when the file is opened: reopen a growing
    file to see more (its size and mtime, hence its cache identity, have changed). Trailing bytes
    are ignored.

Errors
    Whatever is wrong with the content is a :class:`CorruptFileError` (the registry then tries
    its next candidate) carrying the file name, never its directory. Sizes are checked against
    the real file size before anything is allocated. A failing ``read()`` is a server-side
    :class:`ImageBioError`, not a corrupt file.

Reads are positional (``os.preadv``), so one dataset serves many threads; a plane read touches
only the bytes of its section and allocates about one section.
"""

from __future__ import annotations

import contextlib
import math
import os
import stat
import struct
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

import numpy as np
from mrcfile.dtypes import get_ext_header_dtype

from imagebio.core.errors import (
    AccessDeniedError,
    CorruptFileError,
    ImageBioError,
    InvalidRequestError,
    NotFoundError,
    UnsupportedFormatError,
)
from imagebio.core.model import (
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

__all__ = ["HEADER_SIZE", "READER", "MrcDataset", "MrcHeader", "MrcReader", "parse_header", "read_header"]

HEADER_SIZE: Final = 1024
_IMOD_STAMP: Final = 1146047817  # "IMOD" as a little-endian int32
_IMOD_BYTES_SIGNED: Final = 1
_IMOD_4BIT_BYTES: Final = 16

_COMPLEX_MODES: Final = frozenset({3, 4})
_SAMPLE_TYPES: Final[Mapping[int, str]] = {1: "i2", 2: "f4", 6: "u2", 12: "f2"}
_KNOWN_MODES: Final = frozenset({0, 16, 101, *_SAMPLE_TYPES, *_COMPLEX_MODES})

# nx ny nz mode nstart(3) m(3) | cella(3) cellb(3) | mapc mapr maps | dmin dmax dmean | ispg nsymbt |
# extra | exttyp | nversion | extra | imodStamp imodFlags | extra | origin(3) | map | machst | rms |
# nlabl | labels
_FIELDS: Final = "10i6f3i3f2i8s4si40s2i36s3f4s4sfi800s"
_STRUCTS: Final = {order: struct.Struct(order + _FIELDS) for order in "<>"}
assert all(s.size == HEADER_SIZE for s in _STRUCTS.values())

#: Plausible pixel sizes, in Å. Anything else in a header is an unset or corrupt field.
_PIXEL_RANGE: Final = (1e-4, 1e8)
_PIXEL_TOLERANCE: Final = 0.01

_FEI_RECORD_RANGE: Final = (768, 4096)  # bytes; FEI1 records are 768 bytes long, FEI2 ones 888
_FEI_MAX_RECORDS: Final = 512
_FEI_SERIES_FIELDS: Final = (
    "Timestamp", "Dose", "Alpha tilt", "Beta tilt", "X-Stage", "Y-Stage", "Z-Stage", "Tilt axis angle",
    "Defocus", "Applied defocus", "Integration time", "Fraction number", "Start frame", "End frame",
    "Acquisition time stamp",
)  # fmt: skip
_TILT_SERIES_MIN_RANGE: Final = 0.5  # degrees of alpha tilt between sections

_KIND_BY_SUFFIX: Final[Mapping[str, StackKind]] = {
    ".mrcs": StackKind.STACK,
    ".st": StackKind.TILT_SERIES,
    ".ali": StackKind.TILT_SERIES,
    ".map": StackKind.VOLUME,
    ".rec": StackKind.VOLUME,
}
_KIND_OPTIONS: Final[Mapping[str, StackKind]] = {
    k.value: k for k in (StackKind.MOVIE, StackKind.STACK, StackKind.TILT_SERIES, StackKind.VOLUME)
}
_RGB_CHANNELS: Final = (
    ChannelInfo("R", (255, 0, 0)),
    ChannelInfo("G", (0, 255, 0)),
    ChannelInfo("B", (0, 0, 255)),
)
_UINT8: Final = np.dtype(np.uint8)


# ─── Header ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Layout:
    """How the sections of a file are stored and handed out."""

    stored: np.dtype[Any]  # sample type in the file, with the file's byte order
    plane: np.dtype[Any]  # dtype of the planes returned
    size_x: int  # pixels per row
    row_nbytes: int  # bytes per stored row
    channels: int = 1
    nibbles: bool = False


@dataclass(frozen=True, slots=True)
class MrcHeader:
    """A validated MRC header: readable mode, positive dimensions, non-negative ``NSYMBT``."""

    nx: int
    ny: int
    nz: int
    mode: int
    nstart: tuple[int, int, int]
    sampling: tuple[int, int, int]  # MX, MY, MZ
    cell: tuple[float, float, float]  # CELLA, in Å
    cell_angles: tuple[float, float, float]
    axis_order: tuple[int, int, int]  # MAPC, MAPR, MAPS
    dmin: float
    dmax: float
    dmean: float
    rms: float | None  # None in pre-2000 files (no 'MAP ' signature), like origin
    ispg: int
    nsymbt: int
    exttyp: str
    nversion: int
    imod_flags: int | None  # None unless the IMOD stamp is present
    origin: tuple[float, float, float] | None
    labels: tuple[str, ...]
    byte_order: str  # "<" or ">"
    stamp_order: str | None  # what the machine stamp announces; None when missing or invalid
    has_map_id: bool

    @property
    def data_offset(self) -> int:
        return HEADER_SIZE + self.nsymbt

    @property
    def bytes_signed(self) -> bool:
        """Whether mode-0 bytes are signed (see the module docstring)."""
        if self.imod_flags is not None:
            return bool(self.imod_flags & _IMOD_BYTES_SIGNED)
        return not (self.dmin >= 0 and 127 < self.dmax <= 255)

    @property
    def _layout(self) -> _Layout:
        nx = self.nx
        if self.mode == 0:
            if self.imod_flags is not None and self.imod_flags & _IMOD_4BIT_BYTES:
                return _Layout(_UINT8, _UINT8, 2 * nx, nx, nibbles=True)
            byte = np.dtype(np.int8 if self.bytes_signed else np.uint8)
            return _Layout(byte, byte, nx, nx)
        if self.mode == 101:
            return _Layout(_UINT8, _UINT8, nx, (nx + 1) // 2, nibbles=True)
        if self.mode == 16:
            return _Layout(_UINT8, _UINT8, nx, 3 * nx, channels=3)
        stored = np.dtype(self.byte_order + _SAMPLE_TYPES[self.mode])
        plane = np.dtype(np.float32) if self.mode == 12 else stored.newbyteorder("=")
        return _Layout(stored, plane, nx, nx * stored.itemsize)

    @property
    def section_nbytes(self) -> int:
        """Bytes of one stored section."""
        return self._layout.row_nbytes * self.ny

    @property
    def expected_size(self) -> int:
        """Size of the complete file. A smaller file is truncated or still being written."""
        return self.data_offset + self.nz * self.section_nbytes


def _text(raw: bytes) -> str:
    return "".join(ch if " " <= ch <= "~" else " " for ch in raw.decode("latin-1")).strip()


def _unpack(buf: bytes, order: str, stamp_order: str | None, has_map_id: bool) -> MrcHeader:
    (
        nx, ny, nz, mode, nxstart, nystart, nzstart, mx, my, mz, cx, cy, cz, alpha, beta, gamma,
        mapc, mapr, maps, dmin, dmax, dmean, ispg, nsymbt, _, exttyp, nversion, _, imod_stamp, imod_flags, _,
        ox, oy, oz, _, _, rms, nlabl, labels,
    ) = _STRUCTS[order].unpack_from(buf)  # fmt: skip
    texts = (_text(labels[80 * i : 80 * i + 80]) for i in range(min(max(nlabl, 0), 10)))
    return MrcHeader(
        nx=nx, ny=ny, nz=nz, mode=mode, nstart=(nxstart, nystart, nzstart), sampling=(mx, my, mz),
        cell=(cx, cy, cz), cell_angles=(alpha, beta, gamma), axis_order=(mapc, mapr, maps),
        dmin=dmin, dmax=dmax, dmean=dmean, rms=rms if has_map_id else None, ispg=ispg, nsymbt=nsymbt,
        exttyp=_text(exttyp), nversion=nversion, imod_flags=imod_flags if imod_stamp == _IMOD_STAMP else None,
        origin=(ox, oy, oz) if has_map_id else None, labels=tuple(t for t in texts if t),
        byte_order=order, stamp_order=stamp_order, has_map_id=has_map_id,
    )  # fmt: skip


def _problem(h: MrcHeader) -> str | None:
    """Why the header cannot describe a readable file, or None."""
    if min(h.nx, h.ny, h.nz) < 1:
        return f"invalid dimensions {h.nx} × {h.ny} × {h.nz}"
    if h.mode not in _KNOWN_MODES:
        return f"unsupported MRC mode {h.mode}"
    if h.nsymbt < 0:
        return f"invalid extended header size ({h.nsymbt} bytes)"
    return None


def parse_header(buf: bytes, name: str) -> MrcHeader:
    """Parse and validate the first 1024 bytes of an MRC file.

    ``name`` is the file name used in error messages. Raises :class:`CorruptFileError`.
    """
    if len(buf) < HEADER_SIZE:
        raise CorruptFileError(
            f"{name}: shorter than the {HEADER_SIZE}-byte MRC header (truncated or still being written)"
        )
    # 'MAP' without the trailing space is accepted, as mrcfile does: some writers omit it
    has_map_id = buf[208:211] == b"MAP"
    stamp = buf[212:214] if has_map_id else b""  # pre-2000 files keep the origin in these bytes
    stamped = "<" if stamp in (b"\x44\x44", b"\x44\x41") else ">" if stamp == b"\x11\x11" else None
    orders = "><" if stamped == ">" else "<>"
    candidates = [_unpack(buf, order, stamped, has_map_id) for order in orders]
    sane = (h for h in candidates if _problem(h) is None and all(0 <= axis <= 3 for axis in h.axis_order))
    header = next(sane, None)
    if header is None:
        if not has_map_id:
            raise CorruptFileError(f"{name} is not an MRC file (no 'MAP ' signature and no plausible header)")
        header = candidates[0]  # the announced byte order: report its problem, or accept odd MAPC/MAPR/MAPS
        if (problem := _problem(header)) is not None:
            raise CorruptFileError(f"{name}: {problem}")
    if header.mode in _COMPLEX_MODES:
        raise CorruptFileError(
            f"{name}: MRC mode {header.mode} (complex data, e.g. a Fourier transform) is not supported"
        )
    return header


def read_header(path: str | os.PathLike[str]) -> MrcHeader:
    """The validated header of ``path`` (``header.expected_size`` tells whether the file is complete)."""
    name = Path(path).name
    fd = _open_fd(Path(path))
    try:
        return parse_header(_read_bytes(fd, HEADER_SIZE, 0, name), name)
    finally:
        os.close(fd)


# ─── Positional I/O ───────────────────────────────────────────────────────────

_READ_CHUNK = 1 << 30  # a single read() never returns more than 2 GiB on Linux
_seek_lock = threading.Lock()


def _preadv_into(fd: int, buf: memoryview, offset: int) -> int:
    """Fill ``buf`` from ``offset``; the count is short only at end of file. Thread-safe."""
    done = 0
    while done < len(buf):
        n = os.preadv(fd, [buf[done : done + _READ_CHUNK]], offset + done)
        if n <= 0:
            break
        done += n
    return done


def _seek_read_into(fd: int, buf: memoryview, offset: int) -> int:
    """Same contract for platforms without ``preadv`` (Windows): seek + read under a lock."""
    done = 0
    while done < len(buf):
        with _seek_lock:
            os.lseek(fd, offset + done, os.SEEK_SET)
            chunk = os.read(fd, min(len(buf) - done, _READ_CHUNK))
        if not chunk:
            break
        buf[done : done + len(chunk)] = chunk
        done += len(chunk)
    return done


_read_into = _preadv_into if hasattr(os, "preadv") else _seek_read_into


def _io_error(name: str, error: OSError) -> ImageBioError:
    """A server-side failure (not a property of the file content), without the server path."""
    return ImageBioError(f"{name}: I/O error ({error.strerror or type(error).__name__})")


def _read_bytes(fd: int, nbytes: int, offset: int, name: str) -> bytes:
    """Up to ``nbytes`` bytes from ``offset`` (fewer at end of file)."""
    buf = bytearray(nbytes)
    try:
        got = _read_into(fd, memoryview(buf), offset)
    except OSError as e:
        raise _io_error(name, e) from None
    return bytes(buf[:got])


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


# ─── FEI extended header ──────────────────────────────────────────────────────


def _plausible_pixel(angstrom: float) -> float | None:
    return angstrom if math.isfinite(angstrom) and _PIXEL_RANGE[0] <= angstrom <= _PIXEL_RANGE[1] else None


def _fei_value(value: Any) -> Any:
    """A JSON-safe FEI field value, or None when empty or not finite."""
    if isinstance(value, bytes):
        return _text(value.split(b"\0", 1)[0]) or None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class _FeiHeader:
    metadata: dict[str, Any]
    pixel: tuple[float | None, float | None]  # Å, from the first record
    tilt_range: float  # degrees of alpha tilt spanned by the records read


def _read_fei(fd: int, header: MrcHeader, name: str) -> _FeiHeader | None:
    """Parse FEI1/FEI2 records, or return None when they cannot be interpreted as such."""
    # every record starts with its own size ("Metadata size", int32): the stride between records
    endian: Literal["little", "big"] = "little" if header.byte_order == "<" else "big"
    stride = int.from_bytes(_read_bytes(fd, 4, HEADER_SIZE, name), endian, signed=True)
    if not _FEI_RECORD_RANGE[0] <= stride <= _FEI_RECORD_RANGE[1]:
        return None
    total = min(header.nz, header.nsymbt // stride)
    count = min(total, _FEI_MAX_RECORDS)
    blob = _read_bytes(fd, count * stride, HEADER_SIZE, name)
    if count == 0 or len(blob) < count * stride:
        return None
    # FEI2 appends fields to the 768-byte FEI1 record; a record too short for them is read as FEI1
    version = b"FEI2" if header.exttyp == "FEI2" and stride >= 888 else b"FEI1"
    layout: np.dtype[Any] = get_ext_header_dtype(version, header.byte_order)
    rows = np.frombuffer(blob, dtype=np.uint8).reshape(count, stride)[:, : layout.itemsize]
    records = np.ascontiguousarray(rows).view(layout).reshape(count)
    names = [
        n
        for n in layout.names or ()
        if not n.startswith(("Bitmask", "Unused", "Metadata size")) and "reserved" not in n
    ]
    first = {n: v for n in names if (v := _fei_value(records[n][0].item())) is not None}
    series: dict[str, list[Any]] = {}
    for n in _FEI_SERIES_FIELDS:
        if n in names and (records[n] != records[n][0]).any():
            series[n] = [_fei_value(v) for v in records[n].tolist()]
    tilts = [float(v) for v in records["Alpha tilt"].tolist()]
    metadata = {
        "sections": total,
        "record_nbytes": stride,
        "first_section": first,
        "per_section": series,
        "per_section_truncated": total > count,
    }
    pixel = (
        _plausible_pixel(float(records["Pixel size X"][0]) * 1e10),
        _plausible_pixel(float(records["Pixel size Y"][0]) * 1e10),
    )
    tilt_range = max(tilts) - min(tilts) if all(map(math.isfinite, tilts)) else 0.0
    return _FeiHeader(metadata, pixel, tilt_range)


# ─── Calibration and axes ─────────────────────────────────────────────────────


def _calibration(header: MrcHeader, fei: _FeiHeader | None, has_z: bool, warnings: list[str]) -> Calibration:
    spacing = [
        _plausible_pixel(length / n) if n > 0 and math.isfinite(length) else None
        for length, n in zip(header.cell, header.sampling, strict=True)
    ]
    order = header.axis_order
    if sorted(order) != [1, 2, 3]:
        order = (1, 2, 3)  # unset in many files: the standard order is assumed, as every reader does
    elif order != (1, 2, 3):
        mapc, mapr, maps = order
        warnings.append(
            f"non-standard axis order (MAPC/MAPR/MAPS = {mapc}/{mapr}/{maps}): sections are shown as stored"
        )
    along_columns, along_rows, along_sections = (spacing[axis - 1] for axis in order)

    def axis(from_header: float | None, from_fei: float | None) -> AxisCalibration:
        if from_header is None and from_fei is None:
            warnings.append("pixel size unknown: not set in the file, distances are in pixels")
            return UNKNOWN_PIXEL
        if from_header is None:
            assert from_fei is not None
            return AxisCalibration(from_fei, "Å", CalibrationSource.EXTENDED_HEADER)
        if from_fei is not None and abs(from_fei - from_header) > _PIXEL_TOLERANCE * from_header:
            warnings.append(
                f"pixel size: the header says {from_header:.4g} Å but the {header.exttyp} extended header "
                f"says {from_fei:.4g} Å (header value kept)"
            )
        return AxisCalibration(from_header, "Å", CalibrationSource.HEADER)

    fei_x, fei_y = fei.pixel if fei else (None, None)
    x, y, z = axis(along_columns, fei_x), axis(along_rows, fei_y), None
    if has_z and along_sections is not None:
        z = AxisCalibration(along_sections, "Å", CalibrationSource.HEADER)
    elif has_z:
        warnings.append("Z spacing unknown: not set in the header")
        z = UNKNOWN_PIXEL
    return Calibration(x=x, y=y, z=z)


def _axes(
    header: MrcHeader,
    sections: int,
    suffix: str,
    fei: _FeiHeader | None,
    forced: StackKind | None,
    warnings: list[str],
) -> tuple[int, int, StackKind]:
    """``(size_t, size_z, kind)`` of a file holding ``sections`` complete sections."""
    if sections == 1:
        return 1, 1, StackKind.IMAGE
    kind = forced or _KIND_BY_SUFFIX.get(suffix)
    if kind is None and 401 <= header.ispg <= 630:
        mz = header.sampling[2]
        if mz < 1 or header.nz % mz:
            warnings.append(
                f"volume stack (ISPG {header.ispg}) whose NZ = {header.nz} is not a multiple of MZ = {mz}: "
                "shown as a single volume"
            )
            kind = StackKind.VOLUME
        elif mz == 1:
            kind = StackKind.STACK
        else:  # only whole volumes are exposed
            volumes = sections // mz
            if volumes >= 2:
                return volumes, mz, StackKind.HYPERSTACK
            return 1, mz if volumes else sections, StackKind.VOLUME
    if kind is None and 1 <= header.ispg <= 230:
        kind = StackKind.VOLUME
    if kind is None:
        tilted = fei is not None and fei.tilt_range > _TILT_SERIES_MIN_RANGE
        kind = StackKind.TILT_SERIES if tilted else StackKind.MOVIE
    return (1, sections, kind) if kind is StackKind.VOLUME else (sections, 1, kind)


def _options(options: Mapping[str, Any] | None) -> StackKind | None:
    opts = dict(options or {})
    kind = opts.pop("stack_kind", None)
    if opts:
        raise InvalidRequestError(f"unknown MRC reader option(s): {', '.join(sorted(map(str, opts)))}")
    if kind is None:
        return None
    if not isinstance(kind, str) or kind not in _KIND_OPTIONS:
        raise InvalidRequestError(f"stack_kind must be one of: {', '.join(_KIND_OPTIONS)}")
    return _KIND_OPTIONS[kind]


# ─── Dataset and reader ───────────────────────────────────────────────────────


class MrcDataset(Dataset):
    """An open MRC file. ``header`` is the parsed main header."""

    def __init__(self, path: Path, info: DatasetInfo, header: MrcHeader, fd: int) -> None:
        super().__init__(path, info)
        self.header = header
        self._layout = header._layout
        self._section_nbytes = header.section_nbytes
        self._fd = _SharedFd(fd)

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        section = t * self._info.size_z + z
        raw = np.empty(self._section_nbytes, dtype=np.uint8)
        offset = self.header.data_offset + section * self._section_nbytes
        name = self._path.name
        try:
            with self._fd.borrow() as fd:
                got = _read_into(fd, memoryview(raw), offset)
        except OSError as e:
            raise _io_error(name, e) from None
        if got != raw.size:
            raise CorruptFileError(f"{name}: section {section} is incomplete (the file was truncated)")
        return self._decode(raw, c)

    def _decode(self, raw: Plane, c: int) -> Plane:
        layout, ny = self._layout, self._info.size_y
        if layout.nibbles:
            packed = raw.reshape(ny, layout.row_nbytes)
            plane = np.empty((ny, layout.size_x), dtype=np.uint8)
            plane[:, 0::2] = packed & 0x0F  # the first pixel of a byte is its low nibble
            plane[:, 1::2] = (packed >> 4)[:, : layout.size_x // 2]  # drops the padding nibble
            return plane
        if layout.channels > 1:
            return np.ascontiguousarray(raw.reshape(ny, layout.size_x, layout.channels)[:, :, c])
        plane = raw.view(layout.stored).reshape(ny, layout.size_x)
        if not layout.stored.isnative:
            plane = plane.byteswap(inplace=True).view(layout.stored.newbyteorder("="))
        return plane if plane.dtype == layout.plane else plane.astype(layout.plane)

    def close(self) -> None:
        self._fd.close()

    def __del__(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            fd.close()


def _metadata(header: MrcHeader, sections: int, fei: _FeiHeader | None) -> dict[str, Any]:
    h = header
    meta: dict[str, Any] = {
        "mode": h.mode,
        "byte_order": "little" if h.byte_order == "<" else "big",
        "byte_order_source": "machine_stamp" if h.stamp_order == h.byte_order else "header",
        "nx": h.nx,
        "ny": h.ny,
        "nz": h.nz,
        "sections_in_file": sections,
        "complete": sections == h.nz,
        "ispg": h.ispg,
        "nsymbt": h.nsymbt,
        "exttyp": h.exttyp,
        "nversion": h.nversion,
        "mapc": h.axis_order[0],
        "mapr": h.axis_order[1],
        "maps": h.axis_order[2],
        "nstart": h.nstart,
        "sampling": h.sampling,
        "cell": h.cell,
        "cell_angles": h.cell_angles,
        "origin": h.origin,
        # written by the acquisition software, never recomputed here: hints, not measurements
        "statistics_hint": {"dmin": h.dmin, "dmax": h.dmax, "dmean": h.dmean, "rms": h.rms},
        "labels": h.labels,
    }
    if h.imod_flags is not None:
        meta["imod_flags"] = h.imod_flags
    if h.mode == 0 and not h._layout.nibbles:
        meta["bytes_signed"] = h.bytes_signed
    if h.nsymbt:
        meta["extended_header"] = {"type": h.exttyp, "nbytes": h.nsymbt} | (fei.metadata if fei else {})
    return meta


class MrcReader(Reader):
    """MRC2014 files and their IMOD and FEI variants. Option: ``stack_kind`` = ``"movie"``,
    ``"stack"``, ``"tilt_series"`` or ``"volume"`` to override the axis heuristic."""

    name: ClassVar[str] = "mrc"
    extensions: ClassVar[tuple[str, ...]] = (".mrc", ".mrcs", ".map", ".rec", ".st", ".ali")

    def sniff(self, path: Path, head: bytes) -> bool:
        if len(head) < HEADER_SIZE or head[208:211] != b"MAP":
            return False
        try:
            parse_header(head[:HEADER_SIZE], path.name)
        except CorruptFileError:
            return False
        return True

    def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
        forced = _options(options)
        path = Path(path)
        fd = _open_fd(path)
        try:
            info, header = self._describe(fd, path.name, path.suffix.lower(), forced)
            return MrcDataset(path, info, header, fd)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _describe(fd: int, name: str, suffix: str, forced: StackKind | None) -> tuple[DatasetInfo, MrcHeader]:
        size = os.fstat(fd).st_size
        header = parse_header(_read_bytes(fd, HEADER_SIZE, 0, name), name)
        layout = header._layout
        if header.data_offset > size:
            raise CorruptFileError(
                f"{name}: the extended header ({header.nsymbt} bytes) runs past the end of the file "
                "(truncated or still being written)"
            )
        sections = min(header.nz, (size - header.data_offset) // header.section_nbytes)
        if sections == 0:
            raise CorruptFileError(
                f"{name}: the header says {header.nx} × {header.ny} × {header.nz} (mode {header.mode}) but "
                "the file does not hold one complete section (truncated or still being written)"
            )
        warnings: list[str] = []
        if sections < header.nz:
            warnings.append(
                f"header says {header.nz} sections, file holds {sections}: truncated or still being written"
            )
        if not header.has_map_id:
            warnings.append("no 'MAP ' signature: read as a pre-2000 MRC file")
        if header.stamp_order not in (None, header.byte_order):
            order = "little" if header.byte_order == "<" else "big"
            warnings.append(
                f"the machine stamp contradicts the header, which only makes sense as {order}-endian"
            )
        if header.mode == 0 and layout.nibbles:
            warnings.append(
                f"IMOD 4-bit flag: each byte holds two 4-bit pixels, the image is {layout.size_x} pixels wide"
            )
        elif header.mode == 0 and not header.bytes_signed and header.imod_flags is None:
            warnings.append(
                f"mode 0 bytes read as unsigned: the header maximum ({header.dmax:g}) rules out signed bytes"
            )
        fei = None
        if header.nsymbt and header.exttyp in ("FEI1", "FEI2"):
            fei = _read_fei(fd, header, name)
            if fei is None:
                warnings.append(f"the {header.exttyp} extended header cannot be interpreted and is ignored")
        size_t, size_z, kind = _axes(header, sections, suffix, fei, forced, warnings)
        if layout.channels > 1 and kind is not StackKind.IMAGE:
            kind = StackKind.HYPERSTACK
        info = DatasetInfo(
            format="mrc",
            reader=MrcReader.name,
            size_x=layout.size_x,
            size_y=header.ny,
            dtype=layout.plane,
            size_t=size_t,
            size_c=layout.channels,
            size_z=size_z,
            calibration=_calibration(header, fei, size_z > 1, warnings),
            stack_kind=kind,
            rgb=layout.channels == 3,
            channels=_RGB_CHANNELS if layout.channels == 3 else (),
            metadata=_metadata(header, sections, fei),
            warnings=tuple(dict.fromkeys(warnings)),
        )
        return info, header


READER: Final = MrcReader()
