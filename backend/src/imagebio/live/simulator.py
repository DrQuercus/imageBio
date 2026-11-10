"""A microscope that writes an EPU-like session, for tests and demonstrations.

``MicroscopeSim.step(now)`` advances the acquisition to time ``now``: exposures start at the
configured rate and each one writes, the way EPU does, a movie (``…_Fractions.mrc``) frame by
frame, then the integrated image, a JPEG preview and at last the metadata ``.xml``. Some writes
*pause* for longer than a monitor's patience, with a file that is valid so far but not
finished: this is the situation in which the legacy monitor declared half-written micrographs
complete. The simulator knows the truth (``completed``: when each file received its last
byte), so a test can check that nothing was called complete before it was.

Time is an argument: every file and directory gets ``now`` as its modification time, so that a
test replays an hour of acquisition in seconds. ``imagebio simulate DIR`` runs it against the
real clock to try the Live mode by hand.
"""

from __future__ import annotations

import heapq
import io
import os
import random
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt

__all__ = ["MicroscopeSim", "SimSettings", "mrc_bytes"]

_SO: Final = "http://schemas.datacontract.org/2004/07/Fei.SharedObjects"
_ARRAYS: Final = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
_MRC_MODES: Final = {"int8": 0, "int16": 1, "float32": 2, "uint16": 6}


def mrc_bytes(data: npt.NDArray[np.generic], apix: float, *, stack: bool = False) -> bytes:
    """A complete MRC2014 file holding ``data`` (2-D image or 3-D stack), little-endian."""
    array = np.ascontiguousarray(data)
    volume = array.ndim == 3 and not stack
    if array.ndim == 2:
        array = array[None]
    nz, ny, nx = array.shape
    header = bytearray(1024)
    mz = nz if volume else 1
    struct.pack_into("<10i", header, 0, nx, ny, nz, _MRC_MODES[array.dtype.name], 0, 0, 0, nx, ny, mz)
    struct.pack_into("<6f", header, 40, nx * apix, ny * apix, mz * apix, 90.0, 90.0, 90.0)
    struct.pack_into("<3i", header, 64, 1, 2, 3)
    values: npt.NDArray[np.float64] = np.asarray(array, dtype=np.float64)
    struct.pack_into("<3f", header, 76, float(np.min(values)), float(np.max(values)), float(np.mean(values)))
    struct.pack_into("<2i", header, 88, 1 if volume else 0, 0)  # space group 0: an image or a stack of images
    struct.pack_into("<i", header, 108, 20140)
    header[208:212] = b"MAP "
    header[212:216] = bytes([0x44, 0x44, 0, 0])
    struct.pack_into("<f", header, 216, float(values.std()))
    return bytes(header) + array.astype(array.dtype.newbyteorder("<"), copy=False).tobytes()


@dataclass(frozen=True, slots=True)
class SimSettings:
    rate_per_hour: float = 600.0
    shape: tuple[int, int] = (64, 64)
    #: Frames of the movie of each exposure; 0: no movie, only the integrated image.
    frames: int = 6
    #: Seconds to write a movie without incident.
    write_s: float = 3.0
    #: Share of the exposures whose movie write stops for a while in the middle.
    pause_probability: float = 0.2
    #: Length of such a pause, seconds: longer than the 10 s a monitor waits for a stable size.
    pause_s: tuple[float, float] = (15.0, 45.0)
    #: The metadata file arrives this long after the image.
    xml_delay_s: float = 1.5
    preview: bool = True
    per_square: int = 30
    pixel_size: float = 0.83
    seed: int = 0


def _key_value(key: str, value: str, kind: str) -> str:
    return (
        f'<a:KeyValueOfstringanyType><a:Key>{key}</a:Key><a:Value i:type="b:{kind}" '
        f'xmlns:b="http://www.w3.org/2001/XMLSchema">{value}</a:Value></a:KeyValueOfstringanyType>'
    )


