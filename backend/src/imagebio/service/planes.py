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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import numpy as np

from imagebio.core.cache import ByteLRU, DatasetPool, options_key
from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError
from imagebio.core.model import Calibration, DatasetInfo, FileId, Plane
from imagebio.cryo.gain import (
    OrientationCheck,
    apply_gain,
    check_orientation,
    fit_gain,
    listed_defects,
    orient,
    prepare_gain,
)
from imagebio.io.registry import Registry, default_registry
from imagebio.proc.binning import bin_mean, binned_shape, crop
from imagebio.proc.ctf import CtfEstimate, estimate_ctf
from imagebio.proc.fourier import fourier_crop, fourier_filter, power_spectrum
from imagebio.proc.stats import PlaneStats, plane_stats

__all__ = ["MAX_BIN", "GainCheck", "GainSpec", "PlaneResult", "PlaneService", "Processing"]

MAX_BIN: Final = 64
_STATS_CACHE_ITEMS: Final = 4096
_GAIN_CHECK_FRAMES: Final = 20  # planes averaged to check a gain orientation
_GAIN_CHECK_CACHE_ITEMS: Final = 256

Region = tuple[int, int, int, int]  # x, y, w, h in full-resolution pixels


@dataclass(frozen=True, slots=True)
class GainSpec:
    """A gain reference to apply. ``path`` is a real path, already authorised by the caller;
    ``file_id`` is its identity, so that a reference replaced on disk is read again.

    ``orientation``: one of the eight of :mod:`imagebio.cryo.gain`. ``invert``: the file is a
    flat-field counts image, not the multiplicative gain itself.
    """

    path: Path
    file_id: FileId
    orientation: int = 0
    invert: bool = False

    @classmethod
    def of(cls, path: Path, orientation: int = 0, invert: bool = False) -> GainSpec:
        return cls(path, FileId.of(path), orientation, invert)

    def key(self) -> tuple[Any, ...]:
        return (self.file_id.path, self.file_id.size, self.file_id.mtime_ns, self.orientation, self.invert)


@dataclass(frozen=True, slots=True)
class Processing:
    """What is done to a plane before it is cropped and binned, in this order.

    ``t_end``: the plane becomes the mean of frames ``t..t_end`` (inclusive). ``gain``: it is
    multiplied by a gain reference and its defective pixels are filled. ``lowpass`` and
    ``highpass``: resolution filters in Å, of the sharp (Butterworth) profile when ``sharp``.
    ``apix``: Å per pixel given by the user, for files that do not store their pixel size (it
    takes precedence over the file's value).

    ``fourier_bin`` is about the binning that follows: the plane is reduced by keeping the low
    frequencies of its Fourier transform instead of averaging blocks of pixels. A filtered
    plane is always reduced that way, before it is filtered (ADR 013).
    """

    t_end: int | None = None
    lowpass: float | None = None
    highpass: float | None = None
    apix: float | None = None
    gain: GainSpec | None = None
    fourier_bin: bool = False
    sharp: bool = False

    @property
    def identity(self) -> bool:
        return self.t_end is None and not self.filtered and self.gain is None

    @property
    def filtered(self) -> bool:
        return self.lowpass is not None or self.highpass is not None

    @property
    def unfiltered(self) -> Processing:
        """The same frames and gain, without the resolution filters."""
        return Processing(t_end=self.t_end, gain=self.gain)

    @property
    def full_resolution(self) -> Processing:
        """What is done to the plane itself, whatever the way it is binned afterwards."""
        return replace(self, fourier_bin=False) if self.fourier_bin else self

    def key(self) -> tuple[Any, ...]:
        filtered = self.filtered
        gain = self.gain.key() if self.gain is not None else None
        return (
            self.t_end,
            self.lowpass,
            self.highpass,
            self.apix if filtered else None,
            gain,
            self.fourier_bin,
            self.sharp and filtered,
        )


NO_PROCESSING: Final = Processing()


def _filter_pixel(info: DatasetInfo, proc: Processing) -> tuple[float, float]:
    """Å per pixel of the stored plane along x and y, to filter it by resolution."""
    apix_x = proc.apix or info.calibration.x.to_angstrom()
    apix_y = proc.apix or info.calibration.y.to_angstrom()
    if not apix_x or not apix_y:
        raise InvalidRequestError("pixel size unknown: give it (apix, in Å) to filter by resolution")
    return apix_x, apix_y


