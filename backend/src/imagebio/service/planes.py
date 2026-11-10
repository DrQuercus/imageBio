"""Plane service: what the API (and later the Cryo and Live modes) call to get pixels.

It owns the pool of open datasets and the byte-bounded plane cache. Callers pass *real* paths
that have already been resolved through :class:`imagebio.fs.roots.RootSet`: authorisation is
the caller's job and happens before any call here, hence before any cache lookup.

Every cache key starts with the :class:`FileId` of the file (path, size, mtime) and the reader
options, never the path alone: a file that changes on disk is read again.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from imagebio.core.cache import ByteLRU, DatasetPool, options_key
from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Calibration, DatasetInfo, FileId, Plane
from imagebio.io.registry import Registry, default_registry
from imagebio.proc.binning import bin_mean, binned_shape, crop
from imagebio.proc.fourier import fourier_filter, power_spectrum
from imagebio.proc.stats import PlaneStats, plane_stats

__all__ = ["MAX_BIN", "PlaneResult", "PlaneService", "Processing"]

MAX_BIN: Final = 64
_STATS_CACHE_ITEMS: Final = 4096

Region = tuple[int, int, int, int]  # x, y, w, h in full-resolution pixels


@dataclass(frozen=True, slots=True)
class Processing:
    """What is done to a plane before it is cropped and binned.

    ``t_end``: the plane becomes the mean of frames ``t..t_end`` (inclusive). ``lowpass`` and
    ``highpass``: resolution filters in Å. ``apix``: Å per pixel given by the user, for files
    that do not store their pixel size (it takes precedence over the file's value).
    """

    t_end: int | None = None
    lowpass: float | None = None
    highpass: float | None = None
    apix: float | None = None

    @property
    def identity(self) -> bool:
        return self.t_end is None and self.lowpass is None and self.highpass is None

    def key(self) -> tuple[Any, ...]:
        filtered = self.lowpass is not None or self.highpass is not None
        return (self.t_end, self.lowpass, self.highpass, self.apix if filtered else None)


NO_PROCESSING: Final = Processing()


@dataclass(frozen=True, slots=True)
class PlaneResult:
    array: Plane  # read-only
    bin: int
    region: Region
    calibration: Calibration  # of the binned pixels
    file_id: FileId


class PlaneService:
    def __init__(self, settings: Settings, registry: Registry | None = None) -> None:
        self._registry = registry or default_registry()
        self._pool = DatasetPool(settings.max_open_datasets, self._registry.open)
        self._cache = ByteLRU(settings.plane_cache_bytes)
        self._max_response_pixels = settings.max_response_pixels
        #: Largest raw plane read into memory (a K3 super-resolution frame is 0.4 GiB as float32).
        self.max_plane_bytes = 4 * 2**30
        self._stats: OrderedDict[Hashable, PlaneStats] = OrderedDict()
        self._stats_lock = threading.Lock()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _raw(self, dataset: Any, file_id: FileId, okey: str, t: int, c: int, z: int) -> Plane:
        dataset.check_index(t, c, z)
        info: DatasetInfo = dataset.info
        if info.plane_nbytes > self.max_plane_bytes:
            raise InvalidRequestError(
                f"plane too large to read ({info.size_x} × {info.size_y} pixels, "
                f"{info.plane_nbytes >> 20} MiB)"
            )
        return self._cache.get_or_load(("raw", file_id, okey, t, c, z), lambda: dataset.read_plane(t, c, z))

    def _mean_of_frames(
        self, dataset: Any, file_id: FileId, okey: str, t: int, t_end: int, c: int, z: int
    ) -> Plane:
        """Mean of frames ``t..t_end``, accumulated in float64. The frames are read straight
        from the file: caching forty raw frames to show one average would empty the cache."""
        info: DatasetInfo = dataset.info
        if not isinstance(t_end, int) or isinstance(t_end, bool) or not t <= t_end < info.size_t:
            raise InvalidRequestError(f"t_end={t_end!r} must be between t={t} and {info.size_t - 1}")
        acc = np.zeros((info.size_y, info.size_x), np.float64)
        for k in range(t, t_end + 1):
            frame = self._cache.get(("raw", file_id, okey, k, c, z))
            acc += dataset.read_plane(k, c, z) if frame is None else frame
        acc /= t_end - t + 1
        return acc.astype(np.float32)

    def _source(
        self, dataset: Any, file_id: FileId, okey: str, t: int, c: int, z: int, proc: Processing
    ) -> Plane:
        """The full-resolution plane after ``proc`` (frame average, resolution filters)."""
        if proc.identity:
            return self._raw(dataset, file_id, okey, t, c, z)
        dataset.check_index(t, c, z)

        def load() -> Plane:
            if proc.t_end is None:
                plane = self._raw(dataset, file_id, okey, t, c, z)
            else:
                plane = self._mean_of_frames(dataset, file_id, okey, t, proc.t_end, c, z)
            if proc.lowpass is None and proc.highpass is None:
                return plane
            cal = dataset.info.calibration
            apix_x = proc.apix or cal.x.to_angstrom()
            apix_y = proc.apix or cal.y.to_angstrom()
            if not apix_x or not apix_y:
                raise InvalidRequestError("pixel size unknown: give it (apix, in Å) to filter by resolution")
            return fourier_filter(
                plane, apix_x=apix_x, apix_y=apix_y, lowpass=proc.lowpass, highpass=proc.highpass
            )

        return self._cache.get_or_load(("proc", file_id, okey, t, c, z, proc.key()), load)

    # ── API ──────────────────────────────────────────────────────────────────
    def info(self, path: Path, options: Mapping[str, Any] | None = None) -> tuple[DatasetInfo, FileId]:
        file_id = FileId.of(path)
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            return dataset.info, file_id

    def plane(
        self,
        path: Path,
        t: int = 0,
        c: int = 0,
        z: int = 0,
        *,
        bin: int = 1,
        region: Region | None = None,
        options: Mapping[str, Any] | None = None,
        proc: Processing = NO_PROCESSING,
    ) -> PlaneResult:
        """Plane ``(t, c, z)`` after ``proc``, cropped to ``region`` (full-resolution pixels),
        then binned.

        Without processing, ``bin == 1`` returns the stored values in their dtype; ``bin > 1``
        the float32 block means. A processed plane is float32.
        """
        if not isinstance(bin, int) or isinstance(bin, bool) or not 1 <= bin <= MAX_BIN:
            raise InvalidRequestError(f"bin must be an integer between 1 and {MAX_BIN}")
        file_id = FileId.of(path)
        okey = options_key(options)
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            info = dataset.info
            dataset.check_index(t, c, z)
            full: Region = (0, 0, info.size_x, info.size_y)
            if region is None:
                region = full
            x, y, w, h = region
            crop(np.empty((info.size_y, info.size_x), np.bool_), x, y, w, h)  # validates the region
            out_h, out_w = binned_shape(h, w, bin)
            if out_h * out_w > self._max_response_pixels:
                raise InvalidRequestError(
                    f"the requested plane has {out_w} × {out_h} pixels, above the limit of "
                    f"{self._max_response_pixels}: use a larger bin or request a region"
                )
            if bin == 1 and region == full:
                array = self._source(dataset, file_id, okey, t, c, z, proc)
            else:
                array = self._cache.get_or_load(
                    ("view", file_id, okey, t, c, z, proc.key(), bin, region),
                    lambda: np.ascontiguousarray(
                        bin_mean(crop(self._source(dataset, file_id, okey, t, c, z, proc), x, y, w, h), bin)
                    ),
                )
            return PlaneResult(array, bin, region, info.calibration.binned(bin), file_id)

    def stats(
        self,
        path: Path,
        t: int = 0,
        c: int = 0,
        z: int = 0,
        options: Mapping[str, Any] | None = None,
        proc: Processing = NO_PROCESSING,
    ) -> tuple[PlaneStats, FileId]:
        """Statistics of the full-resolution plane ``(t, c, z)`` after ``proc``."""
        file_id = FileId.of(path)
        okey = options_key(options)
        key = (file_id, okey, t, c, z, proc.key())
        with self._stats_lock:
            cached = self._stats.get(key)
            if cached is not None:
                self._stats.move_to_end(key)
                return cached, file_id
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            result = plane_stats(self._source(dataset, file_id, okey, t, c, z, proc))
        with self._stats_lock:
            self._stats[key] = result
            while len(self._stats) > _STATS_CACHE_ITEMS:
                self._stats.popitem(last=False)
        return result, file_id

    def spectrum(
        self,
        path: Path,
        t: int = 0,
        c: int = 0,
        z: int = 0,
        *,
        t_end: int | None = None,
        tile: int = 512,
        options: Mapping[str, Any] | None = None,
    ) -> tuple[Plane, FileId]:
        """Power spectrum (averaged periodogram) of plane ``(t, c, z)``, or of the mean of
        frames ``t..t_end``. Square, zero frequency at the centre."""
        file_id = FileId.of(path)
        okey = options_key(options)
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            dataset.check_index(t, c, z)
            source = Processing(t_end=t_end)
            power = self._cache.get_or_load(
                ("spectrum", file_id, okey, t, c, z, t_end, tile),
                lambda: power_spectrum(self._source(dataset, file_id, okey, t, c, z, source), tile=tile)[0],
            )
        return power, file_id

    def cache_stats(self) -> dict[str, dict[str, int]]:
        return {"planes": self._cache.stats(), "datasets": self._pool.stats()}

    def close(self) -> None:
        self._pool.close_all()
        self._cache.clear()
