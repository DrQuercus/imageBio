"""Synthetic EER files (Falcon 4/4i electron-event movies) and independent references.

Nothing here uses the reader under test. The bitstream follows the format description of
``docs/research/cryoem-live.md`` (section 1), checked there against RELION's ``renderEER.cpp``:
LSB-first codes, a run length of ``skip_bits`` bits where the all-ones value means "skip, no
event", otherwise followed by the sub-pixel position (``horz_bits`` low bits, then ``vert_bits``),
and a last run that lands exactly on the pixel count.

* :func:`encode_fraction_naive` is the research encoder (``docs/research/eer_synthetic_encoder.py``)
  generalised to any code size; :func:`encode_fraction` is its vectorised twin for large frames.
* :func:`decode_fraction_naive` is a strict pure-Python decoder (RELION's loop).
* :func:`render` builds the expected image from the event list alone.
* :func:`write_tiff` lays out a classic TIFF or BigTIFF by hand, in either byte order, the way
  libtiff does (strip data first, then the directory) or directory first.
* :func:`write_eer` puts it all together and returns the ground truth with the file layout, so
  that tests can damage a precise strip, tag or directory pointer.
"""

from __future__ import annotations

import itertools
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# TIFF field types
BYTE, ASCII, SHORT, LONG, UNDEFINED, LONG8 = 1, 2, 3, 4, 7, 16
_FORMATS = {BYTE: "B", SHORT: "H", LONG: "I", LONG8: "Q"}

# TIFF tags
IMAGE_WIDTH, IMAGE_LENGTH, BITS_PER_SAMPLE, COMPRESSION, PHOTOMETRIC = 256, 257, 258, 259, 262
STRIP_OFFSETS, SAMPLES_PER_PIXEL, ROWS_PER_STRIP, STRIP_BYTE_COUNTS = 273, 277, 278, 279
ACQUISITION_XML, SKIP_BITS, HORZ_BITS, VERT_BITS = 65001, 65007, 65008, 65009


@dataclass(frozen=True)
class Encoding:
    """An EER compression variant: TIFF compression code and code sizes."""

    compression: int = 65001
    skip_bits: int = 7
    horz_bits: int = 2
    vert_bits: int = 2

    @property
    def sub_bits(self) -> int:
        return self.horz_bits + self.vert_bits

    @property
    def max_skip(self) -> int:
        return (1 << self.skip_bits) - 1


V0 = Encoding(65000, 8, 2, 2)  # legacy 8-bit run length
V1 = Encoding(65001, 7, 2, 2)  # the usual Falcon 4 encoding


def v2(skip_bits: int = 7, horz_bits: int = 2, vert_bits: int = 2) -> Encoding:
    """The "detailed" variant: code sizes in tags 65007-65009."""
    return Encoding(65002, skip_bits, horz_bits, vert_bits)


@dataclass(frozen=True)
class Events:
    """The electrons of one fraction: sorted unique flat pixel indices and stored sub-pixel values."""

    pixels: np.ndarray
    sub_x: np.ndarray
    sub_y: np.ndarray

    def __len__(self) -> int:
        return len(self.pixels)


def random_events(
    rng: np.random.Generator, n_pixels: int, count: int, encoding: Encoding = V1, hot: Sequence[int] = ()
) -> Events:
    """``count`` random events plus one on each ``hot`` pixel."""
    cold = np.setdiff1d(np.arange(n_pixels), np.asarray(hot, dtype=np.int64))
    pixels = np.sort(
        np.concatenate([rng.choice(cold, count, replace=False), np.asarray(hot, dtype=np.int64)])
    )
    return Events(
        pixels.astype(np.int64),
        rng.integers(0, 1 << encoding.horz_bits, len(pixels)),
        rng.integers(0, 1 << encoding.vert_bits, len(pixels)),
    )


# ─── Bitstream ────────────────────────────────────────────────────────────────


