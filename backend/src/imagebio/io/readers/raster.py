"""Raster reader for everyday image formats (PNG, JPEG, BMP, GIF, WebP) through Pillow.

Format decisions
----------------
Pixels
    Exactly as stored. The EXIF orientation is *not* applied: it is recorded in
    ``metadata["exif_orientation"]`` and a warning says so when it is not the identity.
Modes
    ``L`` -> uint8; 16-bit gray (``I;16*``) -> uint16; ``I`` -> int32; ``F`` -> float32;
    ``1`` -> uint8 with 0/255; ``RGB``/``RGBA`` -> C = 3/4 with ``rgb=True``; ``LA`` -> C = 2
    (gray + alpha, not rgb); palette images are expanded through their palette to RGB, or RGBA
    when they carry transparency (never shown as palette indices); CMYK, YCbCr and other colour
    spaces are converted to RGB with a warning.
16-bit colour PNG
    Pillow reduces 16-bit RGB(A) and gray+alpha PNG files to 8 bits per channel. They are
    decoded with ``imagecodecs`` instead, so the values are exact (uint16).
Animations
    GIF, APNG and animated WebP expose their frames on ``T`` (``StackKind.STACK``), every frame
    composed and converted to RGBA so that all frames have the same layout. Only the first
    picture of a multi-picture JPEG (MPO) is exposed.
Pixel size
    From PNG ``pHYs``, JPEG JFIF/EXIF density or the BMP header, as ``METADATA``. A density of
    exactly 72 or 96 dpi (what software writes when it knows nothing) is a placeholder, not a
    pixel size: the calibration stays ``DEFAULT`` and a warning explains why.
Safety
    The pixel count is checked against :data:`MAX_DECODED_BYTES` on the header, before anything
    is decoded (Pillow's own decompression-bomb check is replaced by this one). Decoding errors
    of truncated or corrupt files are :class:`CorruptFileError`.
Decoding is whole-image
    The last decoded frame is kept (bounded by :data:`_MEMO_MAX_BYTES`), so reading the three
    channels of an RGB image decodes the file once. A lock serialises the decoder.
"""

from __future__ import annotations

import contextlib
import struct
import threading
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any, ClassVar, Final

import imagecodecs
import numpy as np
from PIL import Image, UnidentifiedImageError

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
    ChannelInfo,
    Dataset,
    DatasetInfo,
    Plane,
    StackKind,
)
from imagebio.io.registry import Reader

__all__ = ["MAX_DECODED_BYTES", "READER", "RasterDataset", "RasterReader"]

#: Largest decoded image accepted (all channels of one frame).
MAX_DECODED_BYTES: Final = 2 * 2**30
_MEMO_MAX_BYTES: Final = 256 * 2**20
_MAX_FRAMES: Final = 100_000
_TEXT_LIMIT: Final = 500
_PLACEHOLDER_DPI: Final = (72.0, 96.0)
_PIL_FORMATS: Final = ("PNG", "JPEG", "BMP", "GIF", "WEBP")
_ANIMATED_FORMATS: Final = frozenset({"PNG", "GIF", "WEBP"})
_GRAY_MODES: Final[Mapping[str, str]] = {
    "L": "uint8", "I;16": "uint16", "I;16L": "uint16", "I;16B": "uint16", "I;16N": "uint16",
    "I": "int32", "F": "float32",
}  # fmt: skip
_DECODE_ERRORS: Final = (OSError, SyntaxError, ValueError, EOFError, struct.error, zlib.error, MemoryError)

# The pixel count is checked here, on the header, with a clean error (see _check_size).
Image.MAX_IMAGE_PIXELS = None
Image.init()  # register every plugin: Image.open(formats=...) needs them loaded


def _sniff(head: bytes) -> bool:
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or (head.startswith(b"BM") and len(head) >= 26)
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    )


def _png_deep_colour(head: bytes) -> bool:
    """True for a PNG with 16 bits per sample and more than one sample per pixel."""
    if not head.startswith(b"\x89PNG\r\n\x1a\n") or head[12:16] != b"IHDR" or len(head) < 26:
        return False
    bit_depth, colour_type = head[24], head[25]
    return bit_depth == 16 and colour_type in (2, 4, 6)


def _placeholder(dpi: float) -> bool:
    return any(abs(dpi - p) < 0.05 for p in _PLACEHOLDER_DPI)


