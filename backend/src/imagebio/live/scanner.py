"""The scanner of a watched directory tree.

One :meth:`Scanner.cycle` brings the index up to date with the disk. It is built for network
storage, where every ``stat`` is a round trip and file-change notifications do not exist:

* a directory is listed again only when its modification time changed, or was recent at the
  last look (a file added within the same second would otherwise be missed), or at the
  periodic full rescan;
* only files that are *not yet complete* are looked at one by one, each cycle, until they pass
  the completeness test (:mod:`imagebio.live.completeness`); files completed recently are
  checked a little longer in case they change after all;
* a directory tree met for the first time is indexed from its listings alone: what lies in
  directories that have been quiet for a long time is taken as complete, flagged *unverified*,
  without a single ``stat``. "Quiet" is measured against the most recent directory date of the
  tree itself, not against this server's clock: the two machines need not agree on the time.

A cycle is one database transaction. If it fails, the database is as before the cycle and the
scanner reloads its memory from it; a restarted server does the same. Time is an argument
(``now``), never read from the clock here: tests replay hours of acquisition in seconds.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import stat as stat_module
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from imagebio.core.errors import CorruptFileError, ImageBioError
from imagebio.cryo.mdoc import read_mdoc
from imagebio.io.registry import Registry
from imagebio.live.completeness import Integrity, check_integrity, is_stable
from imagebio.live.index import Index
from imagebio.live.layout import PRIMARY_ROLES, Member, category_of, classify, name_time, primary_rank

__all__ = ["CycleReport", "Entry", "FileVersion", "Fs", "ScanSettings", "Scanner"]

log = logging.getLogger(__name__)

_NS: Final = 1_000_000_000
#: A file as it was at some moment: its name, its size and its date.
FileVersion = tuple[str, int | None, int | None]
_WAITING_FOR_XML: Final = "waiting for the EPU metadata file (.xml)"
_NO_XML: Final = "completed without its EPU metadata file (.xml)"
_ROLE_ORDER: Final = {"xml": 0, "mdoc": 0, "preview": 1}  # metadata first: images wait for it
_RECHECK_S: Final = 60.0  # a file that failed its check and did not change is checked again after this
_STUCK_S: Final = 300.0  # unchanged and failing for this long: looked at every _RECHECK_S only
_DAY_NS: Final = 86_400 * 1_000_000_000
#: A date this far ahead of this server's clock is not taken as "the time on the file server".
_AHEAD_NS: Final = 120 * 1_000_000_000
#: First scan of a large tree: the index is committed every time this many files were added.
_CHECKPOINT_FILES: Final = 5000


@dataclass(frozen=True, slots=True)
class ScanSettings:
    #: A file must keep its size and date for this long, over at least two polls.
    min_stable_s: float = 10.0
    #: How long a complete EPU image waits for its ``.xml`` before being accepted without it.
    partner_timeout_s: float = 120.0
    #: First scan of a tree: directories quiet for longer are indexed without looking at files.
    settle_s: float = 900.0
    full_rescan_s: float = 600.0
    #: Completed files are looked at again for this long, in case they change after all.
    verify_s: float = 300.0
    #: A directory modified less than this before the last look is listed again next cycle.
    hot_s: float = 30.0
    max_depth: int = 12
    max_dirs: int = 50_000
    max_files: int = 2_000_000
    #: Integrity checks per cycle (each opens a file): bounds the duration of a cycle.
    max_checks: int = 200


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    is_dir: bool
    is_file: bool


class Fs:
    """The only filesystem calls the scanner makes. Symbolic links are never followed: a link
    is neither a directory to enter nor a file to index."""

    def stat(self, path: str) -> os.stat_result:
        return os.stat(path, follow_symlinks=False)

    def scandir(self, path: str) -> list[Entry]:
        with os.scandir(path) as it:
            return [
                Entry(e.name, e.is_dir(follow_symlinks=False), e.is_file(follow_symlinks=False)) for e in it
            ]

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)


@dataclass(slots=True)
class _Dir:
    id: int
    rel: str
    mtime_ns: int  # as stored in the index: -1 while the directory is to be listed again
    names: set[str] | None = None  # tracked file names; None: not loaded from the index yet
    seen_mtime_ns: int | None = None  # as last listed by this scanner; None: not listed yet
    hot_until: float = 0.0  # listed again at every cycle until then (this server's clock)


@dataclass(slots=True)
class _Pending:
    id: int
    dir_rel: str
    name: str
    role: str
    exposure_id: int
    category: str
    size: int | None
    mtime_ns: int | None
    stable_since: float | None = None
    polls: int = 0
    integrity: Integrity | None = None  # of the file as it is now; None: not checked yet
    checked_at: float = 0.0
    polled_at: float = 0.0
    ready_since: float | None = None


@dataclass(slots=True)
class _Recent:
    dir_rel: str
    name: str
    role: str
    exposure_id: int
    category: str
    size: int | None
    mtime_ns: int | None
    completed_at: float


@dataclass(slots=True)
class CycleReport:
    now: float
    full: bool = False
    dirs_listed: int = 0
    new_files: int = 0
    completed: int = 0
    removed: int = 0
    pending: int = 0
    checks: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    changed: set[int] = field(default_factory=set)  # exposures touched


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _parent(rel: str) -> str:
    return rel.rpartition("/")[0]


def _storable(name: str) -> bool:
    """Whether a file name can go in the index and in a logical path."""
    if not name or name.startswith(".") or "\\" in name or "/" in name:
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:  # bytes that are not text: no way to name the file to a client
        return False
    return name.isprintable()


def _chunks(items: list[Any], size: int = 500) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Scanner:
    """Keeps the index of one watched directory (``root``, a real path) up to date."""

    def __init__(
        self,
        index: Index,
        root_id: int,
        root: Path,
        registry: Registry,
        settings: ScanSettings | None = None,
        fs: Fs | None = None,
        *,
        remeasure_with_movie: bool = False,
        reprocess_with_movie: bool = False,
    ) -> None:
        #: What was measured on an exposure is forgotten when its movie completes afterwards:
        #: for a service that also measures the movie (its drift).
        self._remeasure_with_movie = remeasure_with_movie
        #: The same for what the external programs gave, when one of them works on the movie.
        self._reprocess_with_movie = reprocess_with_movie
        self._index = index
        self._root_id = root_id
        self._registry = registry
        self.settings = settings or ScanSettings()
        self._fs = fs or Fs()
        # every path below is built from this real path: a path that is not its own real path
        # goes through a symbolic link, and is not followed
        self._root = self._fs.realpath(os.fspath(root))
        #: Measurements waiting for the next cycle: (exposure, primary file, size, date, metrics).
        self._measured: list[
            tuple[int, str, int | None, int | None, dict[str, Any], dict[str, Any] | None]
        ] = []
        #: Results of the external programs waiting for the next cycle, the same way: already
        #: written as JSON, and with the movie they were worked out from.
        self._processed: list[tuple[int, str, int | None, int | None, str, FileVersion | None]] = []
        self._load()

    # ── memory, rebuilt from the index ───────────────────────────────────────
    def _load(self) -> None:
        db = self._index.connection()
        row = db.execute("SELECT seq FROM roots WHERE id = ?", (self._root_id,)).fetchone()
        if row is None:
            raise ValueError(f"no root {self._root_id} in the live index")
        self._seq: int = row["seq"]
        self._cold = True  # the first cycle of this instance looks at everything
        self._full_at: float | None = None
        self._dirs: dict[str, _Dir] = {}
        self._children: dict[str, set[str]] = {}
        for d in db.execute("SELECT id, rel, mtime_ns FROM dirs WHERE root_id = ?", (self._root_id,)):
            self._remember_dir(_Dir(d["id"], d["rel"], d["mtime_ns"]))
        self._server_ns = max((d.mtime_ns for d in self._dirs.values()), default=0)
        self._n_files: int = db.execute(
            "SELECT COUNT(*) FROM files f JOIN dirs d ON d.id = f.dir_id "
            "WHERE d.root_id = ? AND f.state != 'deleted'",
            (self._root_id,),
        ).fetchone()[0]
        self._pending: dict[int, _Pending] = {}
        rows = db.execute(
            "SELECT f.id, d.rel, f.name, f.role, f.exposure_id, e.category, f.size, f.mtime_ns "
            "FROM files f JOIN dirs d ON d.id = f.dir_id JOIN exposures e ON e.id = f.exposure_id "
            "WHERE d.root_id = ? AND f.state = 'pending'",
            (self._root_id,),
        )
        for f in rows:
            # no trust in timers from a previous life: two fresh polls are required again
            self._pending[f["id"]] = _Pending(
                f["id"],
                f["rel"],
                f["name"],
                f["role"],
                f["exposure_id"],
                f["category"],
                f["size"],
                f["mtime_ns"],
            )
        self._recent: dict[int, _Recent] = {}
        self._recent_loaded = False  # filled at the next cycle, which knows what time it is

    def _load_recent(self, db: sqlite3.Connection, now: float) -> None:
        """Files completed a short while ago are looked at again, also after a restart or a
        failed cycle: one that changed since must not keep its old size for good."""
        rows = db.execute(
            "SELECT f.id, d.rel, f.name, f.role, f.exposure_id, e.category, f.size, f.mtime_ns, "
            "f.completed_at "
            "FROM files f JOIN dirs d ON d.id = f.dir_id JOIN exposures e ON e.id = f.exposure_id "
            "WHERE d.root_id = ? AND f.state = 'complete' AND f.verified = 1 "
            "AND f.completed_at > ?",
            (self._root_id, now - self.settings.verify_s),
        )
        for f in rows:
            self._recent[f["id"]] = _Recent(
                f["rel"],
                f["name"],
                f["role"],
                f["exposure_id"],
                f["category"],
                f["size"],
                f["mtime_ns"],
                f["completed_at"],
            )
        self._recent_loaded = True

    def _remember_dir(self, d: _Dir) -> None:
        self._dirs[d.rel] = d
        if d.rel:
            self._children.setdefault(_parent(d.rel), set()).add(d.rel)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _path(self, rel: str, name: str = "") -> str:
        """The path of a directory or of a file in it. No trailing separator: ``stat`` would
        then follow a symbolic link standing where a directory was."""
        parts = [part for part in rel.split("/") if part]
        if name:
            parts.append(name)
        return os.path.join(self._root, *parts) if parts else self._root

    def _is_inside(self, path: str) -> bool:
        """Whether ``path`` reaches what it names without going through a symbolic link."""
        try:
            return self._fs.realpath(path) == path
        except OSError:
            return False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ── measurements made on the images, by somebody else ────────────────────
    def add_metrics(
        self,
        exposure_id: int,
        primary: str,
        size: int | None,
        mtime_ns: int | None,
        metrics: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Hand over what was measured on the file ``primary`` of an exposure, as it was
        (``size``, ``mtime_ns``) when it was read. It enters the index at the next cycle, with a
        new sequence number, unless the file changed meanwhile.

        ``meta``: metadata read on the same occasion for an exposure that had none (an image
        found already written, indexed without its files being opened). What the scanner read
        itself is never replaced by it. Raises :class:`ValueError` for what cannot be written
        (a number that is not finite): better here than in the middle of a cycle."""
        json.dumps((metrics, meta), allow_nan=False)
        self._measured.append((exposure_id, primary, size, mtime_ns, metrics, meta))

    @property
    def measuring(self) -> frozenset[int]:
        """The exposures whose measurements wait for the next cycle."""
        return frozenset(m[0] for m in self._measured)

    def add_processing(
        self,
        exposure_id: int,
        primary: str,
        size: int | None,
        mtime_ns: int | None,
        result: dict[str, Any],
        movie: FileVersion | None = None,
    ) -> None:
        """Hand over what the external programs gave for an exposure whose file ``primary`` was
        as said (``size``, ``mtime_ns``) and whose movie was ``movie`` (name, size, date; None:
        it had no complete one) when they were started. Like a measurement, it enters the index
        at the next cycle, with a new sequence number, unless one of the two changed meanwhile.
        Raises :class:`ValueError` for a result that cannot be written (a number that is not
        finite): better here than in the middle of a cycle."""
        self._processed.append(
            (exposure_id, primary, size, mtime_ns, json.dumps(result, allow_nan=False), movie)
        )

    @staticmethod
    def movie_of(db: sqlite3.Connection, exposure_id: int) -> FileVersion | None:
        """The complete movie of an exposure, as it is now: name, size and date."""
        row = db.execute(
            "SELECT name, size, mtime_ns FROM files WHERE exposure_id = ? AND role = 'movie' "
            "AND state = 'complete' ORDER BY name LIMIT 1",
            (exposure_id,),
        ).fetchone()
        return (row["name"], row["size"], row["mtime_ns"]) if row is not None else None

    def _apply_processing(self, db: sqlite3.Connection) -> None:
        for exposure_id, primary, size, mtime_ns, result, movie in self._processed:
            if self.movie_of(db, exposure_id) != movie:
                continue  # the movie came, went or was written again: to be run again
            done = db.execute(
                "UPDATE exposures SET processing = ?, seq = ? WHERE id = ? AND root_id = ? "
                "AND state = 'complete' AND primary_name IS ? AND size IS ? AND mtime_ns IS ?",
                (result, self._seq + 1, exposure_id, self._root_id, primary, size, mtime_ns),
            )
            if done.rowcount:
                self._seq += 1

    def _apply_metrics(self, db: sqlite3.Connection) -> None:
        for exposure_id, primary, size, mtime_ns, metrics, meta in self._measured:
            mean = metrics.get("mean")
            done = db.execute(
                "UPDATE exposures SET metrics = ?, mean_counts = ?, seq = ? WHERE id = ? AND root_id = ? "
                "AND state = 'complete' AND primary_name IS ? AND size IS ? AND mtime_ns IS ?",
                (
                    json.dumps(metrics, allow_nan=False),
                    mean if isinstance(mean, int | float) else None,
                    self._seq + 1,
                    exposure_id,
                    self._root_id,
                    primary,
                    size,
                    mtime_ns,
                ),
            )
            if done.rowcount:
                self._seq += 1
                if meta:
                    row = db.execute(
                        "SELECT meta, time_source FROM exposures WHERE id = ?", (exposure_id,)
                    ).fetchone()
                    merged = {**meta, **json.loads(row["meta"])}
                    db.execute(
                        "UPDATE exposures SET meta = ? WHERE id = ?",
                        (json.dumps(merged, allow_nan=False), exposure_id),
                    )
                    when = meta.get("epu", {}).get("acquired_at")
                    if isinstance(when, int | float) and row["time_source"] != "xml":
                        db.execute(
                            "UPDATE exposures SET time = ?, time_source = 'xml' WHERE id = ?",
                            (float(when), exposure_id),
                        )

    # ── a cycle ──────────────────────────────────────────────────────────────
    def cycle(self, now: float) -> CycleReport:
        """Bring the index up to date. Never raises for what the disk does; an internal error
        rolls the cycle back and is reported in ``CycleReport.error`` and in the index."""
        started = time.perf_counter()
        report = CycleReport(now)
        report.full = (
            self._cold or self._full_at is None or now - self._full_at >= self.settings.full_rescan_s
        )
        began = False
        try:
            # the whole tree is listed before the index is locked: other writers do not wait for it
            walked = self._walk() if report.full else None
            with self._index.write() as db:
                began = True
                if not self._recent_loaded:
                    self._load_recent(db, now)
                self._apply_metrics(db)
                self._apply_processing(db)
                self._scan(db, now, report, walked)
                self._poll(db, now, report)
                self._verify(db, now, report)
                self._refresh(db, report.changed)
                report.duration_ms = (time.perf_counter() - started) * 1000
                db.execute(
                    "UPDATE roots SET cycle_at = ?, full_at = COALESCE(?, full_at), cycle_ms = ?, "
                    "error = NULL, "
                    "seq = ? WHERE id = ?",
                    (now, now if report.full else None, report.duration_ms, self._seq, self._root_id),
                )
        except sqlite3.OperationalError as e:
            if not began:  # the index is busy with another writer: nothing was done, try next time
                report.error = "the live index is busy"
                return report
            self._recover(now, report, e)
            return report
        except Exception as e:
            self._recover(now, report, e)
            return report
        if report.full:
            self._full_at = now
        self._cold = False
        self._measured.clear()  # they are in the index now (a failed cycle keeps them for the next)
        self._processed.clear()
        report.pending = len(self._pending)
        return report

    def _recover(self, now: float, report: CycleReport, error: Exception) -> None:
        """A cycle failed and its transaction rolled back: memory follows the index again.
        The scanner goes on as it was: one failure does not make what arrives next look like
        something that was there all along."""
        log.error("live scan cycle failed for root %s", self._root_id, exc_info=error)
        report.error = (
            error.public_message
            if isinstance(error, ImageBioError)
            else f"internal error ({type(error).__name__})"
        )
        was_cold = self._cold
        self._load()
        self._cold = was_cold
        with contextlib.suppress(sqlite3.Error), self._index.write() as db:
            db.execute(
                "UPDATE roots SET cycle_at = ?, error = ? WHERE id = ?", (now, report.error, self._root_id)
            )

    # ── directories ──────────────────────────────────────────────────────────
    def _list(self, rel: str) -> tuple[int, list[Entry]] | bool:
        """``(mtime_ns, entries)`` of a directory; False when it is gone (or is no longer a
        directory of this tree: a symbolic link took its place); True when it could not be
        read this time. The date is taken before the listing: what is added meanwhile changes
        it again."""
        path = self._path(rel)
        try:
            st = self._fs.stat(path)
            if not stat_module.S_ISDIR(st.st_mode) or not self._is_inside(path):
                return False
            return st.st_mtime_ns, self._fs.scandir(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True  # a hiccup of the storage: what is known of it stays

    def _walk(self) -> tuple[list[tuple[str, int, list[Entry]]], set[str]]:
        """Every directory of the tree, breadth first, with its date and listing; and those
        that could not be read this time."""
        out: list[tuple[str, int, list[Entry]]] = []
        unreadable: set[str] = set()
        queue: deque[tuple[str, int]] = deque([("", 0)])
        while queue:
            rel, depth = queue.popleft()
            listed = self._list(rel)
            if isinstance(listed, bool):
                if listed:
                    unreadable.add(rel)
                continue
            out.append((rel, *listed))
            if depth >= self.settings.max_depth:
                continue
            for e in listed[1]:
                if e.is_dir and _storable(e.name) and len(out) + len(queue) < self.settings.max_dirs:
                    queue.append((_join(rel, e.name), depth + 1))
        return out, unreadable

    def _checkpoint(self, db: sqlite3.Connection, report: CycleReport) -> None:
        """Commit what was indexed so far and go on in a new transaction: the first scan of a
        very large tree must not keep every other writer waiting until its end."""
        self._refresh(db, report.changed)
        report.changed.clear()
        db.execute("UPDATE roots SET seq = ? WHERE id = ?", (self._seq, self._root_id))
        db.execute("COMMIT")
        db.execute("BEGIN IMMEDIATE")

    def _scan(
        self,
        db: sqlite3.Connection,
        now: float,
        report: CycleReport,
        walked: tuple[list[tuple[str, int, list[Entry]]], set[str]] | None,
    ) -> None:
        if walked is not None:
            listing, unreadable = walked
            self._note_dates((mtime for _, mtime, _ in listing), now)
            seen: set[str] = set()
            committed = 0
            for rel, mtime_ns, entries in listing:
                seen.add(rel)
                settled = self._cold and self._long_at_rest(mtime_ns, now)
                self._apply(db, rel, mtime_ns, entries, now, settled, report)
                if self._cold and report.new_files - committed >= _CHECKPOINT_FILES:
                    committed = report.new_files
                    self._checkpoint(db, report)
            for rel in sorted(set(self._dirs) - seen):
                # what could not be read this time is not gone, and neither is what lies below
                if any(rel == u or rel.startswith(u + "/") or not u for u in unreadable):
                    continue
                self._drop_dir(db, rel, report)
            return
        snapshot = set(self._dirs)
        queue: deque[str] = deque(self._dirs)
        while queue:
            rel = queue.popleft()
            known = self._dirs.get(rel)
            if known is None and rel in snapshot:
                continue  # dropped a moment ago with its parent: not to be looked at through it
            path = self._path(rel)
            try:
                st = self._fs.stat(path)
            except (FileNotFoundError, NotADirectoryError):
                if known is not None:
                    self._drop_dir(db, rel, report)
                continue
            except OSError:
                continue  # a hiccup of the storage: try again next cycle
            if not stat_module.S_ISDIR(st.st_mode):
                if known is not None:
                    self._drop_dir(db, rel, report)
                continue
            self._note_dates((st.st_mtime_ns,), now)
            if known is not None and st.st_mtime_ns == known.mtime_ns:
                continue
            if not self._is_inside(path):  # one of its parents became a symbolic link
                if known is not None:
                    self._drop_dir(db, rel, report)
                continue
            try:
                entries = self._fs.scandir(path)
            except OSError:
                continue
            depth = rel.count("/") + 1 if rel else 0
            for name in self._apply(db, rel, st.st_mtime_ns, entries, now, False, report):
                child = _join(rel, name)
                if (
                    child not in self._dirs
                    and depth < self.settings.max_depth
                    and len(self._dirs) + len(queue) < self.settings.max_dirs
                ):
                    queue.append(child)

    def _note_dates(self, dates: Iterable[int], now: float) -> None:
        """Advance what is known of the file server's clock: the most recent date seen in the
        tree. A date ahead of this server's clock is not taken: were it a mistake of whoever
        wrote it, everything else would look old, a file being written included. If the file
        server's clock really is ahead, ignoring its recent dates only makes more files go
        through the completeness test."""
        limit = int(now * _NS) + _AHEAD_NS
        self._server_ns = max([self._server_ns, *(m for m in dates if m <= limit)])

    def _at_rest(self, mtime_ns: int, now: float) -> bool:
        """Whether something last modified at ``mtime_ns`` has been quiet for ``settle_s``.

        Measured against the most recent directory date of the tree, which is on the same clock
        as ``mtime_ns`` (the file server's): this server's clock may be minutes away from it.
        A whole day by this server's clock is beyond any such difference, and settles the case
        of a tree where nothing is recent.
        """
        settle_ns = int(self.settings.settle_s * _NS)
        return self._server_ns - mtime_ns > settle_ns or int(now * _NS) - mtime_ns > _DAY_NS

    def _long_at_rest(self, mtime_ns: int, now: float) -> bool:
        """Whether a *directory* last changed more than a day ago: its files are then indexed
        without being looked at. A day, not ``settle_s``: the date of a directory only tells
        when an entry was last added or removed, and a file in it may be written for long
        after that (a tilt series appended to for an hour)."""
        return self._server_ns - mtime_ns > _DAY_NS or int(now * _NS) - mtime_ns > _DAY_NS

    def _apply(
        self,
        db: sqlite3.Connection,
        rel: str,
        mtime_ns: int,
        entries: list[Entry],
        now: float,
        settled: bool,
        report: CycleReport,
    ) -> set[str]:
        """Record the listing of directory ``rel``; returns the names of its sub-directories."""
        report.dirs_listed += 1
        d = self._dirs.get(rel)
        if d is None:
            db.execute(
                "INSERT INTO dirs (root_id, rel, mtime_ns) VALUES (?, ?, -1) "
                "ON CONFLICT (root_id, rel) DO NOTHING",
                (self._root_id, rel),
            )
            row = db.execute(
                "SELECT id FROM dirs WHERE root_id = ? AND rel = ?", (self._root_id, rel)
            ).fetchone()
            d = _Dir(row["id"], rel, -1)
            self._remember_dir(d)
        if d.names is None:
            d.names = {
                r["name"]
                for r in db.execute("SELECT name FROM files WHERE dir_id = ? AND state != 'deleted'", (d.id,))
            }
        tracked: dict[str, Member] = {}
        subdirs: set[str] = set()
        for e in entries:
            if not _storable(e.name):
                continue
            if e.is_dir:
                subdirs.add(e.name)
            elif e.is_file:
                member = classify(e.name)
                if member is not None:
                    tracked[e.name] = member
        new = {name: member for name, member in tracked.items() if name not in d.names}
        gone = d.names - tracked.keys()
        added = self._add_files(db, d, new, now, settled, report) if new else set()
        if gone:
            self._remove_files(db, d, gone, report)
        d.names = (d.names | added) - gone
        # A directory that just changed is listed again at the next cycles whatever its date
        # says: a file added within the same second would not change it. "Just" is counted on
        # this server's clock from the moment the change was seen, so that it ends.
        first_look = d.seen_mtime_ns is None  # then only a recent date makes it "just changed"
        fresh = not self._at_rest(mtime_ns, now) if first_look else mtime_ns != d.seen_mtime_ns
        d.seen_mtime_ns = mtime_ns
        if fresh:
            d.hot_until = now + self.settings.hot_s
        stored = -1 if now < d.hot_until else mtime_ns
        if stored != d.mtime_ns:
            d.mtime_ns = stored
            db.execute("UPDATE dirs SET mtime_ns = ? WHERE id = ?", (stored, d.id))
        for child in sorted(self._children.get(rel, ())):
            if child.rpartition("/")[2] not in subdirs:
                self._drop_dir(db, child, report)
        return subdirs

    def _drop_dir(self, db: sqlite3.Connection, rel: str, report: CycleReport) -> None:
        """Directory ``rel`` and everything under it are gone."""
        doomed = [r for r in self._dirs if r == rel or r.startswith(rel + "/")]
        for r in doomed:
            d = self._dirs.pop(r)
            self._children.pop(r, None)
            self._children.get(_parent(r), set()).discard(r)
            rows = db.execute(
                "SELECT DISTINCT exposure_id FROM files WHERE dir_id = ? AND state != 'deleted'", (d.id,)
            ).fetchall()
            report.changed.update(row["exposure_id"] for row in rows)
            cursor = db.execute(
                "UPDATE files SET state = 'deleted' WHERE dir_id = ? AND state != 'deleted'", (d.id,)
            )
            report.removed += cursor.rowcount
            self._n_files -= cursor.rowcount
            db.execute("UPDATE dirs SET mtime_ns = -1 WHERE id = ?", (d.id,))
        gone = set(doomed)
        for file_id in [i for i, p in self._pending.items() if p.dir_rel in gone]:
            del self._pending[file_id]
        for file_id in [i for i, r in self._recent.items() if r.dir_rel in gone]:
            del self._recent[file_id]

    # ── files appearing and disappearing ─────────────────────────────────────
    def _exposure_ids(
        self, db: sqlite3.Connection, dir_id: int, names: list[str]
    ) -> dict[str, tuple[int, str]]:
        """``{exposure name: (id, category)}`` for the given names of a directory."""
        out: dict[str, tuple[int, str]] = {}
        for chunk in _chunks(names):
            marks = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT id, name, category FROM exposures WHERE dir_id = ? AND name IN ({marks})",  # noqa: S608
                (dir_id, *chunk),
            ):
                out[r["name"]] = (r["id"], r["category"])
        return out

    def _add_files(
        self,
        db: sqlite3.Connection,
        d: _Dir,
        new: dict[str, Member],
        now: float,
        settled: bool,
        report: CycleReport,
    ) -> set[str]:
        """Index the new files of a directory; returns the names recorded.

        ``settled``: the directory has been quiet for long, its files are recorded as complete
        without being looked at. Otherwise each new file is looked at once; on the first scan
        of a tree, a file that is itself old is complete too, and only recent ones go through
        the completeness test. Files that appear later always do.
        """
        room = self.settings.max_files - self._n_files
        if room <= 0:
            return set()
        if len(new) > room:
            new = dict(list(new.items())[:room])
        names = sorted({m.exposure for m in new.values()})
        exposures = self._exposure_ids(db, d.id, names)
        missing = [n for n in names if n not in exposures]
        if missing:
            rows = []
            for name in missing:
                when = name_time(name)
                rows.append(
                    (
                        self._root_id,
                        d.id,
                        name,
                        category_of(d.rel, name),
                        now,
                        when,
                        "name" if when else None,
                        0 if self._cold else 1,
                    )
                )
            db.executemany(
                "INSERT INTO exposures "
                "(root_id, dir_id, name, category, state, first_seen, time, time_source, arrived, seq) "
                "VALUES (?, ?, ?, ?, 'incoming', ?, ?, ?, ?, 0)",
                rows,
            )
            exposures.update(self._exposure_ids(db, d.id, missing))

        file_rows: list[tuple[Any, ...]] = []
        pending: list[tuple[str, Member, int | None, int | None]] = []
        looked: dict[str, os.stat_result] = {}
        if not settled:
            for name in new:
                with contextlib.suppress(OSError):  # gone already: the next listing will tell
                    found = self._fs.stat(self._path(d.rel, name))
                    if stat_module.S_ISREG(found.st_mode):
                        looked[name] = found
            if self._cold:  # the files tell the time of their server better than their directory
                self._note_dates((st.st_mtime_ns for st in looked.values()), now)
        for name, member in new.items():
            exposure_id = exposures[member.exposure][0]
            if settled:
                file_rows.append((d.id, exposure_id, name, member.role, "complete", None, None, None, now))
                continue
            st = looked.get(name)
            if st is None:
                continue
            if self._cold and self._at_rest(st.st_mtime_ns, now):
                file_rows.append(
                    (d.id, exposure_id, name, member.role, "complete", st.st_size, st.st_mtime_ns, None, now)
                )
                continue
            file_rows.append(
                (d.id, exposure_id, name, member.role, "pending", st.st_size, st.st_mtime_ns, now, None)
            )
            pending.append((name, member, st.st_size, st.st_mtime_ns))
        db.executemany(
            "INSERT INTO files "
            "(dir_id, exposure_id, name, role, state, size, mtime_ns, stable_since, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (dir_id, name) DO UPDATE SET "
            "exposure_id = excluded.exposure_id, role = excluded.role, "
            "state = excluded.state, size = excluded.size, mtime_ns = excluded.mtime_ns, "
            "stable_since = excluded.stable_since, ready_since = NULL, completed_at = excluded.completed_at, "
            "verified = 0, note = NULL",
            file_rows,
        )
        self._n_files += len(file_rows)
        report.new_files += len(file_rows)
        if pending:
            ids: dict[str, int] = {}
            for chunk in _chunks([name for name, *_ in pending]):
                marks = ",".join("?" * len(chunk))
                for r in db.execute(
                    f"SELECT id, name FROM files WHERE dir_id = ? AND name IN ({marks})",  # noqa: S608
                    (d.id, *chunk),
                ):
                    ids[r["name"]] = r["id"]
            for name, member, size, mtime_ns in pending:
                exposure_id, category = exposures[member.exposure]
                self._pending[ids[name]] = _Pending(
                    ids[name], d.rel, name, member.role, exposure_id, category, size, mtime_ns, now, 1
                )
        report.changed.update(exposures[m.exposure][0] for m in new.values())
        return {row[2] for row in file_rows}

    def _remove_files(self, db: sqlite3.Connection, d: _Dir, gone: set[str], report: CycleReport) -> None:
        for chunk in _chunks(sorted(gone)):
            marks = ",".join("?" * len(chunk))
            rows = db.execute(
                f"SELECT id, exposure_id FROM files WHERE dir_id = ? AND name IN ({marks})",  # noqa: S608
                (d.id, *chunk),
            ).fetchall()
            for r in rows:
                report.changed.add(r["exposure_id"])
                self._pending.pop(r["id"], None)
                self._recent.pop(r["id"], None)
            cursor = db.execute(
                "UPDATE files SET state = 'deleted' "  # noqa: S608
                f"WHERE dir_id = ? AND state != 'deleted' AND name IN ({marks})",
                (d.id, *chunk),
            )
            self._n_files -= cursor.rowcount
            report.removed += cursor.rowcount

    # ── files being written ──────────────────────────────────────────────────
    def _poll(self, db: sqlite3.Connection, now: float, report: CycleReport) -> None:
        order = sorted(self._pending.values(), key=lambda p: (_ROLE_ORDER.get(p.role, 2), p.id))
        for p in order:
            # A file that has failed its check without changing for minutes (a transfer that
            # died) is not worth a round trip to the storage at every cycle.
            stuck = (
                p.integrity is not None
                and not p.integrity.ok
                and p.stable_since is not None
                and now - p.stable_since > _STUCK_S
            )
            if stuck and now - p.polled_at < _RECHECK_S:
                continue
            p.polled_at = now
            try:
                st = self._fs.stat(self._path(p.dir_rel, p.name))
            except (FileNotFoundError, NotADirectoryError):
                continue  # the listing of its directory will record the removal
            except OSError:
                continue
            if not stat_module.S_ISREG(st.st_mode):
                continue  # no longer a file (a link took its place): its listing will tell
            if p.polls == 0 or (st.st_size, st.st_mtime_ns) != (p.size, p.mtime_ns):
                grew = st.st_size != p.size
                p.size, p.mtime_ns = st.st_size, st.st_mtime_ns
                p.stable_since, p.polls, p.integrity, p.ready_since = now, 1, None, None
                db.execute(
                    "UPDATE files SET size = ?, mtime_ns = ?, stable_since = ?, ready_since = NULL, "
                    "note = NULL "
                    "WHERE id = ?",
                    (p.size, p.mtime_ns, now, p.id),
                )
                if grew:
                    report.changed.add(p.exposure_id)
                continue
            p.polls += 1
            assert p.stable_since is not None
            if not is_stable(p.stable_since, p.polls, now, self.settings.min_stable_s):
                continue
            if p.integrity is not None and not p.integrity.ok and now - p.checked_at >= _RECHECK_S:
                p.integrity = None  # the same bytes rarely pass later, but a read error may have been passing
            if p.integrity is None:
                if report.checks >= self.settings.max_checks:
                    continue  # next cycle
                path = self._path(p.dir_rel, p.name)
                if not self._is_inside(path):
                    continue  # a link somewhere on the way: it is not opened
                report.checks += 1
                p.integrity = check_integrity(Path(path), p.role, self._registry)
                p.checked_at = now
                if not p.integrity.ok:
                    cursor = db.execute(
                        "UPDATE files SET note = ? WHERE id = ? AND note IS NOT ?",
                        (p.integrity.reason, p.id, p.integrity.reason),
                    )
                    if cursor.rowcount:
                        report.changed.add(p.exposure_id)
                    continue
                p.ready_since = now
                db.execute("UPDATE files SET ready_since = ? WHERE id = ?", (now, p.id))
            if not p.integrity.ok:
                continue
            warning = None
            if p.role in PRIMARY_ROLES and p.category == "data" and not self._has_xml(db, p.exposure_id):
                assert p.ready_since is not None
                if now - p.ready_since < self.settings.partner_timeout_s:
                    cursor = db.execute(
                        "UPDATE files SET note = ? WHERE id = ? AND note IS NOT ?",
                        (_WAITING_FOR_XML, p.id, _WAITING_FOR_XML),
                    )
                    if cursor.rowcount:
                        report.changed.add(p.exposure_id)
                    continue
                warning = _NO_XML
            self._complete(db, p, now, warning, report)

    def _has_xml(self, db: sqlite3.Connection, exposure_id: int) -> bool:
        row = db.execute(
            "SELECT 1 FROM files WHERE exposure_id = ? AND role = 'xml' AND state = 'complete' LIMIT 1",
            (exposure_id,),
        ).fetchone()
        return row is not None

    def _complete(
        self, db: sqlite3.Connection, p: _Pending, now: float, warning: str | None, report: CycleReport
    ) -> None:
        db.execute(
            "UPDATE files SET state = 'complete', completed_at = ?, verified = 1, note = ? WHERE id = ?",
            (now, warning, p.id),
        )
        del self._pending[p.id]
        self._recent[p.id] = _Recent(
            p.dir_rel, p.name, p.role, p.exposure_id, p.category, p.size, p.mtime_ns, now
        )
        report.completed += 1
        report.changed.add(p.exposure_id)
        self._record_metadata(db, p)

    def _record_metadata(self, db: sqlite3.Connection, p: _Pending) -> None:
        """Keep what the completeness check learnt about the exposure."""
        integrity = p.integrity
        update: dict[str, Any] = {}
        when: tuple[float, str] | None = None
        if p.role == "xml" and integrity is not None and integrity.epu is not None:
            update["epu"] = integrity.epu.to_json()
            if integrity.epu.acquired_at is not None:
                when = (integrity.epu.acquired_at, "xml")
        elif p.role == "mdoc":
            try:
                sections = read_mdoc(Path(self._path(p.dir_rel, p.name))).images()
            except CorruptFileError:
                sections = ()
            if sections:
                summary = sections[0].summary()
                update["mdoc"] = {**summary, "sections": len(sections)}
                if "acquired_at" in summary:
                    when = (summary["acquired_at"], "mdoc")
        elif integrity is not None and integrity.info is not None:
            info = integrity.info
            update[p.role] = {
                "x": info.size_x,
                "y": info.size_y,
                "frames": info.size_t * info.size_z,
                "dtype": info.dtype.name,
                "pixel_size": info.calibration.x.to_angstrom(),
                "pixel_source": info.calibration.x.source.value,
            }
        if not update:
            return
        row = db.execute("SELECT meta, time_source FROM exposures WHERE id = ?", (p.exposure_id,)).fetchone()
        meta = {**json.loads(row["meta"]), **update}
        db.execute(
            "UPDATE exposures SET meta = ? WHERE id = ?", (json.dumps(meta, allow_nan=False), p.exposure_id)
        )
        # the XML knows best, then the mdoc; a time read from the file name only fills a gap
        if when is not None and (when[1] == "xml" or row["time_source"] != "xml"):
            db.execute("UPDATE exposures SET time = ?, time_source = ? WHERE id = ?", (*when, p.exposure_id))

    def _verify(self, db: sqlite3.Connection, now: float, report: CycleReport) -> None:
        """Files completed recently are looked at again: if one changed, it is not complete."""
        for file_id, r in list(self._recent.items()):
            if now - r.completed_at > self.settings.verify_s:
                del self._recent[file_id]
                continue
            try:
                st = self._fs.stat(self._path(r.dir_rel, r.name))
            except OSError:
                continue
            if not stat_module.S_ISREG(st.st_mode) or (st.st_size, st.st_mtime_ns) == (r.size, r.mtime_ns):
                continue
            del self._recent[file_id]
            self._pending[file_id] = _Pending(
                file_id,
                r.dir_rel,
                r.name,
                r.role,
                r.exposure_id,
                r.category,
                st.st_size,
                st.st_mtime_ns,
                now,
                1,
            )
            db.execute(
                "UPDATE files SET state = 'pending', size = ?, mtime_ns = ?, stable_since = ?, "
                "ready_since = NULL, "
                "completed_at = NULL, verified = 0, note = 'changed after it looked complete' WHERE id = ?",
                (st.st_size, st.st_mtime_ns, now, file_id),
            )
            report.changed.add(r.exposure_id)

    # ── exposures ────────────────────────────────────────────────────────────
    def _refresh(self, db: sqlite3.Connection, exposure_ids: set[int]) -> None:
        """Recompute the given exposures from their files; each one that changed, or whose
        metadata changed, gets the next sequence number."""
        for exposure_id in sorted(exposure_ids):
            members = db.execute(
                "SELECT name, role, state, size, mtime_ns, completed_at, verified, note FROM files "
                "WHERE exposure_id = ? AND state != 'deleted'",
                (exposure_id,),
            ).fetchall()
            old = db.execute(
                "SELECT state, primary_name, time, time_source, members FROM exposures WHERE id = ?",
                (exposure_id,),
            ).fetchone()
            shown = [m for m in members if m["role"] in PRIMARY_ROLES]
            states: dict[str, str] = {}
            for m in members:  # of several files in one role, the one still being written counts
                if states.get(m["role"]) != "pending":
                    states[m["role"]] = m["state"]
            if not shown:
                # Nothing to draw. An exposure that was announced is now gone, and clients are
                # told; one that never was (only its metadata arrived so far) stays unlisted.
                if old["primary_name"] is not None and old["state"] != "deleted":
                    db.execute(
                        "UPDATE exposures SET state = 'deleted', members = ?, seq = ? WHERE id = ?",
                        (json.dumps(states), self._next_seq(), exposure_id),
                    )
                else:
                    db.execute(
                        "UPDATE exposures SET members = ? WHERE id = ?", (json.dumps(states), exposure_id)
                    )
                continue
            primary = min(shown, key=lambda m: (primary_rank(m["role"]), m["name"]))
            complete = primary["state"] == "complete"
            movie_completed = (
                states.get("movie") == "complete" and json.loads(old["members"]).get("movie") != "complete"
            )
            movie_arrived = self._remeasure_with_movie and movie_completed
            movie_to_process = self._reprocess_with_movie and movie_completed
            when, source = old["time"], old["time_source"]
            if source in (None, "mtime") and primary["mtime_ns"] is not None:
                when, source = primary["mtime_ns"] / _NS, "mtime"
            db.execute(
                # what was measured on another file, or on this one before it changed, is forgotten
                # (the right-hand sides see the row as it was); so is, when the movie is measured
                # too, what was measured before the movie was complete
                "UPDATE exposures SET metrics = CASE WHEN ? = 0 AND primary_name IS ? AND size IS ? "
                "AND mtime_ns IS ? THEN metrics END, mean_counts = CASE WHEN ? = 0 AND primary_name IS ? "
                "AND size IS ? AND mtime_ns IS ? THEN mean_counts END, "
                "processing = CASE WHEN ? = 0 AND primary_name IS ? AND size IS ? AND mtime_ns IS ? "
                "THEN processing END, "
                "state = ?, primary_name = ?, primary_role = ?, size = ?, mtime_ns = ?, "
                "completed_at = ?, verified = ?, note = ?, members = ?, time = ?, time_source = ?, seq = ? "
                "WHERE id = ?",
                (
                    *(int(movie_arrived), primary["name"], primary["size"], primary["mtime_ns"]) * 2,
                    *(int(movie_to_process), primary["name"], primary["size"], primary["mtime_ns"]),
                    "complete" if complete else "incoming",
                    primary["name"],
                    primary["role"],
                    primary["size"],
                    primary["mtime_ns"],
                    primary["completed_at"] if complete else None,
                    primary["verified"] if complete else 0,
                    primary["note"],
                    json.dumps(states),
                    when,
                    source,
                    self._next_seq(),
                    exposure_id,
                ),
            )
