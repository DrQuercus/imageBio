"""The quality report of a watched session: what a collection gave, in numbers.

``build_report`` is pure: it takes the exposures of the session as the index holds them and
what the watch knows (thresholds, ice reference, microscope, alerts), and returns the report
as a JSON-able dict, schema ``imagebio.live-report`` (documented in docs/rapport-live.md).
Rendering it (``live/report_render.py``) and storing it (``live/service.py``) are elsewhere.

The judgement of each image is the one the interface makes (``live/quality.py``): the report
says what its user saw. What could not be measured is said, not guessed.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from imagebio.live.quality import (
    ICE_RING_LIMIT,
    QualityRules,
    ctf_of,
    drift_of,
    image_flags,
    rejections,
    relative_ice,
    verdict,
)

__all__ = [
    "CSV_COLUMNS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "Exposure",
    "ReportContext",
    "build_report",
    "exposure_rows",
]

SCHEMA: Final = "imagebio.live-report"
SCHEMA_VERSION: Final = 1
#: What counts as an acquisition (the rest are views of the grid: atlas, squares, holes).
ACQUIRED: Final = ("data", "image")
_TIMELINE_BUCKETS: Final = (60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400)
_MAX_BINS: Final = 96
_HISTOGRAM_BINS: Final = 24
_MAX_POINTS: Final = 1500  # per series: enough for a chart, small enough for a file
_MAX_STAGE: Final = 5000
_MAX_SQUARES: Final = 500
_MAX_VALUES: Final = 20  # distinct values listed for a parameter
_MAX_CHANGES: Final = 20
_MAX_PAUSES: Final = 50
_PAUSE_MIN_S: Final = 600.0  # a gap is a pause from ten minutes, or ten typical intervals
_SHOWN: Final = 6  # best and worst images
_SQUARE: Final = re.compile(r"GridSquare_(\d+)")

#: Acquisition parameters the report follows over the session: key, unit, kind. "discrete":
#: values that should not change (or only at chosen moments); "continuous": a spread;
#: "targets": a set of values cycled through on purpose.
PARAMETERS: Final = (
    ("voltage", "kV", "discrete"),
    ("magnification", "x", "discrete"),
    ("pixel_size", "Å", "discrete"),
    ("detector", "", "discrete"),
    ("binning", "", "discrete"),
    ("fractions", "", "discrete"),
    ("exposure_time", "s", "discrete"),
    ("spot_size", "", "discrete"),
    ("dose", "e/Å²", "continuous"),
    ("target_defocus", "µm", "targets"),
    ("tilt_angle", "°", "continuous"),
    ("software_version", "", "discrete"),
    ("instrument", "", "discrete"),
)


@dataclass(frozen=True, slots=True)
class Exposure:
    """One exposure of the session, as the index holds it."""

    id: int
    name: str
    rel: str  # its directory, relative to the watched one
    path: str  # logical path of the file its tile is drawn from
    category: str
    state: str
    time: float | None
    arrived: bool
    verified: bool
    meta: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] | None = None
    processing: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReportContext:
    """What the watch knows, besides its exposures."""

    session: str  # logical path of the watched directory
    watch_id: int
    watching_since: float
    stopped_at: float | None
    generated_at: float
    trigger: str  # manual | stop | idle
    version: str
    rules: QualityRules = field(default_factory=QualityRules)
    ice: dict[str, Any] = field(default_factory=dict)  # LiveService._ice
    microscope: dict[str, Any] = field(default_factory=dict)  # LiveService._microscope
    programs: dict[str, Any] = field(default_factory=dict)  # LiveService._processing_of
    alerts: Sequence[dict[str, Any]] = ()  # {condition, started, ended}


def _number(value: Any) -> float | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _percentile(ordered: Sequence[float], q: float) -> float:
    """Linear interpolation between ranks, as numpy's default does."""
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values: Iterable[float]) -> dict[str, Any] | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return {
        "n": len(ordered),
        "min": _round(ordered[0]),
        "p10": _round(_percentile(ordered, 0.1)),
        "median": _round(_percentile(ordered, 0.5)),
        "p90": _round(_percentile(ordered, 0.9)),
        "max": _round(ordered[-1]),
        "mean": _round(statistics.fmean(ordered)),
    }


