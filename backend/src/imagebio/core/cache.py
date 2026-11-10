"""Bounded caches: decoded planes (:class:`ByteLRU`) and open datasets (:class:`DatasetPool`).

Both are keyed on the identity of the file *content* (:class:`~imagebio.core.model.FileId`:
path, size, mtime), never on the path alone: a file that changes on disk is read again.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from imagebio.core.model import Dataset, FileId, Plane

__all__ = ["ByteLRU", "DatasetPool", "options_key"]


def options_key(options: Mapping[str, Any] | None) -> str:
    """Canonical, hashable form of reader options (key order does not matter)."""
    return json.dumps(dict(options), sort_keys=True, default=str) if options else ""


def _held_nbytes(a: Plane) -> int:
    """Bytes kept alive by ``a``: its own, or those of the buffer it is a view of."""
    root: Any = a
    while isinstance(root, np.ndarray) and root.base is not None:
        root = root.base
    if isinstance(root, np.ndarray):
        return int(root.nbytes)
    try:
        return len(memoryview(root).cast("B"))
    except (TypeError, ValueError):
        return int(a.nbytes)


class _Flight:
    """A load in progress, shared by every caller waiting for the same key."""

    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Plane | None = None
        self.error: BaseException | None = None


class ByteLRU:
    """Thread-safe LRU cache of arrays, bounded by the sum of their ``nbytes``.

    Stored arrays are read-only. A small view of a large buffer is copied first, so that the
    accounted size is the memory really held.
    """

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        self.max_bytes = max_bytes
        self._data: OrderedDict[Hashable, Plane] = OrderedDict()
        self._bytes = 0
        self._flights: dict[Hashable, _Flight] = {}
        self._lock = threading.Lock()
        self._hits = self._misses = self._evictions = 0

    # ── internals (lock held) ────────────────────────────────────────────────
    def _store(self, key: Hashable, value: Plane) -> None:
        old = self._data.pop(key, None)
        if old is not None:
            self._bytes -= old.nbytes
        if value.nbytes > self.max_bytes:
            return
        self._data[key] = value
        self._bytes += value.nbytes
        while self._bytes > self.max_bytes:
            _, evicted = self._data.popitem(last=False)
            self._bytes -= evicted.nbytes
            self._evictions += 1

    @staticmethod
    def _freeze(value: Plane) -> Plane:
        value = np.asarray(value)
        if _held_nbytes(value) > value.nbytes + value.nbytes // 4:
            value = value.copy()
        if value.flags.writeable:
            value.flags.writeable = False
        return value

    # ── API ──────────────────────────────────────────────────────────────────
    def get(self, key: Hashable) -> Plane | None:
        with self._lock:
            value = self._data.get(key)
            if value is None:
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: Hashable, value: Plane) -> None:
        """Store ``value``. Arrays larger than ``max_bytes`` are not stored."""
        frozen = self._freeze(value)
        with self._lock:
            self._store(key, frozen)

    def get_or_load(self, key: Hashable, loader: Callable[[], Plane]) -> Plane:
        """The cached value, or ``loader()`` stored and returned.

        Single flight: concurrent callers of the same missing key trigger one ``loader`` call and
        all get its result (or its exception). The loader runs without the cache lock, so loads
        of different keys run concurrently.
        """
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
                self._hits += 1
                return value
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
                self._misses += 1
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.value is not None
            return flight.value
        try:
            value = self._freeze(loader())
            flight.value = value
        except BaseException as e:
            flight.error = e
            raise
        finally:
            with self._lock:
                del self._flights[key]
                if flight.value is not None:
                    self._store(key, flight.value)
            flight.done.set()
        return value

    def discard(self, predicate: Callable[[Hashable], bool]) -> int:
        """Remove the entries whose key satisfies ``predicate``; returns how many."""
        with self._lock:
            doomed = [k for k in self._data if predicate(k)]
            for k in doomed:
                self._bytes -= self._data.pop(k).nbytes
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "bytes": self._bytes,
                "items": len(self._data),
                "max_bytes": self.max_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
            }


class _Entry:
    __slots__ = ("dataset", "error", "key", "leases", "ready", "retired")

    def __init__(self, key: tuple[FileId, str]) -> None:
        self.key = key
        self.dataset: Dataset | None = None
        self.error: BaseException | None = None
        self.leases = 0
        self.retired = False  # no longer in the pool: closed when the last lease is released
        self.ready = threading.Event()


Opener = Callable[[Path, Mapping[str, Any] | None], Dataset]


def _default_opener(path: Path, options: Mapping[str, Any] | None) -> Dataset:
    from imagebio.io.registry import open_dataset  # imported late: the registry imports the model

    return open_dataset(path, options)


class DatasetPool:
    """Keeps at most ``max_open`` datasets open for reuse.

    ``lease(path, options)`` yields an open dataset. Datasets are keyed on ``(FileId, options)``:
    when a file changes on disk, the next lease opens it again and the stale dataset is closed.
    A dataset is never closed while leased; if it is evicted or becomes stale meanwhile, it is
    closed when its last lease is released. Concurrent leases of the same key share one open.
    """

    def __init__(self, max_open: int, opener: Opener = _default_opener) -> None:
        if max_open < 1:
            raise ValueError("max_open must be >= 1")
        self.max_open = max_open
        self._opener = opener
        self._entries: OrderedDict[tuple[FileId, str], _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._opens = self._evictions = 0

    def _retire(self, entry: _Entry, closing: list[Dataset]) -> None:
        """Take ``entry`` out of the pool (lock held); close it now if nobody uses it."""
        self._entries.pop(entry.key, None)
        entry.retired = True
        if entry.leases == 0 and entry.dataset is not None:
            closing.append(entry.dataset)
            entry.dataset = None

    def _trim(self, closing: list[Dataset]) -> None:
        """Evict idle datasets, oldest first, while the pool is over its limit (lock held)."""
        if len(self._entries) <= self.max_open:
            return
        for entry in list(self._entries.values()):
            if len(self._entries) <= self.max_open:
                break
            if entry.leases == 0 and entry.ready.is_set():
                self._retire(entry, closing)
                self._evictions += 1

    @staticmethod
    def _close(datasets: list[Dataset]) -> None:
        for ds in datasets:
            with contextlib.suppress(Exception):
                ds.close()

    @contextlib.contextmanager
    def lease(
        self, path: str | Path, options: Mapping[str, Any] | None = None, *, file_id: FileId | None = None
    ) -> Iterator[Dataset]:
        """Lease the dataset of ``path``. ``file_id`` lets a caller that already took the identity
        of the file (for its own cache keys) lease exactly that version."""
        file_id = file_id or FileId.of(path)
        key = (file_id, options_key(options))
        closing: list[Dataset] = []
        with self._lock:
            entry = self._entries.get(key)
            leader = entry is None
            if entry is None:
                for other in list(
                    self._entries.values()
                ):  # the same file with another size or mtime is stale
                    if other.key[0].path == file_id.path and other.key[0] != file_id:
                        self._retire(other, closing)
                entry = self._entries[key] = _Entry(key)
                self._opens += 1
            else:
                self._entries.move_to_end(key)
            entry.leases += 1
        self._close(closing)

        if leader:
            try:
                entry.dataset = self._opener(Path(path), options)
            except BaseException as e:
                entry.error = e
                with self._lock:
                    self._entries.pop(key, None)
                    entry.retired = True
                    entry.leases -= 1
                entry.ready.set()
                raise
            entry.ready.set()
        else:
            entry.ready.wait()
            if entry.error is not None:
                with self._lock:
                    entry.leases -= 1
                raise entry.error
        closing = []
        with self._lock:
            self._trim(closing)
        self._close(closing)

        dataset = entry.dataset
        assert dataset is not None
        try:
            yield dataset
        finally:
            closing = []
            with self._lock:
                entry.leases -= 1
                if entry.leases == 0:
                    if entry.retired and entry.dataset is not None:
                        closing.append(entry.dataset)
                        entry.dataset = None
                    else:
                        self._trim(closing)
            self._close(closing)

    def close_all(self) -> None:
        """Close every idle dataset now, and the leased ones as soon as they are released."""
        closing: list[Dataset] = []
        with self._lock:
            for entry in list(self._entries.values()):
                self._retire(entry, closing)
        self._close(closing)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "open": len(self._entries),
                "leased": sum(1 for e in self._entries.values() if e.leases),
                "max_open": self.max_open,
                "opens": self._opens,
                "evictions": self._evictions,
            }
