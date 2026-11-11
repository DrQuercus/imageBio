"""Backup and restore of what the server keeps: the index of the Live mode (watches, alert
rules, thresholds, what was indexed and measured) and the configuration file.

The data themselves are read-only for imageBio and are not its to back up; caches are thrown
away, the thumbnails kept in the index among them (they are made again when asked for). A
backup can be taken while the server runs (SQLite's online backup gives a consistent copy); a
restore needs the server stopped.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Final

from imagebio import __version__
from imagebio.core.config import ConfigError, Settings
from imagebio.live.index import SCHEMA_VERSION, hold_state_dir

__all__ = ["INDEX_NAME", "MANIFEST_NAME", "backup", "prune", "restore"]

INDEX_NAME: Final = "live.sqlite"
MANIFEST_NAME: Final = "manifest.json"
_CONFIG_NAME: Final = "imagebio.toml"
_STAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z")


def _now() -> str:
    """The name of a backup made now: its date (UTC), so that names sort by age."""
    return time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())


def _schema_version(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0])


def _checked(path: Path) -> tuple[int, int]:
    """Schema version and number of watches of an index file that is sound; raises
    :class:`ConfigError` otherwise."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ConfigError("the index of the backup is damaged")
            return _schema_version(db), int(db.execute("SELECT COUNT(*) FROM watches").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError):
        raise ConfigError("the backup does not hold a Live index that can be read") from None


def backup(settings: Settings, destination: Path, config: Path | None = None) -> Path:
    """Write a backup into a new, dated directory under ``destination`` and return it. Holds a
    consistent copy of the Live index (if the Live mode was ever used), the configuration file
    ``config`` (if given) and a manifest."""
    stamp = _now()
    target = destination / stamp
    try:
        destination.mkdir(parents=True, exist_ok=True)
        target.mkdir(mode=0o700)  # the configuration may hold the addresses of alert channels
    except FileExistsError:
        raise ConfigError(f"a backup named {stamp} already exists there: try again in a second") from None
    manifest: dict[str, Any] = {"imagebio": __version__, "created": stamp, "index": None, "config": False}
    source = settings.live_state_dir / INDEX_NAME
    if source.is_file():
        # opened as the server opens it: a write-ahead log left by a server that was killed
        # could not be read through a read-only connection
        with (
            contextlib.closing(sqlite3.connect(source, timeout=30)) as live,
            contextlib.closing(sqlite3.connect(target / INDEX_NAME, isolation_level=None)) as copy,
        ):
            live.backup(copy)
            # thumbnails are a cache, and by far the largest part of the index
            copy.execute("DELETE FROM thumbs")
            # one plain file, readable from a medium that cannot be written to
            copy.execute("PRAGMA journal_mode = DELETE")
            copy.execute("VACUUM")
        version, watches = _checked(target / INDEX_NAME)
        manifest["index"] = {
            "schema_version": version,
            "watches": watches,
            "bytes": (target / INDEX_NAME).stat().st_size,
        }
    if config is not None:
        shutil.copyfile(config, target / _CONFIG_NAME)
        manifest["config"] = True
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def prune(destination: Path, keep: int) -> list[str]:
    """Remove the oldest backups of ``destination`` so that ``keep`` remain, and return their
    names. Only directories named and filled as :func:`backup` does are touched: one that
    holds anything else is left alone."""
    if keep < 1:
        raise ConfigError("at least one backup must be kept")
    made = sorted(
        entry
        for entry in destination.iterdir()
        if _STAMP.fullmatch(entry.name) and not entry.is_symlink() and (entry / MANIFEST_NAME).is_file()
    )
    removed = []
    for old in made[:-keep]:
        if {entry.name for entry in old.iterdir()} - {INDEX_NAME, _CONFIG_NAME, MANIFEST_NAME}:
            continue  # not only a backup: somebody put something there
        for name in (INDEX_NAME, _CONFIG_NAME, MANIFEST_NAME):
            (old / name).unlink(missing_ok=True)
        old.rmdir()
        removed.append(old.name)
    return removed


def restore(settings: Settings, source: Path, *, force: bool = False) -> dict[str, Any]:
    """Put the Live index of the backup ``source`` (a directory written by :func:`backup`) in
    place. The server must be stopped. An index already there is kept aside under another
    name, and only replaced with ``force``. Returns the manifest of the backup."""
    try:
        manifest = json.loads((source / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        raise ConfigError("not a backup of imageBio: no manifest in this directory") from None
    if not isinstance(manifest, dict) or manifest.get("index") is None:
        raise ConfigError("this backup holds no Live index: there is nothing to restore")
    version, _ = _checked(source / INDEX_NAME)
    if version > SCHEMA_VERSION:
        raise ConfigError(
            f"this backup was made by a newer imageBio (index version {version}, this one reads "
            f"up to {SCHEMA_VERSION}): restore it with that version"
        )
    state = settings.live_state_dir
    state.mkdir(parents=True, exist_ok=True)
    try:  # a server that runs holds the state directory, even when it writes nothing
        held = hold_state_dir(state, alone=True)
    except BlockingIOError:
        raise ConfigError("the Live index is in use: stop the server before restoring") from None
    try:
        current = state / INDEX_NAME
        if current.exists():
            if not force:
                raise ConfigError(
                    "there is already a Live index here: use --force to replace it (it is kept aside)"
                )
            # what a stopped server left in its write-ahead log belongs to the index: it is
            # folded into the file before the file is moved aside. An index too damaged for
            # that (a likely reason to restore) is moved as it is, its log with it.
            with (
                contextlib.suppress(sqlite3.Error),
                contextlib.closing(sqlite3.connect(current, timeout=5)) as old,
            ):
                old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            aside = f"{INDEX_NAME}.before-restore-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
            current.rename(state / aside)
            for tail in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    (state / f"{INDEX_NAME}{tail}").rename(state / f"{aside}{tail}")
        with (
            contextlib.closing(sqlite3.connect(f"file:{source / INDEX_NAME}?mode=ro", uri=True)) as saved,
            contextlib.closing(sqlite3.connect(current)) as fresh,
        ):
            saved.backup(fresh)
    finally:
        if held is not None:
            held.close()
    return manifest
