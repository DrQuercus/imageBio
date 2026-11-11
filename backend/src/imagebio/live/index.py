"""The persistent index of the live monitor: one SQLite file.

It holds what must survive a restart (ADR 007): the watches of each user, the directories and
files seen in every watched tree, and the *exposures* they form, which is what the interface
lists. Every change to an exposure gets the next number of its root's sequence
(``exposures.seq``): a client that knows the state up to number N asks for "what changed since
N" and receives only that.

One connection per thread; write-ahead logging, so that the API reads while a scanner writes.
Server paths are stored here (the watched directories): this file is server state, never sent
to a client.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Final

__all__ = ["SCHEMA_VERSION", "Index"]

SCHEMA_VERSION: Final = 2

#: Statements that bring an index from version N to N + 1 (tables are created by the schema
#: itself: only what "CREATE TABLE IF NOT EXISTS" cannot do is here).
_MIGRATIONS: Final[dict[int, tuple[str, ...]]] = {
    1: ("ALTER TABLE watches ADD COLUMN alerts TEXT", "ALTER TABLE watches ADD COLUMN alerts_at REAL"),
}

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS roots (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,     -- real path of the watched directory
    created_at REAL NOT NULL,
    cycle_at   REAL,                     -- end of the last scan cycle
    full_at    REAL,                     -- last full rescan
    cycle_ms   REAL,                     -- duration of the last cycle
    error      TEXT,                     -- public message of the last failed cycle
    seq        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watches (
    id         INTEGER PRIMARY KEY,
    user       TEXT NOT NULL,
    logical    TEXT NOT NULL,            -- the path as the user sees it
    root_id    INTEGER NOT NULL REFERENCES roots(id),
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    stopped_at REAL,
    alerts     TEXT,                     -- JSON rules (live/alerts.py), NULL: none
    alerts_at  REAL,                     -- when they were set: silence is counted from then
    UNIQUE (user, logical)
);

CREATE TABLE IF NOT EXISTS alert_states (
    watch_id  INTEGER NOT NULL REFERENCES watches(id),
    condition TEXT NOT NULL,             -- no_image | disk | rate_drop | scanner
    since     REAL NOT NULL,             -- when the condition started to hold
    notified  INTEGER NOT NULL DEFAULT 0,  -- 1: its notice was delivered to every channel
    attempts  INTEGER NOT NULL DEFAULT 0,
    error     TEXT,                      -- public message of the last failed delivery
    PRIMARY KEY (watch_id, condition)
);

CREATE TABLE IF NOT EXISTS dirs (
    id       INTEGER PRIMARY KEY,
    root_id  INTEGER NOT NULL REFERENCES roots(id),
    rel      TEXT NOT NULL,              -- relative to the root, '' for the root itself
    mtime_ns INTEGER NOT NULL,           -- as last scanned; -1: scan again
    UNIQUE (root_id, rel)
);

CREATE TABLE IF NOT EXISTS exposures (
    id           INTEGER PRIMARY KEY,
    root_id      INTEGER NOT NULL REFERENCES roots(id),
    dir_id       INTEGER NOT NULL REFERENCES dirs(id),
    name         TEXT NOT NULL,          -- shared by the files of the exposure
    category     TEXT NOT NULL,          -- data | gridsquare | foilhole | atlas | image
    state        TEXT NOT NULL,          -- incoming | complete | deleted
    primary_name TEXT,                   -- the file a tile is drawn from; NULL: nothing to show yet
    primary_role TEXT,
    size         INTEGER,                -- of the primary file, when it was looked at
    mtime_ns     INTEGER,
    time         REAL,                   -- acquisition time, see time_source
    time_source  TEXT,                   -- xml | mdoc | name | mtime
    first_seen   REAL NOT NULL,
    arrived      INTEGER NOT NULL DEFAULT 0,  -- 1: appeared while a scanner was watching
    completed_at REAL,
    verified     INTEGER NOT NULL DEFAULT 0,  -- 1: passed the completeness test
    note         TEXT,                   -- why it is not complete yet, or a warning
    members      TEXT NOT NULL DEFAULT '{}',  -- JSON {role: state}
    meta         TEXT NOT NULL DEFAULT '{}',  -- JSON from the XML, the mdoc and the reader
    seq          INTEGER NOT NULL,
    UNIQUE (dir_id, name)
);
CREATE INDEX IF NOT EXISTS exposures_seq ON exposures (root_id, seq);
CREATE INDEX IF NOT EXISTS exposures_time ON exposures (root_id, time);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    dir_id       INTEGER NOT NULL REFERENCES dirs(id),
    exposure_id  INTEGER NOT NULL REFERENCES exposures(id),
    name         TEXT NOT NULL,
    role         TEXT NOT NULL,          -- image | movie | preview | xml | mdoc
    state        TEXT NOT NULL,          -- pending | complete | deleted
    size         INTEGER,
    mtime_ns     INTEGER,
    stable_since REAL,
    ready_since  REAL,                   -- stable and intact since; waiting for its metadata file
    completed_at REAL,
    verified     INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    UNIQUE (dir_id, name)
);
CREATE INDEX IF NOT EXISTS files_exposure ON files (exposure_id);
CREATE INDEX IF NOT EXISTS files_pending ON files (state) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS thumbs (
    path     TEXT NOT NULL,              -- real path of the file
    px       INTEGER NOT NULL,           -- requested size
    size     INTEGER NOT NULL,           -- identity of the content the thumbnail was made from
    mtime_ns INTEGER NOT NULL,
    data     BLOB NOT NULL,              -- JPEG
    used_at  REAL NOT NULL,
    PRIMARY KEY (path, px)
);
CREATE INDEX IF NOT EXISTS thumbs_used ON thumbs (used_at);
"""


class Index:
    """The SQLite index. ``path`` may be ``":memory:"`` for a throw-away index in one thread."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._local = threading.local()
        self._memory: sqlite3.Connection | None = None
        self._all: list[sqlite3.Connection] = []
        self._guard = threading.Lock()
        db = self.connection()
        known = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
        row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() if known else None
        version = int(row[0]) if row else None  # None: a new index
        if version is not None and version > SCHEMA_VERSION:
            raise RuntimeError(
                f"the live index has schema version {version}, this program expects {SCHEMA_VERSION}"
            )
        if version is not None:
            for step in range(version, SCHEMA_VERSION):  # an index written by an earlier version
                with self.write() as tx:
                    for statement in _MIGRATIONS[step]:
                        tx.execute(statement)
                    tx.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(step + 1),))
        # executescript manages its own transaction; every statement of the schema is idempotent
        db.executescript(_SCHEMA)
        with self.write() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)  # explicit transactions
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        if self._path != ":memory:":
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")  # durable across a process crash
        with self._guard:
            self._all.append(db)
        return db

    def connection(self) -> sqlite3.Connection:
        """The connection of the calling thread."""
        if self._path == ":memory:":  # one database per connection: share the only one
            if self._memory is None:
                self._memory = self._open()
            return self._memory
        db: sqlite3.Connection | None = getattr(self._local, "db", None)
        if db is None:
            db = self._local.db = self._open()
        return db

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: committed on exit, rolled back if the block raises."""
        db = self.connection()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def close(self) -> None:
        """Close every connection. Only when no thread uses the index any more."""
        with self._guard:
            connections, self._all = self._all, []
        for db in connections:
            with contextlib.suppress(sqlite3.Error):
                db.close()
        self._memory = None
        self._local = threading.local()
