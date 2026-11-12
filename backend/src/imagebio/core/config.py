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

    [processing]                    # optional: programs run on each exposure of a watched session
    slots = 1                       # how many run at once (one per GPU)

    [processing.motion]             # on the movie; see docs/exploitation.md for a full example
    argv = ["/opt/MotionCor3/MotionCor3", "-InMrc", "{movie}", "-OutMrc", "{out}/aligned.mrc", "..."]
    result = "{out}/aligned.star"   # what the parser reads
    parser = "motion-star"          # or "motion-log"
    image = "{out}/aligned.mrc"     # the sum it wrote, handed to the CTF step
    timeout = 600

    [processing.ctf]                # on that sum, else on the image the microscope integrated
    argv = ["/opt/ctffind/bin/ctffind"]
    stdin = ["{image}", "{out}/diag.mrc", "{apix}", "{kv}", "{cs}", "{amp}", "..."]
    result = "{out}/diag.txt"
    parser = "ctffind"

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

from imagebio.tools.templates import check_template

ROOT_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ADDRESS_RE: Final = re.compile(r"[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}")
_HTTP_URL_RE: Final = re.compile(r"https?://[^\s/?#]+(/[^\s]*)?")
_SIZE_RE: Final = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B|B)?\s*", re.IGNORECASE)
_SIZE_FACTORS: Final[Mapping[str, int]] = {
    "b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
}  # fmt: skip

AuthMode = Literal["none", "proxy"]
#: What a microscope parameter may be: accelerating voltage (kV), spherical aberration (mm),
#: amplitude contrast (fraction).
MICROSCOPE_BOUNDS: Final[Mapping[str, tuple[float, float]]] = {
    "kv": (20.0, 1500.0),
    "cs_mm": (0.0, 20.0),
    "amplitude_contrast": (0.0, 0.5),
}
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
    """A directory tree that users may read. ``groups`` empty = every authenticated user.

    ``kv``, ``cs_mm`` and ``amplitude_contrast``: the microscope whose sessions this tree holds,
    when it is not the one of ``[microscope]`` (a lab with a 200 kV and a 300 kV microscope)."""

    name: str
    path: Path
    groups: frozenset[str] = frozenset()
    kv: float | None = None
    cs_mm: float | None = None
    amplitude_contrast: float | None = None

    def __post_init__(self) -> None:
        if not ROOT_NAME_RE.fullmatch(self.name) or self.name in (".", ".."):
            raise ConfigError(f"invalid root name {self.name!r} (letters, digits, '_', '-', '.'; max 64)")
        p = Path(self.path).expanduser()
        if not p.is_absolute():
            raise ConfigError(f"root {self.name!r}: path must be absolute, got {str(self.path)!r}")
        object.__setattr__(self, "path", p)
        object.__setattr__(self, "groups", frozenset(self.groups))
        for key, value in (
            ("kv", self.kv),
            ("cs_mm", self.cs_mm),
            ("amplitude_contrast", self.amplitude_contrast),
        ):
            if value is None:
                continue
            low, high = MICROSCOPE_BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
                raise ConfigError(f"root {self.name!r}: {key} must be between {low:g} and {high:g}")
            object.__setattr__(self, key, float(value))


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


_MOVIE_FORMATS: Final = frozenset({"mrc", "mrcs", "tif", "tiff", "eer"})
_MAX_ARGUMENTS: Final = 256
_MAX_ARGUMENT_CHARS: Final = 4096
_MAX_STDIN_LINES: Final = 64


