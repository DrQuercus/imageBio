"""Alerts of the live monitor: telling somebody that a collection needs attention.

A user arms *rules* on one of their watches (no image for N minutes, disk almost full, rate
dropping) and picks *channels* among those the administrator configured (ntfy, webhook,
e-mail). Users never give an address: the server only writes to places its own configuration
names.

Each condition is a small state machine (:class:`AlertState`, :func:`step`): when it starts
to hold, one notice is sent to each channel; when it stops, one "resolved" notice is sent to
the channels that were told. Nothing is repeated while it lasts, and a channel that fails is
tried again on its own: the others are not told twice. Rules are judged right after each scan
cycle (:func:`evaluate` is pure: a reading in, the conditions that hold out), so a notice
leaves within one cycle of its threshold being crossed.

A value that hovers around its threshold must not send a notice at every cycle: a condition is
harder to leave than to enter (:func:`evaluate` is told what was already announced), and the
noisy ones must last, or be gone, for a while before anything is said (:data:`HOLD_S`).
"""

from __future__ import annotations

import json
import os
import smtplib
import time
import urllib.error
import urllib.request
from collections.abc import Collection
from dataclasses import asdict, dataclass, replace
from email.message import EmailMessage
from typing import Any, Final

from imagebio.core.config import AlertChannel, SmtpConfig
from imagebio.core.errors import ImageBioError, InvalidRequestError

__all__ = [
    "CONDITIONS",
    "HOLD_S",
    "AlertDeliveryError",
    "AlertRules",
    "AlertState",
    "Notice",
    "Notifier",
    "Reading",
    "evaluate",
    "notice_for",
    "step",
]

#: Conditions an alert can be about, in the order they are reported.
CONDITIONS: Final = ("no_image", "disk", "rate_drop", "scanner")
#: A rate is only said to drop when the last hour saw at least this many images: under one
#: image a minute, five minutes hold too few images to tell a drop from chance (the "no image
#: for N minutes" rule is the one for slow sessions).
_MIN_HOURLY: Final = 60
#: For each condition: how long it must hold before it is told, and how long it must be gone
#: before it is told resolved (seconds). Silence and a failing scanner are told at once.
HOLD_S: Final[dict[str, tuple[float, float]]] = {
    "no_image": (0.0, 0.0),
    "disk": (0.0, 120.0),
    "rate_drop": (120.0, 120.0),
    "scanner": (0.0, 60.0),
}
_USER_AGENT: Final = "imagebio-live"


class AlertDeliveryError(ImageBioError):
    """A notice could not be delivered. The message is public: no address, no credentials."""

    status = 502
    code = "alert_not_delivered"


def _in(value: float | None, low: float, high: float, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise InvalidRequestError(f"{what} must be between {low:g} and {high:g}")
    return float(value)


@dataclass(frozen=True, slots=True)
class AlertRules:
    """What a user wants to be told about one watch. A threshold left at None is not watched."""

    channels: tuple[str, ...] = ()
    #: Alert when no image arrived for this many minutes.
    no_image_min: float | None = None
    #: Alert when the free space of the volume falls under this percentage.
    disk_free_pct: float | None = None
    #: Alert when the rate over five minutes falls under this percentage of the hourly rate.
    rate_drop_pct: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", tuple(dict.fromkeys(self.channels)))
        object.__setattr__(self, "no_image_min", _in(self.no_image_min, 1, 1440, "no_image (minutes)"))
        object.__setattr__(self, "disk_free_pct", _in(self.disk_free_pct, 0.1, 50, "disk_free (percent)"))
        object.__setattr__(self, "rate_drop_pct", _in(self.rate_drop_pct, 1, 99, "rate_drop (percent)"))

    @property
    def armed(self) -> bool:
        """Whether anything can be sent: at least one channel."""
        return bool(self.channels)

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "channels": list(self.channels)}

    @classmethod
    def from_json(cls, text: str | None) -> AlertRules:
        if not text:
            return cls()
        data = json.loads(text)
        return cls(
            tuple(data.get("channels", ())),
            data.get("no_image_min"),
            data.get("disk_free_pct"),
            data.get("rate_drop_pct"),
        )