def pack_codes(codes: Sequence[tuple[int, int]]) -> bytes:
    """Pack ``(value, bit count)`` codes LSB-first."""
    acc = n = 0
    out = bytearray()
    for value, nbits in codes:
        acc |= value << n
        n += nbits
        while n >= 8:
            out.append(acc & 255)
            acc >>= 8
            n -= 8
    if n:
        out.append(acc & 255)
    return bytes(out)


def even_length(data: bytes) -> bytes:
    return data + b"\0" if len(data) % 2 else data  # imagecodecs requires an even byte count


def encode_fraction_naive(events: Events, n_pixels: int, encoding: Encoding = V1) -> bytes:
    """Reference encoder: one Python loop over the events."""
    e = encoding
    codes: list[tuple[int, int]] = []
    previous = -1
    for pixel, sx, sy in zip(
        events.pixels.tolist(), events.sub_x.tolist(), events.sub_y.tolist(), strict=True
    ):
        skip = pixel - previous - 1
        while skip >= e.max_skip:
            codes.append((e.max_skip, e.skip_bits))
            skip -= e.max_skip
        codes.append((skip, e.skip_bits))
        codes.append((sx | (sy << e.horz_bits), e.sub_bits))
        previous = pixel
    skip = n_pixels - previous - 1  # the last run must land exactly on n_pixels
    while skip >= e.max_skip:
        codes.append((e.max_skip, e.skip_bits))
        skip -= e.max_skip
    codes.append((skip, e.skip_bits))
    return even_length(pack_codes(codes))


def encode_fraction(events: Events, n_pixels: int, encoding: Encoding = V1) -> bytes:
    """Vectorised encoder, byte-identical to :func:`encode_fraction_naive`."""
    e = encoding
    n = len(events)
    gaps = np.diff(np.concatenate(([-1], events.pixels, [n_pixels]))) - 1
    n_max, rest = gaps // e.max_skip, gaps % e.max_skip
    per_gap = n_max + 1 + (np.arange(n + 1) < n)  # max-skip codes, the rest, the sub-pixel code
    starts = np.concatenate(([0], np.cumsum(per_gap)[:-1]))
    values = np.full(int(per_gap.sum()), e.max_skip, np.int64)
    widths = np.full(len(values), e.skip_bits, np.int64)
    values[starts + n_max] = rest
    sub_index = (starts + n_max)[:n] + 1
    values[sub_index] = events.sub_x | (events.sub_y << e.horz_bits)
    widths[sub_index] = e.sub_bits
    first_bit = np.concatenate(([0], np.cumsum(widths)))
    bits = np.zeros(int(first_bit[-1]), np.uint8)
    for b in range(max(e.skip_bits, e.sub_bits)):
        wide = widths > b
        bits[first_bit[:-1][wide] + b] = (values[wide] >> b) & 1
    return even_length(np.packbits(bits, bitorder="little").tobytes())


def sub_position(sub: Any, bits: int, superres: int) -> Any:
    """Offset of an event inside its pixel on a grid refined ``2**superres`` times.

    The stored value is XOR-ed with its top bit (RELION: ``symbol ^ 0x0A`` for 2 + 2 bits).
    """
    if superres == 0:
        return sub * 0
    if not 1 <= superres <= bits <= 2:
        raise ValueError("the convention is only established for 1 or 2 sub-pixel bits")
    return (sub ^ (1 << (bits - 1))) >> (bits - superres)


def decode_fraction_naive(
    data: bytes, shape: tuple[int, int], encoding: Encoding = V1, superres: int = 0
) -> np.ndarray:
    """Strict pure-Python decoder (RELION's loop): counts per pixel of the refined grid.

    Raises ``ValueError`` unless the stream reaches exactly the last pixel within ``data``.
    """
    e, s = encoding, superres
    ny, nx = shape
    out = np.zeros((ny << s, nx << s), np.uint8)
    acc = have = position = pixel = 0

    def take(nbits: int) -> int:
        nonlocal acc, have, position
        while have < nbits:
            if position >= len(data):
                raise ValueError("the stream ends before the last pixel")
            acc |= data[position] << have
            position += 1
            have += 8
        value = acc & ((1 << nbits) - 1)
        acc >>= nbits
        have -= nbits
        return value

    while True:
        skip = take(e.skip_bits)
        pixel += skip
        if pixel >= ny * nx:
            break
        if skip == e.max_skip:
            continue
        code = take(e.sub_bits)
        y, x = divmod(pixel, nx)
        sx, sy = code & ((1 << e.horz_bits) - 1), code >> e.horz_bits
        out[(y << s) | sub_position(sy, e.vert_bits, s), (x << s) | sub_position(sx, e.horz_bits, s)] += 1
        pixel += 1
    if pixel != ny * nx:
        raise ValueError("the stream runs past the last pixel")
    return out


