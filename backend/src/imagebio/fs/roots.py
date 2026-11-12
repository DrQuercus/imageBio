"""Data roots and safe path resolution.

Clients never send or receive a server path. They use *logical* paths,
``<root name>/<relative/path>`` with forward slashes; the bare root name is the root directory.

A logical path is accepted only if

1. its syntax is plain: no absolute path, no backslash, no control character, no ``.`` or ``..``
   segment (trailing and duplicate slashes are normalised away);
2. its root exists and is visible to the user (roots restricted to groups);
3. the *real* path of the target (symlinks resolved) lies inside the real path of a root visible
   to that user. A symlink into another permitted root is fine; anything else is refused.

Whether something exists is only revealed (``NotFoundError``) once rule 3 holds for its deepest
existing ancestor: probing for files outside the roots always gives the same ``AccessDeniedError``.
Error messages are public and never contain a server path.

Limitation: the check and the later ``open()`` are two steps. Someone able to write inside a
root could swap a path component for a symlink in between; data roots should be mounted
read-only for the service account.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from imagebio.core.config import RootConfig
from imagebio.core.errors import AccessDeniedError, InvalidRequestError, NotFoundError

__all__ = ["LOCAL_USER", "DirEntry", "DirListing", "ResolvedPath", "RootSet", "User"]

MAX_PATH_LENGTH: Final = 4096
MAX_SEGMENT_LENGTH: Final = 255
MAX_LIST_LIMIT: Final = 10_000
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS: Final = re.compile(r"(\d+)")
_DENIED: Final = "access denied"

EntryKind = Literal["dir", "file", "broken", "other"]


@dataclass(frozen=True, slots=True)
class User:
    """Who is asking. ``all_roots`` is the standalone mode: one local user who sees every root.
    ``admin``: may open the settings of the server (``admin/``)."""

    name: str
    groups: frozenset[str] = frozenset()
    all_roots: bool = False
    admin: bool = False


LOCAL_USER: Final = User("local", frozenset(), all_roots=True, admin=True)


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    logical: str  # normalised logical path
    real: Path  # server path, symlinks resolved: internal, never sent to a client
    root: RootConfig
    is_dir: bool
    is_file: bool


@dataclass(frozen=True, slots=True)
class DirEntry:
    name: str
    path: str  # logical
    kind: EntryKind
    size: int
    mtime: float
    is_symlink: bool
    supported: bool  # a reader claims this file name

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "path": self.path, "kind": self.kind, "size": self.size,
            "mtime": self.mtime, "is_symlink": self.is_symlink, "supported": self.supported,
        }  # fmt: skip


@dataclass(frozen=True, slots=True)
class DirListing:
    path: str
    parent: str | None  # None at the top of a root
    entries: tuple[DirEntry, ...]
    total: int  # entries before pagination
    offset: int
    limit: int

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path, "parent": self.parent, "total": self.total, "offset": self.offset,
            "limit": self.limit, "entries": [e.to_json() for e in self.entries],
        }  # fmt: skip


def _inside(child: str, parent: str) -> bool:
    """``child`` is ``parent`` or below it (both real, absolute paths)."""
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _expressible(name: str) -> bool:
    """Whether a file name can be written in a logical path."""
    return (
        bool(name)
        and name not in (".", "..")
        and "\\" not in name
        and "/" not in name
        and not _CONTROL.search(name)
    )


def _natural_key(entry: DirEntry) -> tuple[Any, ...]:
    tokens = tuple(
        (0, int(tok), "") if tok.isdigit() else (1, 0, tok.casefold())
        for tok in _DIGITS.split(entry.name)
        if tok
    )
    return (entry.kind != "dir", tokens, entry.name)


class RootSet:
    """The configured data roots. The real path of each root is resolved when the roots are
    set: at start-up, and when an administrator changes them (:meth:`replace`)."""

    def __init__(self, roots: Sequence[RootConfig]) -> None:
        self._roots: dict[str, RootConfig] = {}
        self._real: dict[str, str | None] = {}
        self.replace(roots)

    def replace(self, roots: Sequence[RootConfig]) -> None:
        """Serve these roots from now on. A request under way may still see the previous ones,
        never a mixture that grants what neither would: a root missing from either table is
        unavailable."""
        table: dict[str, RootConfig] = {}
        real_paths: dict[str, str | None] = {}
        for root in roots:
            if root.name in table:
                raise ValueError(f"duplicate root name {root.name!r}")
            table[root.name] = root
            try:
                real = os.path.realpath(root.path, strict=True)
                real_paths[root.name] = real if os.path.isdir(real) else None
            except OSError:
                real_paths[root.name] = None  # missing or unreadable: kept, reported as unavailable
        self._real = real_paths
        self._roots = table

    # ── visibility ───────────────────────────────────────────────────────────
    def _visible(self, root: RootConfig, user: User) -> bool:
        return user.all_roots or not root.groups or bool(root.groups & user.groups)

    def visible_roots(self, user: User) -> list[RootConfig]:
        return [r for r in self._roots.values() if self._visible(r, user)]

    def available(self, root: RootConfig) -> bool:
        return self._real.get(root.name) is not None

    def _allowed(self, user: User) -> list[str]:
        return [real for r in self.visible_roots(user) if (real := self._real.get(r.name)) is not None]

    # ── resolution ───────────────────────────────────────────────────────────
    def _parse(self, logical: object, user: User) -> tuple[RootConfig, list[str]]:
        """Validate the syntax and the root; return the root and the path segments below it."""
        if not isinstance(logical, str) or not logical or len(logical) > MAX_PATH_LENGTH:
            raise AccessDeniedError(_DENIED)
        if logical.startswith("/") or "\\" in logical or _CONTROL.search(logical):
            raise AccessDeniedError(_DENIED)
        try:
            os.fsencode(logical)
        except (UnicodeError, ValueError):
            raise AccessDeniedError(_DENIED) from None
        segments = [s for s in logical.split("/") if s]
        if not segments or any(s in (".", "..") or len(s) > MAX_SEGMENT_LENGTH for s in segments):
            raise AccessDeniedError(_DENIED)
        root = self._roots.get(segments[0])
        if root is None or not self._visible(root, user):
            raise AccessDeniedError(_DENIED)  # same answer for an unknown root and a forbidden one
        return root, segments[1:]

    def resolve(self, logical: str, user: User) -> ResolvedPath:
        """Resolve a logical path. Raises :class:`AccessDeniedError` or :class:`NotFoundError`."""
        root, segments = self._parse(logical, user)
        normalised = "/".join([root.name, *segments])
        root_real = self._real.get(root.name)
        if root_real is None:
            raise NotFoundError(f"data root {root.name!r} is not available")
        # Non-strict realpath resolves every symlink of the existing part and appends the rest
        # as is; without '..' segments the appended part cannot climb out.
        real = os.path.realpath(os.path.join(root_real, *segments))
        if not any(_inside(real, allowed) for allowed in self._allowed(user)):
            raise AccessDeniedError(_DENIED)
        try:
            st = os.stat(real)
        except PermissionError:
            raise AccessDeniedError(_DENIED) from None
        except OSError:  # missing, not a directory on the way, symlink loop...
            raise NotFoundError(f"not found: {normalised}") from None
        return ResolvedPath(normalised, Path(real), root, stat.S_ISDIR(st.st_mode), stat.S_ISREG(st.st_mode))

    # ── listing ──────────────────────────────────────────────────────────────
    def list_dir(
        self,
        logical: str,
        user: User,
        *,
        supports: Callable[[str], bool] | None = None,
        offset: int = 0,
        limit: int = 1000,
        show_hidden: bool = False,
    ) -> DirListing:
        """List a directory: directories first, then natural, case-insensitive name order.

        Broken symlinks are listed as ``"broken"``; symlinks leading outside the roots visible to
        the user, unreadable entries and names that a logical path cannot express are omitted.
        """
        for name, value, low, high in (("offset", offset, 0, None), ("limit", limit, 1, MAX_LIST_LIMIT)):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < low
                or (high and value > high)
            ):
                raise InvalidRequestError(f"invalid {name}: {value!r}")
        target = self.resolve(logical, user)
        if not target.is_dir:
            raise InvalidRequestError(f"not a directory: {target.logical}")
        allowed = self._allowed(user)
        entries: list[DirEntry] = []
        try:
            scan = os.scandir(target.real)
        except PermissionError:
            raise AccessDeniedError(_DENIED) from None
        except OSError:
            raise NotFoundError(f"not found: {target.logical}") from None
        with scan:
            for item in scan:
                name = item.name
                if not _expressible(name) or (name.startswith(".") and not show_hidden):
                    continue
                try:
                    link = item.is_symlink()
                    if link:
                        real = os.path.realpath(item.path)
                        if not any(_inside(real, a) for a in allowed):
                            continue  # leads outside the roots: not even listed
                        try:
                            st = os.stat(real)
                        except OSError:
                            lst = item.stat(follow_symlinks=False)
                            entries.append(
                                DirEntry(
                                    name, f"{target.logical}/{name}", "broken", 0, lst.st_mtime, True, False
                                )
                            )
                            continue
                    else:
                        st = item.stat(follow_symlinks=False)
                except OSError:
                    continue  # vanished or unreadable: never breaks the listing
                kind: EntryKind = (
                    "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
                )
                entries.append(
                    DirEntry(
                        name=name,
                        path=f"{target.logical}/{name}",
                        kind=kind,
                        size=st.st_size if kind == "file" else 0,
                        mtime=st.st_mtime,
                        is_symlink=link,
                        supported=bool(kind == "file" and supports is not None and supports(name)),
                    )
                )
        entries.sort(key=_natural_key)
        parent = target.logical.rsplit("/", 1)[0] if "/" in target.logical else None
        return DirListing(
            target.logical, parent, tuple(entries[offset : offset + limit]), len(entries), offset, limit
        )