@dataclass(frozen=True, slots=True)
class Reading:
    """The state of a watched session, as the rules are judged on it. Times are on this
    server's clock (when the scanner accepted an image), not the microscope's."""

    now: float
    #: When the alerts were set: silence is counted from then at the earliest.
    armed_at: float
    #: When the last image arrived while watching, or None if none did yet.
    last_arrival: float | None
    acquired: int
    #: Arrivals per hour, over the last five minutes and over the last hour.
    rate_5min: int
    rate_hour: int
    disk_free_pct: float | None = None
    disk_free_bytes: int | None = None
    scanner_error: str | None = None


def _clock(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60:02d} min"


def _gigabytes(n: int | None) -> str:
    return "" if n is None else f" ({n / 1e9:.0f} Go)"


def evaluate(rules: AlertRules, r: Reading, told: Collection[str] = ()) -> dict[str, str | None]:
    """For each condition, the sentence that describes it when it holds, None when it does not.
    A condition whose threshold is not set never holds; the scanner failing is always told.

    ``told``: the conditions that were already announced. Those are left less easily than they
    are entered: the disk must be clearly above its threshold again, and a rate is back when
    images arrive, not when the hour it is compared to has emptied."""
    out: dict[str, str | None] = dict.fromkeys(CONDITIONS)
    if rules.no_image_min is not None:
        since = max(r.armed_at, r.last_arrival if r.last_arrival is not None else r.armed_at)
        if r.now - since >= rules.no_image_min * 60:
            last = (
                f"dernière image à {_clock(r.last_arrival)}"
                if r.last_arrival is not None
                else "aucune depuis le réglage des alertes"
            )
            out["no_image"] = f"Aucune image depuis {_minutes(r.now - since)} ({last})."
    if rules.disk_free_pct is not None and r.disk_free_pct is not None:
        limit = rules.disk_free_pct
        if "disk" in told:
            limit += max(0.5, limit / 10)
        if r.disk_free_pct < limit:
            out["disk"] = f"Il reste {r.disk_free_pct:.1f} % d'espace libre{_gigabytes(r.disk_free_bytes)}."
    if rules.rate_drop_pct is not None:
        was_told = "rate_drop" in told
        reference = max(r.rate_hour, _MIN_HOURLY) if was_told else r.rate_hour
        if (was_told or r.rate_hour >= _MIN_HOURLY) and r.rate_5min < reference * rules.rate_drop_pct / 100:
            out["rate_drop"] = f"{r.rate_5min} images/h sur 5 minutes, contre {r.rate_hour} dans l'heure."
    if r.scanner_error:
        out["scanner"] = f"Le dossier ne peut plus être lu : {r.scanner_error}"
    return out


@dataclass(frozen=True, slots=True)
class AlertState:
    """Where one condition of one watch stands.

    ``pending``: it holds, not for long enough to be told. ``firing``: it was told, or is being
    told (``todo``: the channels that did not take the notice yet). ``resolving``: it is gone
    and the channels that had been told are being told so."""

    condition: str
    since: float  # when it started to hold
    phase: str = "pending"
    clear_since: float | None = None  # firing: since when it is no longer seen
    delivered: tuple[str, ...] = ()  # channels that got the "firing" notice
    todo: tuple[str, ...] = ()  # channels still owed the notice of this phase
    attempts: int = 0  # cycles in which the notice of this phase was sent
    error: str | None = None  # public message of the last failed delivery

    @property
    def told(self) -> bool:
        """Whether somebody may have been told that the condition holds."""
        return self.phase == "firing"


def step(
    state: AlertState | None, condition: str, holds: bool, now: float, channels: tuple[str, ...]
) -> AlertState | None:
    """The state of ``condition`` after one more reading. Pure: delivering the notices that
    the new state owes (``todo``) is the caller's business. None: nothing to remember."""
    hold_on, hold_off = HOLD_S[condition]
    if state is None:
        if not holds:
            return None
        state = AlertState(condition, now)
    if state.phase == "pending":
        if not holds:
            return None
        if now - state.since >= hold_on:
            return replace(state, phase="firing", todo=channels, attempts=0, error=None)
        return state
    if state.phase == "firing":
        if holds:
            return replace(state, clear_since=None) if state.clear_since is not None else state
        clear_since = state.clear_since if state.clear_since is not None else now
        if now - clear_since < hold_off:
            return replace(state, clear_since=clear_since)
        if not state.delivered:
            return None  # nobody was told that it started
        return replace(
            state, phase="resolving", clear_since=clear_since, todo=state.delivered, attempts=0, error=None
        )
    return state  # resolving: until its notice is delivered or given up


