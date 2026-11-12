"""Alert rules, their judgement, and what is sent to each kind of channel."""

from __future__ import annotations

import json
import smtplib
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from imagebio.core.config import AlertChannel, ConfigError, Settings, SmtpConfig
from imagebio.core.errors import InvalidRequestError
from imagebio.live.alerts import (
    CONDITIONS,
    HOLD_S,
    AlertDeliveryError,
    AlertRules,
    AlertState,
    Notice,
    Notifier,
    Reading,
    evaluate,
    notice_for,
    step,
)
from imagebio.live.index import SCHEMA_VERSION, Index, IndexVersionError

NOW = 1_800_000_000.0


def reading(**patch: object) -> Reading:
    base = {
        "now": NOW,
        "armed_at": NOW - 3600,
        "last_arrival": NOW - 30,
        "acquired": 250,
        "rate_5min": 600,
        "rate_hour": 580,
    }
    return Reading(**{**base, **patch})  # type: ignore[arg-type]


# ── rules ─────────────────────────────────────────────────────────────────────


def test_rules_round_trip_and_ignore_repeated_channels() -> None:
    rules = AlertRules(("phone", "team", "phone"), no_image_min=10, disk_free_pct=5, rate_drop_pct=50)
    assert rules.channels == ("phone", "team") and rules.armed
    assert AlertRules.from_json(json.dumps(rules.to_json())) == rules
    assert AlertRules.from_json(None) == AlertRules() and not AlertRules().armed
    assert AlertRules.from_json("") == AlertRules()


@pytest.mark.parametrize(
    "kw",
    [
        {"no_image_min": 0.5},
        {"no_image_min": 1441},
        {"disk_free_pct": 0},
        {"disk_free_pct": 51},
        {"rate_drop_pct": 0},
        {"rate_drop_pct": 100},
        {"no_image_min": float("nan")},
        {"no_image_min": True},
        {"disk_free_pct": "5"},
    ],
)
def test_thresholds_out_of_range_are_refused(kw: dict[str, object]) -> None:
    with pytest.raises(InvalidRequestError):
        AlertRules(("phone",), **kw)  # type: ignore[arg-type]


# ── judgement ─────────────────────────────────────────────────────────────────


def test_nothing_holds_without_thresholds_or_while_all_is_well() -> None:
    assert evaluate(AlertRules(("phone",)), reading()) == dict.fromkeys(CONDITIONS)
    watched = AlertRules(("phone",), no_image_min=10, disk_free_pct=5, rate_drop_pct=50)
    assert evaluate(watched, reading(disk_free_pct=40.0)) == dict.fromkeys(CONDITIONS)


def test_silence_is_counted_from_the_last_image_or_from_the_setting_of_the_alert() -> None:
    rules = AlertRules(("phone",), no_image_min=10)
    assert evaluate(rules, reading(last_arrival=NOW - 599))["no_image"] is None
    said = evaluate(rules, reading(last_arrival=NOW - 600))["no_image"]
    assert said is not None and said.startswith("Aucune image depuis 10 min (dernière image à ")
    # set five minutes ago on a session that has been silent for an hour: five minutes of silence so far
    assert evaluate(rules, reading(last_arrival=NOW - 3600, armed_at=NOW - 300))["no_image"] is None
    never = evaluate(rules, reading(last_arrival=None, armed_at=NOW - 7500))["no_image"]
    assert never == "Aucune image depuis 2 h 05 min (aucune depuis le réglage des alertes)."