def _epu_xml(
    when: float, apix: float, dose: float, defocus_um: float, stage: tuple[float, float], frames: int
) -> bytes:
    stamp = datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0+00:00"
    custom = (
        _key_value("AppliedDefocus", f"{defocus_um * 1e-6:.6E}", "double")
        + _key_value("Dose", f"{dose * 1e20:.6E}", "double")
        + _key_value("Detectors[EF-Falcon].EerGainReference", "20261110_EER_GainReference.gain", "string")
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<MicroscopeImage xmlns="{_SO}" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">'
        f'<CustomData xmlns:a="{_ARRAYS}">{custom}</CustomData>'
        f"<microscopeData><acquisition><acquisitionDateTime>{stamp}</acquisitionDateTime><camera>"
        f'<CameraSpecificInput xmlns:a="{_ARRAYS}">{_key_value("NumberOffractions", str(frames), "int")}'
        f"</CameraSpecificInput><ExposureTime>4.0</ExposureTime><Name>EF-Falcon</Name></camera></acquisition>"
        f"<core><ApplicationSoftwareVersion>3.6.0</ApplicationSoftwareVersion></core>"
        f"<gun><AccelerationVoltage>300000</AccelerationVoltage></gun>"
        f"<optics><SpotIndex>5</SpotIndex><TemMagnification><NominalMagnification>96000</NominalMagnification>"
        f"</TemMagnification></optics>"
        f"<stage><Position><A>0</A><B>0</B><X>{stage[0] * 1e-6:.6E}</X><Y>{stage[1] * 1e-6:.6E}</Y><Z>0</Z>"
        f"</Position></stage></microscopeData>"
        f"<SpatialScale><pixelSize><x><numericValue>{apix * 1e-10:.6E}</numericValue></x>"
        f"<y><numericValue>{apix * 1e-10:.6E}</numericValue></y></pixelSize></SpatialScale>"
        f"</MicroscopeImage>"
    ).encode()