@dataclass(frozen=True, slots=True)
class ToolStep:
    """A program the Live mode may run on each exposure, as the administrator declares it: an
    argument list (never a command line: nothing is split or handed to a shell) whose arguments
    may name placeholders (``imagebio.tools.templates.PLACEHOLDERS``)."""

    #: The program (an absolute path) and its arguments.
    argv: tuple[str, ...]
    #: The file the parser reads once the program ended.
    result: str
    #: ``ctffind``, ``motion-star`` or ``motion-log`` (``imagebio.tools.parsers``).
    parser: str
    #: Lines typed to the program (CTFFIND asks its questions on standard input).
    stdin: tuple[str, ...] = ()
    #: Argument lists for movies of a given extension (``eer``, ``tiff``...), instead of ``argv``.
    formats: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Motion step: the sum it writes, which the CTF step then takes. Inside ``{out}``.
    image: str = ""
    timeout_s: float = 300.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "stdin", tuple(self.stdin))
        object.__setattr__(self, "formats", {str(k).lower(): tuple(v) for k, v in self.formats.items()})
        for extension, argv in (("", self.argv), *self.formats.items()):
            where = f"for .{extension} movies" if extension else "argv"
            if extension and extension not in _MOVIE_FORMATS:
                raise ConfigError(
                    f"processing: unknown movie format {extension!r} ({sorted(_MOVIE_FORMATS)})"
                )
            if not argv or len(argv) > _MAX_ARGUMENTS or not all(isinstance(a, str) for a in argv):
                raise ConfigError(f"processing: {where} must be a list of 1 to {_MAX_ARGUMENTS} strings")
            if not Path(argv[0]).is_absolute() or "{" in argv[0]:
                raise ConfigError(f"processing: {where} must start with the absolute path of the program")
        if len(self.stdin) > _MAX_STDIN_LINES or not all(isinstance(line, str) for line in self.stdin):
            raise ConfigError(f"processing: stdin must be a list of at most {_MAX_STDIN_LINES} strings")
        if not 1 <= self.timeout_s <= 86400:
            raise ConfigError("processing: timeout must be between 1 and 86400 seconds")
        for template in self.templates():
            if len(template) > _MAX_ARGUMENT_CHARS:
                raise ConfigError(f"processing: an argument is longer than {_MAX_ARGUMENT_CHARS} characters")
            try:
                check_template(template)
            except ValueError as e:
                raise ConfigError(f"processing: {e} in {template!r}") from None
        if not self.result.startswith("{out}/") or (self.image and not self.image.startswith("{out}/")):
            raise ConfigError("processing: result and image must be files of {out}/")

    def templates(self) -> tuple[str, ...]:
        """Every template of the step."""
        every = [*self.argv, *self.stdin, self.result, *(a for argv in self.formats.values() for a in argv)]
        return (*every, self.image) if self.image else tuple(every)

    def names(self, placeholder: str) -> bool:
        return any(f"{{{placeholder}}}" in template for template in self.templates())


@dataclass(frozen=True, slots=True)
class ProcessingConfig:
    """What the Live mode runs on the exposures of a session whose user asked for it: a motion
    correction on the movie, then a CTF estimation on the micrograph. Either may be absent."""

    motion: ToolStep | None = None
    ctf: ToolStep | None = None
    #: Programs running at once (one per GPU, as a rule).
    slots: int = 1
    #: Where each run gets its directory; None: ``work`` in the state directory.
    work_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.motion is None and self.ctf is None:
            raise ConfigError("[processing] declares neither [processing.motion] nor [processing.ctf]")
        if not 1 <= self.slots <= 16:
            raise ConfigError("processing.slots must be between 1 and 16")
        if self.work_dir is not None and not Path(self.work_dir).is_absolute():
            raise ConfigError("processing.work_dir must be an absolute path")
        if self.motion is not None:
            if self.motion.parser not in ("motion-star", "motion-log"):
                raise ConfigError("processing.motion: parser must be 'motion-star' or 'motion-log'")
            argvs = (self.motion.argv, *self.motion.formats.values())
            if self.motion.names("image") or not all(any("{movie}" in a for a in argv) for argv in argvs):
                raise ConfigError("processing.motion: every argv must name {movie}, and none {image}")
        if self.ctf is not None:
            if self.ctf.parser != "ctffind":
                raise ConfigError("processing.ctf: parser must be 'ctffind'")
            if not self.ctf.names("image") or self.ctf.formats or self.ctf.image:
                raise ConfigError("processing.ctf: it must name {image}, and has neither formats nor image")
            if any(self.ctf.names(name) for name in ("movie", "dose", "frames")):
                raise ConfigError(
                    "processing.ctf works on the micrograph: {movie}, {dose} and {frames} are not its"
                )


