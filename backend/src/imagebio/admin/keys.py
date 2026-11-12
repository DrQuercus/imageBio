"""Personal keys: what lets a script or an assistant (an AI, local or not) read imageBio with
the rights of the person who made the key, without a password (ADR 015).

A key is shown once, when it is made; only its SHA-256 is kept, in ``<state>/keys.json``
(written whole, mode 0600). It reads only (GET and HEAD), never administers, expires (90 days
by default, a year at most), and is revoked by its owner or by an administrator. Its groups are
those the proxy gave its owner when it was made, plus those given in the settings page now.

Keys are not in the backups: a backup restored must not bring back a key revoked since.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from imagebio.core.errors import InvalidRequestError, NotFoundError

__all__ = ["FILE_NAME", "PREFIX", "KeyInfo", "KeyStore"]

FILE_NAME: Final = "keys.json"
SCHEMA: Final = "imagebio.keys"
#: What a key starts with: the proxy recognises a key by it (and lets it through without asking
#: for a password), the server by it too.
PREFIX: Final = "ibk_"
_SECRET_BYTES: Final = 32
DAYS_DEFAULT: Final = 90
DAYS_MAX: Final = 366
_MAX_PER_USER: Final = 10
_MAX_NAME: Final = 64
_SEEN_WRITE_S: Final = 600.0  # "last used" reaches the file at most this often
_LOOK_EVERY_S: Final = 1.0  # how often the file is looked at for a change made by another process


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """What a key is, without the key itself."""

    id: str
    user: str
    name: str
    hint: str  # the first characters of the key, to tell keys apart
    created: float
    expires: float
    last_used: float | None
    groups: tuple[str, ...]  # those of the proxy when the key was made

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hint": self.hint,
            "created": self.created,
            "expires": self.expires,
            "last_used": self.last_used,
        }


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and 1 <= len(name.strip()) <= _MAX_NAME
        and not any(unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") for ch in name)
    )


class KeyStore:
    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = directory / FILE_NAME
        self._clock = clock
        self._lock = threading.RLock()
        self._keys: dict[str, dict[str, Any]] = {}  # by digest
        self._file: tuple[int, int] | None = None
        self._looked_at = 0.0
        self._used: dict[str, float] = {}  # digest -> last use not yet written
        self._written_at = 0.0
        self._load()

    # ── the file ─────────────────────────────────────────────────────────────
    def _stat(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def _load(self) -> None:
        """Read the file. One that does not read is taken as no key at all: a key that cannot
        be checked does not open anything (and its owner makes another)."""
        self._file = self._stat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._keys = {}
            return
        keys = data.get("keys") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
        self._keys = {
            k["hash"]: k
            for k in (keys if isinstance(keys, list) else [])
            if isinstance(k, dict)
            and isinstance(k.get("hash"), str)
            and isinstance(k.get("user"), str)
            and isinstance(k.get("expires"), int | float)
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for digest, when in self._used.items():
            if digest in self._keys:
                self._keys[digest]["last_used"] = when
        self._used.clear()
        staging = self.path.with_name(f".{FILE_NAME}.part")
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema": SCHEMA, "keys": list(self._keys.values())}, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(staging, self.path)
        self._file = self._stat()
        self._written_at = self._clock()

    def _fresh(self) -> None:
        """Take what another process wrote (the lock is held)."""
        if self._stat() != self._file:
            self._load()

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """Read, change, write, with no other thread nor process in between: two servers on
        one state directory lose none of each other's changes, and a key revoked by one is not
        written back by the other."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path.with_name("keys.lock"), "a")  # noqa: SIM115 - held until the end
            try:
                try:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX)
                except ImportError:  # not a POSIX system: one process only
                    pass
                self._fresh()
                yield
                self._save()
            finally:
                handle.close()

    @staticmethod
    def _info(k: dict[str, Any]) -> KeyInfo:
        return KeyInfo(
            id=str(k.get("id", "")),
            user=str(k["user"]),
            name=str(k.get("name", "")),
            hint=str(k.get("hint", "")),
            created=float(k.get("created", 0.0)),
            expires=float(k["expires"]),
            last_used=k.get("last_used"),
            groups=tuple(g for g in k.get("groups", []) if isinstance(g, str)),
        )

    # ── what users do ────────────────────────────────────────────────────────
    def create(self, user: str, name: object, days: object, groups: frozenset[str]) -> tuple[str, KeyInfo]:
        """A new key of ``user``: the key itself (shown once) and what it is."""
        if not _valid_name(name):
            raise InvalidRequestError(f"a key is named with 1 to {_MAX_NAME} characters")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= DAYS_MAX:
            raise InvalidRequestError(f"a key lasts 1 to {DAYS_MAX} days")
        assert isinstance(name, str)
        secret = PREFIX + secrets.token_urlsafe(_SECRET_BYTES)
        now = self._clock()
        record: dict[str, Any] = {
            "id": secrets.token_hex(6),
            "user": user,
            "name": name.strip(),
            "hint": secret[: len(PREFIX) + 4],
            "hash": _digest(secret),
            "created": now,
            "expires": now + days * 86400.0,
            "last_used": None,
            "groups": sorted(groups),
        }
        with self._writing():
            mine = [k for k in self._keys.values() if k["user"] == user and k["expires"] > now]
            if len(mine) >= _MAX_PER_USER:
                raise InvalidRequestError(f"no more than {_MAX_PER_USER} keys each: revoke one first")
            # expired keys go when another is made
            self._keys = {d: k for d, k in self._keys.items() if k["expires"] > now}
            self._keys[_digest(secret)] = record
        return secret, self._info(record)

    def of(self, user: str) -> list[KeyInfo]:
        """The keys of ``user`` still valid, newest first."""
        now = self._clock()
        with self._lock:
            self._fresh()
            mine = [self._info(k) for k in self._keys.values() if k["user"] == user and k["expires"] > now]
        return sorted(mine, key=lambda k: -k.created)

    def revoke(self, user: str, key_id: str) -> None:
        with self._writing():
            found = [d for d, k in self._keys.items() if k["user"] == user and k.get("id") == key_id]
            if not found:
                raise NotFoundError("no such key")
            for digest in found:
                del self._keys[digest]

    def revoke_all(self, user: str) -> int:
        """Revoke every key of ``user`` (an administrator, when somebody leaves)."""
        with self._writing():
            gone = [d for d, k in self._keys.items() if k["user"] == user]
            for digest in gone:
                del self._keys[digest]
        return len(gone)

    def counts(self) -> dict[str, int]:
        now = self._clock()
        with self._lock:
            self._fresh()
            out: dict[str, int] = {}
            for k in self._keys.values():
                if k["expires"] > now:
                    out[k["user"]] = out.get(k["user"], 0) + 1
        return out

    # ── checking a request ───────────────────────────────────────────────────
    def check(self, secret: str) -> KeyInfo | None:
        """The key ``secret`` is, if it is a valid key; None otherwise (unknown, revoked,
        expired). Its last use is noted, and written at most every ten minutes."""
        if not secret.startswith(PREFIX) or len(secret) > 200:
            return None
        digest = _digest(secret)
        now = self._clock()
        with self._lock:
            if time.monotonic() - self._looked_at >= _LOOK_EVERY_S:
                self._looked_at = time.monotonic()
                self._fresh()  # a key revoked by another process stops at once
            record = self._keys.get(digest)
            if record is None or record["expires"] <= now:
                return None
            self._used[digest] = now
            if now - self._written_at >= _SEEN_WRITE_S:
                self._written_at = now  # one try per period, whatever happens
                with contextlib.suppress(OSError), self._writing():  # a full disk does not stop a key
                    pass
                if digest not in self._keys:  # revoked by another process meanwhile
                    return None
            return self._info({**record, "last_used": now})
