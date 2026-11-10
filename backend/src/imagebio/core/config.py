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

    [[roots]]
    name = "glacios"                # first component of every client-visible path
    path = "/srv/glacios"
    groups = ["cryoem"]             # empty or missing: every authenticated user

or from ``Settings.local([...])`` for the standalone mode (``imagebio serve --local DIR``).
Clients never see or send server paths: they use *logical* paths, ``<root name>/<relative path>``.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

ROOT_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SIZE_RE: Final = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B|B)?\s*", re.IGNORECASE)
_SIZE_FACTORS: Final[Mapping[str, int]] = {
    "b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
}  # fmt: skip

AuthMode = Literal["none", "proxy"]


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
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(self.roots))
        object.__setattr__(
            self, "allowed_hosts", frozenset(h.strip().lower() for h in self.allowed_hosts if h.strip())
        )
        names = [r.name for r in self.roots]
        if len(set(names)) != len(names):
            raise ConfigError("root names must be unique")
        if self.auth_mode not in ("none", "proxy"):
            raise ConfigError(f"auth.mode must be 'none' or 'proxy', got {self.auth_mode!r}")
        if not 0 <= self.port <= 65535:
            raise ConfigError(f"invalid port {self.port!r}")
        if self.plane_cache_bytes < 0 or self.max_open_datasets < 1 or self.max_response_pixels < 1:
            raise ConfigError("cache sizes must be positive")
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

        unknown_top = set(data) - {"server", "auth", "cache", "roots"}
        if unknown_top:
            raise ConfigError(f"unknown section(s) {sorted(unknown_top)}")
        server = section("server", {"host", "port", "allowed_hosts", "trust_network"})
        auth = section("auth", {"mode", "user_header", "groups_header"})
        cache = section("cache", {"plane_bytes", "max_open_datasets", "max_response_pixels"})
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