def test_disk_and_rate() -> None:
    rules = AlertRules(("phone",), disk_free_pct=5, rate_drop_pct=50)
    assert evaluate(rules, reading(disk_free_pct=5.0))["disk"] is None
    assert (
        evaluate(rules, reading(disk_free_pct=4.94, disk_free_bytes=120_000_000_000))["disk"]
        == "Il reste 4.9 % d'espace libre (120 Go)."
    )
    assert evaluate(rules, reading(disk_free_pct=None))["disk"] is None  # unknown is not "full"
    assert (
        evaluate(rules, reading(rate_5min=300, rate_hour=600))["rate_drop"] is None
    )  # exactly half: not under
    assert (
        evaluate(rules, reading(rate_5min=288, rate_hour=600))["rate_drop"]
        == "288 images/h sur 5 minutes, contre 600 dans l'heure."
    )
    assert (
        evaluate(rules, reading(rate_5min=0, rate_hour=59))["rate_drop"] is None
    )  # too few images to speak of a rate
    assert evaluate(rules, reading(rate_5min=0, rate_hour=60))["rate_drop"] is not None


def test_a_condition_is_harder_to_leave_than_to_enter() -> None:
    """Once announced, the disk must be clearly above its threshold, and the rate is compared
    to an hour that counts for at least one image a minute: it comes back with images."""
    rules = AlertRules(("phone",), disk_free_pct=5, rate_drop_pct=50)
    told = {"disk", "rate_drop"}
    assert evaluate(rules, reading(disk_free_pct=5.2))["disk"] is None
    assert evaluate(rules, reading(disk_free_pct=5.2), told)["disk"] is not None
    assert evaluate(rules, reading(disk_free_pct=5.49), told)["disk"] is not None
    assert evaluate(rules, reading(disk_free_pct=5.5), told)["disk"] is None
    small = AlertRules(("phone",), disk_free_pct=0.2)  # a small threshold still has a margin
    assert evaluate(small, reading(disk_free_pct=0.6), {"disk"})["disk"] is not None
    assert evaluate(small, reading(disk_free_pct=0.7), {"disk"})["disk"] is None

    assert evaluate(rules, reading(rate_5min=0, rate_hour=0))["rate_drop"] is None
    assert evaluate(rules, reading(rate_5min=0, rate_hour=0), told)["rate_drop"] is not None
    assert evaluate(rules, reading(rate_5min=24, rate_hour=2), told)["rate_drop"] is not None
    assert evaluate(rules, reading(rate_5min=36, rate_hour=3), told)["rate_drop"] is None
    assert evaluate(rules, reading(rate_5min=288, rate_hour=600), told)["rate_drop"] is not None
    assert evaluate(rules, reading(rate_5min=300, rate_hour=600), told)["rate_drop"] is None
    # what was told of one condition changes nothing for the others
    assert evaluate(rules, reading(disk_free_pct=5.2), {"rate_drop", "no_image"})["disk"] is None


def walk(condition: str, readings: list[tuple[float, bool]], channels=("phone", "hook")) -> list[str]:
    """The phases a condition goes through over ``readings`` (seconds, holds), delivering at once."""
    state: AlertState | None = None
    phases = []
    for at, holds in readings:
        state = step(state, condition, holds, NOW + at, channels)
        if state is not None and state.todo:  # what the service does: deliver, then forget a resolved one
            delivered = state.delivered + state.todo if state.phase == "firing" else state.delivered
            state = AlertState(state.condition, state.since, state.phase, state.clear_since, delivered)
            if state.phase == "resolving":
                phases.append("resolved")
                state = None
                continue
        phases.append(state.phase if state is not None else "-")
    return phases


def test_the_steps_of_a_condition() -> None:
    assert HOLD_S.keys() == set(CONDITIONS)
    # silence is told at once, and resolved at once
    assert walk("no_image", [(0, False), (5, True), (10, True), (15, False), (20, False)]) == [
        "-",
        "firing",
        "firing",
        "resolved",
        "-",
    ]
    # a rate must stay low for two minutes before it is told, and a blip is never told
    low = [(t, True) for t in range(0, 125, 5)]
    assert walk("rate_drop", low)[-2:] == ["pending", "firing"]
    assert walk("rate_drop", [(0, True), (60, True), (65, False), (70, True), (185, True), (190, True)]) == [
        "pending",
        "pending",
        "-",
        "pending",
        "pending",
        "firing",
    ]
    # and be back for two minutes before that is told; coming and going keeps it firing
    back = walk("disk", [(0, True), (5, False), (60, False), (120, False), (125, False), (130, False)])
    assert back == ["firing", "firing", "firing", "firing", "resolved", "-"]
    flapping = [(t, t % 10 == 0) for t in range(0, 600, 5)]
    assert set(walk("disk", flapping)) == {"firing"}
    assert walk("scanner", [(0, True), (5, False), (60, False), (65, False)])[-1] == "resolved"


