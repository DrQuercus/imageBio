"""Reading tables out of STAR files (RELION, crYOLO's ``.cbox``): just enough of the format.

A STAR file is a sequence of ``data_<name>`` blocks. A block holds ``_label value`` pairs
and tables: ``loop_``, then one ``_label`` per line (RELION appends ``#3``, the column number),
then one row per line until a blank line or the next block. Values are separated by blanks;
one that contains blanks is quoted. Multi-line text fields (``;`` ... ``;``) do not occur in the
tables read here and are not supported.

Files of millions of rows exist (``particles.star``): rows are read one line at a time, never
the whole file, and the caller says which columns it wants.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from imagebio.core.errors import CorruptFileError

__all__ = ["MAX_LINE_BYTES", "StarTable", "find_table"]

#: A line longer than this is not a line of a STAR table (a binary file with a ``.star`` name).
MAX_LINE_BYTES: Final = 1 << 16
_MAX_COLUMNS: Final = 512


def _split(line: str) -> list[str]:
    if "'" in line or '"' in line:
        try:
            return shlex.split(line, comments=False, posix=True)
        except ValueError:
            return line.split()
    return line.split()


class StarTable:
    """One ``loop_`` of a STAR file: its block, its column labels (without the leading ``_``)
    and, through :meth:`rows`, its rows as lists of strings."""

    def __init__(self, path: Path, block: str, labels: tuple[str, ...], offset: int) -> None:
        self.path = path
        self.block = block
        self.labels = labels
        self._offset = offset  # of the first row, in bytes

    def has(self, *labels: str) -> bool:
        return all(label in self.labels for label in labels)

    def rows(self, columns: Sequence[str]) -> Iterator[tuple[str, ...]]:
        """The values of ``columns`` (labels that exist in the table) for each row, in file
        order. A row with too few values ends the table with :class:`CorruptFileError`."""
        wanted = [self.labels.index(c) for c in columns]
        last = max(wanted, default=-1)
        plain = last < 0 or all(i == j for j, i in enumerate(wanted))
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            started = False
            while True:
                raw = f.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_LINE_BYTES:
                    raise CorruptFileError("not a STAR file: a line is too long")
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    if started:
                        return  # a blank line ends the table
                    continue
                if line.startswith(("data_", "loop_", "_")):
                    return
                if line.startswith("#"):
                    continue
                started = True
                values = _split(line)
                if len(values) <= last:
                    raise CorruptFileError("the STAR table has a row with too few values")
                yield tuple(values[: last + 1]) if plain else tuple(values[i] for i in wanted)


def find_table(path: Path, required: Sequence[str], block: str | None = None) -> StarTable | None:
    """The first table of ``path`` that has every label of ``required`` (and is in block
    ``data_<block>`` when one is named). None when there is no such table."""
    name = ""
    with open(path, "rb") as f:
        while True:
            raw = f.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return None
            if len(raw) > MAX_LINE_BYTES:
                raise CorruptFileError("not a STAR file: a line is too long")
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data_"):
                name = line[5:]
            elif line == "loop_":
                labels: list[str] = []
                while True:
                    position = f.tell()
                    raw = f.readline(MAX_LINE_BYTES + 1)
                    if len(raw) > MAX_LINE_BYTES:
                        raise CorruptFileError("not a STAR file: a line is too long")
                    text = raw.decode("utf-8", "replace").strip()
                    if text.startswith("_"):
                        if len(labels) >= _MAX_COLUMNS:
                            raise CorruptFileError("not a STAR file: too many columns")
                        labels.append(text.split()[0][1:])
                        continue
                    if text.startswith("#") or (not text and raw and not labels):
                        continue
                    break
                if labels and (block is None or name == block) and all(r in labels for r in required):
                    return StarTable(path, name, tuple(labels), position)
                f.seek(position)  # the line that ended the labels is read again: it may open a block
