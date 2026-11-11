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
    AlertDeliveryError,
    AlertRules,
    Notice,
    Notifier,
    Reading,
    evaluate,
    notice_for,
)
from imagebio.live.index import SCHEMA_VERSION, Index

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
        evaluate(rules, reading(rate_5min=0, rate_hour=11))["rate_drop"] is None
    )  # too few images to speak of a rate


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


def test_an_index_written_before_alerts_existed_is_brought_up_to_date(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite"
    index = Index(path)
    with index.write() as db:
        db.execute("INSERT INTO roots (path, created_at) VALUES ('/data/session', 1.0)")
        db.execute(
            "INSERT INTO watches (user, logical, root_id, active, created_at) VALUES ('alice', 'cryo/session', 1, 1, 2.0)"
        )
    index.close()
    old = sqlite3.connect(path)  # what version 1 looked like
    old.executescript(
        "ALTER TABLE watches DROP COLUMN alerts; ALTER TABLE watches DROP COLUMN alerts_at; "
        "DROP TABLE alert_states; UPDATE meta SET value = '1' WHERE key = 'schema_version';"
    )
    old.close()

    index = Index(path)
    db = index.connection()
    assert (
        db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        == str(SCHEMA_VERSION)
        == "2"
    )
    row = db.execute("SELECT user, logical, active, alerts, alerts_at FROM watches").fetchone()
    assert tuple(row) == ("alice", "cryo/session", 1, None, None)  # what was there is kept
    assert db.execute("SELECT COUNT(*) FROM alert_states").fetchone()[0] == 0
    index.close()
    Index(path).close()  # and opening it again changes nothing

    newer = sqlite3.connect(path)
    newer.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    newer.commit()
    newer.close()
    with pytest.raises(RuntimeError, match="schema version 99"):
        Index(path)