def test_a_step_says_which_channels_are_owed_a_notice() -> None:
    started = step(None, "disk", True, NOW, ("phone", "hook"))
    assert started == AlertState("disk", NOW, "firing", todo=("phone", "hook"))
    assert started.told and step(started, "disk", True, NOW + 5, ("phone", "hook")) is started
    assert not AlertState("rate_drop", NOW).told  # pending: nobody was told

    # one channel took it: when the condition ends, only that one is told
    partly = AlertState("disk", NOW, "firing", delivered=("hook",), todo=("phone",), attempts=5, error="x")
    leaving = step(partly, "disk", False, NOW + 10, ("phone", "hook"))
    assert leaving == AlertState(
        "disk",
        NOW,
        "firing",
        clear_since=NOW + 10,
        delivered=("hook",),
        todo=("phone",),
        attempts=5,
        error="x",
    )
    assert step(leaving, "disk", True, NOW + 20, ()).clear_since is None  # it is back: the count starts again
    gone = step(leaving, "disk", False, NOW + 130, ("phone", "hook"))
    assert gone == AlertState(
        "disk", NOW, "resolving", clear_since=NOW + 10, delivered=("hook",), todo=("hook",)
    )
    assert step(gone, "disk", True, NOW + 135, ("phone",)) is gone  # finishes being told before anything else

    # nobody ever got the notice: there is nothing to resolve
    unheard = AlertState("no_image", NOW, "firing", todo=("phone",), attempts=5, error="x")
    assert step(unheard, "no_image", False, NOW + 10, ("phone",)) is None


def test_the_scanner_failing_is_always_told() -> None:
    said = evaluate(AlertRules(("phone",)), reading(scanner_error="internal error (OSError)"))
    assert said["scanner"] == "Le dossier ne peut plus être lu : internal error (OSError)"
    assert [c for c, s in said.items() if s] == ["scanner"]


def test_notices_say_what_started_and_what_ended() -> None:
    started = notice_for("no_image", True, "Aucune image depuis 10 min.", "cryo/session", reading())
    assert started == Notice(
        "no_image",
        True,
        "imageBio : plus d'image",
        "cryo/session — Aucune image depuis 10 min. 250 images dans la session.",
        "cryo/session",
        NOW,
    )
    ended = notice_for("no_image", False, None, "cryo/session", reading(acquired=1))
    assert (
        ended.title == "imageBio : les images arrivent de nouveau"
        and ended.body == "cryo/session — 1 image dans la session."
    )
    assert not ended.firing
    for condition in CONDITIONS:
        assert (
            notice_for(condition, True, "x", "s", reading()).title
            != notice_for(condition, False, None, "s", reading()).title
        )


# ── delivery ──────────────────────────────────────────────────────────────────