def render(
    fractions: Sequence[Events], shape: tuple[int, int], encoding: Encoding = V1, superres: int = 0
) -> np.ndarray:
    """Expected sum of ``fractions`` (int64 counts), from the event lists alone."""
    s = superres
    ny, nx = shape
    out = np.zeros((ny << s, nx << s), np.int64)
    for ev in fractions:
        y, x = np.divmod(ev.pixels, nx)
        np.add.at(
            out,
            (
                (y << s) | sub_position(ev.sub_y, encoding.vert_bits, s),
                (x << s) | sub_position(ev.sub_x, encoding.horz_bits, s),
            ),
            1,
        )
    return out


# ─── TIFF container ───────────────────────────────────────────────────────────

TagValue = tuple[int, "Sequence[int] | bytes"]  # (field type, values)


@dataclass
class Page:
    tags: dict[int, TagValue]
    strips: list[bytes]


@dataclass(frozen=True)
class TiffLayout:
    """Where things are in a file written by :func:`write_tiff`."""

    bigtiff: bool
    byteorder: str
    ifd_offsets: tuple[int, ...]
    next_fields: tuple[int, ...]  # offset of the "next directory" pointer that follows each directory
    value_fields: tuple[Mapping[int, int], ...]  # per page: tag -> offset of its value/offset field
    strips: tuple[tuple[tuple[int, int], ...], ...]  # per page: (offset, byte count) of each strip

    @property
    def first_ifd_field(self) -> int:
        """Offset of the header's pointer to the first directory."""
        return 8 if self.bigtiff else 4

    @property
    def offset_format(self) -> str:
        return self.byteorder + ("Q" if self.bigtiff else "I")


def _pack_value(order: str, value: TagValue) -> tuple[int, int, bytes]:
    field_type, values = value
    if isinstance(values, bytes):
        return field_type, len(values), values
    return field_type, len(values), struct.pack(f"{order}{len(values)}{_FORMATS[field_type]}", *values)