@dataclass(frozen=True, slots=True)
class Settings:
    roots: tuple[RootConfig, ...] = ()
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: frozenset[str] = frozenset()
    trust_network: bool = False
    #: Serve ``/metrics`` (Prometheus text format): to this computer only, or to whoever
    #: presents the token of the environment variable IMAGEBIO_METRICS_TOKEN.
    metrics: bool = False
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
    #: Estimate the CTF of each image of a watched session (defocus, how far the rings go).
    live_ctf: bool = True
    #: The microscope, for the CTF: accelerating voltage (kV, used when a file does not say),
    #: spherical aberration (mm) and amplitude contrast.
    kv: float = 300.0
    cs_mm: float = 2.7
    amplitude_contrast: float = 0.07
    #: Also measure how far each movie drifted between its first and last frames: reads eight
    #: frames of every movie that arrives. Off unless asked for.
    live_drift: bool = False
    #: Mean free path of electrons in ice for this microscope (aperture-limited scattering), nm:
    #: ice thickness is then shown in nm. None: relative values only, unless a user gives one.
    live_mfp_nm: float | None = None
    #: A quality report is made when a watched session has received nothing for this long
    #: (seconds) after images arrived; 0: only when the watch is stopped, or asked for.
    live_report_idle_s: float = 7200.0
    thumb_cache_bytes: int = 2 * 2**30
    #: Where the live monitor may send alerts.
    alert_channels: tuple[AlertChannel, ...] = ()
    smtp: SmtpConfig | None = None
    #: Programs run on the exposures of a watched session (motion correction, CTF); None: none.
    processing: ProcessingConfig | None = None
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
        if not (20 <= self.kv <= 1500 and 0 <= self.cs_mm <= 20 and 0 <= self.amplitude_contrast <= 0.5):
            raise ConfigError(
                "microscope: kv must be 20 to 1500, cs_mm 0 to 20 and amplitude_contrast 0 to 0.5"
            )
        if self.live_mfp_nm is not None and not 50 <= self.live_mfp_nm <= 5000:
            raise ConfigError("live.mean_free_path_nm must be between 50 and 5000 (nanometres)")
        if not (self.live_report_idle_s == 0 or 600 <= self.live_report_idle_s <= 7 * 86400):
            raise ConfigError("live.report_after_idle_min must be 0 (never) or between 10 and 10080 minutes")
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

        unknown_top = set(data) - {
            "server",
            "auth",
            "cache",
            "live",
            "microscope",
            "alerts",
            "processing",
            "roots",
        }
        if unknown_top:
            raise ConfigError(f"unknown section(s) {sorted(unknown_top)}")
        server = section("server", {"host", "port", "allowed_hosts", "trust_network", "metrics"})
        auth = section("auth", {"mode", "user_header", "groups_header"})
        cache = section("cache", {"plane_bytes", "max_open_datasets", "max_response_pixels", "thumb_bytes"})
        live = section(
            "live",
            {
                "state_dir",
                "interval",
                "min_stable",
                "scan",
                "metrics",
                "ctf",
                "drift",
                "mean_free_path_nm",
                "report_after_idle_min",
            },
        )
        microscope = section("microscope", {"kv", "cs_mm", "amplitude_contrast"})
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
        processing = None
        if "processing" in data:
            declared = section("processing", {"slots", "work_dir", "motion", "ctf"})
            steps: dict[str, ToolStep | None] = {"motion": None, "ctf": None}
            for kind in steps:
                if kind not in declared:
                    continue
                raw_step = declared[kind]
                keys = {"argv", "stdin", "result", "parser", "image", "timeout", "formats"}
                lists = (
                    [raw_step.get(k, []) for k in ("argv", "stdin")] if isinstance(raw_step, Mapping) else []
                )
                formats = raw_step.get("formats", {}) if isinstance(raw_step, Mapping) else None
                if (
                    not isinstance(raw_step, Mapping)
                    or set(raw_step) - keys
                    or not {"argv", "result", "parser"} <= set(raw_step)
                    or not all(isinstance(x, list) for x in lists)
                    or not isinstance(formats, Mapping)
                    or not all(isinstance(v, list) for v in formats.values())
                ):
                    raise ConfigError(
                        f"[processing.{kind}]: expected argv (a list), result, parser and optional "
                        f"{sorted(keys - {'argv', 'result', 'parser'})}"
                    )
                try:
                    timeout = float(raw_step.get("timeout", 300))
                except (TypeError, ValueError):
                    raise ConfigError(f"[processing.{kind}]: timeout must be a number of seconds") from None
                steps[kind] = ToolStep(
                    argv=tuple(raw_step["argv"]),
                    result=str(raw_step["result"]),
                    parser=str(raw_step["parser"]),
                    stdin=tuple(raw_step.get("stdin", ())),
                    formats={str(k): tuple(v) for k, v in formats.items()},
                    image=str(raw_step.get("image", "")),
                    timeout_s=timeout,
                )
            slots = declared.get("slots", 1)
            if isinstance(slots, bool) or not isinstance(slots, int):
                raise ConfigError("processing.slots must be a whole number")
            processing = ProcessingConfig(
                motion=steps["motion"],
                ctf=steps["ctf"],
                slots=slots,
                work_dir=Path(str(declared["work_dir"])) if "work_dir" in declared else None,
            )
        raw_roots = data.get("roots", [])
        if not isinstance(raw_roots, list):
            raise ConfigError("[[roots]] must be an array of tables")
        roots = []
        for i, r in enumerate(raw_roots):
            if (
                not isinstance(r, Mapping)
                or set(r) - {"name", "path", "groups", "kv", "cs_mm", "amplitude_contrast"}
                or not {"name", "path"} <= set(r)
            ):
                raise ConfigError(
                    f"[[roots]] #{i + 1}: expected keys name, path and optional groups, kv, cs_mm, "
                    "amplitude_contrast"
                )
            roots.append(
                RootConfig(
                    str(r["name"]),
                    Path(str(r["path"])),
                    frozenset(map(str, r.get("groups", []))),
                    kv=r.get("kv"),
                    cs_mm=r.get("cs_mm"),
                    amplitude_contrast=r.get("amplitude_contrast"),
                )
            )
        defaults = cls()
        return cls(
            roots=tuple(roots),
            host=str(server.get("host", defaults.host)),
            port=int(server.get("port", defaults.port)),
            allowed_hosts=frozenset(map(str, server.get("allowed_hosts", []))),
            trust_network=bool(server.get("trust_network", False)),
            metrics=bool(server.get("metrics", False)),
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
            live_drift=bool(live.get("drift", False)),
            live_ctf=bool(live.get("ctf", True)),
            kv=float(microscope.get("kv", defaults.kv)),
            cs_mm=float(microscope.get("cs_mm", defaults.cs_mm)),
            amplitude_contrast=float(microscope.get("amplitude_contrast", defaults.amplitude_contrast)),
            live_mfp_nm=float(live["mean_free_path_nm"]) if "mean_free_path_nm" in live else None,
            live_report_idle_s=60
            * float(live.get("report_after_idle_min", defaults.live_report_idle_s / 60)),
            thumb_cache_bytes=parse_size(cache.get("thumb_bytes", defaults.thumb_cache_bytes)),
            alert_channels=tuple(channels),
            smtp=smtp,
            processing=processing,
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