def _filter(plane: Plane, proc: Processing, apix_x: float, apix_y: float) -> Plane:
    return fourier_filter(
        plane, apix_x=apix_x, apix_y=apix_y, lowpass=proc.lowpass, highpass=proc.highpass, sharp=proc.sharp
    )


@dataclass(frozen=True, slots=True)
class GainCheck:
    """A gain orientation check and what it was made on."""

    check: OrientationCheck
    frames: int  # planes of the movie that were averaged
    defects: int  # defective pixels of the reference
    gain_shape: tuple[int, int]
    file_id: FileId
    #: Entries of the defect list the reference carries without it being applied (None: none).
    listed_defects: int | None = None


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
        self._gain_checks: OrderedDict[Hashable, GainCheck] = OrderedDict()
        self._ctfs: OrderedDict[Hashable, CtfEstimate | None] = OrderedDict()
        self._stats_lock = threading.Lock()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _check_plane_size(self, info: DatasetInfo, bytes_per_pixel: int = 0) -> None:
        """Refuse a plane that would not fit in memory, as stored or at ``bytes_per_pixel``."""
        nbytes = max(info.plane_nbytes, info.size_x * info.size_y * bytes_per_pixel)
        if nbytes > self.max_plane_bytes:
            raise InvalidRequestError(
                f"plane too large to read ({info.size_x} × {info.size_y} pixels, {nbytes >> 20} MiB)"
            )

    def _raw(self, dataset: Any, file_id: FileId, okey: str, t: int, c: int, z: int) -> Plane:
        dataset.check_index(t, c, z)
        self._check_plane_size(dataset.info)
        return self._cache.get_or_load(("raw", file_id, okey, t, c, z), lambda: dataset.read_plane(t, c, z))

    def _mean_of_frames(
        self, dataset: Any, file_id: FileId, okey: str, t: int, t_end: int, c: int, z: int
    ) -> Plane:
        """Mean of frames ``t..t_end``, accumulated in float64. The frames are read straight
        from the file: caching forty raw frames to show one average would empty the cache."""
        info: DatasetInfo = dataset.info
        if not isinstance(t_end, int) or isinstance(t_end, bool) or not t <= t_end < info.size_t:
            raise InvalidRequestError(f"t_end={t_end!r} must be between t={t} and {info.size_t - 1}")
        self._check_plane_size(info, bytes_per_pixel=8)  # the accumulator is float64
        acc = np.zeros((info.size_y, info.size_x), np.float64)
        for k in range(t, t_end + 1):
            frame = self._cache.get(("raw", file_id, okey, k, c, z))
            acc += dataset.read_plane(k, c, z) if frame is None else frame
        acc /= t_end - t + 1
        return acc.astype(np.float32)

    def _gain_reference(self, spec: GainSpec) -> Plane:
        """The multiplicative gain of ``spec`` as stored (not oriented), defects at 0."""

        def load() -> Plane:
            with self._pool.lease(spec.path, None, file_id=spec.file_id) as dataset:
                info: DatasetInfo = dataset.info
                if info.rgb or info.plane_nbytes > self.max_plane_bytes:
                    raise InvalidRequestError(f"{spec.path.name} cannot be used as a gain reference")
                # a reference is one image; of a stack, the first plane is taken
                return prepare_gain(dataset.read_plane(0, 0, 0), invert=spec.invert)

        return self._cache.get_or_load(("gain", spec.file_id, spec.invert), load)

    def _gain(self, spec: GainSpec, shape: tuple[int, int]) -> Plane:
        """The gain of ``spec``, oriented and on the grid of a plane of ``shape``."""
        return self._cache.get_or_load(
            ("gain", spec.key(), shape),
            lambda: fit_gain(
                np.ascontiguousarray(orient(self._gain_reference(spec), spec.orientation)), shape
            ),
        )

    def _source(
        self, dataset: Any, file_id: FileId, okey: str, t: int, c: int, z: int, proc: Processing
    ) -> Plane:
        """The full-resolution plane after ``proc`` (frame average, resolution filters)."""
        proc = proc.full_resolution  # one plane in the cache, however it is binned afterwards
        if proc.identity:
            return self._raw(dataset, file_id, okey, t, c, z)
        dataset.check_index(t, c, z)

        def load() -> Plane:
            if proc.t_end is None:
                plane = self._raw(dataset, file_id, okey, t, c, z)
            else:
                plane = self._mean_of_frames(dataset, file_id, okey, t, proc.t_end, c, z)
            if proc.gain is not None:
                plane = apply_gain(plane, self._gain(proc.gain, (plane.shape[0], plane.shape[1])))
            if not proc.filtered:
                return plane
            apix_x, apix_y = _filter_pixel(dataset.info, proc)
            return _filter(plane, proc, apix_x, apix_y)

        return self._cache.get_or_load(("proc", file_id, okey, t, c, z, proc.key()), load)

    def _reduced(
        self, dataset: Any, file_id: FileId, okey: str, t: int, c: int, z: int, proc: Processing, bin: int
    ) -> Plane:
        """The whole plane after ``proc``, reduced ``bin`` times by Fourier cropping.

        A filter is applied *after* the reduction, at the pixel size of the reduced plane. The
        crop keeps the low frequencies of the transform as they are and the filter multiplies
        each of them by its gain, so the two commute: the result is the reduction of the
        filtered plane, except along the edges, which each treats in its own way (ADR 013).
        Moving a filter then costs the transform of the reduced plane (some 35 ms for a 4k
        image at bin 4), not that of the full plane (more than a second); the reduction of the
        unfiltered plane is made once and kept.
        """
        info: DatasetInfo = dataset.info
        shape = binned_shape(info.size_y, info.size_x, bin)
        base = proc.unfiltered

        def unfiltered() -> Plane:
            return self._cache.get_or_load(
                ("fourier", file_id, okey, t, c, z, base.key(), bin),
                lambda: fourier_crop(self._source(dataset, file_id, okey, t, c, z, base), shape),
            )

        if not proc.filtered:
            return unfiltered()
        apix_x, apix_y = _filter_pixel(info, proc)
        return self._cache.get_or_load(
            ("filtered", file_id, okey, t, c, z, proc.key(), bin),
            lambda: _filter(
                unfiltered(),
                proc,
                apix_x * info.size_x / shape[1],  # the pitch of the reduced grid, axis by axis
                apix_y * info.size_y / shape[0],
            ),
        )

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

        A filtered plane at ``bin > 1`` is reduced by Fourier cropping, then filtered
        (:meth:`_reduced`), as with ``fourier_bin``; a region must then start on a multiple of
        the bin, or the plane is filtered at full resolution and binned by blocks, as before.
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
            aligned = x % bin == 0 and y % bin == 0
            # a filtered view is reduced in Fourier space when it can be; a plane too large for
            # its transform is filtered and binned by blocks, as before ADR 013, not refused
            fourier_fits = max(info.plane_nbytes, info.size_x * info.size_y * 24) <= self.max_plane_bytes
            if bin == 1 and region == full:
                array = self._source(dataset, file_id, okey, t, c, z, proc)
            elif bin > 1 and (proc.fourier_bin or (proc.filtered and aligned and fourier_fits)):
                # the whole plane is reduced once, and regions are cut out of the result
                if not aligned:
                    raise InvalidRequestError(
                        "with Fourier binning, a region must start on a multiple of the bin"
                    )
                self._check_plane_size(info, bytes_per_pixel=24)  # float32 plane and its transform
                whole = self._reduced(dataset, file_id, okey, t, c, z, proc, bin)
                array = whole[y // bin : y // bin + out_h, x // bin : x // bin + out_w]
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
        bin: int = 1,
    ) -> tuple[PlaneStats, FileId]:
        """Statistics of plane ``(t, c, z)`` after ``proc``: of the full-resolution plane, or of
        the whole plane reduced ``bin`` times as :meth:`plane` serves it. A view that follows
        a filter while it moves asks for those of what it shows: a filter at full resolution
        takes more than a second on a 4k image."""
        file_id = FileId.of(path)
        okey = options_key(options)
        key = (file_id, okey, t, c, z, proc.key(), bin)
        with self._stats_lock:
            cached = self._stats.get(key)
            if cached is not None:
                self._stats.move_to_end(key)
                return cached, file_id
        if bin != 1:
            reduced = self.plane(path, t, c, z, bin=bin, options=options, proc=proc)
            result, file_id = plane_stats(reduced.array), reduced.file_id
            key = (file_id, okey, t, c, z, proc.key(), bin)
        else:
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
        tile: int = 512,
        options: Mapping[str, Any] | None = None,
        proc: Processing = NO_PROCESSING,
    ) -> tuple[Plane, FileId]:
        """Power spectrum (averaged periodogram) of plane ``(t, c, z)`` after the frame average
        and the gain of ``proc``; its resolution filters are ignored, they would only draw
        their own envelope. Square, zero frequency at the centre."""
        file_id = FileId.of(path)
        okey = options_key(options)
        source = proc.unfiltered
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            dataset.check_index(t, c, z)
            power = self._cache.get_or_load(
                ("spectrum", file_id, okey, t, c, z, source.key(), tile),
                lambda: power_spectrum(self._source(dataset, file_id, okey, t, c, z, source), tile=tile)[0],
            )
        return power, file_id

    def ctf(
        self,
        path: Path,
        t: int = 0,
        c: int = 0,
        z: int = 0,
        *,
        options: Mapping[str, Any] | None = None,
        proc: Processing = NO_PROCESSING,
        kv: float = 300.0,
        cs_mm: float = 2.7,
        amplitude: float = 0.07,
    ) -> tuple[CtfEstimate | None, FileId]:
        """The CTF of plane ``(t, c, z)`` after the frame average and the gain of ``proc``
        (``proc.apix`` when the file does not know its pixel size); None when its spectrum
        shows no Thon rings. Resolution filters are ignored, as for the spectrum."""
        file_id = FileId.of(path)
        okey = options_key(options)
        source = proc.unfiltered
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            dataset.check_index(t, c, z)
            apix = proc.apix or dataset.info.calibration.x.to_angstrom()
            if not apix:
                raise InvalidRequestError("pixel size unknown: give it (apix, in Å) to estimate the CTF")
            if dataset.info.rgb:
                raise InvalidRequestError("a colour picture has no CTF")
            key = (file_id, okey, t, c, z, source.key(), apix, kv, cs_mm, amplitude)
            with self._stats_lock:
                if key in self._ctfs:
                    self._ctfs.move_to_end(key)
                    return self._ctfs[key], file_id
            found = estimate_ctf(
                self._source(dataset, file_id, okey, t, c, z, source),
                apix,
                kv=kv,
                cs_mm=cs_mm,
                amplitude=amplitude,
            )
        with self._stats_lock:
            self._ctfs[key] = found
            while len(self._ctfs) > _GAIN_CHECK_CACHE_ITEMS:
                self._ctfs.popitem(last=False)
        return found, file_id

    def gain_check(self, path: Path, gain: GainSpec, options: Mapping[str, Any] | None = None) -> GainCheck:
        """Which orientation of ``gain`` flattens the movie ``path`` best, judged on the mean
        of its first frames (channel 0, slice 0). ``gain.orientation`` plays no part."""
        file_id = FileId.of(path)
        okey = options_key(options)
        key = (file_id, okey, gain.file_id, gain.invert)
        with self._stats_lock:
            cached = self._gain_checks.get(key)
            if cached is not None:
                self._gain_checks.move_to_end(key)
                return cached
        reference = self._gain_reference(gain)
        with self._pool.lease(path, options, file_id=file_id) as dataset:
            frames = min(dataset.info.size_t, _GAIN_CHECK_FRAMES)
            if frames > 1:
                image = self._mean_of_frames(dataset, file_id, okey, 0, frames - 1, 0, 0)
            else:
                image = self._raw(dataset, file_id, okey, 0, 0, 0)
            check = check_orientation(image, reference)
        result = GainCheck(
            check,
            frames,
            int((reference == 0).sum()),
            (reference.shape[0], reference.shape[1]),
            file_id,
            listed_defects(Path(gain.file_id.path)),
        )
        with self._stats_lock:
            self._gain_checks[key] = result
            while len(self._gain_checks) > _GAIN_CHECK_CACHE_ITEMS:
                self._gain_checks.popitem(last=False)
        return result

    def cache_stats(self) -> dict[str, dict[str, int]]:
        return {"planes": self._cache.stats(), "datasets": self._pool.stats()}

    def close(self) -> None:
        self._pool.close_all()
        self._cache.clear()