def write_tiff(
    path: Path, pages: Sequence[Page], *, bigtiff: bool = True, byteorder: str = "<", ifd_first: bool = False
) -> TiffLayout:
    """Write ``pages`` (tags 273 and 279 are added from ``Page.strips``)."""
    order = byteorder
    magic = b"II" if order == "<" else b"MM"
    if bigtiff:
        buf = bytearray(magic + struct.pack(order + "HHHQ", 43, 8, 0, 0))
        count_format, entry_format, inline, pointer = "Q", "HHQ", 8, "Q"
    else:
        buf = bytearray(magic + struct.pack(order + "HI", 42, 0))
        count_format, entry_format, inline, pointer = "H", "HHI", 4, "I"
    count_size, entry_size, pointer_size = (
        struct.calcsize(order + f) for f in (count_format, entry_format + f"{inline}s", pointer)
    )
    pointer_field = len(buf) - pointer_size
    ifd_offsets, next_fields, value_fields, all_strips = [], [], [], []

    def even(n: int) -> int:
        return n + (n & 1)

    for page in pages:
        ifd_size = count_size + (len(page.tags) + 2) * entry_size + pointer_size
        cursor = even(len(buf))
        ifd_offset = cursor
        if ifd_first:
            cursor += ifd_size
        placed = []
        for strip in page.strips:
            placed.append((cursor, strip))
            cursor = even(cursor + len(strip))
        offset_type = LONG8 if bigtiff else LONG
        tags = dict(page.tags)
        tags[STRIP_OFFSETS] = (offset_type, [offset for offset, _ in placed])
        tags[STRIP_BYTE_COUNTS] = (offset_type, [len(strip) for _, strip in placed])
        entries, outside = [], []
        for code in sorted(tags):
            field_type, count, raw = _pack_value(order, tags[code])
            if len(raw) > inline:
                outside.append((cursor, raw))
                raw = struct.pack(order + pointer, cursor)
                cursor = even(cursor + len(outside[-1][1]))
            entries.append((code, field_type, count, raw.ljust(inline, b"\0")))
        if not ifd_first:
            ifd_offset = cursor
            cursor += ifd_size
        buf.extend(bytes(cursor - len(buf)))
        for offset, raw in placed + outside:
            buf[offset : offset + len(raw)] = raw
        struct.pack_into(order + count_format, buf, ifd_offset, len(entries))
        fields: dict[int, int] = {}
        for k, (code, field_type, count, raw) in enumerate(entries):
            at = ifd_offset + count_size + k * entry_size
            struct.pack_into(order + entry_format + f"{inline}s", buf, at, code, field_type, count, raw)
            fields[code] = at + entry_size - inline
        struct.pack_into(order + pointer, buf, pointer_field, ifd_offset)
        pointer_field = ifd_offset + ifd_size - pointer_size
        ifd_offsets.append(ifd_offset)
        next_fields.append(pointer_field)
        value_fields.append(fields)
        all_strips.append(tuple((offset, len(strip)) for offset, strip in placed))
    path.write_bytes(buf)
    return TiffLayout(
        bigtiff, order, tuple(ifd_offsets), tuple(next_fields), tuple(value_fields), tuple(all_strips)
    )


# ─── EER files ────────────────────────────────────────────────────────────────


def acquisition_xml(items: Mapping[str, Any]) -> bytes:
    """The acquisition metadata of TIFF tag 65001. A value may be ``(value, unit)``."""
    parts = []
    for name, value in items.items():
        value, unit = value if isinstance(value, tuple) else (value, None)
        attribute = f' unit="{unit}"' if unit else ""
        parts.append(f'<item name="{name}"{attribute}>{value}</item>')
    return ("<metadata>" + "".join(parts) + "</metadata>").encode()


def default_acquisition(n_fractions: int, shape: tuple[int, int], events: int) -> dict[str, Any]:
    """Items shaped like those of a Falcon 4i file; the doses match the synthetic events."""
    ny, nx = shape
    total_dose = n_fractions * events / (ny * nx)
    return {
        "acquisitionID": "20261110_101500_synthetic",
        "cameraName": "BM-Falcon",
        "commercialName": "Falcon 4i",
        "eerGainReference": "20261110_090000_EER_GainReference.gain",
        "exposureTime": (4.0, "s"),
        "hasFinalImage": "No",
        "meanDoseRate": (total_dose / 4.0, "e/pixel/s"),
        "numberOfFrames": n_fractions,
        "sensorImageHeight": (ny, "pixel"),
        "sensorImageWidth": (nx, "pixel"),
        "sensorPixelSize.height": (1.4e-05, "m"),
        "sensorPixelSize.width": (1.4e-05, "m"),
        "serialNumber": "004217",
        "timestamp": "2026-11-10T10:15:00.123+01:00",
        "totalDose": (total_dose, "e/pixel"),
    }


