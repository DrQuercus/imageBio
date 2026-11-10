"""Shared by the live tests: an index, a scanner on a temporary tree, a simulated clock."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from imagebio.io.registry import default_registry
from imagebio.live.index import Index
from imagebio.live.scanner import Entry, Fs, Scanner, ScanSettings

#: Origin of the simulated clock: every test time is BASE + seconds.
BASE = 1_800_000_000.0


class CountingFs(Fs):
    """The scanner's filesystem calls, counted."""

    def __init__(self) -> None:
        self.stats = 0
        self.listings = 0

    def stat(self, path: str) -> os.stat_result:
        self.stats += 1
        return super().stat(path)

    def scandir(self, path: str) -> list[Entry]:
        self.listings += 1
        return super().scandir(path)

    def reset(self) -> None:
        self.stats = self.listings = 0


@dataclass
class Watch:
    index: Index
    root: Path
    root_id: int
    fs: CountingFs
    settings: ScanSettings

    def scanner(self) -> Scanner:
        """A new scanner on the same index: what a restarted server builds."""
        return Scanner(self.index, self.root_id, self.root, default_registry(), self.settings, self.fs)

    def rows(self, sql: str, *args: object) -> list[dict[str, object]]:
        return [dict(r) for r in self.index.connection().execute(sql, args)]

    def files(self, state: str | None = None) -> dict[str, dict[str, object]]:
        """``{relative path: row}`` of the indexed files, optionally in one state."""
        rows = self.rows(
            "SELECT d.rel, f.* FROM files f JOIN dirs d ON d.id = f.dir_id WHERE d.root_id = ?", self.root_id
        )
        return {
            (f"{r['rel']}/{r['name']}" if r["rel"] else str(r["name"])): r
            for r in rows
            if state is None or r["state"] == state
        }

    def exposures(self, listed: bool = True) -> dict[str, dict[str, object]]:
        rows = self.rows("SELECT * FROM exposures WHERE root_id = ?", self.root_id)
        return {str(r["name"]): r for r in rows if not listed or r["primary_name"] is not None}


def make_watch(base: Path, settings: ScanSettings | None = None) -> Watch:
    """An empty session directory under ``base`` and an index that watches it."""
    (base / "state").mkdir()
    index = Index(base / "state" / "live.sqlite")
    root = base / "session"
    root.mkdir()
    os.utime(root, (BASE, BASE))
    with index.write() as db:
        root_id = db.execute(
            "INSERT INTO roots (path, created_at) VALUES (?, ?)", (str(root), BASE)
        ).lastrowid
    assert root_id is not None
    return Watch(index, root, root_id, CountingFs(), settings or ScanSettings())