class Receiver:
    """A local HTTP server that keeps what it is sent."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, object]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append((self.path, dict(self.headers), json.loads(body)))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


NOTICE = Notice(
    "no_image",
    True,
    "imageBio : plus d'image",
    "cryo/séance — Aucune image depuis 10 min.",
    "cryo/séance",
    NOW,
)


def test_ntfy_gets_its_json_form_with_the_topic_in_the_body(receiver: Receiver) -> None:
    channel = AlertChannel("phone", "ntfy", f"{receiver.url}/a-secret-topic")
    Notifier().send(channel, NOTICE)
    Notifier().send(
        channel,
        Notice(
            "no_image",
            False,
            "imageBio : les images arrivent de nouveau",
            "cryo/séance — 3 images.",
            "cryo/séance",
            NOW,
        ),
    )
    (path, headers, body), (_, _, resolved) = receiver.requests
    assert path == "/" and headers["Content-Type"] == "application/json"
    assert body == {
        "topic": "a-secret-topic",
        "title": "imageBio : plus d'image",
        "message": "cryo/séance — Aucune image depuis 10 min.",
        "priority": 4,
        "tags": ["warning"],
    }
    assert resolved["priority"] == 3 and resolved["tags"] == ["white_check_mark"]


def test_a_webhook_gets_the_notice_as_json(receiver: Receiver) -> None:
    Notifier().send(AlertChannel("hook", "webhook", f"{receiver.url}/services/T0P/SECRET?x=1"), NOTICE)
    ((path, _, body),) = receiver.requests
    assert path == "/services/T0P/SECRET?x=1"
    assert {k: body[k] for k in ("source", "condition", "state", "title", "message", "session")} == {
        "source": "imagebio", "condition": "no_image", "state": "firing", "title": "imageBio : plus d'image",
        "message": "cryo/séance — Aucune image depuis 10 min.", "session": "cryo/séance",
    }  # fmt: skip
    assert str(body["time"]).startswith("2027-01-1")  # local time of the notice, ISO 8601


def test_a_failed_delivery_says_why_without_the_address() -> None:
    refusing = Receiver(status=403)
    try:
        with pytest.raises(AlertDeliveryError) as error:
            Notifier().send(AlertChannel("hook", "webhook", f"{refusing.url}/T0P/SECRET"), NOTICE)
        assert error.value.public_message == "the alert service answered 403"
    finally:
        refusing.close()
    with pytest.raises(AlertDeliveryError) as error:  # nothing listens there any more
        Notifier(timeout=2).send(AlertChannel("phone", "ntfy", f"{refusing.url}/a-secret-topic"), NOTICE)
    message = error.value.public_message
    assert message.startswith("channel 'phone': ") and "secret" not in message and "127.0.0.1" not in message


def test_email_goes_through_the_configured_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[dict[str, object]] = []

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.log: dict[str, object] = {"host": host, "port": port, "timeout": timeout}
            sessions.append(self.log)

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *args: object) -> None:
            self.log["closed"] = True

        def starttls(self) -> None:
            self.log["tls"] = True

        def login(self, user: str, password: str) -> None:
            self.log["login"] = (user, password)

        def send_message(self, message) -> None:
            self.log["message"] = message

    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setenv("IMAGEBIO_SMTP_PASSWORD", "s3cret")
    channel = AlertChannel("team", "email", to=("a@lab.example", "b@lab.example"))
    Notifier(
        SmtpConfig("smtp.lab.example", 587, "imagebio@lab.example", True, "bot", "IMAGEBIO_SMTP_PASSWORD")
    ).send(channel, NOTICE)
    Notifier(SmtpConfig("relay.lab.example")).send(channel, NOTICE)
    first, second = sessions
    assert (first["host"], first["port"], first["tls"], first["login"], first["closed"]) == (
        "smtp.lab.example",
        587,
        True,
        ("bot", "s3cret"),
        True,
    )
    message = first["message"]
    assert message["Subject"] == "imageBio : plus d'image" and message["From"] == "imagebio@lab.example"  # type: ignore[index]
    assert message["To"] == "a@lab.example, b@lab.example"  # type: ignore[index]
    assert message.get_content() == "cryo/séance — Aucune image depuis 10 min.\n"  # type: ignore[attr-defined]
    assert "tls" not in second and "login" not in second and second["port"] == 25
    with pytest.raises(AlertDeliveryError, match="no mail relay"):
        Notifier().send(channel, NOTICE)

    def refused(*args: object, **kwargs: object) -> None:
        raise smtplib.SMTPConnectError(421, "busy")

    monkeypatch.setattr(smtplib, "SMTP", refused)
    with pytest.raises(AlertDeliveryError, match="channel 'team': SMTPConnectError"):
        Notifier(SmtpConfig("smtp.lab.example")).send(channel, NOTICE)


# ── configuration ─────────────────────────────────────────────────────────────


def test_channels_come_from_the_configuration_file(tmp_path: Path) -> None:
    cfg = tmp_path / "imagebio.toml"
    cfg.write_text("""
