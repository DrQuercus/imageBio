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
import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Final

from imagebio.core.config import ConfigError, Settings
from imagebio.core.errors import ImageBioError, InvalidRequestError, NotFoundError
from imagebio.core.model import FileId
from imagebio.cryo.epu import read_epu_xml
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
from imagebio.live.index import Index, hold_state_dir
from imagebio.live.metrics import Microscope, measure, measure_drift
from imagebio.live.quality import QualityRules
from imagebio.live.scanner import CycleReport, FileVersion, Scanner, ScanSettings
from imagebio.live.thumbs import MAX_PX, MIN_PX, render_thumbnail
from imagebio.service.planes import PlaneService
from imagebio.tools.pipeline import Job, ProgramUnavailableError, process
from imagebio.tools.runner import clean_environment

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
#: Images of a session that was found already written (they did not arrive while it was
#: watched) that are measured: the most recent ones. Measuring reads each image in full.
MEASURED_WHEN_FOUND: Final = 200
_MEASURE_BATCH: Final = 16  # candidates looked up at a time
#: The brightest images of a session stand for "no ice": this share of them is above the
#: reference the others are compared to, which a single odd image therefore cannot set.
_REFERENCE_TOP: Final = 0.02
_REFERENCE_MIN: Final = 20  # measured images under which a session has no reference yet
#: Seconds an exposure waits for its movie before the external programs run without it.
MOVIE_WAIT_S: Final = 120.0
_PROCESS_IDLE_S: Final = 2.0  # a worker with nothing to run looks again after this
_RUN_PREFIX: Final = "run-"  # directories of the runs of the external programs (tools/pipeline.py)
_UNAVAILABLE_PAUSE_S: Final = 60.0  # after a program could not be started at all


