"""Server settings: data roots, authentication mode, cache sizes.

Settings come from a TOML file (``imagebio serve --config imagebio.toml``)::

    [server]
    host = "127.0.0.1"
    port = 8000
    allowed_hosts = ["imagebio.lab.example"]   # public names, e.g. behind a reverse proxy
    # trust_network = true    # only with auth.mode = "proxy": allow a non-loopback host when the
    #                         # network itself is private to the proxy (e.g. a container network)

    [auth]
    mode = "proxy"                  # "none" (local use, loopback only) or "proxy"
    user_header = "X-Forwarded-User"
    groups_header = "X-Forwarded-Groups"

    [cache]
    plane_bytes = "2GiB"
    max_open_datasets = 32

    [live]
    state_dir = "/var/lib/imagebio" # index of the watched sessions and thumbnails (SQLite)
    interval = 5                    # seconds between two scans of a watched directory
    min_stable = 10                 # a file must keep its size this long to be complete
    # scan = false                  # serve the index without scanning (a read-only instance)

    [alerts.smtp]                   # only for channels of kind "email"
    host = "smtp.lab.example"
    from = "imagebio@lab.example"

    [[alerts.channels]]             # where the live monitor may send alerts; users pick by name
    name = "telephone"
    kind = "ntfy"                   # "ntfy", "webhook" or "email"
    url = "https://ntfy.sh/a-topic-hard-to-guess"

    [[alerts.channels]]
    name = "equipe"
    kind = "email"
    to = ["cryoem@lab.example"]

    [[roots]]
    name = "glacios"                # first component of every client-visible path
    path = "/srv/glacios"
    groups = ["cryoem"]             # empty or missing: every authenticated user

or from ``Settings.local([...])`` for the standalone mode (``imagebio serve --local DIR``).
Clients never see or send server paths: they use *logical* paths, ``<root name>/<relative path>``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

ROOT_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ADDRESS_RE: Final = re.compile(r"[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}")
_HTTP_URL_RE: Final = re.compile(r"https?://[^\s/?#]+(/[^\s]*)?")
_SIZE_RE: Final = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B|B)?\s*", re.IGNORECASE)
_SIZE_FACTORS: Final[Mapping[str, int]] = {
    "b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
}  # fmt: skip

AuthMode = Literal["none", "proxy"]
AlertKind = Literal["ntfy", "webhook", "email"]


class ConfigError(ValueError):
    """The configuration is invalid."""


def parse_size(value: int | str) -> int:
    """``"2GiB"`` -> 2147483648. Plain integers are bytes."""
    if isinstance(value, bool):
        raise ConfigError(f"invalid size: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"invalid size: {value!r}")
        return value
    m = _SIZE_RE.fullmatch(str(value))
    if not m:
        raise ConfigError(f"invalid size: {value!r} (expected e.g. 512MiB, 2GiB)")
    return int(float(m.group(1)) * _SIZE_FACTORS[(m.group(2) or "B").lower()])


def is_loopback_host(host: str) -> bool:
    return host in ("localhost", "::1") or re.fullmatch(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}", host) is not None


@dataclass(frozen=True, slots=True)
class RootConfig:
    """A directory tree that users may read. ``groups`` empty = every authenticated user."""

    name: str
    path: Path
    groups: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not ROOT_NAME_RE.fullmatch(self.name) or self.name in (".", ".."):
            raise ConfigError(f"invalid root name {self.name!r} (letters, digits, '_', '-', '.'; max 64)")
        p = Path(self.path).expanduser()
        if not p.is_absolute():
            raise ConfigError(f"root {self.name!r}: path must be absolute, got {str(self.path)!r}")
        object.__setattr__(self, "path", p)
        object.__setattr__(self, "groups", frozenset(self.groups))


@dataclass(frozen=True, slots=True)
class AlertChannel:
    """A destination for the alerts of the live monitor, set by the administrator. Users choose
    channels by name and never give an address themselves: the server only ever writes to
    places its configuration names."""

    name: str
    kind: AlertKind
    #: ntfy topic address (``https://ntfy.sh/<topic>``) or webhook address.
    url: str = ""
    #: Recipients, for ``email``.
    to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "to", tuple(self.to))
        if not ROOT_NAME_RE.fullmatch(self.name):
            raise ConfigError(f"invalid alert channel name {self.name!r}")
        if self.kind not in ("ntfy", "webhook", "email"):
            raise ConfigError(f"alert channel {self.name!r}: kind must be 'ntfy', 'webhook' or 'email'")
        if self.kind == "email":
            if not self.to or not all(_ADDRESS_RE.fullmatch(a) for a in self.to):
                raise ConfigError(f"alert channel {self.name!r}: 'to' must list e-mail addresses")
        elif not _HTTP_URL_RE.fullmatch(self.url):
            raise ConfigError(f"alert channel {self.name!r}: 'url' must be an http(s) address")
        elif self.kind == "ntfy" and not urlsplit(self.url).path.strip("/"):
            raise ConfigError(f"alert channel {self.name!r}: the ntfy address must end with a topic")


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """The mail relay alerts of kind ``email`` go through."""

    host: str
    port: int = 25
    sender: str = "imagebio@localhost"
    starttls: bool = False
    username: str | None = None
    #: Name of the environment variable that holds the password (never the password itself).
    password_env: str | None = None

    def __post_init__(self) -> None:
        if not self.host or not 1 <= self.port <= 65535 or not _ADDRESS_RE.fullmatch(self.sender):
            raise ConfigError("[alerts.smtp]: host, port (1-65535) and a valid 'from' address are required")


@dataclass(frozen=True, slots=True)
class Settings:
    roots: tuple[RootConfig, ...] = ()
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: frozenset[str] = frozenset()
    trust_network: bool = False
    auth_mode: AuthMode = "none"
    user_header: str = "X-Forwarded-User"
    groups_header: str = "X-Forwarded-Groups"
    plane_cache_bytes: int = 2 * 2**30
    max_open_datasets: int = 32
    #: Largest plane (in pixels, after binning/cropping) served by one request.
    max_response_pixels: int = 8192 * 8192
    #: Directory of the live index; None: the per-user state directory (see ``live_state_dir``).
    state_dir: Path | None = None
    live_interval_s: float = 5.0
    #: A file must keep its size and date for this long, over two scans, to be complete.
    live_min_stable_s: float = 10.0
    #: False: watched directories are not scanned by this process.
    live_scan: bool = True
    #: Measure each image of a watched session (mean counts, ice ring): reads every image once.
    live_metrics: bool = True
    thumb_cache_bytes: int = 2 * 2**30
    #: Where the live monitor may send alerts.
    alert_channels: tuple[AlertChannel, ...] = ()
    smtp: SmtpConfig | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(self.roots))
        object.__setattr__(
            self, "allowed_hosts", frozenset(h.strip().lower() for h in self.allowed_hosts if h.strip())
        )
        object.__setattr__(self, "alert_channels", tuple(self.alert_channels))
        names = [r.name for r in self.roots]
        if len(set(names)) != len(names):
            raise ConfigError("root names must be unique")
        channels = [c.name for c in self.alert_channels]
        if len(set(channels)) != len(channels):
            raise ConfigError("alert channel names must be unique")
        if self.smtp is None and any(c.kind == "email" for c in self.alert_channels):
            raise ConfigError("an alert channel of kind 'email' needs [alerts.smtp]")
        if self.auth_mode not in ("none", "proxy"):
            raise ConfigError(f"auth.mode must be 'none' or 'proxy', got {self.auth_mode!r}")
        if not 0 <= self.port <= 65535:
            raise ConfigError(f"invalid port {self.port!r}")
        if self.plane_cache_bytes < 0 or self.max_open_datasets < 1 or self.max_response_pixels < 1:
            raise ConfigError("cache sizes must be positive")
        if not 0.5 <= self.live_interval_s <= 3600 or self.thumb_cache_bytes < 0:
            raise ConfigError("live.interval must be between 0.5 and 3600 seconds")
        if not 1 <= self.live_min_stable_s <= 3600:
            raise ConfigError("live.min_stable must be between 1 and 3600 seconds")
        # Without authentication, only loopback is acceptable. In "proxy" mode the identity
        # headers are trusted, so the reverse proxy must be the only possible client: loopback,
        # or a network private to the proxy that the operator explicitly vouches for.
        if not is_loopback_host(self.host) and not (self.auth_mode == "proxy" and self.trust_network):
            raise ConfigError(
                f"refusing to listen on {self.host}: bind to a loopback address (127.0.0.1) behind an "
                "authenticating reverse proxy, or set auth.mode = 'proxy' and server.trust_network = true "
                "when only the proxy can reach this address"
            )
        if self.trust_network and self.auth_mode != "proxy":
            raise ConfigError("server.trust_network requires auth.mode = 'proxy'")

    @property
    def live_state_dir(self) -> Path:
        """Where the live index is kept: ``state_dir``, else ``$XDG_STATE_HOME/imagebio``
        (``~/.local/state/imagebio``)."""
        if self.state_dir is not None:
            return self.state_dir
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        return Path(base) / "imagebio"

    @classmethod
    def local(cls, directories: Iterable[str | Path], **overrides: Any) -> Settings:
        """Standalone mode: no authentication, loopback only, one root per directory."""
        roots: list[RootConfig] = []
        used: set[str] = set()
        for d in directories:
            p = Path(d).expanduser().resolve()
            base = re.sub(r"[^A-Za-z0-9_.-]", "_", p.name).lstrip("._-") or "data"
            name, n = base[:60], 2
            while name in used:
                name, n = f"{base[:60]}-{n}", n + 1
            used.add(name)
            roots.append(RootConfig(name, p))
        return cls(roots=tuple(roots), auth_mode="none", **overrides)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Settings:
        """Build settings from parsed TOML. Unknown keys are errors (typos must not pass silently)."""

        def section(name: str, allowed: set[str]) -> Mapping[str, Any]:
            sec = data.get(name, {})
            if not isinstance(sec, Mapping):
                raise ConfigError(f"[{name}] must be a table")
            unknown = set(sec) - allowed
            if unknown:
                raise ConfigError(f"[{name}]: unknown key(s) {sorted(unknown)}")
            return sec

        unknown_top = set(data) - {"server", "auth", "cache", "live", "alerts", "roots"}
        if unknown_top:
            raise ConfigError(f"unknown section(s) {sorted(unknown_top)}")
        server = section("server", {"host", "port", "allowed_hosts", "trust_network"})
        auth = section("auth", {"mode", "user_header", "groups_header"})
        cache = section("cache", {"plane_bytes", "max_open_datasets", "max_response_pixels", "thumb_bytes"})
        live = section("live", {"state_dir", "interval", "min_stable", "scan", "metrics"})
        alerts = section("alerts", {"smtp", "channels"})
        raw_channels = alerts.get("channels", [])
        if not isinstance(raw_channels, list):
            raise ConfigError("[[alerts.channels]] must be an array of tables")
        channels = []
        for i, c in enumerate(raw_channels):
            if (
                not isinstance(c, Mapping)
                or set(c) - {"name", "kind", "url", "to"}
                or not {"name", "kind"} <= set(c)
            ):
                raise ConfigError(f"[[alerts.channels]] #{i + 1}: expected keys name, kind and url or to")
            to = c.get("to", [])
            if not isinstance(to, list):
                raise ConfigError(f"[[alerts.channels]] #{i + 1}: 'to' must be a list of addresses")
            channels.append(
                AlertChannel(
                    str(c["name"]),
                    cast(AlertKind, str(c["kind"])),
                    str(c.get("url", "")),
                    tuple(map(str, to)),
                )
            )
        smtp = None
        if "smtp" in alerts:
            raw = alerts["smtp"]
            allowed = {"host", "port", "from", "starttls", "username", "password_env"}
            if not isinstance(raw, Mapping) or set(raw) - allowed or "host" not in raw:
                raise ConfigError(f"[alerts.smtp]: expected host and optional {sorted(allowed - {'host'})}")
            smtp = SmtpConfig(
                host=str(raw["host"]),
                port=int(raw.get("port", 25)),
                sender=str(raw.get("from", "imagebio@localhost")),
                starttls=bool(raw.get("starttls", False)),
                username=str(raw["username"]) if "username" in raw else None,
                password_env=str(raw["password_env"]) if "password_env" in raw else None,
            )
        raw_roots = data.get("roots", [])
        if not isinstance(raw_roots, list):
            raise ConfigError("[[roots]] must be an array of tables")
        roots = []
        for i, r in enumerate(raw_roots):
            if (
                not isinstance(r, Mapping)
                or set(r) - {"name", "path", "groups"}
                or not {"name", "path"} <= set(r)
            ):
                raise ConfigError(f"[[roots]] #{i + 1}: expected keys name, path and optional groups")
            roots.append(
                RootConfig(str(r["name"]), Path(str(r["path"])), frozenset(map(str, r.get("groups", []))))
            )
        defaults = cls()
        return cls(
            roots=tuple(roots),
            host=str(server.get("host", defaults.host)),
            port=int(server.get("port", defaults.port)),
            allowed_hosts=frozenset(map(str, server.get("allowed_hosts", []))),
            trust_network=bool(server.get("trust_network", False)),
            auth_mode=cast(AuthMode, str(auth.get("mode", defaults.auth_mode))),
            user_header=str(auth.get("user_header", defaults.user_header)),
            groups_header=str(auth.get("groups_header", defaults.groups_header)),
            plane_cache_bytes=parse_size(cache.get("plane_bytes", defaults.plane_cache_bytes)),
            max_open_datasets=int(cache.get("max_open_datasets", defaults.max_open_datasets)),
            max_response_pixels=int(cache.get("max_response_pixels", defaults.max_response_pixels)),
            state_dir=Path(str(live["state_dir"])) if "state_dir" in live else None,
            live_interval_s=float(live.get("interval", defaults.live_interval_s)),
            live_min_stable_s=float(live.get("min_stable", defaults.live_min_stable_s)),
            live_scan=bool(live.get("scan", True)),
            live_metrics=bool(live.get("metrics", True)),
            thumb_cache_bytes=parse_size(cache.get("thumb_bytes", defaults.thumb_cache_bytes)),
            alert_channels=tuple(channels),
            smtp=smtp,
        )

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        try:
            with Path(path).open("rb") as f:
                data = tomllib.load(f)
        except OSError as e:
            raise ConfigError(f"cannot read configuration file: {e.strerror or e}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"invalid TOML: {e}") from None
        return cls.from_mapping(data)
