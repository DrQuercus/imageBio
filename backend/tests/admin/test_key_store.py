"""The store of personal keys: only digests kept, expiry, revocation seen by another process,
limits, and a damaged file opening nothing."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from imagebio.admin import keys as keys_module
from imagebio.admin.keys import DAYS_MAX, PREFIX, KeyStore, KeysUnavailableError
from imagebio.core.errors import InvalidRequestError, NotFoundError


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> KeyStore:
    return KeyStore(tmp_path, clock)


def test_only_the_digest_of_a_key_is_kept(store: KeyStore, tmp_path: Path) -> None:
    secret, info = store.create("alice", "  laptop  ", 30, frozenset({"cryoem"}))
    assert secret.startswith(PREFIX) and len(secret) > 40
    assert info.name == "laptop" and info.user == "alice" and info.groups == ("cryoem",)
    assert info.hint == secret[:8] and info.expires - info.created == 30 * 86400
    text = (tmp_path / "keys.json").read_text()
    assert secret not in text and secret[len(PREFIX) :] not in text
    assert hashlib.sha256(secret.encode()).hexdigest() in text
    assert stat.S_IMODE((tmp_path / "keys.json").stat().st_mode) == 0o600
    assert "user" not in info.to_json() and "groups" not in info.to_json()
    assert store.check(secret) is not None


def test_a_key_stops_when_it_expires(store: KeyStore, clock: Clock) -> None:
    secret, _ = store.create("alice", "one day", 1, frozenset())
    clock.now += 86400 - 1
    assert store.check(secret) is not None
    clock.now += 1
    assert store.check(secret) is None
    assert store.of("alice") == [] and store.counts() == {}


def test_unknown_or_malformed_secrets_open_nothing(store: KeyStore) -> None:
    secret, _ = store.create("alice", "k", 10, frozenset())
    for wrong in ("", secret[:-1], secret + "x", secret[len(PREFIX) :], "ibk_" + "a" * 300, secret.upper()):
        assert store.check(wrong) is None


def test_revocation_by_the_owner_or_for_everything(store: KeyStore) -> None:
    a, info = store.create("alice", "a", 10, frozenset())
    b, _ = store.create("alice", "b", 10, frozenset())
    c, _ = store.create("bob", "c", 10, frozenset())
    with pytest.raises(NotFoundError):
        store.revoke("bob", info.id)  # not his
    assert store.check(a) is not None
    store.revoke("alice", info.id)
    assert store.check(a) is None and store.check(b) is not None
    assert store.revoke_all("alice") == 1
    assert store.check(b) is None and store.check(c) is not None
    assert store.counts() == {"bob": 1}


def test_a_revocation_by_another_process_stops_the_key_at_once(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keys_module, "_LOOK_EVERY_S", 0.0)
    one, other = KeyStore(tmp_path, clock), KeyStore(tmp_path, clock)
    secret, info = one.create("alice", "k", 10, frozenset())
    assert other.check(secret) is not None  # made by the other process: seen
    other.revoke("alice", info.id)
    assert one.check(secret) is None


def test_last_use_never_writes_back_a_key_revoked_meanwhile(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, other = KeyStore(tmp_path, clock), KeyStore(tmp_path, clock)
    secret, info = one.create("alice", "k", 10, frozenset())
    assert other.check(secret) is not None
    one.revoke("alice", info.id)
    monkeypatch.setattr(keys_module, "_LOOK_EVERY_S", 1e9)  # `other` does not look at the file…
    clock.now += 3600  # …but it is time to write the last use
    assert other.check(secret) is None
    assert KeyStore(tmp_path, clock).check(secret) is None
    assert json.loads((tmp_path / "keys.json").read_text())["keys"] == []


def test_last_use_is_written_now_and_then(store: KeyStore, tmp_path: Path, clock: Clock) -> None:
    secret, _ = store.create("alice", "k", 10, frozenset())
    clock.now += 700
    assert store.check(secret) is not None
    assert KeyStore(tmp_path, clock).of("alice")[0].last_used == clock.now
    written = (tmp_path / "keys.json").stat().st_mtime_ns
    clock.now += 60
    assert store.check(secret) is not None  # noted, not written
    assert (tmp_path / "keys.json").stat().st_mtime_ns == written
    assert store.of("alice")[0].last_used == clock.now - 60


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "a\nb", "a‮b", "a\u0000", 3, None])
def test_a_key_has_a_plain_name(store: KeyStore, name: object) -> None:
    with pytest.raises(InvalidRequestError):
        store.create("alice", name, 10, frozenset())


@pytest.mark.parametrize("days", [0, -1, DAYS_MAX + 1, True, 1.5, "30", None])
def test_a_key_lasts_a_year_at_most(store: KeyStore, days: object) -> None:
    with pytest.raises(InvalidRequestError):
        store.create("alice", "k", days, frozenset())


def test_ten_keys_each_at_most_and_expired_ones_do_not_count(store: KeyStore, clock: Clock) -> None:
    store.create("alice", "short", 1, frozenset())
    for i in range(9):
        store.create("alice", f"k{i}", 10, frozenset())
    with pytest.raises(InvalidRequestError):
        store.create("alice", "one more", 10, frozenset())
    store.create("bob", "his", 10, frozenset())  # each his own
    clock.now += 86400
    store.create("alice", "one more", 10, frozenset())
    assert len(store.of("alice")) == 10


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"schema": "other", "keys": []}',
        b'{"schema": "imagebio.keys", "keys": [1, {}]}',
    ],
)
def test_a_damaged_file_opens_nothing(tmp_path: Path, clock: Clock, content: bytes) -> None:
    secret, _ = KeyStore(tmp_path, clock).create("alice", "k", 10, frozenset())
    (tmp_path / "keys.json").write_bytes(content)
    store = KeyStore(tmp_path, clock)
    assert store.check(secret) is None and store.of("alice") == []
    _, info = store.create("alice", "new", 10, frozenset())  # and a new one can be made
    assert [k.id for k in store.of("alice")] == [info.id]


# ── regressions found by the independent review of lot E ──────────────────────
def _flaky_reads(monkeypatch: pytest.MonkeyPatch, failures: int) -> dict[str, int]:
    """The next ``failures`` reads of keys.json fail (EMFILE, EIO, an NFS hiccup)."""
    real_bytes, real_text = Path.read_bytes, Path.read_text
    left = {"n": failures}

    def fail(path: Path) -> None:
        if path.name == "keys.json" and left["n"] > 0:
            left["n"] -= 1
            raise OSError(24, "Too many open files")

    def read_bytes(self: Path) -> bytes:
        fail(self)
        return real_bytes(self)

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        fail(self)
        return real_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    return left


def test_a_failed_read_is_tried_again_and_never_wipes_the_keys_of_others(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keys_module, "_LOOK_EVERY_S", 0.0)
    one, two = KeyStore(tmp_path, clock), KeyStore(tmp_path, clock)
    bob, _ = one.create("bob", "his", 30, frozenset())
    _flaky_reads(monkeypatch, 1)
    assert two.check(bob) is None  # cannot be checked just now: opens nothing
    assert two.check(bob) is not None  # read again at the next look (before: refused for good)
    two.create("alice", "hers", 30, frozenset())  # before: written from an empty view
    assert sorted(k["user"] for k in json.loads((tmp_path / "keys.json").read_text())["keys"]) == [
        "alice",
        "bob",
    ]
    assert one.check(bob) is not None


def test_nothing_is_written_over_a_file_that_does_not_read(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keys_module, "_LOOK_EVERY_S", 0.0)
    one, two = KeyStore(tmp_path, clock), KeyStore(tmp_path, clock)
    bob, _ = one.create("bob", "his", 30, frozenset())
    before = (tmp_path / "keys.json").read_bytes()
    _flaky_reads(monkeypatch, 10)
    with pytest.raises(KeysUnavailableError):
        two.create("alice", "hers", 30, frozenset())
    clock.now += 3600  # time to write the last use: not over a file that does not read
    assert two.check(bob) is None
    with open(tmp_path / "keys.json", "rb") as f:  # past the failing reads of the store
        assert f.read() == before


def test_a_change_of_the_same_size_at_the_same_time_is_seen(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coarse file times (some network file systems): a revocation and a new key of the same size
    within one tick. Before, the other process missed it, and its next write brought the
    revoked key back."""
    monkeypatch.setattr(keys_module, "_LOOK_EVERY_S", 0.0)
    monkeypatch.setattr(keys_module, "_SEEN_WRITE_S", 1e12)  # no last use written: same sizes
    one, two = KeyStore(tmp_path, clock), KeyStore(tmp_path, clock)
    first, info = one.create("alice", "k1", 30, frozenset())
    assert two.check(first) is not None
    path = tmp_path / "keys.json"
    stat = path.stat()
    one.revoke("alice", info.id)
    second, _ = one.create("alice", "k2", 30, frozenset())
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns
    assert two.check(first) is None and two.check(second) is not None
    two.create("bob", "his", 30, frozenset())
    assert one.check(first) is None


def test_a_file_that_is_not_a_file_of_keys_is_set_aside(tmp_path: Path, clock: Clock) -> None:
    (tmp_path / "keys.json").write_text("[]")
    store = KeyStore(tmp_path, clock)
    store.create("alice", "k", 10, frozenset())
    kept = list(tmp_path.glob("keys.json.unusable-*"))
    assert len(kept) == 1 and kept[0].read_text() == "[]"