class _NotAsIndexedError(Exception):
    """A file of an exposure is no longer the regular file the scanner indexed."""


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
        self._held: IO[str] | None = None  # the lock of the state directory, while the index is open
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
        #: Seconds per cycle spent measuring images (one image at least); tests replace it.
        self.measure_budget_s = 0.4 * settings.live_interval_s
        #: Delivers alert notices, and tells how full a volume is; tests replace both.
        self.notifier = notifier or Notifier(settings.smtp)
        self.disk_usage: Callable[[Path], Any] = shutil.disk_usage
        self._channels = {c.name: c for c in settings.alert_channels}
        self._tested: dict[str, float] = {}  # user -> their last test notice
        self._seen: dict[int, float] = {}  # watch -> last time its user was allowed to look at it
        #: The external programs the administrator declared (tools/), None: none.
        self._processing = settings.processing
        self._aligns_movies = settings.processing is not None and settings.processing.motion is not None
        # Runs get their directories in a place that is this server's alone, whatever else the
        # administrator keeps next to it: at start, what is left there is removed.
        owner = hashlib.sha256(os.path.realpath(settings.live_state_dir).encode()).hexdigest()[:12]
        self._work = (
            (settings.processing.work_dir if settings.processing is not None else None)
            or settings.live_state_dir / "work"
        ) / f"imagebio-runs-{owner}"
        secret = settings.smtp.password_env if settings.smtp is not None else None
        self._program_env = clean_environment({secret} if secret else ())
        #: Results waiting for the scanner of their directory, which alone numbers changes.
        self._results: dict[
            int, list[tuple[int, str, int | None, int | None, dict[str, Any], FileVersion | None]]
        ] = {}
        #: (directory, exposure) being run, or run and not yet recorded: not to be picked again.
        self._running: set[tuple[int, int]] = set()
        self._handed: dict[int, set[int]] = {}  # given to a scanner whose cycle has not succeeded yet
        self._active: set[tuple[int, int]] = set()  # those of _running a worker is busy with now
        self._paused_until = 0.0  # time.monotonic() before which no program is started
        self._turn_user = ""  # whose exposure ran last: the next turn is somebody else's
        self._turn_root: dict[str, int] = {}  # and, for each user, in which of their directories
        self._ran = {"ok": 0, "failed": 0}
        self._workers: list[threading.Thread] = []
        self._stop_processing = threading.Event()

    # ── index and scanners ───────────────────────────────────────────────────
    @property
    def index(self) -> Index:
        """The index, opened at first use: a server whose users never open the Live mode
        writes nothing to disk."""
        with self._lock:
            if self._index_value is None:
                directory = self._settings.live_state_dir
                directory.mkdir(parents=True, exist_ok=True)
                self._held = hold_state_dir(directory)  # tells a restore that the index is in use
                self._index_value = Index(directory / "live.sqlite")
            return self._index_value

    def check(self) -> None:
        """Open the index, if there is one, so that one this program cannot use (written by a
        newer version, damaged) is found when the server starts, not at the first request."""
        if (self._settings.live_state_dir / "live.sqlite").is_file():
            _ = self.index
        if self._processing is not None:
            for step in (self._processing.motion, self._processing.ctf):
                for argv in (step.argv, *step.formats.values()) if step is not None else ():
                    if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
                        raise ConfigError(f"processing: {argv[0]} is not there, or cannot be run")

    def start(self) -> None:
        """Resume the watches that were active when the server stopped, if any were ever made."""
        if (self._settings.live_state_dir / "live.sqlite").is_file():
            self._sync_scanners()
            self._purge_orphans()
        if self._processing is not None and self._settings.live_scan:
            self._clear_work()  # what a server that was killed left behind
            for n in range(self._processing.slots):
                worker = threading.Thread(
                    target=self._process_until,
                    args=(self._stop_processing,),
                    name=f"live-programs-{n}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            threads = list(self._threads.values())
            self._threads.clear()
        self._stop_processing.set()  # a program that runs is killed, with what it started
        for _, stop in threads:
            stop.set()
        for thread, _ in threads:
            thread.join(timeout=30)
        for worker in self._workers:
            worker.join(timeout=30)
        self._workers.clear()
        with self._lock:
            if self._index_value is not None:
                self._index_value.close()
                self._index_value = None
            if self._held is not None:
                self._held.close()
                self._held = None

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
            scanner = Scanner(
                self.index,
                root_id,
                path,
                self._registry,
                self._scan_settings,
                remeasure_with_movie=self._settings.live_drift,
                reprocess_with_movie=self._aligns_movies,
            )
            self._new_scanner(root_id)
        except Exception:
            log.exception("cannot start the live scanner of root %s", root_id)
            return
        wait = 0.0
        while not stop.wait(wait):
            try:
                started = time.perf_counter()
                now = self.clock()
                handed = self._hand_results(scanner, root_id)
                report = scanner.cycle(now)
                self._recorded(root_id, handed, report)
                self._pending[root_id] = report.pending
                self.check_alerts(root_id, now)
                if report.error is None:
                    self._measure(scanner, root_id, path, stop)
                took = time.perf_counter() - started
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
                    self.index,
                    root_id,
                    path,
                    self._registry,
                    self._scan_settings,
                    remeasure_with_movie=self._settings.live_drift,
                    reprocess_with_movie=self._aligns_movies,
                )
            when = self.clock() if now is None else now
            handed = self._hand_results(scanner, root_id)
            reports[root_id] = scanner.cycle(when)
            self._recorded(root_id, handed, reports[root_id])
            self._pending[root_id] = reports[root_id].pending
            self.check_alerts(root_id, when)
            if reports[root_id].error is None:
                self._measure(scanner, root_id, path)
        return reports

    def _late_metadata(self, root: Path, row: sqlite3.Row) -> dict[str, Any] | None:
        """The EPU metadata of an exposure that was indexed without its files being opened (a
        session found already written): read now that the image is, for the same images."""
        xml = (
            self.index.connection()
            .execute(
                "SELECT name FROM files WHERE exposure_id = ? AND role = 'xml' AND state = 'complete' "
                "ORDER BY name LIMIT 1",
                (row["id"],),
            )
            .fetchone()
        )
        path = self._plain_file(root, row["rel"], xml["name"]) if xml is not None else None
        if path is None:
            return None
        try:
            found = read_epu_xml(path)
        except (ImageBioError, OSError, ValueError):
            return None
        return {"epu": found.to_json()}

    def _drift(self, root: Path, row: sqlite3.Row) -> dict[str, Any]:
        """The drift of the movie of an exposure that arrived (``live/metrics.py``); nothing
        when it has no complete movie, or when the movie cannot be read."""
        name = row["primary_name"]
        if row["primary_role"] != "movie":
            movie = (
                self.index.connection()
                .execute(
                    "SELECT name FROM files WHERE exposure_id = ? AND role = 'movie' AND state = 'complete' "
                    "ORDER BY name LIMIT 1",
                    (row["id"],),
                )
                .fetchone()
            )
            if movie is None:
                return {}
            name = movie["name"]
        path = self._plain_file(root, row["rel"], name)
        if path is None:
            return {}
        try:
            return measure_drift(self._planes, path)
        except (ImageBioError, OSError, ValueError):
            return {}

    # ── measurements on the images ───────────────────────────────────────────
    def _unmeasured(self, root_id: int, skip: frozenset[int]) -> list[sqlite3.Row]:
        """Exposures to measure, newest first: those that arrived while the directory was
        watched, then the most recent of those that were found in it."""
        db = self.index.connection()
        marks = ",".join("?" * len(_ACQUIRED))
        # with the drift, an exposure waits for its movie to be complete: it is measured once
        waiting = (
            "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.exposure_id = e.id AND f.role = 'movie' "
            "AND f.state = 'pending') "
            if self._settings.live_drift
            else ""
        )
        select = (
            "SELECT e.id, e.primary_name, e.primary_role, e.size, e.mtime_ns, e.meta, e.arrived, d.rel "  # noqa: S608
            "FROM exposures e JOIN dirs d ON d.id = e.dir_id "
            f"WHERE e.root_id = ? AND e.metrics IS NULL AND e.state = 'complete' AND e.category IN ({marks}) "
            f"AND e.primary_role IN ('image', 'movie') AND e.time IS NOT NULL {waiting}AND "
        )
        order = " ORDER BY e.time DESC LIMIT ?"
        limit = _MEASURE_BATCH + len(skip)
        rows = db.execute(select + "e.arrived = 1" + order, (root_id, *_ACQUIRED, limit)).fetchall()
        if len(rows) < limit:
            oldest = db.execute(
                f"SELECT time FROM exposures WHERE root_id = ? AND category IN ({marks}) "  # noqa: S608
                "AND time IS NOT NULL ORDER BY time DESC LIMIT 1 OFFSET ?",
                (root_id, *_ACQUIRED, MEASURED_WHEN_FOUND - 1),
            ).fetchone()
            rows += db.execute(
                select + "e.arrived = 0 AND e.time >= ?" + order,
                (root_id, *_ACQUIRED, oldest["time"] if oldest is not None else -1e300, limit),
            ).fetchall()
        return [row for row in rows if row["id"] not in skip][:_MEASURE_BATCH]

    def _measure(
        self, scanner: Scanner, root_id: int, root: Path, stop: threading.Event | None = None
    ) -> int:
        """Measure images that were not yet (``live/metrics.py``), for ``measure_budget_s``
        seconds at most, one image at least; the scanner writes the results at its next cycle.
        An image that cannot be read is given empty measurements: it is not tried again."""
        if not self._settings.live_metrics:
            return 0
        started = time.perf_counter()
        done = 0
        while not (stop is not None and stop.is_set()):
            rows = self._unmeasured(root_id, scanner.measuring)
            for row in rows:
                epu = json.loads(row["meta"]).get("epu")
                late = None
                if epu is None:  # found already written: its metadata file was never opened
                    late = self._late_metadata(root, row)
                    epu = late["epu"] if late else {}
                apix = epu.get("pixel_size")
                microscope = None
                if self._settings.live_ctf:  # the voltage the acquisition itself gives comes first
                    kv = epu.get("voltage")
                    microscope = Microscope(
                        kv if isinstance(kv, int | float) and 20 <= kv <= 1500 else self._settings.kv,
                        self._settings.cs_mm,
                        self._settings.amplitude_contrast,
                    )
                path = self._plain_file(root, row["rel"], row["primary_name"])
                try:
                    if path is None:  # no longer the regular file that was indexed
                        raise OSError("not a regular file")
                    metrics = measure(
                        self._planes,
                        path,
                        row["primary_role"],
                        apix if row["primary_role"] == "image" and isinstance(apix, int | float) else None,
                        microscope,
                    )
                except (ImageBioError, OSError, ValueError):
                    metrics = {}
                if metrics and self._settings.live_drift and row["arrived"]:
                    metrics.update(self._drift(root, row))
                identity = (row["id"], row["primary_name"], row["size"], row["mtime_ns"])
                try:
                    scanner.add_metrics(*identity, metrics, late)
                except ValueError:  # a number that cannot be written: as an image that cannot be read
                    scanner.add_metrics(*identity, {}, None)
                done += 1
                if time.perf_counter() - started >= self.measure_budget_s or (
                    stop is not None and stop.is_set()
                ):
                    return done
            if len(rows) < _MEASURE_BATCH:
                break
        return done

    # ── external programs on the exposures ───────────────────────────────────
    def _clear_work(self) -> None:
        """Remove the directories of runs that never ended (the server was killed)."""
        if self._work.is_dir():
            for entry in self._work.iterdir():
                if entry.name.startswith(_RUN_PREFIX) and entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)

    def _hand_results(self, scanner: Scanner, root_id: int) -> list[int]:
        """Give the scanner of a directory what the programs gave for its exposures."""
        with self._lock:
            results = self._results.pop(root_id, [])
        for result in results:
            scanner.add_processing(*result)
        return [result[0] for result in results]

    def _recorded(self, root_id: int, handed: list[int], report: CycleReport) -> None:
        """After a cycle: what was handed to the scanner is in the index, unless the cycle
        failed (the scanner then keeps it for its next one)."""
        with self._lock:
            waiting = self._handed.setdefault(root_id, set())
            waiting.update(handed)
            if report.error is None:
                self._running.difference_update((root_id, exposure) for exposure in waiting)
                waiting.clear()

    def _new_scanner(self, root_id: int) -> None:
        """A scanner starts for a directory: what the one before it held and never recorded is
        lost with it, and those exposures may be run again."""
        with self._lock:
            for exposure in self._handed.pop(root_id, ()):
                self._running.discard((root_id, exposure))

    def _unprocessed(self, root_id: int, now: float, skip: set[int]) -> sqlite3.Row | None:
        """The newest exposure of a directory the programs have not run on: among those that
        arrived while it was watched (a session found already written is not gone through).
        With a motion step, an exposure waits for its movie to be complete, ``MOVIE_WAIT_S``
        at most: a movie that never completes does not keep its image from the CTF step.
        Without one, a movie is not waited for at all."""
        marks = ",".join("?" * len(_ACQUIRED))
        movie = (
            "AND (e.completed_at <= ? OR (EXISTS (SELECT 1 FROM files f WHERE f.exposure_id = e.id "
            "AND f.role = 'movie' AND f.state = 'complete') AND NOT EXISTS (SELECT 1 FROM files f "
            "WHERE f.exposure_id = e.id AND f.role = 'movie' AND f.state = 'pending'))) "
            if self._aligns_movies
            else "AND ? IS NOT NULL "
        )
        rows = (
            self.index.connection()
            .execute(
                "SELECT e.id, e.primary_name, e.primary_role, e.size, e.mtime_ns, e.meta, d.rel "  # noqa: S608
                "FROM exposures e JOIN dirs d ON d.id = e.dir_id "
                "WHERE e.root_id = ? AND e.processing IS NULL AND e.arrived = 1 AND e.state = 'complete' "
                f"AND e.category IN ({marks}) AND e.primary_role IN ('image', 'movie') "
                f"AND e.time IS NOT NULL {movie}ORDER BY e.time DESC LIMIT ?",
                (root_id, *_ACQUIRED, now - MOVIE_WAIT_S, len(skip) + 1),
            )
            .fetchall()
        )
        return next((row for row in rows if row["id"] not in skip), None)

    def _next_job(self, now: float) -> tuple[int, Path, sqlite3.Row] | None:
        """The exposure to run the programs on next. Users take turns, and each user's
        directories take turns within theirs: somebody who follows eight sessions, or a session
        with thousands of movies waiting, does not keep the programs from anybody else."""
        if self._closed or not (self._settings.live_state_dir / "live.sqlite").is_file():
            return None
        if time.monotonic() < self._paused_until:  # a program cannot be started: see process_once
            return None
        with self._lock:
            rows = (
                self.index.connection()
                .execute(
                    "SELECT DISTINCT w.user, w.root_id, r.path FROM watches w "
                    "JOIN roots r ON r.id = w.root_id "
                    "WHERE w.active = 1 AND w.processing = 1 ORDER BY w.user, w.root_id"
                )
                .fetchall()
            )
            wanted = {row["root_id"] for row in rows}
            for root_id in [r for r in self._results if r not in wanted]:  # nobody asks any more
                del self._results[root_id]
            # what a worker is running stays known whatever its watch becomes: switched off and
            # on again, the same exposure would otherwise be started a second time next to it
            self._running = {key for key in self._running if key[0] in wanted or key in self._active}
            by_user: dict[str, list[tuple[int, Path]]] = {}
            for row in rows:
                by_user.setdefault(row["user"], []).append((row["root_id"], Path(row["path"])))
            users = sorted(by_user)
            for user in [u for u in users if u > self._turn_user] + [
                u for u in users if u <= self._turn_user
            ]:
                last = self._turn_root.get(user, 0)
                roots = by_user[user]
                for root_id, path in [r for r in roots if r[0] > last] + [r for r in roots if r[0] <= last]:
                    skip = {exposure for root, exposure in self._running if root == root_id}
                    row = self._unprocessed(root_id, now, skip)
                    if row is not None:
                        self._turn_user, self._turn_root[user] = user, root_id
                        self._running.add((root_id, row["id"]))
                        self._active.add((root_id, row["id"]))
                        return root_id, path, row
        return None

    @staticmethod
    def _plain_file(root: Path, rel: str, name: str) -> Path | None:
        """The path of a file of a watched directory, if it still is what the scanner indexed:
        a regular file, reached through no link. Between two cycles, whoever writes to the
        directory can put a link to somebody else's file, or a pipe, in its place."""
        parts = [part for part in rel.split("/") if part]
        path = root.joinpath(*parts, name)
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            if os.path.realpath(path) != os.path.join(os.path.realpath(root), *parts, name):
                return None
        except OSError:
            return None
        return path

    def _job_of(self, root: Path, row: sqlite3.Row) -> tuple[Job, FileVersion | None]:
        """What the programs need to know of an exposure, from what its acquisition says, and
        the movie it has at this moment. Raises :class:`_NotAsIndexedError` when a file is no
        longer the regular file that was indexed."""
        epu = json.loads(row["meta"]).get("epu") or {}
        primary = self._plain_file(root, row["rel"], row["primary_name"])
        version = Scanner.movie_of(self.index.connection(), row["id"])
        movie = self._plain_file(root, row["rel"], version[0]) if version is not None else None
        if primary is None or (version is not None and movie is None):
            raise _NotAsIndexedError

        def positive(value: Any, low: float = 0.0, high: float = math.inf) -> float | None:
            ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
            return float(value) if ok and low < value <= high else None

        # a pixel size is a fraction of an ångström to a few hundred: anything else is a mistake
        # in a metadata file, or somebody trying their luck
        apix = positive(epu.get("pixel_size"), 0.01, 1000.0)
        if apix is None:  # no metadata file: what the image itself says
            try:
                x = self._planes.info(primary)[0].calibration.x
                apix = positive(x.value, 0.01, 1000.0) if x.known and x.unit == "Å" else None
            except (ImageBioError, OSError, ValueError):
                apix = None
        kv = positive(epu.get("voltage"), 20.0, 1500.0)
        frames = positive(epu.get("fractions"), 0.0, 1e6)
        dose = positive(epu.get("dose"), 0.0, 1e6)
        job = Job(
            movie=movie,
            image=primary if row["primary_role"] == "image" else None,
            apix=apix,
            kv=kv if kv is not None else self._settings.kv,
            cs_mm=self._settings.cs_mm,
            amplitude=self._settings.amplitude_contrast,
            dose=dose / frames if dose is not None and frames is not None else None,
            frames=int(frames) if frames is not None else None,
        )
        return job, version

    def process_once(self, now: float | None = None, stop: threading.Event | None = None) -> bool:
        """Run the declared programs on one exposure, in the calling thread; False when none
        waits. The result enters the index at the next cycle of the scanner of its directory."""
        if self._processing is None:
            return False
        picked = self._next_job(self.clock() if now is None else now)
        if picked is None:
            return False
        root_id, root, row = picked
        key = (root_id, row["id"])
        movie: FileVersion | None = None
        try:
            job, movie = self._job_of(root, row)
            result = process(job, self._processing, self._work, stop=stop, env=self._program_env)
            if result is not None:
                json.dumps(result, allow_nan=False)  # what could not be written is not a result
        except _NotAsIndexedError:
            # Left as it is until the scanner has looked at the directory again: it will see
            # what the file has become. Meanwhile it is not picked again.
            with self._lock:
                self._active.discard(key)
                self._handed.setdefault(root_id, set()).add(row["id"])
            return True
        except ProgramUnavailableError as e:
            # A matter of installation, not of this exposure: nothing is recorded against it,
            # and nothing is tried for a while rather than marking a whole session as failed.
            log.error(
                "the %s program could not be started: no program is run for %g s", e, _UNAVAILABLE_PAUSE_S
            )
            with self._lock:
                self._active.discard(key)
                self._running.discard(key)
                self._paused_until = time.monotonic() + _UNAVAILABLE_PAUSE_S
            return False
        except Exception:  # recorded, so that the exposure is not tried again for ever
            log.exception("the external programs could not be run on an exposure of root %s", root_id)
            result = {"error": "the programs could not be run"}
            with contextlib.suppress(Exception):
                movie = Scanner.movie_of(self.index.connection(), row["id"])
        with self._lock:
            self._active.discard(key)
            if result is None:  # the server is stopping: the run does not count
                self._running.discard(key)
                return False
            failed = "error" in result or any(isinstance(v, dict) and "error" in v for v in result.values())
            self._ran["failed" if failed else "ok"] += 1
            self._results.setdefault(root_id, []).append(
                (row["id"], row["primary_name"], row["size"], row["mtime_ns"], result, movie)
            )
        return True

    def _process_until(self, stop: threading.Event) -> None:
        wait = 0.0
        while not stop.wait(wait):
            try:
                wait = 0.0 if self.process_once(stop=stop) else _PROCESS_IDLE_S
            except Exception:
                log.exception("the worker of the external programs failed")
                wait = _PROCESS_IDLE_S

    def _processing_of(self, db: sqlite3.Connection, watch: WatchInfo) -> dict[str, Any]:
        """Whether the programs are declared, asked for by this watch, and how far they are."""
        declared = self._processing
        steps = [name for name in ("motion", "ctf") if declared is not None and getattr(declared, name)]
        on = bool(db.execute("SELECT processing FROM watches WHERE id = ?", (watch.id,)).fetchone()[0])
        done = db.execute(
            "SELECT COUNT(*) FROM exposures WHERE root_id = ? AND processing IS NOT NULL", (watch.root_id,)
        ).fetchone()[0]
        waiting = 0
        if on and declared is not None:
            marks = ",".join("?" * len(_ACQUIRED))
            waiting = db.execute(
                "SELECT COUNT(*) FROM exposures WHERE root_id = ? AND processing IS NULL AND arrived = 1 "  # noqa: S608
                f"AND state = 'complete' AND category IN ({marks})",
                (watch.root_id, *_ACQUIRED),
            ).fetchone()[0]
        return {"available": declared is not None, "steps": steps, "on": on, "done": done, "waiting": waiting}

    def set_processing(self, watch: WatchInfo, on: bool) -> dict[str, Any]:
        """Ask for, or no longer ask for, the external programs on the exposures of a watch."""
        if on and self._processing is None:
            raise InvalidRequestError("no processing program is declared on this server")
        with self.index.write() as db:
            db.execute("UPDATE watches SET processing = ? WHERE id = ?", (int(on), watch.id))
        return self._processing_of(self.index.connection(), watch)

    def metrics(self) -> dict[str, Any]:
        """Figures for a monitoring system: no path, no user. Empty when the Live mode was
        never used (the index is not created for the occasion)."""
        index_file = self._settings.live_state_dir / "live.sqlite"
        if not index_file.is_file():
            return {}
        db = self.index.connection()
        now = self.clock()
        states = dict.fromkeys(("active", "stopped"), 0)
        for row in db.execute("SELECT active, COUNT(*) AS n FROM watches GROUP BY active"):
            states["active" if row["active"] else "stopped"] = row["n"]
        counts = {
            row["root_id"]: row["n"]
            for row in db.execute(
                "SELECT root_id, COUNT(*) AS n FROM exposures WHERE state != 'deleted' GROUP BY root_id"
            )
        }
        roots = [
            {
                "id": row["id"],
                "age": now - row["cycle_at"] if row["cycle_at"] is not None else None,
                "duration": row["cycle_ms"] / 1000 if row["cycle_ms"] is not None else None,
                "failing": 1 if row["error"] else 0,
                "pending": self._pending.get(row["id"], 0),
                "exposures": counts.get(row["id"], 0),
            }
            for row in db.execute("SELECT id, cycle_at, cycle_ms, error FROM roots ORDER BY id")
        ]
        thumbs = db.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM thumbs").fetchone()[0]
        with self._lock:
            programs = {**self._ran, "running": len(self._running)} if self._processing is not None else None
        return {
            "watches": states,
            "roots": roots,
            "index_bytes": index_file.stat().st_size,
            "thumb_bytes": thumbs,
            "programs": programs,
        }

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
            "ice": self._ice(db, watch),
            "quality": self.quality(watch),
            "processing": self._processing_of(db, watch),
            "last": dict(last) if last is not None else None,
            "disk": disk,
        }

    @staticmethod
    def _ice_reference(db: sqlite3.Connection, root_id: int) -> dict[str, Any]:
        """What the mean counts of an image are compared to: the counts of the brightest
        images of the session (the thinnest ice, or none). None until enough were measured."""
        measured = db.execute(
            "SELECT COUNT(*) FROM exposures WHERE root_id = ? AND mean_counts IS NOT NULL "
            "AND state = 'complete'",
            (root_id,),
        ).fetchone()[0]
        reference = None
        if measured >= _REFERENCE_MIN:
            reference = db.execute(
                "SELECT mean_counts FROM exposures WHERE root_id = ? AND mean_counts IS NOT NULL "
                "AND state = 'complete' ORDER BY mean_counts DESC LIMIT 1 OFFSET ?",
                (root_id, int(measured * _REFERENCE_TOP)),
            ).fetchone()[0]
        return {"measured": measured, "reference": reference}

    def _ice(self, db: sqlite3.Connection, watch: WatchInfo) -> dict[str, Any]:
        """What ice thickness is computed from for a watch: the counts of an empty hole when
        its user gave them (``calibrated``), else the brightest images of the session; and the
        mean free path, the user's or the server's, when one is known."""
        found = self._ice_reference(db, watch.root_id)
        own = db.execute("SELECT ice_i0, ice_mfp_nm FROM watches WHERE id = ?", (watch.id,)).fetchone()
        i0, mfp = own["ice_i0"], own["ice_mfp_nm"]
        return {
            "measured": found["measured"],
            "reference": i0 if i0 is not None else found["reference"],
            "calibrated": i0 is not None,
            "session": found["reference"],
            "i0": i0,
            "mfp_nm": mfp if mfp is not None else self._settings.live_mfp_nm,
            "mfp_own": mfp,
        }

    def quality(self, watch: WatchInfo) -> dict[str, Any]:
        """The acceptance thresholds of a watch; a threshold that is not set is None."""
        row = self.index.connection().execute("SELECT quality FROM watches WHERE id = ?", (watch.id,))
        return QualityRules.from_json(row.fetchone()[0]).to_json()

    def set_quality(self, watch: WatchInfo, rules: QualityRules) -> dict[str, Any]:
        with self.index.write() as db:
            db.execute(
                "UPDATE watches SET quality = ? WHERE id = ?",
                (json.dumps(rules.to_json()) if rules != QualityRules() else None, watch.id),
            )
        return rules.to_json()

    def set_ice(self, watch: WatchInfo, i0: float | None, mfp_nm: float | None) -> dict[str, Any]:
        """Set, or forget (None), the counts of an empty hole and the mean free path of a watch."""
        if i0 is not None and not (math.isfinite(i0) and 0 < i0 <= 1e12):
            raise InvalidRequestError("i0 (mean counts of an empty hole) must be a positive number")
        if mfp_nm is not None and not (math.isfinite(mfp_nm) and 50 <= mfp_nm <= 5000):
            raise InvalidRequestError("mfp (mean free path) must be between 50 and 5000 nanometres")
        with self.index.write() as db:
            db.execute("UPDATE watches SET ice_i0 = ?, ice_mfp_nm = ? WHERE id = ?", (i0, mfp_nm, watch.id))
        return self._ice(self.index.connection(), watch)

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
            "e.members, e.meta, e.metrics, e.processing, d.rel "
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
                "metrics": json.loads(row["metrics"]) if row["metrics"] is not None else None,
                "processing": json.loads(row["processing"]) if row["processing"] is not None else None,
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
