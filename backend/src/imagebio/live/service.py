"""The live monitor as the API sees it: watches per user, scanners per directory.

A *watch* is "user U follows directory D", kept in the index so that it survives a restart.
Scanning is shared: one scanner per watched directory, whoever watches it, running as long as
at least one active watch points at it. Stopping one's own watch never touches anybody else's
(the legacy monitor had one global watch that every user overwrote).

Callers pass real paths that were resolved through ``RootSet`` for the current user, and must
check on every request that the user may still read the watched directory (the API does).
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError, NotFoundError
from imagebio.core.model import FileId
from imagebio.io.registry import Registry
from imagebio.live.index import Index
from imagebio.live.scanner import CycleReport, Scanner, ScanSettings
from imagebio.live.thumbs import MAX_PX, MIN_PX, render_thumbnail
from imagebio.service.planes import PlaneService

__all__ = ["CATEGORIES", "MAX_ACTIVE_WATCHES", "LiveService", "WatchInfo"]

log = logging.getLogger(__name__)

CATEGORIES: Final = ("data", "image", "gridsquare", "foilhole", "atlas")
#: Categories that count as acquisitions (rates, last image).
_ACQUIRED: Final = ("data", "image")
MAX_ACTIVE_WATCHES: Final = 8  # per user
MAX_ITEMS: Final = 5000
_THUMB_MEMORY_BYTES: Final = 64 * 2**20
_RENDER_SLOTS: Final = 4  # thumbnails computed at once: each reads a full image


@dataclass(frozen=True, slots=True)
class WatchInfo:
    id: int
    user: str
    logical: str
    root_id: int
    root_path: Path  # real path: internal, never sent to a client
    active: bool
    created_at: float
    stopped_at: float | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.logical,
            "active": self.active,
            "created_at": self.created_at,
            "stopped_at": self.stopped_at,
        }


def _watch_of(row: sqlite3.Row) -> WatchInfo:
    return WatchInfo(
        row["id"],
        row["user"],
        row["logical"],
        row["root_id"],
        Path(row["path"]),
        bool(row["active"]),
        row["created_at"],
        row["stopped_at"],
    )


_WATCH_SQL: Final = (
    "SELECT w.id, w.user, w.logical, w.root_id, r.path, w.active, w.created_at, w.stopped_at "
    "FROM watches w JOIN roots r ON r.id = w.root_id "
)


class LiveService:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        planes: PlaneService,
        *,
        scan_settings: ScanSettings | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._planes = planes
        self._scan_settings = scan_settings or ScanSettings()
        #: Source of "now" for watches, rates and background scans; tests replace it.
        self.clock = clock
        self._lock = threading.RLock()
        self._index_value: Index | None = None
        self._threads: dict[int, tuple[threading.Thread, threading.Event]] = {}
        self._manual: dict[int, Scanner] = {}
        self._pending: dict[int, int] = {}  # root -> files being followed, as of the last cycle
        self._thumbs: OrderedDict[tuple[FileId, int], bytes] = OrderedDict()
        self._thumb_bytes = 0
        self._thumb_writes = 0
        self._render = threading.BoundedSemaphore(_RENDER_SLOTS)
        self._closed = False

    # ── index and scanners ───────────────────────────────────────────────────
    @property
    def index(self) -> Index:
        """The index, opened at first use: a server whose users never open the Live mode
        writes nothing to disk."""
        with self._lock:
            if self._index_value is None:
                directory = self._settings.live_state_dir
                directory.mkdir(parents=True, exist_ok=True)
                self._index_value = Index(directory / "live.sqlite")
            return self._index_value

    def start(self) -> None:
        """Resume the watches that were active when the server stopped, if any were ever made."""
        if (self._settings.live_state_dir / "live.sqlite").is_file():
            self._sync_scanners()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            threads = list(self._threads.values())
            self._threads.clear()
        for _, stop in threads:
            stop.set()
        for thread, _ in threads:
            thread.join(timeout=30)
        with self._lock:
            if self._index_value is not None:
                self._index_value.close()
                self._index_value = None

    def _active_roots(self) -> dict[int, Path]:
        rows = self.index.connection().execute(
            "SELECT DISTINCT r.id, r.path FROM watches w JOIN roots r ON r.id = w.root_id WHERE w.active = 1"
        )
        return {row["id"]: Path(row["path"]) for row in rows}

    def _sync_scanners(self) -> None:
        """One scanning thread per directory with an active watch, none for the others."""
        if not self._settings.live_scan:
            return
        with self._lock:
            if self._closed:
                return
            wanted = self._active_roots()
            for root_id in list(self._threads):
                if root_id not in wanted:
                    self._threads.pop(root_id)[1].set()
            for root_id, path in wanted.items():
                if root_id not in self._threads:
                    stop = threading.Event()
                    thread = threading.Thread(
                        target=self._run, args=(root_id, path, stop), name=f"live-scan-{root_id}", daemon=True
                    )
                    self._threads[root_id] = (thread, stop)
                    thread.start()

    def _run(self, root_id: int, path: Path, stop: threading.Event) -> None:
        try:
            scanner = Scanner(self.index, root_id, path, self._registry, self._scan_settings)
        except Exception:
            log.exception("cannot start the live scanner of root %s", root_id)
            return
        wait = 0.0
        while not stop.wait(wait):
            try:
                report = scanner.cycle(self.clock())
                self._pending[root_id] = report.pending
                took = report.duration_ms / 1000
            except Exception:  # cycle() reports its own errors; this is a bug or the index gone
                log.exception("live scanner of root %s failed", root_id)
                took = 0.0
            wait = max(0.5, self._settings.live_interval_s - took)

    def scan_once(self, now: float | None = None) -> dict[int, CycleReport]:
        """Run one cycle on every watched directory, in the calling thread. For a process that
        does not scan in the background (``live.scan = false``): tests and one-off indexing."""
        reports: dict[int, CycleReport] = {}
        for root_id, path in self._active_roots().items():
            scanner = self._manual.get(root_id)
            if scanner is None:
                scanner = self._manual[root_id] = Scanner(
                    self.index, root_id, path, self._registry, self._scan_settings
                )
            reports[root_id] = scanner.cycle(self.clock() if now is None else now)
            self._pending[root_id] = reports[root_id].pending
        return reports

    # ── watches ──────────────────────────────────────────────────────────────
    def watches(self, user: str) -> list[WatchInfo]:
        rows = self.index.connection().execute(
            _WATCH_SQL + "WHERE w.user = ? ORDER BY w.active DESC, w.created_at DESC", (user,)
        )
        return [_watch_of(row) for row in rows]

    def watch(self, user: str, watch_id: int) -> WatchInfo:
        """The watch ``watch_id`` of ``user``. Somebody else's watch does not exist."""
        row = (
            self.index.connection()
            .execute(_WATCH_SQL + "WHERE w.id = ? AND w.user = ?", (watch_id, user))
            .fetchone()
        )
        if row is None:
            raise NotFoundError("no such watch")
        return _watch_of(row)

    def start_watch(self, user: str, logical: str, real: Path) -> WatchInfo:
        """Watch directory ``real`` (seen by ``user`` as ``logical``). Watching again what one
        already watches is not an error: the same watch is returned, active."""
        now = self.clock()
        with self.index.write() as db:
            existing = db.execute(
                "SELECT id, active FROM watches WHERE user = ? AND logical = ?", (user, logical)
            ).fetchone()
            if existing is None or not existing["active"]:
                active = db.execute(
                    "SELECT COUNT(*) FROM watches WHERE user = ? AND active = 1", (user,)
                ).fetchone()[0]
                if active >= MAX_ACTIVE_WATCHES:
                    raise InvalidRequestError(
                        f"{MAX_ACTIVE_WATCHES} directories are already being watched: stop one first"
                    )
            db.execute("INSERT OR IGNORE INTO roots (path, created_at) VALUES (?, ?)", (str(real), now))
            root_id = db.execute("SELECT id FROM roots WHERE path = ?", (str(real),)).fetchone()["id"]
            db.execute(
                "INSERT INTO watches (user, logical, root_id, active, created_at) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT (user, logical) DO UPDATE SET active = 1, root_id = excluded.root_id, "
                "stopped_at = NULL",
                (user, logical, root_id, now),
            )
            watch_id = db.execute(
                "SELECT id FROM watches WHERE user = ? AND logical = ?", (user, logical)
            ).fetchone()["id"]
        self._sync_scanners()
        return self.watch(user, watch_id)

    def stop_watch(self, user: str, watch_id: int) -> WatchInfo:
        """Stop one's own watch. The index of the directory is kept; scanning goes on only if
        somebody else still watches it."""
        with self.index.write() as db:
            cursor = db.execute(
                "UPDATE watches SET active = 0, stopped_at = ? WHERE id = ? AND user = ?",
                (self.clock(), watch_id, user),
            )
            if cursor.rowcount == 0:
                raise NotFoundError("no such watch")
        self._sync_scanners()
        with self._lock:
            for root_id in set(self._manual) - set(self._active_roots()):
                del self._manual[root_id]
        return self.watch(user, watch_id)

    # ── what a watch shows ───────────────────────────────────────────────────
    def summary(self, watch: WatchInfo) -> dict[str, Any]:
        db = self.index.connection()
        now = self.clock()
        root = db.execute(
            "SELECT cycle_at, full_at, cycle_ms, error, seq FROM roots WHERE id = ?", (watch.root_id,)
        ).fetchone()
        counts: dict[str, dict[str, int]] = {}
        for row in db.execute(
            "SELECT category, state, COUNT(*) AS n FROM exposures "
            "WHERE root_id = ? AND primary_name IS NOT NULL "
            "AND state != 'deleted' GROUP BY category, state",
            (watch.root_id,),
        ):
            counts.setdefault(row["category"], {})[row["state"]] = row["n"]
        marks = ",".join("?" * len(_ACQUIRED))
        acquired = f"root_id = ? AND category IN ({marks}) AND state = 'complete'"
        last = db.execute(
            f"SELECT name, time, completed_at FROM exposures WHERE {acquired} AND time IS NOT NULL "  # noqa: S608
            "ORDER BY time DESC LIMIT 1",
            (watch.root_id, *_ACQUIRED),
        ).fetchone()

        def arrived_since(seconds: float) -> int:
            # Only what appeared while the directory was being watched: the images of a session
            # opened after the fact were found, they did not arrive. Counted on this server's
            # clock (the time the scanner accepted them), which the microscope's need not match.
            return int(
                db.execute(
                    f"SELECT COUNT(*) FROM exposures WHERE {acquired} AND arrived = 1 AND completed_at > ?",  # noqa: S608
                    (watch.root_id, *_ACQUIRED, now - seconds),
                ).fetchone()[0]
            )

        try:
            usage = shutil.disk_usage(watch.root_path)
            disk: dict[str, Any] | None = {
                "total": usage.total,
                "free": usage.free,
                "percent": round(100 * (1 - usage.free / usage.total), 1) if usage.total else None,
            }
        except OSError:
            disk = None
        scanning = watch.root_id in self._threads or watch.root_id in self._manual
        return {
            "watch": watch.to_json(),
            "now": now,
            "seq": root["seq"],
            "scanner": {
                "running": bool(watch.active and scanning),
                "cycle_at": root["cycle_at"],
                "full_at": root["full_at"],
                "cycle_ms": root["cycle_ms"],
                "error": root["error"],
                "pending": self._pending.get(watch.root_id, 0),
            },
            "counts": counts,
            "acquired": sum(counts.get(c, {}).get("complete", 0) for c in _ACQUIRED),
            "incoming": sum(counts.get(c, {}).get("incoming", 0) for c in _ACQUIRED),
            "rate": {"last_5_min": arrived_since(300) * 12, "last_hour": arrived_since(3600)},
            "last": dict(last) if last is not None else None,
            "disk": disk,
        }

    def items(
        self, watch: WatchInfo, since: int = 0, limit: int = 1000, categories: tuple[str, ...] = _ACQUIRED
    ) -> dict[str, Any]:
        """Exposures that changed after sequence number ``since``, oldest change first."""
        unknown = set(categories) - set(CATEGORIES)
        if unknown or not categories:
            raise InvalidRequestError(f"category must be among {', '.join(CATEGORIES)}")
        limit = max(1, min(MAX_ITEMS, limit))
        db = self.index.connection()
        marks = ",".join("?" * len(categories))
        rows = db.execute(
            "SELECT e.id, e.seq, e.name, e.category, e.state, e.primary_name, e.primary_role, "  # noqa: S608
            "e.size, e.time, e.time_source, e.first_seen, e.completed_at, e.verified, e.note, "
            "e.members, e.meta, d.rel "
            "FROM exposures e JOIN dirs d ON d.id = e.dir_id "
            f"WHERE e.root_id = ? AND e.seq > ? AND e.primary_name IS NOT NULL AND e.category IN ({marks}) "
            "ORDER BY e.seq LIMIT ?",
            (watch.root_id, since, *categories, limit),
        ).fetchall()
        head = db.execute("SELECT seq FROM roots WHERE id = ?", (watch.root_id,)).fetchone()["seq"]
        items = [
            {
                "id": row["id"],
                "seq": row["seq"],
                "name": row["name"],
                "dir": row["rel"],
                "path": "/".join(p for p in (watch.logical, row["rel"], row["primary_name"]) if p),
                "category": row["category"],
                "state": row["state"],
                "role": row["primary_role"],
                "size": row["size"],
                "time": row["time"],
                "time_source": row["time_source"],
                "first_seen": row["first_seen"],
                "completed_at": row["completed_at"],
                "verified": bool(row["verified"]),
                "note": row["note"],
                "members": json.loads(row["members"]),
                "meta": json.loads(row["meta"]),
            }
            for row in rows
        ]
        return {
            "items": items,
            "seq": items[-1]["seq"] if items else since,
            "more": len(items) == limit,
            "head": head,
        }

    def history(self, watch: WatchInfo, bucket: int = 60) -> dict[str, Any]:
        """Acquisitions per time bucket (seconds), by acquisition time: ``[[start, count], …]``."""
        if not 1 <= bucket <= 86400:
            raise InvalidRequestError("bucket must be between 1 and 86400 seconds")
        marks = ",".join("?" * len(_ACQUIRED))
        rows = self.index.connection().execute(
            "SELECT CAST(time / ? AS INTEGER) AS slot, COUNT(*) AS n FROM exposures "  # noqa: S608
            f"WHERE root_id = ? AND category IN ({marks}) AND state = 'complete' AND time IS NOT NULL "
            "GROUP BY slot ORDER BY slot DESC LIMIT 10000",
            (bucket, watch.root_id, *_ACQUIRED),
        )
        counts = [[row["slot"] * bucket, row["n"]] for row in rows]
        counts.reverse()
        return {"bucket": bucket, "counts": counts}

    # ── thumbnails ───────────────────────────────────────────────────────────
    def thumbnail(self, real: Path, px: int) -> tuple[bytes, FileId]:
        """A JPEG thumbnail of the image ``real`` (already authorised), from memory, from the
        index, or computed and kept in both. Keyed on the content of the file."""
        if not MIN_PX <= px <= MAX_PX:
            raise InvalidRequestError(f"size must be between {MIN_PX} and {MAX_PX}")
        file_id = FileId.of(real)
        key = (file_id, px)
        with self._lock:
            hit = self._thumbs.get(key)
            if hit is not None:
                self._thumbs.move_to_end(key)
                return hit, file_id
        row = (
            self.index.connection()
            .execute(
                "SELECT data FROM thumbs WHERE path = ? AND px = ? AND size = ? AND mtime_ns = ?",
                (file_id.path, px, file_id.size, file_id.mtime_ns),
            )
            .fetchone()
        )
        if row is not None:
            data = bytes(row["data"])
        else:
            with self._render:
                data = render_thumbnail(self._planes, real, px)
            self._keep_thumbnail(file_id, px, data)
        with self._lock:
            self._thumbs[key] = data
            self._thumb_bytes += len(data)
            while self._thumb_bytes > _THUMB_MEMORY_BYTES and self._thumbs:
                _, dropped = self._thumbs.popitem(last=False)
                self._thumb_bytes -= len(dropped)
        return data, file_id

    def _keep_thumbnail(self, file_id: FileId, px: int, data: bytes) -> None:
        cap = self._settings.thumb_cache_bytes
        if cap <= 0:
            return
        with (
            contextlib.suppress(sqlite3.OperationalError),
            self.index.write() as db,
        ):  # busy: not worth failing for
            db.execute(
                "INSERT OR REPLACE INTO thumbs (path, px, size, mtime_ns, data, used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (file_id.path, px, file_id.size, file_id.mtime_ns, data, self.clock()),
            )
            self._thumb_writes += 1
            if self._thumb_writes % 200 == 1:  # now and then: drop the oldest tenth when over the cap
                total = db.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM thumbs").fetchone()[0]
                if total > cap:
                    count = db.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0]
                    db.execute(
                        "DELETE FROM thumbs WHERE rowid IN "
                        "(SELECT rowid FROM thumbs ORDER BY used_at LIMIT ?)",
                        (max(1, count // 10),),
                    )
