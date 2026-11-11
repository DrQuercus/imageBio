"""A server of the Live mode driven by hand: it scans only when told to, and its clock moves
only when told to. Shared by the tests of the Live API."""

from __future__ import annotations

from pathlib import Path

from fixtures.api import Tree, make_client
from fixtures.live import BASE
from imagebio.core.config import AlertChannel
from imagebio.live.alerts import AlertDeliveryError, Notice
from imagebio.live.simulator import MicroscopeSim, SimSettings

__all__ = ["WATCHES", "World", "sim_in"]

WATCHES = "/api/live/watches"


class World:
    """A server that scans only when told to, a clock that moves only when told to."""

    def __init__(self, tree: Tree, state: Path, **settings: object) -> None:
        self.tree = tree
        self.state = state
        self.settings = settings
        self.now = BASE
        self.sent: list[tuple[str, Notice]] = []  # (channel, notice) delivered by the server
        self.failures = 0  # deliveries that will fail before one succeeds
        self.broken: set[str] = set()  # channels that refuse everything
        self.client = self._client()

    def _client(self):
        client = make_client(
            self.tree,
            auth_mode="proxy",
            trust_network=True,
            state_dir=self.state,
            live_scan=False,
            **self.settings,
        )
        client.__enter__()
        live = client.app.state.live
        live.clock = lambda: self.now
        live.notifier = self  # notices come here instead of going out
        return client

    def send(self, channel: AlertChannel, notice: Notice) -> None:
        if channel.name in self.broken:
            self.refused = getattr(self, "refused", 0) + 1
            raise AlertDeliveryError(f"channel {channel.name!r}: ConnectionRefusedError")
        if self.failures > 0:
            self.failures -= 1
            raise AlertDeliveryError(f"channel {channel.name!r}: ConnectionRefusedError")
        self.sent.append((channel.name, notice))

    @property
    def live(self):
        return self.client.app.state.live

    def restart(self) -> None:
        """Stop the server and start another on the same state directory."""
        self.client.__exit__(None, None, None)
        self.client = self._client()

    def run(self, sims: list[MicroscopeSim], seconds: int) -> None:
        start = int(self.now - BASE)
        for second in range(start, start + seconds):
            self.now = BASE + second
            for sim in sims:
                sim.step(self.now)
            if second % 5 == 0:
                for report in self.live.scan_once(self.now).values():
                    assert report.error is None, report.error
        self.now = BASE + start + seconds

    def watch(self, path: str, who: dict[str, str]) -> int:
        r = self.client.post(WATCHES, params={"path": path}, headers=who)
        assert r.status_code == 200, r.text
        return int(r.json()["id"])

    def items(self, watch_id: int, who: dict[str, str], **params: object) -> dict:
        r = self.client.get(f"{WATCHES}/{watch_id}/items", params=params, headers=who)
        assert r.status_code == 200, r.text
        return r.json()

    def summary(self, watch_id: int, who: dict[str, str]) -> dict:
        r = self.client.get(f"{WATCHES}/{watch_id}", headers=who)
        assert r.status_code == 200, r.text
        return r.json()


def sim_in(world: World, directory: str, seed: int, **settings: object) -> MicroscopeSim:
    defaults = {"rate_per_hour": 600, "pause_probability": 0.2, "seed": seed}
    return MicroscopeSim(world.tree.base / directory, SimSettings(**{**defaults, **settings}))  # type: ignore[arg-type]