@dataclass(frozen=True)
class EerFile:
    """A synthetic EER file and its ground truth."""

    path: Path
    shape: tuple[int, int]
    encoding: Encoding
    fractions: tuple[Events, ...]
    layout: TiffLayout
    first_page: int  # page of fraction 0 (1 when an integrated image comes first)
    acquisition: Mapping[str, Any]

    def truth(self, start: int = 0, stop: int | None = None, superres: int = 0) -> np.ndarray:
        """Expected sum of fractions ``start..stop`` (int64)."""
        return render(self.fractions[start:stop], self.shape, self.encoding, superres)

    def n_events(self, start: int = 0, stop: int | None = None) -> int:
        return sum(len(ev) for ev in self.fractions[start:stop])

    def strip(self, fraction: int, index: int = 0) -> tuple[int, int]:
        """``(offset, byte count)`` of a strip of ``fraction``."""
        return self.layout.strips[self.first_page + fraction][index]

    def value_field(self, fraction: int, tag: int) -> int:
        return self.layout.value_fields[self.first_page + fraction][tag]


def write_eer(
    path: Path,
    *,
    n_fractions: int = 12,
    shape: tuple[int, int] = (32, 48),
    events: int = 60,
    hot: Sequence[int] = (),
    seed: int = 0,
    encoding: Encoding = V1,
    bigtiff: bool = True,
    byteorder: str = "<",
    xml: bytes | str | None = "auto",
    acquisition: Mapping[str, Any] | None = None,
    strips: int = 1,
    integrated: bool = False,
    ifd_first: bool = False,
    tail: bytes = b"",
    streams: Mapping[int, bytes] | None = None,
) -> EerFile:
    """Write a synthetic EER movie.

    ``hot`` pixels get an event in every fraction. ``xml="auto"`` writes realistic acquisition
    metadata (``acquisition`` overrides items; ``None`` as a value removes one), ``xml=None`` none
    at all. ``strips`` splits each fraction's stream over several TIFF strips, ``integrated`` adds
    the uncompressed sum image some files carry as their first page, ``tail`` is appended to
    every stream inside its strip (padding or junk) and ``streams`` replaces the encoded stream
    of the given fractions (to plant an invalid one).
    """
    ny, nx = shape
    rng = np.random.default_rng(seed)
    fractions = tuple(random_events(rng, ny * nx, events, encoding, hot) for _ in range(n_fractions))
    items = default_acquisition(n_fractions, shape, events + len(hot))
    items.update(acquisition or {})
    items = {name: value for name, value in items.items() if value is not None}
    if xml == "auto":
        xml = acquisition_xml(items)
    elif isinstance(xml, str):
        xml = xml.encode()
    else:
        items = {}

    def base_tags(compression: int, bits: int) -> dict[int, TagValue]:
        return {
            IMAGE_WIDTH: (LONG, [nx]),
            IMAGE_LENGTH: (LONG, [ny]),
            BITS_PER_SAMPLE: (SHORT, [bits]),
            COMPRESSION: (SHORT, [compression]),
            PHOTOMETRIC: (SHORT, [1]),
            SAMPLES_PER_PIXEL: (SHORT, [1]),
        }

    pages: list[Page] = []
    if integrated:
        image = render(fractions, shape, encoding).astype("<u2" if byteorder == "<" else ">u2")
        pages.append(Page(base_tags(1, 16) | {ROWS_PER_STRIP: (LONG, [ny])}, [image.tobytes()]))
    for index, ev in enumerate(fractions):
        stream = (streams or {}).get(index, encode_fraction(ev, ny * nx, encoding)) + tail
        cuts = [len(stream) * k // strips for k in range(strips + 1)]
        tags = base_tags(encoding.compression, 1) | {ROWS_PER_STRIP: (LONG, [-(-ny // strips)])}
        if encoding.compression == 65002:
            tags |= {
                SKIP_BITS: (LONG, [encoding.skip_bits]),
                HORZ_BITS: (LONG, [encoding.horz_bits]),
                VERT_BITS: (LONG, [encoding.vert_bits]),
            }
        pages.append(Page(tags, [stream[a:b] for a, b in itertools.pairwise(cuts)]))
    if xml is not None:
        pages[0].tags[ACQUISITION_XML] = (UNDEFINED, xml)
    layout = write_tiff(path, pages, bigtiff=bigtiff, byteorder=byteorder, ifd_first=ifd_first)
    return EerFile(path, shape, encoding, fractions, layout, int(integrated), items)