def _histogram(values: Sequence[float], bins: int = _HISTOGRAM_BINS) -> dict[str, Any] | None:
    """Counts over the range of the values from the 1st to the 99th percentile; what lies
    outside is counted apart (``below``, ``above``): one stray image does not squash the rest."""
    ordered = sorted(values)
    if not ordered:
        return None
    low, high = _percentile(ordered, 0.01), _percentile(ordered, 0.99)
    if high <= low:
        return {"edges": [_round(low), _round(low)], "counts": [len(ordered)], "below": 0, "above": 0}
    width = (high - low) / bins
    counts = [0] * bins
    below = above = 0
    for value in ordered:
        if value < low:
            below += 1
        elif value > high:
            above += 1
        else:
            counts[min(bins - 1, int((value - low) / width))] += 1
    return {
        "edges": [_round(low + k * width) for k in range(bins + 1)],
        "counts": counts,
        "below": below,
        "above": above,
    }


def _distribution(values: list[float]) -> dict[str, Any] | None:
    stats = _stats(values)
    return None if stats is None else {**stats, "histogram": _histogram(values)}


def _thin(points: list[Any], limit: int = _MAX_POINTS) -> list[Any]:
    """At most ``limit`` of ``points``, evenly spread, the first and the last kept."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(k * step)] for k in range(limit)]


def _bucket(start: float, end: float) -> int:
    span = max(end - start, 1.0)
    return next((b for b in _TIMELINE_BUCKETS if span / b <= _MAX_BINS), _TIMELINE_BUCKETS[-1])


def _parameter(exposure: Exposure, key: str) -> Any:
    """An acquisition parameter of an exposure: from its EPU XML, else its SerialEM mdoc."""
    epu = exposure.meta.get("epu") or {}
    mdoc = exposure.meta.get("mdoc") or {}
    if key == "target_defocus":
        value = _number(epu.get("applied_defocus"))
        if value is None:
            value = _number(mdoc.get("target_defocus"))
        return None if value is None else round(abs(value), 3)
    if key == "fractions":
        value = epu.get("fractions", mdoc.get("frames"))
    elif key in ("voltage", "detector", "binning", "spot_size", "software_version", "instrument"):
        value = epu.get(key)
    else:
        value = epu.get(key, mdoc.get(key))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = _number(value)
        return None if number is None else (int(number) if number.is_integer() else round(number, 4))
    return str(value)[:120]


def _parameters(acquired: list[Exposure]) -> dict[str, Any]:
    """Each acquisition parameter over the session: its values, how often, from when to when,
    and when it changed."""
    timed = sorted((e for e in acquired if e.time is not None), key=lambda e: (e.time or 0.0, e.id))
    out: dict[str, Any] = {}
    for key, unit, kind in PARAMETERS:
        seen = [(e.time, value) for e in timed if (value := _parameter(e, key)) is not None]
        if not seen:
            continue
        entry: dict[str, Any] = {
            "unit": unit,
            "kind": kind,
            "known": len(seen),
            "missing": len(acquired) - len(seen),
        }
        if kind == "continuous":
            entry["stats"] = _stats(float(v) for _, v in seen if isinstance(v, int | float))
        else:
            first: dict[Any, float] = {}
            last: dict[Any, float] = {}
            for when, value in seen:
                first.setdefault(value, when or 0.0)
                last[value] = when or 0.0
            counted = Counter(value for _, value in seen)
            entry["values"] = [
                {"value": value, "count": n, "first": first[value], "last": last[value]}
                for value, n in counted.most_common(_MAX_VALUES)
            ]
            entry["distinct"] = len(counted)
            entry["constant"] = len(counted) == 1
            if kind == "discrete":
                changes = [
                    {"at": when, "from": before, "to": value}
                    for (_, before), (when, value) in itertools.pairwise(seen)
                    if value != before
                ]
                entry["changes"] = changes[:_MAX_CHANGES]
                entry["change_count"] = len(changes)
        out[key] = entry
    return out


def _pauses(times: list[float]) -> list[dict[str, float]]:
    if len(times) < 3:
        return []
    gaps = [b - a for a, b in itertools.pairwise(times)]
    limit = max(_PAUSE_MIN_S, 10 * statistics.median(gaps))
    found = [
        {"start": a, "end": b, "seconds": round(b - a, 1)}
        for a, b in itertools.pairwise(times)
        if b - a > limit
    ]
    found.sort(key=lambda p: -p["seconds"])
    return sorted(found[:_MAX_PAUSES], key=lambda p: p["start"])


def _square_of(exposure: Exposure) -> str | None:
    for part in reversed(exposure.rel.split("/")):
        if _SQUARE.fullmatch(part):
            return part
    return None


def _stage_of(exposure: Exposure) -> tuple[float, float] | None:
    for source in ("epu", "mdoc"):
        stage = (exposure.meta.get(source) or {}).get("stage")
        if isinstance(stage, dict):
            x, y = _number(stage.get("x")), _number(stage.get("y"))
            if x is not None and y is not None:
                return x, y
    return None


def _median(values: list[float]) -> float | None:
    return _round(statistics.median(values)) if values else None


def build_report(exposures: Sequence[Exposure], ctx: ReportContext) -> dict[str, Any]:
    """The report of a session (schema ``imagebio.live-report``, version 1)."""
    rules = ctx.rules
    reference = _number(ctx.ice.get("reference"))
    calibrated = bool(ctx.ice.get("calibrated"))
    mfp = _number(ctx.ice.get("mfp_nm"))
    acquired = [e for e in exposures if e.category in ACQUIRED and e.state != "deleted"]
    complete = sorted(
        (e for e in acquired if e.state == "complete"),
        key=lambda e: (e.time if e.time is not None else math.inf, e.id),
    )
    incoming = sum(1 for e in acquired if e.state == "incoming")
    times = [e.time for e in complete if e.time is not None]

    # ── each image: what it measured, and what its user calls it ──
    judged: dict[int, tuple[str | None, list[tuple[str, str]]]] = {}
    for e in complete:
        v = verdict(e.state, e.metrics, e.processing, rules, reference)
        judged[e.id] = (v, rejections(e.metrics, e.processing, rules, reference) if v == "rejected" else [])

    def values(get: Callable[[Exposure], float | None]) -> list[float]:
        return [v for e in complete if (v := get(e)) is not None]

    def fit(e: Exposure) -> float | None:
        c = ctf_of(e.metrics, e.processing)
        return c["fit"] if c else None

    def defocus(e: Exposure) -> float | None:
        c = ctf_of(e.metrics, e.processing)
        return c["defocus"] if c else None

    def defocus_error(e: Exposure) -> float | None:
        measured, target = defocus(e), _parameter(e, "target_defocus")
        return None if measured is None or not isinstance(target, int | float) else measured - float(target)

    def astigmatism(e: Exposure) -> float | None:
        c = ctf_of(e.metrics, e.processing)
        return c["astigmatism"] if c else None

    def ice(e: Exposure) -> float | None:
        return relative_ice(e.metrics, reference)

    def ice_nm(e: Exposure) -> float | None:
        value = ice(e)
        return None if value is None or mfp is None else value * mfp

    def motion(e: Exposure) -> float | None:
        d = drift_of(e.metrics, e.processing)
        return d["value"] if d and d["unit"] == "A" else None

    def early_motion(e: Exposure) -> float | None:
        m = (e.processing or {}).get("motion")
        return _number(m.get("early")) if isinstance(m, dict) and m.get("unit") == "A" else None

    def from_metrics(key: str) -> Callable[[Exposure], float | None]:
        return lambda e: _number((e.metrics or {}).get(key))

    def thickness(e: Exposure) -> float | None:
        c = (e.processing or {}).get("ctf")
        return _number(c.get("thickness")) if isinstance(c, dict) else None

    getters: dict[str, Callable[[Exposure], float | None]] = {
        "ctf_fit": fit,
        "defocus": defocus,
        "defocus_error": defocus_error,
        "astigmatism": astigmatism,
        "ice": ice,
        "ice_nm": ice_nm,
        "ice_ring": from_metrics("ice_ring"),
        "motion": motion,
        "early_motion": early_motion,
        "ctf_thickness": thickness,
        "mean_counts": from_metrics("mean"),
    }
    distributions = {name: _distribution(values(get)) for name, get in getters.items()}

    measured = sum(1 for e in complete if _number((e.metrics or {}).get("mean")) is not None)
    unreadable = sum(1 for e in complete if e.metrics == {})
    verdicts = Counter(v for v, _ in judged.values() if v is not None)
    reasons = Counter(code for _, why in judged.values() for code, _ in why)
    flags = Counter(flag for e in complete for flag in image_flags(e.metrics, reference, calibrated))
    accepted, rejected = verdicts.get("accepted", 0), verdicts.get("rejected", 0)

    # ── over time ──
    start, end = (times[0], times[-1]) if times else (None, None)
    timeline: dict[str, Any] = {"bucket": None, "bins": []}
    if start is not None and end is not None:
        bucket = _bucket(start, end)
        first = math.floor(start / bucket) * bucket
        bins: dict[int, list[int]] = {}
        for e in complete:
            if e.time is None:
                continue
            slot = int((e.time - first) // bucket)
            row = bins.setdefault(slot, [0, 0, 0])
            row[0] += 1
            v = judged[e.id][0]
            if v == "accepted":
                row[1] += 1
            elif v == "rejected":
                row[2] += 1
        last_slot = int((end - first) // bucket)
        timeline = {
            "bucket": bucket,
            "start": first,
            # acquisitions, accepted, rejected in each bucket, empty buckets included
            "bins": [bins.get(k, [0, 0, 0]) for k in range(last_slot + 1)],
        }
    hours = (end - start) / 3600 if start is not None and end is not None and end > start else None
    peak = None
    if timeline["bins"] and timeline["bucket"] and start is not None and end is not None:
        # the busiest hour: buckets summed over an hour; a shorter session is its own hour
        counts = [row[0] for row in timeline["bins"]]
        window = round(3600 / timeline["bucket"])
        if window >= len(counts):
            peak = len(times) * 3600 / max(end - start, 3600.0) if hours else None
        else:
            peak = (
                max(sum(counts[k : k + window]) for k in range(len(counts) - window + 1))
                * 3600
                / (window * timeline["bucket"])
            )

    def series(get: Callable[[Exposure], float | None]) -> list[list[Any]]:
        return _thin(
            [
                [e.time, _round(v), (judged[e.id][0] or "")[:1]]
                for e in complete
                if e.time is not None and (v := get(e)) is not None
            ]
        )

    target = series(lambda e: _number(_parameter(e, "target_defocus")))

    # ── by grid square, and on the stage ──
    squares: dict[str, list[Exposure]] = {}
    for e in complete:
        square = _square_of(e)
        if square is not None:
            squares.setdefault(square, []).append(e)
    square_rows = []
    for name, members in squares.items():
        member_verdicts = Counter(judged[e.id][0] for e in members)
        stages = [s for e in members if (s := _stage_of(e)) is not None]
        square_rows.append(
            {
                "name": name,
                "exposures": len(members),
                "accepted": member_verdicts.get("accepted", 0),
                "rejected": member_verdicts.get("rejected", 0),
                "first": min((e.time for e in members if e.time is not None), default=None),
                "median_fit": _median([x for e in members if (x := fit(e)) is not None]),
                "median_defocus": _median([x for e in members if (x := defocus(e)) is not None]),
                "median_ice": _median([x for e in members if (x := ice(e)) is not None]),
                "stage": [
                    _round(statistics.fmean(s[0] for s in stages), 2),
                    _round(statistics.fmean(s[1] for s in stages), 2),
                ]
                if stages
                else None,
            }
        )
    square_rows.sort(key=lambda r: (r["first"] if r["first"] is not None else math.inf, r["name"]))
    stage = _thin(
        [
            [_round(s[0], 3), _round(s[1], 3), (judged[e.id][0] or "")[:1]]
            for e in complete
            if (s := _stage_of(e))
        ],
        _MAX_STAGE,
    )

    # ── images to look at ──
    def card(e: Exposure) -> dict[str, Any]:
        v, why = judged[e.id]
        c = ctf_of(e.metrics, e.processing)
        return {
            "id": e.id,
            "name": e.name,
            "path": e.path,
            "time": e.time,
            "verdict": v,
            "why": [text for _, text in why],
            "fit": _round(c["fit"]) if c else None,
            "defocus": _round(c["defocus"]) if c else None,
            "ice": _round(ice(e)),
            "flags": image_flags(e.metrics, reference, calibrated),
        }

    with_fit = [e for e in complete if fit(e) is not None]
    pool = [e for e in with_fit if judged[e.id][0] != "rejected"]
    best = sorted(pool, key=lambda e: (fit(e) or math.inf, -(e.time or 0.0)))[:_SHOWN]
    if rules.any:
        worst = sorted(
            (e for e in complete if judged[e.id][0] == "rejected"),
            key=lambda e: (-len(judged[e.id][1]), -(fit(e) or 0.0), -(e.time or 0.0)),
        )[:_SHOWN]
    else:
        worst = sorted(with_fit, key=lambda e: (-(fit(e) or 0.0), -(e.time or 0.0)))[:_SHOWN]
    worst = [e for e in worst if e not in best]

    # ── what the CTF was estimated with ──
    used = Counter(
        (float(m["kv"]), str(m.get("kv_from")))
        for e in complete
        if isinstance(m := (e.metrics or {}).get("microscope"), dict) and _number(m.get("kv")) is not None
    )

    # ── what cannot be said ──
    limits: list[dict[str, Any]] = []

    def limit(code: str, text: str, count: int | None = None) -> None:
        limits.append({"code": code, "count": count, "text": text})

    if not complete:
        limit("empty", "Aucune image acquise n'est encore complète : il n'y a rien à mesurer.")
    unmeasured = len(complete) - measured - unreadable
    if unmeasured:
        limit(
            "unmeasured",
            f"{unmeasured} image(s) n'ont pas été mesurées : trouvées déjà écrites au-delà des plus "
            "récentes, ou mesurées après ce rapport. Les chiffres portent sur les autres.",
            unmeasured,
        )
    if unreadable:
        limit(
            "unreadable",
            f"{unreadable} image(s) n'ont pas pu être lues : elles ne sont pas mesurées.",
            unreadable,
        )
    if incoming:
        limit("incoming", f"{incoming} image(s) étaient encore en cours d'écriture.", incoming)
    estimates = sum(
        1 for e in complete if (c := ctf_of(e.metrics, e.processing)) and c["source"] == "estimate"
    )
    if estimates:
        limit(
            "ctf_estimate",
            f"Le CTF de {estimates} image(s) est l'estimation rapide d'imageBio : un ajustement à une "
            "dimension, sans astigmatisme. Il sert à suivre la collecte, pas à remplacer CTFFIND.",
            estimates,
        )
    if not rules.any:
        limit("no_rules", "Aucun seuil d'acceptation n'était réglé : aucune image n'est jugée.")
    if reference is not None and not calibrated:
        limit(
            "ice_relative",
            "La glace est relative aux images les plus claires de la session (ln I₀/I) : sans les coups "
            "d'un trou vide, ce n'est pas une épaisseur.",
        )
    elif reference is None and measured:
        limit(
            "ice_unknown",
            "Trop peu d'images mesurées pour une référence de glace : la glace n'est pas donnée.",
        )
    unverified = sum(1 for e in complete if not e.verified)
    if unverified:
        limit(
            "unverified",
            f"{unverified} image(s) ont été indexées depuis un dossier déjà au repos, "
            "sans contrôle de leurs fichiers.",
            unverified,
        )
    defaulted = sum(n for (_, source), n in used.items() if source != "xml")
    if defaulted:
        kv = ctx.microscope.get("kv", {})
        limit(
            "kv_not_from_file",
            f"Pour {defaulted} image(s), la tension utilisée pour le CTF ({kv.get('value', '?')} kV) "
            "ne vient pas de leur fichier : vérifiez-la, elle change le défocus.",
            defaulted,
        )

    if not complete:  # nothing else is worth saying about nothing
        limits = [entry for entry in limits if entry["code"] in ("empty", "incoming")]

    def share(part: int, whole: int) -> float | None:
        return round(part / whole, 4) if whole else None

    fits, defoci, ices, motions = (distributions[k] for k in ("ctf_fit", "defocus", "ice", "motion"))
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "generated_at": ctx.generated_at,
        "generated_by": f"imageBio {ctx.version}",
        "trigger": ctx.trigger,
        "session": {
            "path": ctx.session,
            "watch_id": ctx.watch_id,
            "watching_since": ctx.watching_since,
            "stopped_at": ctx.stopped_at,
            "first": start,
            "last": end,
            "duration_s": round(end - start, 1) if start is not None and end is not None else None,
        },
        "headline": {
            "exposures": len(complete),
            "rate_per_hour": round(len(times) / hours, 1) if hours else None,
            "accepted_share": share(accepted, accepted + rejected),
            "median_ctf_fit": fits["median"] if fits else None,
            "median_defocus": defoci["median"] if defoci else None,
            "median_ice": ices["median"] if ices else None,
            "median_motion": motions["median"] if motions else None,
            "ice_ring_share": share(flags.get("ice_ring", 0), measured),
        },
        "counts": {
            "exposures": len(complete),
            "incoming": incoming,
            "measured": measured,
            "unreadable": unreadable,
            "processed": sum(1 for e in complete if e.processing is not None),
            "arrived_while_watched": sum(1 for e in complete if e.arrived),
            "views": dict(
                Counter(e.category for e in exposures if e.category not in ACQUIRED and e.state == "complete")
            ),
            "flags": dict(flags),
        },
        "rate": {
            "per_hour_mean": round(len(times) / hours, 1) if hours else None,
            "per_hour_peak": round(peak, 1) if peak is not None else None,
            "pauses": _pauses(times),
        },
        "timeline": timeline,
        "acquisition": _parameters(complete),
        "ctf_settings": {
            "kv": ctx.microscope.get("kv"),
            "cs_mm": ctx.microscope.get("cs_mm"),
            "amplitude": ctx.microscope.get("amplitude"),
            "kv_used": [{"kv": kv, "from": source, "count": n} for (kv, source), n in used.most_common()],
        },
        "ice": {
            "reference": reference,
            "calibrated": calibrated,
            "mfp_nm": mfp,
            "ring_limit": ICE_RING_LIMIT,
        },
        "thresholds": rules.to_json(),
        "quality": {
            "judged": accepted + rejected,
            "accepted": accepted,
            "rejected": rejected,
            "accepted_share": share(accepted, accepted + rejected),
            "reasons": dict(reasons),
        }
        if rules.any
        else None,
        "distributions": distributions,
        "series": {
            "defocus": series(defocus),
            "target_defocus": [p[:2] for p in target],
            "ctf_fit": series(fit),
            "ice": series(ice),
            "motion": series(motion),
        },
        "squares": square_rows[:_MAX_SQUARES],
        "square_count": len(square_rows),
        "stage": stage,
        "best": [card(e) for e in best],
        "worst": [card(e) for e in worst],
        "alerts": [
            {
                "condition": a["condition"],
                "started": a["started"],
                "ended": a.get("ended"),
                "seconds": round(a["ended"] - a["started"], 1) if a.get("ended") is not None else None,
            }
            for a in ctx.alerts
        ],
        "programs": ctx.programs,
        "limits": limits,
    }


#: Columns of the table of exposures (exposures.csv), in order.
CSV_COLUMNS: Final = (
    "name",
    "path",
    "time",
    "grid_square",
    "verdict",
    "reasons",
    "flags",
    "mean_counts",
    "ice",
    "ice_nm",
    "ice_ring",
    "ctf_defocus_um",
    "ctf_fit_A",
    "ctf_astigmatism_um",
    "ctf_source",
    "target_defocus_um",
    "motion_A",
    "dose",
    "pixel_size_A",
    "stage_x_um",
    "stage_y_um",
    "verified",
)


def exposure_rows(exposures: Sequence[Exposure], ctx: ReportContext) -> list[dict[str, Any]]:
    """One row per acquired, complete exposure, in acquisition order: what the report counted."""
    reference = _number(ctx.ice.get("reference"))
    calibrated = bool(ctx.ice.get("calibrated"))
    mfp = _number(ctx.ice.get("mfp_nm"))
    rows = []
    for e in sorted(
        (e for e in exposures if e.category in ACQUIRED and e.state == "complete"),
        key=lambda e: (e.time if e.time is not None else math.inf, e.id),
    ):
        v = verdict(e.state, e.metrics, e.processing, ctx.rules, reference)
        why = rejections(e.metrics, e.processing, ctx.rules, reference) if v == "rejected" else []
        c = ctf_of(e.metrics, e.processing)
        d = drift_of(e.metrics, e.processing)
        ice = relative_ice(e.metrics, reference)
        stage = _stage_of(e)
        rows.append(
            {
                "name": e.name,
                "path": e.path,
                "time": e.time,
                "grid_square": _square_of(e),
                "verdict": v,
                "reasons": "; ".join(text for _, text in why),
                "flags": " ".join(image_flags(e.metrics, reference, calibrated)),
                "mean_counts": _round(_number((e.metrics or {}).get("mean"))),
                "ice": _round(ice),
                "ice_nm": _round(ice * mfp, 1) if ice is not None and mfp is not None else None,
                "ice_ring": _round(_number((e.metrics or {}).get("ice_ring"))),
                "ctf_defocus_um": _round(c["defocus"]) if c else None,
                "ctf_fit_A": _round(c["fit"]) if c else None,
                "ctf_astigmatism_um": _round(c["astigmatism"]) if c else None,
                "ctf_source": c["source"] if c else None,
                "target_defocus_um": _parameter(e, "target_defocus"),
                "motion_A": _round(d["value"]) if d and d["unit"] == "A" else None,
                "dose": _parameter(e, "dose"),
                "pixel_size_A": _parameter(e, "pixel_size"),
                "stage_x_um": _round(stage[0], 3) if stage else None,
                "stage_y_um": _round(stage[1], 3) if stage else None,
                "verified": e.verified,
            }
        )
    return rows
