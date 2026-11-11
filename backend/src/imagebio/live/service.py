"""The live monitor as the API sees it: watches per user, scanners per directory.

A *watch* is "user U follows directory D", kept in the index so that it survives a restart.
Scanning is shared: one scanner per watched directory, whoever watches it, running as long as
at least one active watch points at it. Stopping one's own watch never touches anybody else's
(the legacy monitor had one global watch that every user overwrote).

Callers pass real paths that were resolved through ``RootSet`` for the current user, and must
check on every request that the user may still read the watched directory (the API does).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError, NotFoundError
from imagebio.core.model import FileId
from imagebio.io.registry import Registry
from imagebio.live.alerts import (
    CONDITIONS,
    AlertDeliveryError,
    AlertRules,
    AlertState,
    Notice,
    Notifier,
    Reading,
    evaluate,
    notice_for,
    step,
)
from imagebio.live.index import Index
from imagebio.live.scanner import CycleReport, Scanner, ScanSettings
from imagebio.live.thumbs import MAX_PX, MIN_PX, render_thumbnail
from imagebio.service.planes import PlaneService

__all__ = ["CATEGORIES", "MAX_ACTIVE_WATCHES", "MAX_STOPPED_WATCHES", "LiveService", "WatchInfo"]

log = logging.getLogger(__name__)

CATEGORIES: Final = ("data", "image", "gridsquare", "foilhole", "atlas")
#: Categories that count as acquisitions (rates, last image).
_ACQUIRED: Final = ("data", "image")
MAX_ACTIVE_WATCHES: Final = 8  # per user
#: Stopped watches a user keeps (with the index of their directory); older ones are forgotten.
MAX_STOPPED_WATCHES: Final = 32
MAX_ITEMS: Final = 5000
_THUMB_MEMORY_BYTES: Final = 64 * 2**20
_RENDER_SLOTS: Final = 4  # thumbnails computed at once: each reads a full image
_MAX_DELIVERY_ATTEMPTS: Final = 5  # a notice that cannot be delivered is tried at this many cycles
_TEST_INTERVAL_S: Final = 10.0  # between two test notices of one user
#: Alerts are sent for a watch as long as its user came to look at it in the last week: nobody
#: is told for ever about a session they left, or may no longer read.
_ALERT_IDLE_S: Final = 7 * 86400.0
_SEEN_EVERY_S: Final = 600.0  # how often "its user looked at it" is written to the index


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
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._planes = planes
        self._scan_settings = scan_settings or ScanSettings(min_stable_s=settings.live_min_stable_s)
        #: Source of "now" for watches, rates and background scans; tests replace it.
        self.clock = clock
        self._lock = threading.RLock()
        self._index_value: Index | None = None
        self._threads: dict[int, tuple[threading.Thread, threading.Event]] = {}
        #: Scanners told to stop that may still be finishing a cycle, by directory.
        self._retired: dict[int, threading.Thread] = {}
        self._manual: dict[int, Scanner] = {}
        self._pending: dict[int, int] = {}  # root -> files being followed, as of the last cycle
        self._thumbs: OrderedDict[tuple[FileId, int], bytes] = OrderedDict()
        self._thumb_bytes = 0
        self._thumb_db_bytes: int | None = None  # size of the thumbnails in the index, once counted
        self._render = threading.BoundedSemaphore(_RENDER_SLOTS)
        self._closed = False
        #: Delivers alert notices, and tells how full a volume is; tests replace both.
        self.notifier = notifier or Notifier(settings.smtp)
        self.disk_usage: Callable[[Path], Any] = shutil.disk_usage
        self._channels = {c.name: c for c in settings.alert_channels}
        self._tested: dict[str, float] = {}  # user -> their last test notice
        self._seen: dict[int, float] = {}  # watch -> last time its user was allowed to look at it

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
            self._purge_orphans()

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
                    thread, stop = self._threads.pop(root_id)
                    stop.set()
                    self._retired[root_id] = thread  # it may be in the middle of a cycle
            for root_id, path in wanted.items():
                if root_id not in self._threads:
                    stop = threading.Event()
                    thread = threading.Thread(
                        target=self._run,
                        args=(root_id, path, stop, self._retired.pop(root_id, None)),
                        name=f"live-scan-{root_id}",
                        daemon=True,
                    )
                    self._threads[root_id] = (thread, stop)
                    thread.start()
            self._retired = {r: t for r, t in self._retired.items() if t.is_alive()}

    def _run(
        self, root_id: int, path: Path, stop: threading.Event, previous: threading.Thread | None
    ) -> None:
        if previous is not None:
            # one scanner per directory at a time: the one that was stopped finishes its cycle
            # first (two would index the same files, each from its own idea of the tree)
            previous.join()
        if not stop.is_set():
            self._scan_until(stop, root_id, path)
        if not self._closed:
            try:
                self._purge_orphans()  # the watch may have been forgotten while a cycle ran
            except Exception:
                log.exception("cannot tidy the live index after the scanner of root %s", root_id)

    def _scan_until(self, stop: threading.Event, root_id: int, path: Path) -> None:
        try:
            scanner = Scanner(self.index, root_id, path, self._registry, self._scan_settings)
        except Exception:
            log.exception("cannot start the live scanner of root %s", root_id)
            return
        wait = 0.0
        while not stop.wait(wait):
            try:
                now = self.clock()
                report = scanner.cycle(now)
                self._pending[root_id] = report.pending
                took = report.duration_ms / 1000
                self.check_alerts(root_id, now)
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
            when = self.clock() if now is None else now
            reports[root_id] = scanner.cycle(when)
            self._pending[root_id] = reports[root_id].pending
            self.check_alerts(root_id, when)
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
            # a watch that is started again counts silence from now, not from the day its
            # alerts were set: it would otherwise announce at once hours without an image
            db.execute(
                "INSERT INTO watches (user, logical, root_id, active, created_at, seen_at) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT (user, logical) DO UPDATE SET active = 1, root_id = excluded.root_id, "
                "stopped_at = NULL, seen_at = excluded.seen_at, alerts_at = CASE "
                "WHEN watches.active = 1 OR watches.alerts IS NULL THEN watches.alerts_at "
                "ELSE excluded.seen_at END",
                (user, logical, root_id, now, now),
            )
            watch_id = db.execute(
                "SELECT id FROM watches WHERE user = ? AND logical = ?", (user, logical)
            ).fetchone()["id"]
        self._sync_scanners()
        return self.watch(user, watch_id)

    def stop_watch(self, user: str, watch_id: int, *, forget: bool = False) -> WatchInfo:
        """Stop one's own watch. The index of the directory is kept, and scanning goes on only
        if somebody else still watches it. With ``forget`` the watch is removed altogether, and
        the index of its directory with it once no watch, active or stopped, points at it.

        A user keeps their last :data:`MAX_STOPPED_WATCHES` stopped watches: older ones are
        forgotten, so that the index does not grow with every session ever looked at."""
        now = self.clock()
        with self.index.write() as db:
            row = db.execute(_WATCH_SQL + "WHERE w.id = ? AND w.user = ?", (watch_id, user)).fetchone()
            if row is None:
                raise NotFoundError("no such watch")
            watch = _watch_of(row)
            if watch.active:
                watch = replace(watch, active=False, stopped_at=now)
                db.execute("UPDATE watches SET active = 0, stopped_at = ? WHERE id = ?", (now, watch_id))
            doomed = [watch_id] if forget else []
            doomed += [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM watches WHERE user = ? AND active = 0 AND id != ? "
                    "ORDER BY stopped_at DESC, id DESC LIMIT -1 OFFSET ?",
                    (user, watch_id if forget else -1, MAX_STOPPED_WATCHES),
                )
            ]
            db.execute("DELETE FROM alert_states WHERE watch_id = ?", (watch_id,))  # nothing left to resolve
            for doomed_id in doomed:
                db.execute("DELETE FROM alert_states WHERE watch_id = ?", (doomed_id,))
                db.execute("DELETE FROM watches WHERE id = ?", (doomed_id,))
        self._sync_scanners()
        with self._lock:
            for root_id in set(self._manual) - set(self._active_roots()):
                del self._manual[root_id]
            for doomed_id in doomed:
                self._seen.pop(doomed_id, None)
        self._purge_orphans()
        return watch

    def seen(self, watch: WatchInfo) -> None:
        """Note that the user of ``watch`` was just allowed to look at it."""
        self._seen[watch.id] = self.clock()

    def _purge_orphans(self) -> None:
        """Drop the index of the directories that no watch points at any more. A directory
        whose scanner is still finishing a cycle is left to that scanner, on its way out."""
        with self._lock:
            me = threading.current_thread()
            busy = {r for r, t in self._retired.items() if t.is_alive() and t is not me}
            busy |= set(self._threads) | set(self._manual)
        with self.index.write() as db:
            orphans = [
                row["id"]
                for row in db.execute("SELECT id FROM roots WHERE id NOT IN (SELECT root_id FROM watches)")
                if row["id"] not in busy
            ]
            for root_id in orphans:
                db.execute(
                    "DELETE FROM files WHERE dir_id IN (SELECT id FROM dirs WHERE root_id = ?)", (root_id,)
                )
                db.execute("DELETE FROM exposures WHERE root_id = ?", (root_id,))
                db.execute("DELETE FROM dirs WHERE root_id = ?", (root_id,))
                db.execute("DELETE FROM roots WHERE id = ?", (root_id,))
        for root_id in orphans:
            self._pending.pop(root_id, None)

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
        acquired = self._acquired_sql()
        last = db.execute(
            f"SELECT name, time, completed_at FROM exposures WHERE {acquired} AND time IS NOT NULL "  # noqa: S608
            "ORDER BY time DESC LIMIT 1",
            (watch.root_id, *_ACQUIRED),
        ).fetchone()
        disk = self._disk(watch.root_path)
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
            "rate": {
                "last_5_min": self._arrived_since(db, watch.root_id, now, 300) * 12,
                "last_hour": self._arrived_since(db, watch.root_id, now, 3600),
            },
            "alerts": [c for c, state in self._alert_states(db, watch.id).items() if state.told],
            "last": dict(last) if last is not None else None,
            "disk": disk,
        }

    @staticmethod
    def _acquired_sql() -> str:
        marks = ",".join("?" * len(_ACQUIRED))
        return f"root_id = ? AND category IN ({marks}) AND state = 'complete'"

    def _arrived_since(self, db: sqlite3.Connection, root_id: int, now: float, seconds: float) -> int:
        """Images that arrived in the last ``seconds``. Only what appeared while the directory
        was being watched: the images of a session opened after the fact were found, they did
        not arrive. Counted on this server's clock (when the scanner accepted them), which the
        microscope's need not match."""
        return int(
            db.execute(
                f"SELECT COUNT(*) FROM exposures WHERE {self._acquired_sql()} AND arrived = 1 "  # noqa: S608
                "AND completed_at > ?",
                (root_id, *_ACQUIRED, now - seconds),
            ).fetchone()[0]
        )

    def _disk(self, path: Path) -> dict[str, Any] | None:
        try:
            usage = self.disk_usage(path)
        except OSError:
            return None
        return {
            "total": usage.total,
            "free": usage.free,
            "percent": round(100 * (1 - usage.free / usage.total), 1) if usage.total else None,
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

    # ── alerts ───────────────────────────────────────────────────────────────
    def alert_channels(self) -> list[dict[str, str]]:
        """The channels alerts can be sent to: names and kinds, never addresses."""
        return [{"name": c.name, "kind": c.kind} for c in self._channels.values()]

    @staticmethod
    def _alert_states(db: sqlite3.Connection, watch_id: int) -> dict[str, AlertState]:
        """Where each condition of a watch stands, in the order conditions are reported."""
        rows = db.execute(
            "SELECT condition, since, phase, clear_since, delivered, todo, attempts, error "
            "FROM alert_states WHERE watch_id = ?",
            (watch_id,),
        ).fetchall()
        states = {
            row["condition"]: AlertState(
                row["condition"],
                row["since"],
                row["phase"],
                row["clear_since"],
                tuple(json.loads(row["delivered"])),
                tuple(json.loads(row["todo"])),
                row["attempts"],
                row["error"],
            )
            for row in rows
        }
        return {c: states[c] for c in CONDITIONS if c in states}

    def alerts(self, watch: WatchInfo) -> dict[str, Any]:
        """The rules of a watch and the conditions that were announced and still hold."""
        db = self.index.connection()
        row = db.execute("SELECT alerts, alerts_at FROM watches WHERE id = ?", (watch.id,)).fetchone()
        return {
            "rules": AlertRules.from_json(row["alerts"]).to_json(),
            "set_at": row["alerts_at"],
            "channels": self.alert_channels(),
            "firing": [
                {
                    "condition": state.condition,
                    "since": state.since,
                    "notified": not state.todo,  # every channel took the notice
                    "error": state.error,
                }
                for state in self._alert_states(db, watch.id).values()
                if state.told
            ],
        }

    def set_alerts(self, watch: WatchInfo, rules: AlertRules) -> dict[str, Any]:
        """Replace the rules of a watch. Silence is counted from now; what was firing is
        forgotten (the new rules may not even look at it)."""
        unknown = [name for name in rules.channels if name not in self._channels]
        if unknown:
            raise InvalidRequestError(f"unknown alert channel {unknown[0]!r}")
        with self.index.write() as db:
            db.execute(
                "UPDATE watches SET alerts = ?, alerts_at = ? WHERE id = ?",
                (json.dumps(rules.to_json()) if rules != AlertRules() else None, self.clock(), watch.id),
            )
            db.execute("DELETE FROM alert_states WHERE watch_id = ?", (watch.id,))
        return self.alerts(watch)

    def test_alerts(self, watch: WatchInfo) -> list[dict[str, Any]]:
        """Send a test notice to each channel of the watch; says which ones took it."""
        rules = AlertRules.from_json(
            self.index.connection()
            .execute("SELECT alerts FROM watches WHERE id = ?", (watch.id,))
            .fetchone()[0]
        )
        if not rules.channels:
            raise InvalidRequestError("choose at least one alert channel first")
        now = self.clock()
        with self._lock:  # per user, not per watch: several watches are not several times the notices
            last = self._tested.get(watch.user)
            if last is not None and now - last < _TEST_INTERVAL_S:
                raise InvalidRequestError("a test was sent a moment ago: wait a few seconds")
            self._tested[watch.user] = now
        notice = Notice(
            "test",
            True,
            "imageBio : essai des alertes",
            f"{watch.logical} — ceci est un essai : les alertes de cette session arriveront ici.",
            watch.logical,
            now,
        )
        results = []
        for name in rules.channels:
            error = self._send(name, notice)
            results.append({"channel": name, "delivered": error is None, "error": error})
        return results

    def _send(self, channel: str, notice: Notice) -> str | None:
        """Deliver to one channel; the public reason when it fails."""
        configured = self._channels.get(channel)
        if configured is None:
            return f"channel {channel!r} is no longer configured"
        try:
            self.notifier.send(configured, notice)
        except AlertDeliveryError as e:
            return e.public_message
        except Exception as e:  # a notifier must not take the scanner down
            log.exception("alert delivery failed on channel %s", channel)
            return f"channel {channel!r}: {type(e).__name__}"
        return None

    def _reading(
        self, db: sqlite3.Connection, root_id: int, path: Path, armed_at: float, now: float
    ) -> Reading:
        root = db.execute("SELECT error FROM roots WHERE id = ?", (root_id,)).fetchone()
        last = db.execute(
            f"SELECT MAX(completed_at), COUNT(*) FROM exposures WHERE {self._acquired_sql()} AND arrived = 1",  # noqa: S608
            (root_id, *_ACQUIRED),
        ).fetchone()
        total = db.execute(
            f"SELECT COUNT(*) FROM exposures WHERE {self._acquired_sql()}",  # noqa: S608
            (root_id, *_ACQUIRED),
        ).fetchone()[0]
        disk = self._disk(path)
        free_pct = None
        if disk is not None and disk["total"]:
            free_pct = 100 * disk["free"] / disk["total"]
        return Reading(
            now=now,
            armed_at=armed_at,
            last_arrival=last[0],
            acquired=total,
            rate_5min=self._arrived_since(db, root_id, now, 300) * 12,
            rate_hour=self._arrived_since(db, root_id, now, 3600),
            disk_free_pct=free_pct,
            disk_free_bytes=disk["free"] if disk is not None else None,
            scanner_error=root["error"] if root is not None else None,
        )

    def check_alerts(self, root_id: int, now: float) -> list[Notice]:
        """Judge the rules of every active watch on a directory, as of ``now``; send the notices
        that are owed (a condition that started or stopped, a channel that failed last time) and
        return those that reached at least one channel. Called after each scan cycle."""
        db = self.index.connection()
        watches = db.execute(
            "SELECT w.id, w.logical, w.alerts, w.alerts_at, w.seen_at, r.path "
            "FROM watches w JOIN roots r ON r.id = w.root_id WHERE w.root_id = ? AND w.active = 1",
            (root_id,),
        ).fetchall()
        delivered: list[Notice] = []
        for watch in watches:
            seen = max(watch["seen_at"] or 0.0, self._seen.get(watch["id"], 0.0))
            if seen - (watch["seen_at"] or 0.0) >= _SEEN_EVERY_S:
                with self.index.write() as tx:
                    tx.execute("UPDATE watches SET seen_at = ? WHERE id = ?", (seen, watch["id"]))
            rules = AlertRules.from_json(watch["alerts"])
            if not rules.armed or now - max(seen, watch["alerts_at"] or 0.0) > _ALERT_IDLE_S:
                continue
            states = self._alert_states(db, watch["id"])
            reading = self._reading(db, root_id, Path(watch["path"]), watch["alerts_at"] or now, now)
            holds = evaluate(rules, reading, {c for c, state in states.items() if state.told})
            for condition in CONDITIONS:
                before = states.get(condition)
                after = step(before, condition, holds[condition] is not None, now, rules.channels)
                if after is not None and after.todo and after.attempts < _MAX_DELIVERY_ATTEMPTS:
                    after, notice = self._deliver(after, holds[condition], watch["logical"], reading)
                    if notice is not None:
                        delivered.append(notice)
                if (
                    after is not None
                    and after.phase == "resolving"
                    and (not after.todo or after.attempts >= _MAX_DELIVERY_ATTEMPTS)
                ):
                    after = None  # said, or given up: nothing left to remember
                if after != before:
                    self._keep_state(watch["id"], condition, after)
        return delivered

    def _deliver(
        self, state: AlertState, sentence: str | None, session: str, reading: Reading
    ) -> tuple[AlertState, Notice | None]:
        """Send the notice of ``state`` to the channels that still miss it. Returns the state
        after that, and the notice if at least one channel took it."""
        firing = state.phase == "firing"
        if firing and sentence is None:
            return state, None  # on its way out: no longer the moment to announce it
        notice = notice_for(state.condition, firing, sentence, session, reading)
        errors = {name: self._send(name, notice) for name in state.todo}
        got = tuple(name for name, error in errors.items() if error is None)
        state = replace(
            state,
            delivered=state.delivered + got if firing else state.delivered,
            todo=tuple(name for name, error in errors.items() if error is not None),
            attempts=state.attempts + 1,
            error=next((error for error in errors.values() if error is not None), None),
        )
        return state, notice if got else None

    def _keep_state(self, watch_id: int, condition: str, state: AlertState | None) -> None:
        with self.index.write() as db:
            if state is None:
                db.execute(
                    "DELETE FROM alert_states WHERE watch_id = ? AND condition = ?", (watch_id, condition)
                )
                return
            db.execute(
                "INSERT OR REPLACE INTO alert_states (watch_id, condition, since, phase, clear_since, "
                "delivered, todo, attempts, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    watch_id,
                    condition,
                    state.since,
                    state.phase,
                    state.clear_since,
                    json.dumps(state.delivered),
                    json.dumps(state.todo),
                    state.attempts,
                    state.error,
                ),
            )

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
            known = self._thumbs.pop(key, None)  # another request may have made it meanwhile
            if known is not None:
                self._thumb_bytes -= len(known)
            self._thumbs[key] = data
            self._thumb_bytes += len(data)
            while self._thumb_bytes > _THUMB_MEMORY_BYTES and self._thumbs:
                _, dropped = self._thumbs.popitem(last=False)
                self._thumb_bytes -= len(dropped)
        return data, file_id

    def _keep_thumbnail(self, file_id: FileId, px: int, data: bytes) -> None:
        """Keep a thumbnail in the index, which never holds more than ``thumb_cache_bytes``:
        the oldest go first, down to nine tenths of the limit."""
        cap = self._settings.thumb_cache_bytes
        if len(data) > cap:
            return
        try:
            with self.index.write() as db:
                # the running total is only trusted when the transaction that updated it ended well
                total, self._thumb_db_bytes = self._thumb_db_bytes, None
                if total is None:
                    total = db.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM thumbs").fetchone()[0]
                replaced = db.execute(
                    "SELECT LENGTH(data) FROM thumbs WHERE path = ? AND px = ?", (file_id.path, px)
                ).fetchone()
                db.execute(
                    "INSERT OR REPLACE INTO thumbs (path, px, size, mtime_ns, data, used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id.path, px, file_id.size, file_id.mtime_ns, data, self.clock()),
                )
                total += len(data) - (replaced[0] if replaced is not None else 0)
                while total > cap:
                    oldest = db.execute(
                        "SELECT rowid, LENGTH(data) FROM thumbs ORDER BY used_at, rowid LIMIT 256"
                    ).fetchall()
                    doomed: list[tuple[int]] = []
                    for rowid, size in oldest:
                        if total <= cap * 0.9:
                            break
                        doomed.append((rowid,))
                        total -= size
                    if not doomed:
                        break
                    db.executemany("DELETE FROM thumbs WHERE rowid = ?", doomed)
                    if total <= cap * 0.9:
                        break
            self._thumb_db_bytes = total
        except sqlite3.OperationalError:  # busy: not worth failing for
            pass
