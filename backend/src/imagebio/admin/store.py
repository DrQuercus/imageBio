"""The settings an administrator changes in the interface, kept apart from the file.

``imagebio.toml`` is written by whoever installs the server; the settings page writes
``<state>/settings.json``, which holds what was changed there and overrides the file, section by
section. Only these sections can be changed (ADR 014): the data roots, the microscope, part of
the Live settings, and the alert channels. Secrets never are (they come from the environment),
nor the external programs (ADR 011), nor anything that decides who the server trusts (address,
proxy, authentication).

The file also holds the accounts: for each user name the proxy authenticates, the groups
imageBio adds to the ones the proxy gives (they decide which data roots the user reads), and
whether the user administers the server. Passwords stay with the proxy.

Every change is checked by building the settings it would give (the same checks as the file),
saved whole or not at all, kept in a history, written to a log with who made it, and applied at
once: nothing edited here needs a restart. A change made on an out-of-date view of the settings
(another administrator saved meanwhile) is refused.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import logging
import math
import os
import shutil
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from imagebio.core.config import ROOT_NAME_RE, ConfigError, RootConfig, Settings, parse_channels, parse_roots
from imagebio.core.errors import ConflictError, InvalidRequestError

__all__ = [
    "FILE_NAME",
    "LOG_NAME",
    "SECTIONS",
    "SettingsFileError",
    "SettingsStore",
    "effective_settings",
]

SCHEMA: Final = "imagebio.settings"
FILE_NAME: Final = "settings.json"
LOG_NAME: Final = "settings-log.jsonl"
HISTORY_DIR: Final = "settings-history"
_HISTORY_KEPT: Final = 50
_LOG_SHOWN: Final = 200
_MAX_ACCOUNTS: Final = 5000
_MAX_GROUPS: Final = 64
_MAX_SEEN: Final = 5000
#: The sections the settings page may change, in the order of the page.
SECTIONS: Final = ("roots", "microscope", "live", "alerts")
_MICROSCOPE: Final = ("kv", "cs_mm", "amplitude_contrast")
#: Live settings the page may change, and their type.
_LIVE: Final[dict[str, str]] = {
    "report_after_idle_min": "number",
    "mean_free_path_nm": "number or null",
    "metrics": "bool",
    "ctf": "bool",
    "drift": "bool",
}
_REFRESH_EVERY_S: Final = 1.0  # how often the file is looked at for a change made by another process
#: Characters a user name given in the page may not hold: controls, invisible formatting
#: (soft hyphen, zero-width joiners…), private and lone surrogate code points, line breaks.
_BAD_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})

log = logging.getLogger("imagebio.admin")


def valid_user_name(name: object) -> bool:
    """A user name as the proxy gives it, and that cannot pass for another one."""
    return (
        isinstance(name, str)
        and 1 <= len(name) <= 256
        and "," not in name
        and name == name.strip()
        and not any(unicodedata.category(ch) in _BAD_CATEGORIES for ch in name)
    )


class SettingsFileError(ConfigError):
    """``settings.json`` cannot be used: the server does not start on half its settings (a data
    root narrowed in the interface would otherwise open again to the groups of the file)."""


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer of a few hundred digits
        return False


def effective_settings(base: Settings, sections: Mapping[str, Any]) -> Settings:
    """The settings of the file with the sections of the settings page laid over them. Raises
    :class:`ConfigError` for anything the file itself would not be allowed to hold."""
    fields: dict[str, Any] = {}
    unknown = set(sections) - set(SECTIONS)
    if unknown:
        raise ConfigError(f"unknown section(s) {sorted(unknown)}")
    if "roots" in sections:
        fields["roots"] = parse_roots(sections["roots"])
    if "microscope" in sections:
        microscope = sections["microscope"]
        if not isinstance(microscope, Mapping) or set(microscope) - set(_MICROSCOPE):
            raise ConfigError(f"microscope: expected some of {list(_MICROSCOPE)}")
        for key, value in microscope.items():
            if not _number(value):
                raise ConfigError(f"microscope: {key} must be a number")
            fields[key] = float(value)
    if "live" in sections:
        live = sections["live"]
        if not isinstance(live, Mapping) or set(live) - set(_LIVE):
            raise ConfigError(f"live: expected some of {list(_LIVE)}")
        for key, value in live.items():
            kind = _LIVE[key]
            if kind == "bool" and not isinstance(value, bool):
                raise ConfigError(f"live: {key} must be true or false")
            if kind.startswith("number") and not (_number(value) or (value is None and "null" in kind)):
                raise ConfigError(f"live: {key} must be a number")
        if "report_after_idle_min" in live:
            fields["live_report_idle_s"] = 60.0 * float(live["report_after_idle_min"])
        if "mean_free_path_nm" in live:
            mfp = live["mean_free_path_nm"]
            fields["live_mfp_nm"] = None if mfp is None else float(mfp)
        for key, name in (("metrics", "live_metrics"), ("ctf", "live_ctf"), ("drift", "live_drift")):
            if key in live:
                fields[name] = bool(live[key])
    if "alerts" in sections:
        fields["alert_channels"] = parse_channels(sections["alerts"])
    return dataclasses.replace(base, **fields)  # the checks of the file run again


def _inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _real(path: Path | str) -> str | None:
    try:
        real = os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        return None
    return real if os.path.isdir(real) else None


def check_settings_data(base: Settings, data: object) -> int:
    """That ``data`` (a settings file read as JSON) can be used with ``base``; returns its
    version. Raises :class:`SettingsFileError`. Used before a restore puts a file in place."""
    if (
        not isinstance(data, dict)
        or data.get("schema") != SCHEMA
        or not isinstance(data.get("version"), int)
        or not isinstance(data.get("accounts", {}), dict)
        or not isinstance(data.get("sections", {}), dict)
    ):
        raise SettingsFileError("not a settings file of imageBio")
    sections = dict(data.get("sections", {}))
    if base.local_roots:
        sections.pop("roots", None)
    try:
        effective_settings(base, sections)
        SettingsStore._parse_accounts(data.get("accounts", {}))
    except (ConfigError, InvalidRequestError) as e:
        raise SettingsFileError(str(e)) from None
    return int(data["version"])


class SettingsStore:
    """``<state>/settings.json``: read at start-up (a file that cannot be used stops the server),
    changed by administrators through the settings page."""

    def __init__(self, base: Settings, clock: Callable[[], float] = time.time) -> None:
        self.base = base
        self.directory = base.live_state_dir
        self.path = self.directory / FILE_NAME
        self._clock = clock
        self._lock = threading.Lock()
        self._seen_lock = threading.Lock()
        self._listeners: list[Callable[[Settings], None]] = []
        self._seen: dict[str, tuple[float, frozenset[str]]] = {}
        self._looked_at = 0.0
        self._file = self._stat()
        data = self._read()
        try:
            self._effective, self._accounts = self._apply(data)
        except (ConfigError, InvalidRequestError) as e:
            raise SettingsFileError(
                f"{self.path}: {e} (move the file aside to start on the settings of the configuration file)"
            ) from None
        self._data = data

    def _apply(self, data: Mapping[str, Any]) -> tuple[Settings, dict[str, tuple[frozenset[str], bool]]]:
        """The settings and accounts ``data`` gives. With ``serve --local``, the roots are those
        of the command line, whatever an earlier server saved."""
        sections = dict(data["sections"])
        if self.base.local_roots:
            sections.pop("roots", None)
        return effective_settings(self.base, sections), self._parse_accounts(data["accounts"])

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def refresh(self) -> None:
        """Take a change another process made to the file (two servers on one state
        directory), at most every second. A file that no longer reads is not taken: the
        settings in force stay, and the error is logged."""
        now = time.monotonic()
        if now - self._looked_at < _REFRESH_EVERY_S:
            return
        self._looked_at = now
        if self._stat() == self._file:
            return
        with self._lock:
            self._reload()

    def _reload(self) -> None:
        """Read the file again (the lock is held)."""
        known = self._stat()
        try:
            data = self._read()
            settings, accounts = self._apply(data)
        except (ConfigError, InvalidRequestError) as e:
            log.error("the settings file changed and cannot be used, the settings in force stay: %s", e)
            self._file = known
            return
        self._data, self._effective, self._accounts, self._file = data, settings, accounts, known
        for listener in self._listeners:
            listener(settings)

    # ── reading ──────────────────────────────────────────────────────────────
    def _read(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"schema": SCHEMA, "version": 0, "accounts": {}, "sections": {}}
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return empty
        except OSError as e:
            raise SettingsFileError(f"{self.path}: cannot be read ({e.strerror or e})") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise SettingsFileError(f"{self.path}: not valid JSON (move it aside to start)") from None
        if (
            not isinstance(data, dict)
            or data.get("schema") != SCHEMA
            or not isinstance(data.get("version"), int)
            or not isinstance(data.get("accounts", {}), dict)
            or not isinstance(data.get("sections", {}), dict)
        ):
            raise SettingsFileError(f"{self.path}: not a settings file of imageBio")
        return {**empty, **data}

    @staticmethod
    def _parse_accounts(raw: Mapping[str, Any]) -> dict[str, tuple[frozenset[str], bool]]:
        if len(raw) > _MAX_ACCOUNTS:
            raise InvalidRequestError(f"no more than {_MAX_ACCOUNTS} accounts")
        out = {}
        for name, account in raw.items():
            if not valid_user_name(name):
                raise InvalidRequestError(f"invalid user name {name[:64]!r}")
            groups = account.get("groups", []) if isinstance(account, Mapping) else None
            admin = account.get("admin", False) if isinstance(account, Mapping) else None
            if (
                not isinstance(groups, list)
                or len(groups) > _MAX_GROUPS
                or not all(isinstance(g, str) and ROOT_NAME_RE.fullmatch(g) for g in groups)
                or not isinstance(admin, bool)
            ):
                raise InvalidRequestError(
                    f"account {name[:64]!r}: groups are names of letters, digits, '_', '-', '.'; "
                    "admin is true or false"
                )
            out[name] = (frozenset(groups), admin)
        return out

    @property
    def effective(self) -> Settings:
        """The settings in force: the file, and what the settings page changed."""
        return self._effective

    @property
    def version(self) -> int:
        return int(self._data["version"])

    def updated(self) -> tuple[float | None, str | None]:
        """When the settings page last changed something, and who did."""
        return self._data.get("updated_at"), self._data.get("updated_by")

    def on_change(self, listener: Callable[[Settings], None]) -> None:
        """Called with the new settings after each change (data roots, Live service)."""
        self._listeners.append(listener)

    # ── accounts ─────────────────────────────────────────────────────────────
    def groups_of(self, name: str) -> frozenset[str]:
        """The groups imageBio gives ``name``, besides those of the proxy."""
        account = self._accounts.get(name)
        return account[0] if account is not None else frozenset()

    def is_admin(self, name: str, proxy_groups: frozenset[str]) -> bool:
        account = self._accounts.get(name)
        return (
            name in self.base.admins
            or bool(self.base.admin_groups & proxy_groups)
            or (account is not None and account[1])
        )

    def saw(self, name: str, proxy_groups: frozenset[str]) -> None:
        """Remember that ``name`` used the server (shown to administrators; not kept on disk)."""
        with self._seen_lock:
            self._seen[name] = (self._clock(), proxy_groups)
            if len(self._seen) > _MAX_SEEN:
                for old in sorted(self._seen, key=lambda n: self._seen[n][0])[: _MAX_SEEN // 10]:
                    self._seen.pop(old, None)

    def accounts(self, also: Iterable[str] = ()) -> list[dict[str, Any]]:
        """The users an administrator may give groups to: those with groups or the admin role,
        the administrators of the file, those seen since the server started, and ``also``."""
        with self._seen_lock:
            seen_now = dict(self._seen)
        names = set(self._accounts) | set(self.base.admins) | set(seen_now) | set(also)
        rows = []
        for name in sorted(names, key=str.lower):
            groups, admin = self._accounts.get(name, (frozenset(), False))
            seen = seen_now.get(name)
            proxy_groups = seen[1] if seen is not None else frozenset()
            if name in self.base.admins:
                role = "file"
            elif self.base.admin_groups & proxy_groups:
                role = "group"
            elif admin:
                role = "interface"
            else:
                role = None
            rows.append(
                {
                    "name": name,
                    "groups": sorted(groups),
                    "proxy_groups": sorted(proxy_groups),
                    "admin": role,
                    "seen": seen[0] if seen is not None else None,
                }
            )
        return rows

    # ── what the settings page shows ─────────────────────────────────────────
    def sections(self) -> dict[str, Any]:
        """Each section as in force, and whether it comes from the file or the settings page."""
        own = self._data["sections"]
        eff, base = self._effective, self.base

        def source(name: str) -> str:
            if name == "roots" and self.base.local_roots:
                return "file"  # the command line, which the page does not replace
            return "interface" if name in own else "file"

        check = self.serving_check()

        def root(r: Any) -> dict[str, Any]:
            real = _real(r.path)
            return {
                "name": r.name,
                "path": str(r.path),
                "groups": sorted(r.groups),
                "kv": r.kv,
                "cs_mm": r.cs_mm,
                "amplitude_contrast": r.amplitude_contrast,
                # as served: a root of the page whose path no longer passes its check is not
                "available": real is not None and (check is None or check(r, real)),
            }

        def live(s: Settings) -> dict[str, Any]:
            return {
                "report_after_idle_min": round(s.live_report_idle_s / 60),
                "mean_free_path_nm": s.live_mfp_nm,
                "metrics": s.live_metrics,
                "ctf": s.live_ctf,
                "drift": s.live_drift,
            }

        def channel(c: Any) -> dict[str, Any]:
            return {"name": c.name, "kind": c.kind, "url": c.url, "to": list(c.to)}

        return {
            "roots": {"source": source("roots"), "value": [root(r) for r in eff.roots]},
            "microscope": {
                "source": source("microscope"),
                "value": {k: getattr(eff, k) for k in _MICROSCOPE},
                "file": {k: getattr(base, k) for k in _MICROSCOPE},
                "own": dict(own.get("microscope", {})),
            },
            "live": {
                "source": source("live"),
                "value": live(eff),
                "file": live(base),
                "own": dict(own.get("live", {})),
            },
            "alerts": {
                "source": source("alerts"),
                "value": [channel(c) for c in eff.alert_channels],
                "email": base.smtp is not None,
            },
        }

    def root_parents(self) -> list[str]:
        """Where a data root of the settings page may lie (real paths): inside a root of the
        file, or under ``server.root_parents``. Nothing wider by default: an administrator
        appointed in the page must not open what the installer did not."""
        out: list[str] = []
        for p in [r.path for r in self.base.roots] + list(self.base.root_parents):
            real = _real(p)
            if real is not None and real not in out:
                out.append(real)
        return out

    def _forbidden(self) -> list[str]:
        """What no root may show, nor lie in: the server's state (settings, index, reports)
        and the working directory of the external programs (the sums of every session)."""
        places = [self.directory]
        if self.base.processing is not None and self.base.processing.work_dir is not None:
            places.append(self.base.processing.work_dir)
        return [os.path.realpath(p) for p in places]

    def root_check(self, root: RootConfig, real: str) -> bool:
        """Whether a root of the settings page may be served, its path resolved now. It was
        saved as a real path: a link that appeared in it since (repointed by whoever writes
        there), a parent narrowed in the file, a directory that now holds the state, and it is
        not served."""
        return (
            real == str(root.path)
            and any(_inside(real, p) for p in self.root_parents())
            and not any(_inside(f, real) or _inside(real, f) for f in self._forbidden())
        )

    def serving_check(self) -> Callable[[RootConfig, str], bool] | None:
        """The check the data roots in force are served with: those of the page are checked
        each time they are set; those of the file are the installer's."""
        own = "roots" in self._data["sections"] and not self.base.local_roots
        return self.root_check if own else None

    def log(self, limit: int = _LOG_SHOWN) -> list[dict[str, Any]]:
        """The latest changes, newest first."""
        try:
            lines = (self.directory / LOG_NAME).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines[-limit:]):
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    # ── changes ──────────────────────────────────────────────────────────────
    def _check_roots(self, value: Any) -> list[dict[str, Any]]:
        """The roots of the settings page, each an existing directory inside a root of the file
        or under ``server.root_parents``, holding neither the state nor the work of the
        programs; returned with their real paths (what is saved, and checked again whenever
        they are served)."""
        parents, forbidden = self.root_parents(), self._forbidden()
        out = []
        for raw, root in zip(value, parse_roots(value), strict=True):
            real = _real(root.path)
            # (no path in these messages: the page shows the allowed places to administrators)
            if real is None:
                raise InvalidRequestError(f"root {root.name!r}: not a directory the server can read")
            if not any(_inside(real, p) for p in parents):
                raise InvalidRequestError(
                    f"root {root.name!r}: must lie inside a data root of the configuration file, "
                    "or under one of its server.root_parents"
                )
            if any(_inside(f, real) or _inside(real, f) for f in forbidden):
                raise InvalidRequestError(f"root {root.name!r}: would show the server's own state or work")
            out.append({**raw, "path": real})
        return out

    def _commit(self, version: int, by: str, change: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
        with self._lock:
            if self._stat() != self._file:  # another process wrote it: take theirs first
                self._reload()
            if version != self.version:
                raise ConflictError("the settings were changed meanwhile: reload them and try again")
            data = copy.deepcopy(self._data)
            what = change(data)
            try:
                settings, accounts = self._apply(data)
            except ConfigError as e:
                raise InvalidRequestError(str(e)) from None
            data["version"] = self.version + 1
            data["updated_at"] = self._clock()
            data["updated_by"] = by
            self._write(data)
            self._data, self._effective, self._accounts, self._file = data, settings, accounts, self._stat()
            for listener in self._listeners:
                listener(settings)
            try:
                self._log({"at": data["updated_at"], "by": by, "version": data["version"], "what": what})
            except OSError as e:  # the change is made and saved; only its line is missing
                log.error("the change %s by %s could not be logged: %s", data["version"], by, e)
            return {"version": data["version"], "what": what}

    def _write(self, data: dict[str, Any]) -> None:
        """The whole file or nothing; the previous version goes to the history."""
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            history = self.directory / HISTORY_DIR
            history.mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(self.path, history / f"settings-v{self.version:06d}.json")
            for old in sorted(history.glob("settings-v*.json"))[:-_HISTORY_KEPT]:
                old.unlink(missing_ok=True)
        staging = self.directory / f".{FILE_NAME}.part"
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(staging, self.path)

    def _log(self, entry: dict[str, Any]) -> None:
        fd = os.open(self.directory / LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def set_section(self, section: str, value: Any, *, by: str, version: int) -> dict[str, Any]:
        """Replace a section with ``value`` (as the file would write it: a list of roots or of
        channels; the microscope or Live keys to set, the others coming from the file)."""
        if section not in SECTIONS:
            raise InvalidRequestError(f"section must be one of {', '.join(SECTIONS)}")
        if section == "roots" and self.base.local_roots:
            raise InvalidRequestError(
                "with serve --local, the data roots are the directories of the command line"
            )
        try:  # checked before anything is described or written
            effective_settings(self.base, {section: value})
            if section == "roots":
                value = self._check_roots(value)
        except ConfigError as e:
            raise InvalidRequestError(str(e)) from None

        def change(data: dict[str, Any]) -> str:
            before = self.sections()[section]["value"]
            data["sections"][section] = copy.deepcopy(value)
            return _describe(section, before, value)

        return self._commit(version, by, change)

    def reset_section(self, section: str, *, by: str, version: int) -> dict[str, Any]:
        """Go back to what the file says for ``section``."""
        if section not in SECTIONS:
            raise InvalidRequestError(f"section must be one of {', '.join(SECTIONS)}")

        def change(data: dict[str, Any]) -> str:
            data["sections"].pop(section, None)
            return f"{_LABELS[section]} : retour au fichier de configuration"

        return self._commit(version, by, change)

    def set_account(
        self, name: str, groups: list[str], admin: bool, *, by: str, version: int
    ) -> dict[str, Any]:
        """Give ``name`` these groups and this role (in addition to those of the proxy and of
        the file). An administrator does not take the role away from themselves."""
        if not valid_user_name(name):
            raise InvalidRequestError("invalid user name")
        if (
            not isinstance(groups, list)
            or len(groups) > _MAX_GROUPS
            or not all(isinstance(g, str) and ROOT_NAME_RE.fullmatch(g) for g in groups)
            or not isinstance(admin, bool)
        ):
            raise InvalidRequestError(
                "groups are names of letters, digits, '_', '-', '.'; admin is true or false"
            )
        if name == by and not admin and self._accounts.get(name, (frozenset(), False))[1]:
            raise InvalidRequestError("an administrator does not take the role away from themselves")

        def change(data: dict[str, Any]) -> str:
            data["accounts"][name] = {"groups": sorted(set(groups)), "admin": admin}
            role = ", administrateur" if admin else ""
            return f"compte {name} : groupes {', '.join(sorted(set(groups))) or 'aucun'}{role}"

        return self._commit(version, by, change)

    def remove_account(self, name: str, *, by: str, version: int) -> dict[str, Any]:
        if name == by and self._accounts.get(name, (frozenset(), False))[1]:
            raise InvalidRequestError("an administrator does not take the role away from themselves")

        def change(data: dict[str, Any]) -> str:
            if data["accounts"].pop(name, None) is None:
                raise InvalidRequestError(f"no groups or role were given to {name[:64]!r} here")
            return f"compte {name} : groupes et rôle retirés"

        return self._commit(version, by, change)


_LABELS: Final = {
    "roots": "dossiers de données",
    "microscope": "microscope",
    "live": "réglages du Live",
    "alerts": "canaux d'alerte",
}


def _describe(section: str, before: Any, after: Any) -> str:
    """What a change did, in words, without addresses (an ntfy topic is as good as a password)."""
    if section in ("roots", "alerts"):
        old = {item["name"]: item for item in before}
        new = {item.get("name"): item for item in after if isinstance(item, Mapping)}
        parts = [f"+{n}" for n in new if n not in old] + [f"-{n}" for n in old if n not in new]
        for name in new.keys() & old.keys():
            fields: tuple[str, ...]
            if section == "roots":
                fields = ("path", "groups", "kv", "cs_mm", "amplitude_contrast")
                was = {k: old[name].get(k) for k in fields}
                now = {k: new[name].get(k, [] if k == "groups" else None) for k in fields}
                was["groups"], now["groups"] = sorted(was["groups"] or []), sorted(now["groups"] or [])
                was["path"], now["path"] = str(was["path"]), str(now["path"])
            else:
                fields = ("kind", "url", "to")
                was = {k: old[name].get(k) for k in fields}
                now = {k: new[name].get(k, [] if k == "to" else "") for k in fields}
            if was != now:
                parts.append(f"~{name}")
        return f"{_LABELS[section]} : {', '.join(sorted(parts)) or 'inchangés'}"
    changed = [
        f"{k} {before.get(k)} → {after[k]}"
        for k in sorted(after)
        if isinstance(after, Mapping) and before.get(k) != after[k]
    ]
    return f"{_LABELS[section]} : {', '.join(changed) or 'inchangé'}"