def _calibration(info: Mapping[Any, Any]) -> tuple[Calibration, list[str]]:
    dpi = info.get("dpi")
    try:
        dx, dy = (float(v) for v in dpi) if dpi is not None else (0.0, 0.0)
    except (TypeError, ValueError):
        dx = dy = 0.0
    if not (np.isfinite(dx) and np.isfinite(dy) and dx > 0 and dy > 0):
        return Calibration(), ["pixel size unknown: the file stores no resolution, distances are in pixels"]
    if _placeholder(dx) and _placeholder(dy):
        return Calibration(), [
            f"pixel size unknown: the stored resolution ({dx:.0f} dpi) is a screen placeholder, "
            "not a calibration"
        ]
    # 1 inch = 25 400 µm
    x = AxisCalibration(25400.0 / dx, "µm", CalibrationSource.METADATA)
    y = AxisCalibration(25400.0 / dy, "µm", CalibrationSource.METADATA)
    return Calibration(x=x, y=y), []


def _clip(value: Any) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"


def _corrupt(name: str, detail: str = "truncated or corrupt") -> CorruptFileError:
    return CorruptFileError(f"{name}: cannot decode the image ({detail})")


class RasterDataset(Dataset):
    """An open raster image. ``target`` is the Pillow mode every frame is converted to
    (``None``: 16-bit colour PNG decoded by imagecodecs)."""

    def __init__(
        self, path: Path, info: DatasetInfo, file: IO[bytes], image: Image.Image | None, target: str | None
    ):
        super().__init__(path, info)
        self._file = file
        self._image = image
        self._target = target
        self._lock = threading.Lock()
        self._memo: tuple[int, Plane] | None = None
        self._closed = False

    def _decode(self, t: int) -> Plane:
        """Frame ``t`` as ``(Y, X)`` or ``(Y, X, C)``. Called with the lock held."""
        name = self._path.name
        try:
            if self._image is None:  # 16-bit colour PNG
                self._file.seek(0)
                frame = np.asarray(imagecodecs.png_decode(self._file.read()))
            else:
                image = self._image
                if self._info.size_t > 1:
                    image.seek(t)
                converted = image if image.mode == self._target else image.convert(self._target)
                frame = np.asarray(converted)
        except Image.DecompressionBombError:
            raise _corrupt(name, "image too large") from None
        except _DECODE_ERRORS:
            raise _corrupt(name) from None
        except Exception as e:  # imagecodecs raises its own error types
            if type(e).__module__.startswith("imagecodecs"):
                raise _corrupt(name) from None
            raise
        i = self._info
        expected = (i.size_y, i.size_x) if i.size_c == 1 else (i.size_y, i.size_x, i.size_c)
        if frame.shape != expected:
            raise _corrupt(name, f"frame {t} does not have the announced size")
        return np.ascontiguousarray(frame)

    def _read_plane(self, t: int, c: int, z: int) -> Plane:
        with self._lock:
            if self._closed:
                raise ValueError("dataset is closed")
            if self._memo is not None and self._memo[0] == t:
                frame = self._memo[1]
            else:
                self._memo = None
                frame = self._decode(t)
                if frame.nbytes <= _MEMO_MAX_BYTES:
                    self._memo = (t, frame)
        return frame if frame.ndim == 2 else np.ascontiguousarray(frame[..., c])

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._memo = None
                if self._image is not None:
                    self._image.close()
                self._file.close()


def _open_file(path: Path) -> IO[bytes]:
    try:
        return path.open("rb")
    except (FileNotFoundError, NotADirectoryError):
        raise NotFoundError(f"file not found: {path.name}") from None
    except IsADirectoryError:
        raise UnsupportedFormatError(f"{path.name} is a directory") from None
    except PermissionError:
        raise AccessDeniedError(f"permission denied: {path.name}") from None
    except OSError as e:
        raise ImageBioError(f"{path.name}: I/O error ({e.strerror or type(e).__name__})") from None


def _check_size(name: str, width: int, height: int, channels: int, itemsize: int) -> None:
    if width < 1 or height < 1:
        raise _corrupt(name, "invalid image size")
    if width * height * channels * itemsize > MAX_DECODED_BYTES:
        raise CorruptFileError(
            f"{name}: image too large to decode ({width} × {height} pixels, "
            f"limit {MAX_DECODED_BYTES >> 20} MiB)"
        )


