"""The report of a session, computed from exposures whose values the test knows. Expected
figures are worked out here (numpy, counts by hand), never by the report itself."""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import math

import numpy as np
import pytest

from imagebio.live.quality import QualityRules
from imagebio.live.report import SCHEMA, Exposure, ReportContext, build_report, exposure_rows
from imagebio.live.report_render import render_csv, render_html

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
    assert report["rate"]["per_hour_peak"] == 30.0  # less than an hour: the busiest hour is all of it

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
    # one image every seven seconds: about 514 in any hour, whatever the buckets of the chart
    assert report["timeline"]["bucket"] == 600 and abs(report["rate"]["per_hour_peak"] - 3600 / 7) < 3
    assert len(report["stage"]) == 0  # no stage position given


# ── regressions: what an independent review of the report found ──────────────


def test_a_session_of_several_days_has_its_report() -> None:
    """Regression. Beyond 96 hours the buckets of the chart were two hours or more, the busiest
    hour was divided by zero, and no report could be made: neither asked for, nor at the end."""
    images = [exposure(0, time=T0), exposure(1, time=T0 + 600), exposure(2, time=T0 + 5 * 86400)]
    report = build_report(images, context(generated_at=T0 + 6 * 86400))
    timeline = report["timeline"]
    assert (
        timeline["bucket"] == 7200
        and len(timeline["bins"]) <= 96
        and sum(b[0] for b in timeline["bins"]) == 3
    )
    assert report["rate"]["per_hour_peak"] == 2.0  # the first two, ten minutes apart
    years = [exposure(0, time=T0 - 20 * 365 * 86400), exposure(1, time=T0)]  # twenty years apart
    timeline = build_report(years, context())["timeline"]
    assert timeline["bucket"] % 86400 == 0 and len(timeline["bins"]) <= 96


def test_a_date_no_calendar_holds_counts_in_the_figures_not_in_time() -> None:
    """Regression. One image dated year 1 (a wrong clock, a wrong file) made 740 000 buckets,
    and a date beyond year 9999 made the page fail."""
    images = [
        *session(5),
        exposure(10, time=-62_135_596_800.0 + 10),
        exposure(11, time=253_402_300_800.0),
        exposure(12, time=1e15),
    ]
    report = build_report(images, context())
    assert report["counts"]["exposures"] == 8 and report["session"]["first"] == T0
    assert {limit["code"]: limit["count"] for limit in report["limits"]}["bad_time"] == 3
    assert len(report["timeline"]["bins"]) <= 96 and sum(b[0] for b in report["timeline"]["bins"]) == 5
    render_html(report)
    json.dumps(report, allow_nan=False)


def test_numbers_out_of_all_proportion_are_left_out_not_summed() -> None:
    """Regression. Finite numbers from a metadata file (a defocus of 1.7e308 m, a dose of 1e308,
    a stage at 1e308 µm) overflowed once scaled or summed, and no report could be made."""
    images = [
        exposure(
            0,
            meta={"epu": {"applied_defocus": -1.7e308, "stage": {"x": 1e308, "y": 0.0}}},
            metrics={"mean": 50.0},
        ),
        exposure(1, meta={"mdoc": {"dose": 1e308, "tilt_angle": 1e308, "stage": {"x": 1e308, "y": 1.0}}}),
        exposure(2, meta={"mdoc": {"dose": 1e308, "tilt_angle": -1e308}}),
        exposure(
            3,
            metrics={"mean": 50.0, "ctf": {"defocus": 1e300, "fit": 1e300, "score": 9.0}},
            processing={"motion": {"total": 1e300, "early": 1.0, "max_step": 1.0, "frames": 2, "unit": "A"}},
        ),
        *session(3),
    ]
    ctx = context(rules=QualityRules(fit_max=5.0))
    report = build_report(images, ctx)
    json.dumps(report, allow_nan=False)
    render_html(report)
    render_csv(exposure_rows(images, ctx))
    # only the doses and tilts one can believe: those of the three ordinary images, none other
    assert report["acquisition"]["dose"]["known"] == 3 and "tilt_angle" not in report["acquisition"]
    assert report["distributions"]["ctf_fit"]["max"] < 10 and report["distributions"]["motion"] is None
    assert len(report["stage"]) == 3  # only the positions one can place
    assert report["quality"]["rejected"] >= 1  # judged as the interface judges it: beyond the threshold


def test_a_cell_cut_on_semicolons_or_trimmed_is_no_formula() -> None:
    """Regression. A spreadsheet that splits on ";" (French settings) made a cell of "=2+6" out
    of a file named "x;=2+6;"; one that trims spaces read " =2+4" as a formula."""
    names = ["x;=2+6;", " =2+4", "  @SUM(A1)", "plain"]
    images = [
        dataclasses.replace(e, name=n, path=f"cryo/session/{n}.mrc")
        for e, n in zip(session(4), names, strict=True)
    ]
    text = render_csv(exposure_rows(images, context()))
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        assert not any(cell.lstrip().startswith(("=", "+", "@")) for cell in row), (
            row
        )  # nothing a sheet would run
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["name"] for r in rows] == ["x;'=2+6;", "' =2+4", "'  @SUM(A1)", "plain"]
    assert rows[0]["path"] == "cryo/session/x;'=2+6;.mrc"


def test_each_square_gets_its_latest_overview_and_the_atlas_is_passed_on() -> None:
    images = session(6)  # squares GridSquare_100, 101 and 102

    def view(n: int, square: int, **kw: object) -> Exposure:
        name = f"GridSquare_2025010{n % 10}_000000"
        rel = f"Images-Disc1/GridSquare_{square}"
        return exposure(
            n, name=name, rel=rel, path=f"cryo/session/{rel}/{name}.jpg", category="gridsquare", **kw
        )

    views = [
        view(100, 100, time=T0 - 50),
        view(101, 100, time=T0 - 10),  # taken again: the latest is shown
        view(102, 101, state="incoming"),  # not finished: none for this square
        view(103, 999),  # a square looked at, where no image was taken
        exposure(104, name="Atlas_1", rel="Atlas", path="cryo/session/Atlas/Atlas_1.mrc", category="atlas"),
    ]
    atlas = {
        "name": "Atlas_1.mrc",
        "path": "cryo/session/Atlas/Atlas_1.mrc",
        "source": "found",
        "problem": None,
    }
    report = build_report(images + views, context(atlas=atlas))
    squares = {s["name"]: s for s in report["squares"]}
    assert set(squares) == {"GridSquare_100", "GridSquare_101", "GridSquare_102"}  # 999 has no image
    assert squares["GridSquare_100"]["overview"] == {
        "id": 101,
        "path": "cryo/session/Images-Disc1/GridSquare_100/GridSquare_20250101_000000.jpg",
    }
    assert squares["GridSquare_101"]["overview"] is None and squares["GridSquare_102"]["overview"] is None
    assert report["counts"]["exposures"] == 6  # views of the grid are not acquisitions
    assert report["atlas"] == atlas
    assert build_report(images, context())["atlas"] is None
