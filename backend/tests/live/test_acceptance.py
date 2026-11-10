"""Acceptance of the live scanner (docs/PLAN.md, M3), against a simulated microscope."""

from __future__ import annotations

import json
import os

import mrcfile
import numpy as np
import pytest

from fixtures.live import BASE, Watch
from imagebio.live.simulator import MicroscopeSim, SimSettings


def run(
    sim: MicroscopeSim, watch: Watch, scanner, start: int, stop: int, *, every: int = 5, check=None
) -> int:
    """Advance the microscope second by second and scan every ``every`` seconds."""
    for second in range(start, stop):
        now = BASE + second
        sim.step(now)
        if second % every == 0:
            report = scanner.cycle(now)
            assert report.error is None, report.error
            if check is not None:
                check(now)
    return stop


def test_no_partial_file_is_ever_marked_complete(watch: Watch) -> None:
    """600 images per hour, with writes that pause for longer than the scanner waits for a
    stable size, then a microscope PC that dies in the middle of a write."""
    sim = MicroscopeSim(watch.root, SimSettings(rate_per_hour=600, pause_probability=0.3, seed=1))
    scanner = watch.scanner()
    checked: set[str] = set()

    def nothing_complete_too_early(now: float) -> None:
        for path, row in watch.files("complete").items():
            if path in checked:
                continue
            checked.add(path)
            assert path in sim.completed, (
                f"{path} is complete in the index, the microscope is still writing it"
            )
            assert sim.completed[path] <= now
            assert row["verified"] == 1 and row["size"] == os.path.getsize(watch.root / path)
        for name, row in watch.exposures().items():
            if row["state"] == "complete":
                primary = f"{watch.rows('SELECT rel FROM dirs WHERE id = ?', row['dir_id'])[0]['rel']}/{row['primary_name']}"
                assert primary in sim.completed, name

    end = run(sim, watch, scanner, 0, 1800, check=nothing_complete_too_early)  # 30 min: 300 exposures
    assert len(sim.exposures) == 300
    paused = [
        p for p in sim.completed if p.endswith("_Fractions.mrc") and sim.completed[p] - sim.started[p] > 14
    ]
    assert len(paused) > 40  # the dangerous case did happen, many times

    # the microscope PC dies with files in progress: they stay partial for ever
    sim.step(BASE + end + 1.2)
    sim.abort()
    partial = sim.partial()
    assert any(p.endswith("_Fractions.mrc") for p in partial), partial
    run(sim, watch, scanner, end + 2, end + 600, check=nothing_complete_too_early)  # ten more minutes
    complete, pending = watch.files("complete"), watch.files("pending")
    assert not partial & complete.keys()
    assert partial <= pending.keys()
    for path in partial:
        if path.endswith(".mrc"):
            assert "truncated or still being written" in str(pending[path]["note"]) or "section" in str(
                pending[path]["note"]
            )

    # everything the microscope did finish is complete, and soon after it was
    assert set(sim.completed) == complete.keys()
    delays = [float(complete[p]["completed_at"]) - sim.completed[p] for p in sim.completed]  # type: ignore[arg-type]
    assert min(delays) >= 10  # never before the size was seen stable for ten seconds
    assert max(delays) <= 10 + 3 * 5 + 2  # two polls, the stability delay, the XML one cycle later

    exposures = watch.exposures()
    data = {n: e for n, e in exposures.items() if e["category"] == "data"}
    finished = [n for n in sim.exposures if any(p.endswith(f"{n}.mrc") for p in sim.completed)]
    assert {n for n, e in data.items() if e["state"] == "complete"} == set(finished)
    assert set(data) == set(sim.exposures) and len(data) == 301  # one more had just started when the PC died
    assert (
        data[sim.exposures[-1]]["state"] == "incoming" and data[sim.exposures[-1]]["primary_role"] == "movie"
    )
    assert (
        sum(1 for e in exposures.values() if e["category"] == "gridsquare") == 11
    )  # 30 exposures per square


def test_what_the_index_knows_about_an_exposure(watch: Watch) -> None:
    sim = MicroscopeSim(watch.root, SimSettings(rate_per_hour=600, pause_probability=0.0, seed=2))
    scanner = watch.scanner()
    run(sim, watch, scanner, 0, 12)
    sim.stop()
    run(sim, watch, scanner, 12, 60)
    exposures = {n: e for n, e in watch.exposures().items() if e["category"] == "data"}
    assert list(exposures) == sim.exposures and len(exposures) == 2
    first = exposures[sim.exposures[0]]
    assert first["state"] == "complete" and first["verified"] == 1 and first["note"] is None
    assert first["primary_role"] == "image" and str(first["primary_name"]).endswith(".mrc")
    assert json.loads(str(first["members"])) == {
        "image": "complete",
        "movie": "complete",
        "preview": "complete",
        "xml": "complete",
    }
    meta = json.loads(str(first["meta"]))
    assert meta["epu"]["pixel_size"] == pytest.approx(0.83) and 37 <= meta["epu"]["dose"] <= 43
    assert meta["epu"]["applied_defocus"] in (-1.0, -1.5, -2.0, -2.5)
    assert meta["image"] == {
        "x": 64,
        "y": 64,
        "frames": 1,
        "dtype": "float32",
        "pixel_size": pytest.approx(0.83, rel=1e-4),
        "pixel_source": "header",
    }
    assert meta["movie"]["frames"] == 6 and meta["movie"]["dtype"] == "uint16"
    assert first["time_source"] == "xml" and first["time"] == pytest.approx(BASE)  # the start of the exposure
    # the files the simulator wrote are real MRC files: mrcfile reads them
    path = watch.root / "Images-Disc1/GridSquare_1000000/Data" / str(first["primary_name"])
    with mrcfile.open(path) as m:
        assert m.data.shape == (64, 64) and float(m.voxel_size.x) == pytest.approx(0.83, rel=1e-4)
    with mrcfile.open(str(path).replace(".mrc", "_Fractions.mrc")) as m:
        assert m.data.shape == (6, 64, 64) and m.data.dtype == np.uint16 and m.is_image_stack()


def test_an_image_waits_for_its_xml_then_gives_up_with_a_warning(watch: Watch) -> None:
    # the XML arrives 50 s after the image for the first microscope, never for the second
    slow = MicroscopeSim(
        watch.root, SimSettings(rate_per_hour=60, frames=0, preview=False, xml_delay_s=50.0, seed=3)
    )
    scanner = watch.scanner()
    run(slow, watch, scanner, 0, 40)
    slow.stop()
    (name,) = slow.exposures
    waiting = watch.exposures()[name]
    assert waiting["state"] == "incoming" and waiting["note"] == "waiting for the EPU metadata file (.xml)"
    run(slow, watch, scanner, 40, 80)
    done = watch.exposures()[name]
    assert done["state"] == "complete" and done["note"] is None and done["time_source"] == "xml"

    never = MicroscopeSim(
        watch.root, SimSettings(rate_per_hour=60, frames=0, preview=False, xml_delay_s=1e9, seed=4)
    )
    run(never, watch, scanner, 100, 105)
    never.stop()
    (orphan,) = never.exposures
    run(never, watch, scanner, 105, 200)
    assert watch.exposures()[orphan]["state"] == "incoming"  # 95 s: still waiting
    run(never, watch, scanner, 200, 260)
    lost = watch.exposures()[orphan]
    assert lost["state"] == "complete" and lost["verified"] == 1
    assert lost["note"] == "completed without its EPU metadata file (.xml)"
    assert lost["time_source"] == "name"  # the time written in the file name