def _describe(image: Image.Image, head: bytes, name: str) -> tuple[DatasetInfo, str | None]:
    """The dataset description and the Pillow mode frames are converted to."""
    fmt = image.format or ""
    mode = image.mode
    width, height = image.size
    warnings: list[str] = []
    frames = int(getattr(image, "n_frames", 1) or 1) if fmt in _ANIMATED_FORMATS else 1
    if not 1 <= frames <= _MAX_FRAMES:
        raise _corrupt(name, "implausible number of frames")

    target: str | None
    if _png_deep_colour(head) and frames == 1:
        target, dtype = None, "uint16"
        channels = {2: 3, 4: 2, 6: 4}[head[25]]
        rgb = channels != 2
    elif frames > 1:
        target, dtype, channels, rgb = "RGBA", "uint8", 4, True
    elif mode in _GRAY_MODES:
        target, dtype, channels, rgb = mode, _GRAY_MODES[mode], 1, False
    elif mode == "1":
        target, dtype, channels, rgb = "L", "uint8", 1, False
    elif mode in ("LA", "La"):
        target, dtype, channels, rgb = "LA", "uint8", 2, False
    elif mode in ("RGB", "RGBA"):
        target, dtype, channels, rgb = mode, "uint8", len(mode), True
    elif mode in ("P", "PA", "RGBX", "RGBa"):
        alpha = mode in ("PA", "RGBa") or "transparency" in image.info
        target, dtype, channels, rgb = ("RGBA", "uint8", 4, True) if alpha else ("RGB", "uint8", 3, True)
    else:  # CMYK, YCbCr, LAB, HSV...
        target, dtype, channels, rgb = "RGB", "uint8", 3, True
        warnings.append(
            f"colour space {mode} converted to RGB by the decoder: values are not the stored ones"
        )
    _check_size(name, width, height, channels, np.dtype(dtype).itemsize)

    calibration, notes = _calibration(image.info)
    warnings += notes
    orientation = None
    with contextlib.suppress(Exception):  # a broken EXIF block must not prevent reading the pixels
        orientation = image.getexif().get(0x0112)
    if isinstance(orientation, int) and orientation not in (0, 1):
        warnings.append(f"EXIF orientation {orientation} is not applied: the image is shown as stored")
    duration = image.info.get("duration")
    if frames > 1 and isinstance(duration, int | float) and duration > 0:
        calibration = Calibration(
            calibration.x,
            calibration.y,
            t=AxisCalibration(duration / 1000.0, "s", CalibrationSource.METADATA),
        )
    text = {
        str(k): _clip(v)
        for k, v in list(image.info.items())[:64]
        if isinstance(v, str | bytes) and k != "icc_profile"
    }
    names = {(3, True): "RGB", (4, True): "RGBA"}.get((channels, rgb))
    channel_info: tuple[ChannelInfo, ...] = ()
    if names:
        channel_info = tuple(
            ChannelInfo({"R": "red", "G": "green", "B": "blue", "A": "alpha"}[ch]) for ch in names
        )
    elif channels == 2:
        channel_info = (ChannelInfo("gray"), ChannelInfo("alpha"))
    info = DatasetInfo(
        format="jpeg" if fmt == "MPO" else fmt.lower(),
        reader=RasterReader.name,
        size_x=width,
        size_y=height,
        size_t=frames,
        size_c=channels,
        dtype=np.dtype(dtype),
        calibration=calibration,
        stack_kind=StackKind.STACK if frames > 1 else StackKind.IMAGE,
        rgb=rgb,
        channels=channel_info,
        metadata={
            "format": fmt,
            "mode": mode,
            "decoded_as": target or "uint16 (imagecodecs)",
            "frames": frames,
            "exif_orientation": orientation if isinstance(orientation, int) else None,
            "icc_profile": "icc_profile" in image.info,
            "text": text,
        },
        warnings=tuple(warnings),
    )
    return info, target


class RasterReader(Reader):
    """PNG, JPEG, BMP, GIF and WebP images. No options."""

    name: ClassVar[str] = "raster"
    extensions: ClassVar[tuple[str, ...]] = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

    def sniff(self, path: Path, head: bytes) -> bool:
        return _sniff(head)

    def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
        if options:
            raise InvalidRequestError(
                f"unknown raster reader option(s): {', '.join(sorted(map(str, options)))}"
            )
        path = Path(path)
        file = _open_file(path)
        try:
            head = file.read(64)
            file.seek(0)
            try:
                image = Image.open(file, formats=_PIL_FORMATS)
            except UnidentifiedImageError:
                raise CorruptFileError(
                    f"{path.name}: not a readable PNG, JPEG, BMP, GIF or WebP image"
                ) from None
            except Image.DecompressionBombError:
                raise _corrupt(path.name, "image too large") from None
            except _DECODE_ERRORS:
                raise _corrupt(path.name) from None
            try:
                info, target = _describe(image, head, path.name)
            except _DECODE_ERRORS:
                raise _corrupt(path.name) from None
            if target is None:  # decoded by imagecodecs from the file itself (image.close() would close it)
                return RasterDataset(path, info, file, None, None)
            return RasterDataset(path, info, file, image, target)
        except BaseException:
            file.close()
            raise


READER: Final = RasterReader()
