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
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Final

from imagebio import __version__
from imagebio.admin.store import FILE_NAME as SETTINGS_NAME
from imagebio.admin.store import LOG_NAME as SETTINGS_LOG_NAME
from imagebio.admin.store import SettingsFileError, check_settings_data
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
    ``config`` (if given), the settings changed in the settings page and their log (if any),
    and a manifest."""
    stamp = _now()
    target = destination / stamp
    try:
        destination.mkdir(parents=True, exist_ok=True)
        target.mkdir(mode=0o700)  # the configuration may hold the addresses of alert channels
    except FileExistsError:
        raise ConfigError(f"a backup named {stamp} already exists there: try again in a second") from None
    manifest: dict[str, Any] = {
        "imagebio": __version__,
        "created": stamp,
        "index": None,
        "config": False,
        "settings": False,
    }
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
    # the settings page writes whole files, renamed into place: a copy is never half of one
    for name in (SETTINGS_NAME, SETTINGS_LOG_NAME):
        if (settings.live_state_dir / name).is_file():
            shutil.copyfile(settings.live_state_dir / name, target / name)
            manifest["settings"] = manifest["settings"] or name == SETTINGS_NAME
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
        ours = (INDEX_NAME, _CONFIG_NAME, MANIFEST_NAME, SETTINGS_NAME, SETTINGS_LOG_NAME)
        if {entry.name for entry in old.iterdir()} - set(ours):
            continue  # not only a backup: somebody put something there
        for name in ours:
            (old / name).unlink(missing_ok=True)
        old.rmdir()
        removed.append(old.name)
    return removed


def restore(
    settings: Settings, source: Path, *, force: bool = False, with_settings: bool = False
) -> dict[str, Any]:
    """Put the Live index of the backup ``source`` (a directory written by :func:`backup`) in
    place, and its settings page too with ``with_settings``. The server must be stopped. What
    is already there is kept aside under another name, and only replaced with ``force``.
    Everything is checked before anything is moved. Returns the manifest of the backup, with
    ``restored``: what was put in place.

    The settings are restored only when asked for: repairing a damaged index must not bring
    back, unnoticed, groups and roles taken away since the backup."""
    try:
        manifest = json.loads((source / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        raise ConfigError("not a backup of imageBio: no manifest in this directory") from None
    if not isinstance(manifest, dict):
        raise ConfigError("not a backup of imageBio: no manifest in this directory")
    has_index = manifest.get("index") is not None
    has_settings = bool(manifest.get("settings"))
    if with_settings and not has_settings:
        raise ConfigError("this backup holds no settings of the settings page")
    if not has_index and not with_settings:
        raise ConfigError(
            "this backup holds no Live index, only settings: restore them with --settings"
            if has_settings
            else "this backup holds no Live index and no settings: there is nothing to restore"
        )
    version = _checked(source / INDEX_NAME)[0] if has_index else 0
    if version > SCHEMA_VERSION:
        raise ConfigError(
            f"this backup was made by a newer imageBio (index version {version}, this one reads "
            f"up to {SCHEMA_VERSION}): restore it with that version"
        )
    saved_settings: dict[str, Any] | None = None
    if with_settings:  # read and checked before anything here is touched
        try:
            saved_settings = json.loads((source / SETTINGS_NAME).read_text(encoding="utf-8"))
            check_settings_data(settings, saved_settings)
        except (OSError, ValueError, SettingsFileError) as e:
            raise ConfigError(
                f"the settings of this backup cannot be used ({e}): nothing was restored"
            ) from None
    state = settings.live_state_dir
    state.mkdir(parents=True, exist_ok=True)
    try:  # a server that runs holds the state directory, even when it writes nothing
        held = hold_state_dir(state, alone=True)
    except BlockingIOError:
        raise ConfigError("the state directory is in use: stop the server before restoring") from None
    try:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        settings_file = state / SETTINGS_NAME
        current = state / INDEX_NAME
        if not force:
            if saved_settings is not None and settings_file.exists():
                raise ConfigError(
                    "there are already settings here: use --force to replace them (they are kept aside)"
                )
            if has_index and current.exists():
                raise ConfigError(
                    "there is already a Live index here: use --force to replace it (it is kept aside)"
                )
        if has_index and current.exists():
            # what a stopped server left in its write-ahead log belongs to the index: it is
            # folded into the file before the file is moved aside. An index too damaged for
            # that (a likely reason to restore) is moved as it is, its log with it.
            with (
                contextlib.suppress(sqlite3.Error),
                contextlib.closing(sqlite3.connect(current, timeout=5)) as old,
            ):
                old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            aside = f"{INDEX_NAME}.before-restore-{stamp}"
            current.rename(state / aside)
            for tail in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    (state / f"{INDEX_NAME}{tail}").rename(state / f"{aside}{tail}")
        if has_index:
            with (
                contextlib.closing(sqlite3.connect(f"file:{source / INDEX_NAME}?mode=ro", uri=True)) as saved,
                contextlib.closing(sqlite3.connect(current)) as fresh,
            ):
                saved.backup(fresh)
        if saved_settings is not None:
            # versions only go up: the history and the check of concurrent changes rely on it
            now_version = 0
            with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                now_version = int(json.loads(settings_file.read_text(encoding="utf-8"))["version"])
            saved_settings["version"] = max(now_version, int(saved_settings["version"])) + 1
            saved_settings["updated_at"] = time.time()
            saved_settings["updated_by"] = "restore"
            staging = state / f".{SETTINGS_NAME}.part"
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(saved_settings, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
            if settings_file.exists():
                settings_file.rename(state / f"{SETTINGS_NAME}.before-restore-{stamp}")
            os.replace(staging, settings_file)
            # the log tells what happened; it is not rewound, it says this happened too
            fd = os.open(state / SETTINGS_LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as log:
                what = f"réglages de la sauvegarde {manifest['created']}"
                entry = {
                    "at": time.time(),
                    "by": "restore",
                    "version": saved_settings["version"],
                    "what": what,
                }
                log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        if held is not None:
            held.close()
    return {**manifest, "restored": {"index": has_index, "settings": saved_settings is not None}}