@dataclass(frozen=True, slots=True)
class Notice:
    """One message to deliver."""

    condition: str  # one of CONDITIONS, or "test"
    firing: bool  # False: the condition no longer holds
    title: str
    body: str
    session: str  # logical path of the watched directory
    at: float


_TITLES: Final = {
    "no_image": ("plus d'image", "les images arrivent de nouveau"),
    "disk": ("disque presque plein", "de la place est revenue sur le disque"),
    "rate_drop": ("le débit chute", "le débit est revenu"),
    "scanner": ("la surveillance échoue", "la surveillance a repris"),
}


def notice_for(condition: str, firing: bool, sentence: str | None, session: str, r: Reading) -> Notice:
    """The notice that announces a condition starting (``firing``) or ending."""
    started, ended = _TITLES[condition]
    count = f"{r.acquired} image{'s' if r.acquired > 1 else ''} dans la session."
    body = f"{sentence} {count}" if firing and sentence else count
    return Notice(
        condition, firing, f"imageBio : {started if firing else ended}", f"{session} — {body}", session, r.now
    )


class Notifier:
    """Delivers notices to the configured channels. One short attempt per call: the caller
    decides whether to try again."""

    def __init__(self, smtp: SmtpConfig | None = None, timeout: float = 5.0) -> None:
        self._smtp = smtp
        self._timeout = timeout

    def send(self, channel: AlertChannel, notice: Notice) -> None:
        """Deliver ``notice`` to ``channel``. Raises :class:`AlertDeliveryError`."""
        try:
            if channel.kind == "ntfy":
                self._ntfy(channel, notice)
            elif channel.kind == "webhook":
                self._post(channel.url, self._payload(notice))
            else:
                self._email(channel, notice)
        except AlertDeliveryError:
            raise
        except (OSError, smtplib.SMTPException, ValueError) as e:
            # the reason, never the address: a topic or a webhook address is a secret
            raise AlertDeliveryError(f"channel {channel.name!r}: {type(e).__name__}") from None

    @staticmethod
    def _payload(notice: Notice) -> dict[str, Any]:
        return {
            "source": "imagebio",
            "condition": notice.condition,
            "state": "firing" if notice.firing else "resolved",
            "title": notice.title,
            "message": notice.body,
            "session": notice.session,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(notice.at)),
        }

    def _post(self, url: str, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(  # noqa: S310 - http(s) only, checked when the configuration is read
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                response.read(1024)
        except urllib.error.HTTPError as e:
            raise AlertDeliveryError(f"the alert service answered {e.code}") from None

    def _ntfy(self, channel: AlertChannel, notice: Notice) -> None:
        # ntfy's JSON form: posted to the server, the topic in the body (text travels as UTF-8)
        server, _, topic = channel.url.rstrip("/").rpartition("/")
        self._post(
            server + "/",
            {
                "topic": topic,
                "title": notice.title,
                "message": notice.body,
                "priority": 4 if notice.firing else 3,
                "tags": ["warning" if notice.firing else "white_check_mark"],
            },
        )

    def _email(self, channel: AlertChannel, notice: Notice) -> None:
        if self._smtp is None:
            raise AlertDeliveryError(f"channel {channel.name!r}: no mail relay is configured")
        message = EmailMessage()
        message["Subject"] = notice.title
        message["From"] = self._smtp.sender
        message["To"] = ", ".join(channel.to)
        message.set_content(notice.body + "\n")
        with smtplib.SMTP(self._smtp.host, self._smtp.port, timeout=self._timeout) as smtp:
            if self._smtp.starttls:
                smtp.starttls()
            if self._smtp.username:
                password = os.environ.get(self._smtp.password_env or "", "")
                smtp.login(self._smtp.username, password)
            smtp.send_message(message)