class MicroscopeSim:
    """Writes a session under ``root``. Deterministic for a given seed and sequence of
    :meth:`step` times."""

    def __init__(self, root: Path, settings: SimSettings | None = None) -> None:
        self.root = Path(root)
        self.settings = settings or SimSettings()
        self._rng = random.Random(self.settings.seed)  # noqa: S311 - a simulation, not a secret
        self._np = np.random.default_rng(self.settings.seed)
        self._events: list[tuple[float, int, Callable[[float], None]]] = []
        self._order = 0
        self._next_start: float | None = None
        self._running = True
        self._count = 0
        #: Relative path of every file created, with the time of its first byte.
        self.started: dict[str, float] = {}
        #: Relative path of every finished file, with the time of its last byte.
        self.completed: dict[str, float] = {}
        #: Names of the exposures started, in order.
        self.exposures: list[str] = []
        ny, nx = self.settings.shape
        y, x = np.mgrid[0:ny, 0:nx]
        blobs = np.zeros((ny, nx))
        for _ in range(12):  # a few "particles" on a gradient, so that thumbnails show something
            cy, cx = self._np.uniform(0, ny), self._np.uniform(0, nx)
            blobs -= 6.0 * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * (min(ny, nx) / 16) ** 2))
        self._scene = 40.0 + 6.0 * x / nx + blobs
        self.root.mkdir(parents=True, exist_ok=True)

    # ── writing, with the truth kept ─────────────────────────────────────────
    def _touch_dirs(self, rel: str, now: float) -> None:
        """Creating an entry changes the date of its directory: set it to the simulated time."""
        os.utime(self.root / rel, (now, now))

    def _mkdir(self, rel: str, now: float) -> None:
        path = self.root / rel
        if path.is_dir():
            return
        parent = os.path.dirname(rel)
        if parent:
            self._mkdir(parent, now)
        path.mkdir()
        os.utime(path, (now, now))
        self._touch_dirs(parent, now)

    def _write(self, rel: str, data: bytes, now: float, *, last: bool, append: bool = False) -> None:
        path = self.root / rel
        created = rel not in self.started
        with path.open("ab" if append else "wb") as f:
            f.write(data)
        os.utime(path, (now, now))
        if created:
            self.started[rel] = now
            self._touch_dirs(os.path.dirname(rel), now)
        if last:
            self.completed[rel] = now

    def _at(self, when: float, action: Callable[[float], None]) -> None:
        self._order += 1
        heapq.heappush(self._events, (when, self._order, action))

    # ── one exposure ─────────────────────────────────────────────────────────
    def _start_exposure(self, now: float) -> None:
        s = self.settings
        n = self._count
        self._count += 1
        square = 1_000_000 + n // s.per_square
        directory = f"Images-Disc1/GridSquare_{square}/Data"
        if not (self.root / directory).is_dir():
            self._mkdir(directory, now)
            overview = (
                f"Images-Disc1/GridSquare_{square}/GridSquare_{datetime.fromtimestamp(now):%Y%m%d_%H%M%S}"
            )
            self._write(f"{overview}.jpg", self._jpeg(self._scene), now, last=True)
        hole = 20_000_000 + n // 4
        name = f"FoilHole_{hole}_Data_{30_000_000 + n % 4}_{n}_{datetime.fromtimestamp(now):%Y%m%d_%H%M%S}"
        self.exposures.append(name)
        base = f"{directory}/{name}"
        ny, nx = s.shape
        dose = 40.0 + self._rng.uniform(-3, 3)
        defocus = -self._rng.choice([1.0, 1.5, 2.0, 2.5])
        stage = (self._rng.uniform(-500, 500), self._rng.uniform(-500, 500))
        image = (self._scene + self._np.normal(0, 3.0, s.shape)).astype(np.float32)

        def write_image(t: float) -> None:
            data = mrc_bytes(image, s.pixel_size)
            half = 1024 + (len(data) - 1024) // 2
            self._write(f"{base}.mrc", data[:half], t, last=False)
            self._at(t + 0.5, lambda t2: self._write(f"{base}.mrc", data[half:], t2, last=True, append=True))
            if s.preview:
                self._at(t + 0.8, lambda t2: self._write(f"{base}.jpg", self._jpeg(image), t2, last=True))
            xml = _epu_xml(now, s.pixel_size, dose, defocus, stage, max(1, s.frames))
            self._at(t + 0.5 + s.xml_delay_s, lambda t2: self._write(f"{base}.xml", xml, t2, last=True))

        if s.frames <= 0:
            write_image(now)
            return
        movie = self._np.poisson(np.clip(self._scene, 1, None) / s.frames, (s.frames, ny, nx)).astype(
            np.uint16
        )
        data = mrc_bytes(movie, s.pixel_size, stack=True)
        frame_bytes = ny * nx * 2
        pause_after = (
            self._rng.randrange(1, s.frames)
            if s.frames > 1 and self._rng.random() < s.pause_probability
            else None
        )
        pause = self._rng.uniform(*s.pause_s)
        when = now
        # the header announces every frame from the start, as acquisition programs write it
        self._write(f"{base}_Fractions.mrc", data[: 1024 + frame_bytes], when, last=s.frames == 1)
        for k in range(1, s.frames):
            when += s.write_s / s.frames + (pause if pause_after == k else 0.0)
            chunk = data[1024 + k * frame_bytes : 1024 + (k + 1) * frame_bytes]
            last = k == s.frames - 1
            self._at(
                when,
                lambda t, chunk=chunk, last=last: self._write(  # type: ignore[misc]
                    f"{base}_Fractions.mrc", chunk, t, last=last, append=True
                ),
            )
        self._at(when + 0.2, write_image)

    @staticmethod
    def _jpeg(image: npt.NDArray[np.floating]) -> bytes:
        from PIL import Image

        lo, hi = np.percentile(image, (1, 99))
        grey = np.clip((image - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
        out = io.BytesIO()
        Image.fromarray(grey).save(out, format="JPEG", quality=80)
        return out.getvalue()

    # ── the clock ────────────────────────────────────────────────────────────
    def step(self, now: float) -> None:
        """Do everything the microscope does up to time ``now``."""
        if self._next_start is None:
            self._next_start = now
        interval = 3600.0 / self.settings.rate_per_hour
        while True:
            start = self._next_start if self._running else None
            event = self._events[0][0] if self._events else None
            if start is not None and start <= now and (event is None or start <= event):
                self._next_start = start + interval
                self._start_exposure(start)
            elif event is not None and event <= now:
                when, _, action = heapq.heappop(self._events)
                action(when)
            else:
                return

    def stop(self) -> None:
        """Start no new exposure; those in progress finish as :meth:`step` is called."""
        self._running = False

    def abort(self) -> None:
        """The microscope PC dies: nothing more is written, files in progress stay as they are."""
        self._running = False
        self._events.clear()

    def partial(self) -> set[str]:
        """Files that received their first byte but not their last."""
        return set(self.started) - set(self.completed)

    @property
    def busy(self) -> bool:
        return bool(self._events)