[alerts.smtp]
host = "smtp.lab.example"
port = 587
from = "imagebio@lab.example"
starttls = true
username = "bot"
password_env = "IMAGEBIO_SMTP_PASSWORD"

[[alerts.channels]]
name = "telephone"
kind = "ntfy"
url = "https://ntfy.sh/a-topic-hard-to-guess"

[[alerts.channels]]
name = "equipe"
kind = "email"
to = ["cryoem@lab.example"]
""")
    s = Settings.load(cfg)
    assert s.alert_channels == (
        AlertChannel("telephone", "ntfy", "https://ntfy.sh/a-topic-hard-to-guess"),
        AlertChannel("equipe", "email", to=("cryoem@lab.example",)),
    )
    assert s.smtp == SmtpConfig(
        "smtp.lab.example", 587, "imagebio@lab.example", True, "bot", "IMAGEBIO_SMTP_PASSWORD"
    )
    assert Settings().alert_channels == () and Settings().smtp is None


@pytest.mark.parametrize(
    "text",
    [
        '[[alerts.channels]]\nname = "a"\nkind = "sms"\nurl = "https://x.example/t"',
        '[[alerts.channels]]\nname = "a"\nkind = "ntfy"\nurl = "file:///etc/passwd"',
        '[[alerts.channels]]\nname = "a"\nkind = "ntfy"\nurl = "https://ntfy.sh/"',  # no topic
        '[[alerts.channels]]\nname = "a"\nkind = "webhook"',  # no address
        '[[alerts.channels]]\nname = "a b"\nkind = "webhook"\nurl = "https://x.example/t"',
        '[[alerts.channels]]\nname = "a"\nkind = "email"\nto = ["cryoem@lab.example"]',  # no relay
        '[alerts.smtp]\nhost = "s"\n[[alerts.channels]]\nname = "a"\nkind = "email"\nto = ["not an address"]',
        '[alerts.smtp]\nhost = "s"\n[[alerts.channels]]\nname = "a"\nkind = "email"\nto = "a@b.c"',
        "[alerts.smtp]\nport = 25",  # no host
        '[alerts.smtp]\nhost = "s"\npassword = "in the file"',  # a password does not belong in the file
        '[[alerts.channels]]\nname = "a"\nkind = "webhook"\nurl = "https://x.example/1"\n'
        '[[alerts.channels]]\nname = "a"\nkind = "webhook"\nurl = "https://x.example/2"',
        "[alerts]\nchanels = []",
    ],
)
def test_bad_alert_configuration_is_refused(tmp_path: Path, text: str) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text(text)
    with pytest.raises(ConfigError):
        Settings.load(cfg)


# ── the index of an earlier version ───────────────────────────────────────────


#: The alert states as version 2 of the index kept them.
_V2_ALERT_STATES = (
    "CREATE TABLE alert_states (watch_id INTEGER NOT NULL REFERENCES watches(id), condition TEXT NOT NULL, "
    "since REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, "
    "error TEXT, PRIMARY KEY (watch_id, condition));"
)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8])
def test_an_index_of_an_earlier_version_is_brought_up_to_date(tmp_path: Path, version: int) -> None:
    path = tmp_path / "live.sqlite"
    index = Index(path)
    with index.write() as db:
        db.execute("INSERT INTO roots (path, created_at) VALUES ('/data/session', 1.0)")
        db.execute(
            "INSERT INTO watches (user, logical, root_id, active, created_at) "
            "VALUES ('alice', 'cryo/session', 1, 1, 2.0)"
        )
        db.execute("INSERT INTO dirs (root_id, rel, mtime_ns) VALUES (1, '', 5)")
        db.execute(
            "INSERT INTO exposures (root_id, dir_id, name, category, state, first_seen, seq) "
            "VALUES (1, 1, 'a', 'image', 'complete', 6.0, 1)"
        )
        db.execute('UPDATE watches SET alerts = \'{"channels": ["phone"]}\', alerts_at = 3.0')
    index.close()
    old = sqlite3.connect(path)  # what the earlier versions looked like
    old.executescript("ALTER TABLE watches DROP COLUMN atlas;")  # 8 and before: no atlas chosen
    if version <= 7:  # no microscope per session, no reports, no alert history
        old.executescript(
            "ALTER TABLE roots DROP COLUMN microscope; ALTER TABLE roots DROP COLUMN microscope_at; "
            "ALTER TABLE watches DROP COLUMN report_auto; DROP TABLE reports; DROP TABLE alert_events;"
        )
    if version <= 6:  # no external programs
        old.executescript(
            "DROP INDEX exposures_unprocessed; ALTER TABLE watches DROP COLUMN processing; "
            "ALTER TABLE exposures DROP COLUMN processing;"
        )
    if version <= 5:  # no acceptance thresholds
        old.executescript("ALTER TABLE watches DROP COLUMN quality;")
    if version <= 4:  # no calibration of the ice thickness
        old.executescript(
            "ALTER TABLE watches DROP COLUMN ice_i0; ALTER TABLE watches DROP COLUMN ice_mfp_nm;"
        )
    if version <= 3:  # images were not measured
        old.executescript(
            "DROP INDEX exposures_unmeasured; DROP INDEX exposures_mean; "
            "ALTER TABLE exposures DROP COLUMN metrics; ALTER TABLE exposures DROP COLUMN mean_counts;"
        )
    if version <= 2:  # alerts without the memory of which channel was told
        old.executescript("ALTER TABLE watches DROP COLUMN seen_at; DROP TABLE alert_states;")
        old.executescript(_V2_ALERT_STATES)
        old.execute("INSERT INTO alert_states VALUES (1, 'no_image', 4.0, 1, 1, NULL)")
    if version == 1:  # before alerts existed
        old.executescript(
            "DROP TABLE alert_states; ALTER TABLE watches DROP COLUMN alerts; "
            "ALTER TABLE watches DROP COLUMN alerts_at;"
        )
    old.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),))
    old.commit()
    old.close()

    index = Index(path)
    db = index.connection()
    assert (
        db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        == str(SCHEMA_VERSION)
        == "9"
    )
    assert db.execute("SELECT atlas FROM watches").fetchone()[0] is None  # the one of the session
    assert db.execute("SELECT report_auto FROM watches").fetchone()[0] == 1  # a report when it ends
    assert db.execute("SELECT microscope, microscope_at FROM roots").fetchone()[:] == (None, None)
    assert db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 0
    assert db.execute("SELECT processing FROM watches").fetchone()[0] == 0  # nothing runs unless asked for
    assert db.execute("SELECT processing FROM exposures").fetchone()[0] is None
    row = db.execute(
        "SELECT user, logical, active, alerts, alerts_at, seen_at, ice_i0, ice_mfp_nm, quality FROM watches"
    ).fetchone()
    rules = (None, None) if version == 1 else ('{"channels": ["phone"]}', 3.0)
    assert tuple(row) == (
        "alice",
        "cryo/session",
        1,
        *rules,
        None,
        None,
        None,
        None,
    )  # what was there is kept
    # the states of version 2 cannot say which channel was told: they start again
    assert db.execute("SELECT COUNT(*) FROM alert_states").fetchone()[0] == 0
    columns = {r["name"] for r in db.execute("PRAGMA table_info(alert_states)")}
    assert {"phase", "clear_since", "delivered", "todo"} <= columns and "notified" not in columns
    exposure = db.execute("SELECT name, state, seq, metrics, mean_counts FROM exposures").fetchone()
    assert tuple(exposure) == ("a", "complete", 1, None, None)  # kept, and waiting to be measured
    index.close()
    Index(path).close()  # and opening it again changes nothing

    newer = sqlite3.connect(path)
    newer.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    newer.commit()
    newer.close()
    with pytest.raises(IndexVersionError, match="index version 99, this one reads up to"):
        Index(path)
