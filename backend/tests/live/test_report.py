"""The report of a session, computed from exposures whose values the test knows. Expected
figures are worked out here (numpy, counts by hand), never by the report itself."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from imagebio.live.quality import QualityRules
from imagebio.live.report import SCHEMA, Exposure, ReportContext, build_report

T0 = 1_800_000_000.0


def exposure(n: int, **kw: object) -> Exposure:
    base: dict = {
        "id": n,
        "name": f"FoilHole_{n}_Data_{n}",
        "rel": f"Images-Disc1/GridSquare_{100 + n % 3}/Data",
        "path": f"cryo/session/Images-Disc1/GridSquare_{100 + n % 3}/Data/FoilHole_{n}_Data_{n}.mrc",
        "category": "data",
        "state": "complete",
        "time": T0 + 120 * n,
        "arrived": True,
        "verified": True,
    }
    return Exposure(**{**base, **kw})  # type: ignore[arg-type]


def session(count: int = 30) -> list[Exposure]:
    """``count`` images two minutes apart in three grid squares. The fit of image n is
    3 + n / 10 Å, its defocus 1 + (n mod 3) / 2 µm as asked, its counts 100 - n."""
    out = []
    for n in range(count):
        target = -(1.0 + (n % 3) * 0.5)
        out.append(
            exposure(
                n,
                meta={
                    "epu": {
                        "voltage": 300.0,
                        "pixel_size": 0.83,
                        "magnification": 130000 if n < 20 else 165000,  # changed at image 20
                        "dose": 40.0 + n * 0.1,
                        "applied_defocus": target,
                        "detector": "EF-Falcon",
                        "stage": {"x": float(n % 3) * 50, "y": float(n // 3)},
                    }
                },
                metrics={
                    "mean": 100.0 - n,
                    "std": 5.0,
                    "ice_ring": 1.0 + (0.5 if n == 7 else 0.0),
                    "ctf": {"defocus": abs(target) + 0.05, "fit": 3.0 + n / 10, "score": 10.0},
                    "microscope": {"kv": 300.0, "kv_from": "xml", "cs_mm": 2.7, "amplitude": 0.07},
                },
            )
        )
    return out


def context(**kw: object) -> ReportContext:
    base: dict = {
        "session": "cryo/session",
        "watch_id": 7,
        "watching_since": T0 - 60,
        "stopped_at": None,
        "generated_at": T0 + 10_000,
        "trigger": "manual",
        "version": "0.1.0",
        "ice": {"reference": 100.0, "calibrated": False, "mfp_nm": None},
        "microscope": {
            "kv": {"value": 300.0, "source": "server"},
            "cs_mm": {"value": 2.7, "source": "server"},
        },
    }
    return ReportContext(**{**base, **kw})  # type: ignore[arg-type]


def test_what_a_session_gave() -> None:
    images = session()
    report = build_report(images, context(rules=QualityRules(fit_max=5.0, ring_max=1.2)))
    json.dumps(report, allow_nan=False)  # a file a library and a script can read
    assert (report["schema"], report["version"], report["trigger"]) == (SCHEMA, 1, "manual")
    assert report["session"]["first"] == T0 and report["session"]["last"] == T0 + 120 * 29
    assert report["session"]["duration_s"] == 120 * 29
    head = report["headline"]
    assert head["exposures"] == 30 and head["rate_per_hour"] == pytest.approx(
        30 / (120 * 29 / 3600), abs=0.05
    )

    # fit 3 + n/10 > 5 for n > 20 (nine images), and image 7 has crystalline ice
    quality = report["quality"]
    assert (quality["judged"], quality["accepted"], quality["rejected"]) == (30, 20, 10)
    assert quality["reasons"] == {"fit": 9, "ring": 1} and head["accepted_share"] == pytest.approx(
        20 / 30, abs=1e-4
    )
    assert report["counts"]["flags"] == {"ice_ring": 1} and head["ice_ring_share"] == pytest.approx(
        1 / 30, abs=1e-4
    )

    fits = [3.0 + n / 10 for n in range(30)]
    dist = report["distributions"]["ctf_fit"]
    assert dist["n"] == 30 and dist["median"] == pytest.approx(float(np.median(fits)))
    assert dist["p10"] == pytest.approx(float(np.percentile(fits, 10))) and dist["p90"] == pytest.approx(
        float(np.percentile(fits, 90))
    )
    hist = dist["histogram"]
    assert (
        sum(hist["counts"]) + hist["below"] + hist["above"] == 30
        and len(hist["edges"]) == len(hist["counts"]) + 1
    )
    ices = [math.log(100.0 / (100.0 - n)) for n in range(30)]
    assert report["distributions"]["ice"]["median"] == pytest.approx(float(np.median(ices)), abs=1e-4)
    assert report["distributions"]["defocus_error"]["median"] == pytest.approx(0.05)  # measured 0.05 µm above
    assert report["distributions"]["astigmatism"] is None  # the quick estimate does not measure it
    assert report["distributions"]["ice_nm"] is None  # no mean free path

    # what changed during the session, and what did not
    acq = report["acquisition"]
    assert acq["voltage"]["constant"] and acq["voltage"]["values"] == [
        {"value": 300, "count": 30, "first": T0, "last": T0 + 120 * 29}
    ]
    mag = acq["magnification"]
    assert not mag["constant"] and mag["change_count"] == 1
    assert mag["changes"] == [{"at": T0 + 120 * 20, "from": 130000, "to": 165000}]
    targets = acq["target_defocus"]
    assert {v["value"]: v["count"] for v in targets["values"]} == {
        1.0: 10,
        1.5: 10,
        2.0: 10,
    } and "changes" not in targets
    assert acq["dose"]["stats"]["median"] == pytest.approx(
        float(np.median([40 + n * 0.1 for n in range(30)]))
    )
    assert (
        acq["pixel_size"]["values"][0]["value"] == 0.83
        and acq["detector"]["values"][0]["value"] == "EF-Falcon"
    )

    # over time: two-minute images over an hour make one bucket per two minutes
    timeline = report["timeline"]
    assert timeline["bucket"] == 60 and sum(b[0] for b in timeline["bins"]) == 30
    assert sum(b[1] for b in timeline["bins"]) == 20 and sum(b[2] for b in timeline["bins"]) == 10
    assert report["rate"]["pauses"] == []

    squares = {s["name"]: s for s in report["squares"]}
    assert set(squares) == {"GridSquare_100", "GridSquare_101", "GridSquare_102"}
    assert all(s["exposures"] == 10 for s in squares.values())
    assert squares["GridSquare_100"]["stage"] == [
        0.0,
        pytest.approx(np.mean([n // 3 for n in range(0, 30, 3)])),
    ]
    assert len(report["stage"]) == 30 and {p[2] for p in report["stage"]} == {"a", "r"}

    # the best images are the accepted ones whose rings go furthest; the worst the rejected
    assert [c["id"] for c in report["best"]] == [0, 1, 2, 3, 4, 5]
    assert report["worst"][0]["verdict"] == "rejected" and report["worst"][0]["why"]
    assert [c["id"] for c in report["worst"]][:5] == [29, 28, 27, 26, 25]
    codes = {limit["code"] for limit in report["limits"]}
    assert codes == {"ctf_estimate", "ice_relative"}  # nothing else is missing


def test_a_pause_in_the_collection_is_found() -> None:
    images = [exposure(n, time=T0 + 60 * n + (1800 if n >= 10 else 0)) for n in range(20)]
    pauses = build_report(images, context())["rate"]["pauses"]
    assert pauses == [{"start": T0 + 540, "end": T0 + 600 + 1800, "seconds": 1860.0}]


def test_without_thresholds_nothing_is_judged_and_it_says_so() -> None:
    report = build_report(session(10), context())
    assert report["quality"] is None and {p[2] for p in report["stage"]} == {""}
    assert "no_rules" in {limit["code"] for limit in report["limits"]}
    assert [c["id"] for c in report["worst"]] == [
        9,
        8,
        7,
        6,
    ]  # the rings that go least far, not among the best
    assert all(c["verdict"] is None for c in report["best"] + report["worst"])


def test_what_was_not_measured_is_counted_not_guessed() -> None:
    images = [
        *session(5),
        exposure(10),  # not measured
        exposure(11, metrics={}),  # could not be read
        exposure(12, state="incoming"),
        exposure(
            13, verified=False, metrics={"mean": 50.0, "microscope": {"kv": 300.0, "kv_from": "server"}}
        ),
        exposure(14, category="gridsquare"),  # a view of the grid, not an acquisition
        exposure(15, state="deleted"),
    ]
    report = build_report(images, context(ice={"reference": None}))
    counts = report["counts"]
    # five of the session and three more are complete; one is being written, one is a view, one gone
    assert (counts["exposures"], counts["measured"], counts["unreadable"], counts["incoming"]) == (8, 6, 1, 1)
    assert counts["views"] == {"gridsquare": 1}
    limits = {limit["code"]: limit["count"] for limit in report["limits"]}
    assert limits["unmeasured"] == 1 and limits["unreadable"] == 1 and limits["incoming"] == 1
    assert limits["unverified"] == 1 and limits["kv_not_from_file"] == 1 and "ice_unknown" in limits
    assert report["distributions"]["ice"] is None  # no reference: no ice, rather than a guess
    assert report["ctf_settings"]["kv_used"] == [
        {"kv": 300.0, "from": "xml", "count": 5},
        {"kv": 300.0, "from": "server", "count": 1},
    ]


def test_the_results_of_the_programs_come_before_the_estimate() -> None:
    images = [
        exposure(
            n,
            metrics={"mean": 80.0, "ctf": {"defocus": 1.0, "fit": 6.0, "score": 9}},
            processing={
                "ctf": {
                    "defocus": 1.2,
                    "defocus_u": 1.25,
                    "defocus_v": 1.15,
                    "astigmatism": 0.1,
                    "fit": 3.5,
                    "thickness": 40.0,
                },
                "motion": {"total": 10.0 + n, "early": 4.0, "max_step": 2.0, "frames": 40, "unit": "A"},
            },
        )
        for n in range(4)
    ]
    report = build_report(images, context(rules=QualityRules(fit_max=4.0, drift_max=12.0)))
    d = report["distributions"]
    assert (
        d["ctf_fit"]["median"] == 3.5
        and d["astigmatism"]["median"] == 0.1
        and d["ctf_thickness"]["median"] == 40.0
    )
    assert d["motion"]["median"] == 11.5 and d["early_motion"]["median"] == 4.0
    assert report["quality"]["reasons"] == {"drift": 1} and report["counts"]["processed"] == 4
    assert "ctf_estimate" not in {limit["code"] for limit in report["limits"]}


def test_an_empty_session_and_a_large_one() -> None:
    empty = build_report([], context())
    assert empty["headline"]["exposures"] == 0 and empty["timeline"]["bins"] == []
    assert {limit["code"] for limit in empty["limits"]} == {"empty"}
    json.dumps(empty, allow_nan=False)

    rng = np.random.default_rng(2)
    many = [
        exposure(
            n,
            time=T0 + 7 * n,
            metrics={"mean": 80.0, "ctf": {"defocus": 1.5, "fit": float(rng.uniform(3, 8)), "score": 9}},
        )
        for n in range(5000)
    ]
    report = build_report(many, context())
    series = report["series"]["ctf_fit"]
    assert len(series) == 1500 and series[0][0] == T0 and series[-1][0] == T0 + 7 * 4999
    assert len(report["timeline"]["bins"]) <= 96 and sum(b[0] for b in report["timeline"]["bins"]) == 5000
    assert len(report["stage"]) == 0  # no stage position given
