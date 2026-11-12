"""The quality report of a session as files: a page to read and print, the same numbers for a
program, and one line per image.

- ``render_html``: one self-contained HTML file. No script at all, every piece of text that
  comes from a file name escaped, images embedded, charts as inline SVG drawn from numbers. It
  is meant to be opened outside imageBio (a lab library, a mail), so it fetches nothing.
- ``render_csv``: a cell that a spreadsheet would take for a formula is written as text.
- ``bundle``: the three, with the JSON, in one zip.
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import math
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from imagebio.live.report import CSV_COLUMNS

__all__ = ["bundle", "render_csv", "render_html", "render_json"]

_ACCEPTED: Final = "#2e7d32"
_REJECTED: Final = "#c62828"
_UNJUDGED: Final = "#3f51b5"
_TARGET: Final = "#9e9e9e"
_COLOURS: Final = {"a": _ACCEPTED, "r": _REJECTED, "": _UNJUDGED}
_WIDTH: Final = 680
_MAX_SQUARE_ROWS: Final = 60

PARAMETER_LABELS: Final = {
    "voltage": "Tension",
    "magnification": "Grandissement",
    "pixel_size": "Taille de pixel",
    "detector": "Détecteur",
    "binning": "Binning",
    "fractions": "Fractions par film",
    "exposure_time": "Temps d'exposition",
    "spot_size": "Spot size",
    "dose": "Dose totale",
    "target_defocus": "Défocus visé",
    "tilt_angle": "Inclinaison",
    "software_version": "Logiciel",
    "instrument": "Microscope",
}
MEASURE_LABELS: Final = {
    "ctf_fit": ("Résolution de l'ajustement du CTF", "Å"),
    "defocus": ("Défocus mesuré", "µm"),
    "defocus_error": ("Écart au défocus visé", "µm"),
    "astigmatism": ("Astigmatisme", "µm"),
    "ice": ("Glace relative", "ln I₀/I"),
    "ice_nm": ("Épaisseur de glace", "nm"),
    "ice_ring": ("Anneau de glace", ""),
    "motion": ("Mouvement de l'échantillon", "Å"),
    "early_motion": ("Mouvement des premières frames", "Å"),
    "ctf_thickness": ("Épaisseur d'après le CTF", "nm"),
    "mean_counts": ("Coups moyens par pixel", ""),
}
REASON_LABELS: Final = {
    "fit": "anneaux de Thon pas assez loin",
    "ice": "glace trop épaisse",
    "ring": "glace cristalline",
    "drift": "mouvement trop grand",
    "defocus": "défocus hors de la plage",
}
FLAG_LABELS: Final = {"ice_ring": "glace cristalline", "dark": "très sombre", "empty": "trou vide"}
CONDITION_LABELS: Final = {
    "no_image": "plus d'image",
    "disk": "disque presque plein",
    "rate_drop": "débit en chute",
    "scanner": "surveillance en échec",
}
TRIGGER_LABELS: Final = {
    "manual": "demandé depuis l'interface",
    "stop": "à l'arrêt de la surveillance",
    "idle": "la collecte est silencieuse depuis longtemps",
}
SOURCE_LABELS: Final = {
    "xml": "fichier d'acquisition",
    "session": "réglé pour cette session",
    "root": "dossier de données",
    "server": "configuration du serveur",
}
THRESHOLD_LABELS: Final = {
    "fit_max": ("Anneaux de Thon retrouvés au moins jusqu'à", "Å"),
    "ice_max": ("Glace relative au plus", ""),
    "ring_max": ("Anneau de glace au plus", ""),
    "drift_max": ("Mouvement au plus", "Å"),
    "defocus_min": ("Défocus mesuré au moins", "µm"),
    "defocus_max": ("Défocus mesuré au plus", "µm"),
}

_STYLE: Final = """
:root { color-scheme: light; }
body { font: 13px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #1c1c1c;
       background: #fff; margin: 0 auto; max-width: 760px; padding: 24px 28px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; padding-bottom: 4px;
     border-bottom: 1px solid #ddd; } h3 { font-size: 13px; margin: 16px 0 6px; }
.sub { color: #555; margin: 0 0 2px; } .note { color: #555; font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 16px 0; }
.card { border: 1px solid #ddd; border-radius: 6px; padding: 8px 10px; }
.card b { display: block; font-size: 18px; } .card span { color: #555; font-size: 11px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 12px; }
th, td { text-align: left; padding: 3px 6px; border-bottom: 1px solid #eee; vertical-align: top; }
th { font-weight: 600; color: #333; background: #fafafa; } td.n, th.n { text-align: right; white-space: nowrap; }
.callout { background: #fff8e1; border: 1px solid #f0d78c; border-radius: 6px; padding: 8px 12px; margin: 12px 0; }
.callout ul { margin: 4px 0 0; padding-left: 18px; }
.legend span { display: inline-block; margin-right: 14px; font-size: 11px; color: #444; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
.thumbs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.thumb { border: 1px solid #ddd; border-radius: 6px; padding: 6px; font-size: 11px; break-inside: avoid; }
.thumb img { width: 100%; height: auto; display: block; background: #222; border-radius: 3px; }
.thumb .name { word-break: break-all; color: #333; margin: 4px 0 2px; }
.cap { font-size: 11px; color: #444; margin: 8px 0 2px; }
.rej { color: #c62828; } .acc { color: #2e7d32; }
svg { display: block; max-width: 100%; height: auto; } svg text { font: 10px system-ui, sans-serif; fill: #444; }
section { break-inside: avoid-page; }
footer { margin-top: 32px; color: #777; font-size: 11px; border-top: 1px solid #ddd; padding-top: 8px; }
@page { size: A4; margin: 14mm; }
@media print { body { padding: 0; max-width: none; } h2 { break-after: avoid; } }
"""


# ── formatting ───────────────────────────────────────────────────────────────


def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _num(value: Any, digits: int = 2) -> str:
    """A number as French writes it: a decimal comma; '—' for nothing."""
    if value is None or isinstance(value, bool):
        return "—"
    if isinstance(value, str):
        return _e(value)
    if not math.isfinite(value):
        return "—"
    if abs(value) >= 1e12:
        return f"{value:.3g}".replace(".", ",")
    if isinstance(value, int) or (float(value).is_integer() and abs(value) >= 1000):
        text = f"{round(value):,}".replace(",", " ")
    else:
        text = f"{value:.{digits}f}"
    return text.replace(".", ",")


def _share(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %".replace(".", ",")


def _moment(t: float) -> datetime | None:
    """A time as a local date; None for one no calendar holds (a wrong clock, a wrong file)."""
    try:
        return datetime.fromtimestamp(t).astimezone()
    except (OverflowError, ValueError, OSError):
        return None


def _when(t: float | None, *, date: bool = True) -> str:
    moment = _moment(t) if t is not None else None
    if moment is None:
        return "—"
    return moment.strftime("%d/%m/%Y %H:%M" if date else "%H:%M")


def _zone(t: float) -> str:
    moment = _moment(t)
    offset = moment.strftime("%z") if moment is not None else ""
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else ""


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} j {hours} h {minutes:02d}"
    return f"{hours} h {minutes:02d}" if hours else f"{minutes} min"


def _unit(unit: str) -> str:
    return f" {_e(unit)}" if unit and unit != "x" else ""


# ── charts: inline SVG, from numbers only ───────────────────────────────────


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if not (math.isfinite(low) and math.isfinite(high) and math.isfinite(high - low)) or high <= low:
        return [low]
    raw = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.ceil(low / step) * step
    ticks: list[float] = []
    value = first
    while value <= high + step * 1e-9 and len(ticks) < 12:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _time_ticks(low: float, high: float) -> list[tuple[float, str]]:
    span = max(high - low, 1.0)
    step = next(
        (s for s in (300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400) if span / s <= 7), 86400
    )
    first = math.ceil(low / step) * step
    start, end = _moment(low), _moment(high)
    if start is None or end is None:
        return []
    several_days = start.date() != end.date()
    out: list[tuple[float, str]] = []
    t = float(first)
    while t <= high and len(out) < 10:
        moment = _moment(t)
        if moment is not None:
            out.append((t, moment.strftime("%d/%m %H:%M" if several_days else "%H:%M")))
        t += step
    return out


class _Frame:
    """A plotting area: data coordinates to SVG coordinates, and its axes."""

    left, right, top, bottom = 52, 12, 10, 26

    def __init__(
        self, x: tuple[float, float], y: tuple[float, float], height: int = 190, width: int = _WIDTH
    ):
        self.width, self.height = width, height
        self.x0, self.x1 = x if x[1] > x[0] else (x[0] - 1, x[0] + 1)
        self.y0, self.y1 = y if y[1] > y[0] else (y[0] - 1, y[0] + 1)

    def px(self, x: float) -> float:
        span = self.width - self.left - self.right
        return self.left + (x - self.x0) / (self.x1 - self.x0) * span

    def py(self, y: float) -> float:
        span = self.height - self.top - self.bottom
        return self.height - self.bottom - (y - self.y0) / (self.y1 - self.y0) * span

    def axes(self, y_label: str, *, time: bool, x_label: str = "") -> str:
        parts = []
        for tick in _nice_ticks(self.y0, self.y1):
            y = self.py(tick)
            parts.append(
                f'<line x1="{self.left}" y1="{y:.1f}" x2="{self.width - self.right}" y2="{y:.1f}" stroke="#eee"/>'
            )
            parts.append(
                f'<text x="{self.left - 4}" y="{y + 3:.1f}" text-anchor="end">{_num(tick, _digits(tick))}</text>'
            )
        ticks = (
            _time_ticks(self.x0, self.x1)
            if time
            else [(t, _num(t, _digits(t))) for t in _nice_ticks(self.x0, self.x1)]
        )
        for value, label in ticks:
            x = self.px(value)
            parts.append(
                f'<line x1="{x:.1f}" y1="{self.height - self.bottom}" x2="{x:.1f}" y2="{self.height - self.bottom + 4}" stroke="#999"/>'
            )
            parts.append(
                f'<text x="{x:.1f}" y="{self.height - self.bottom + 15}" text-anchor="middle">{_e(label)}</text>'
            )
        parts.append(
            f'<line x1="{self.left}" y1="{self.height - self.bottom}" x2="{self.width - self.right}" '
            f'y2="{self.height - self.bottom}" stroke="#999"/>'
        )
        parts.append(
            f'<text x="12" y="{self.top + (self.height - self.top - self.bottom) / 2:.1f}" text-anchor="middle" '
            f'transform="rotate(-90 12 {self.top + (self.height - self.top - self.bottom) / 2:.1f})">{_e(y_label)}</text>'
        )
        if x_label:
            parts.append(
                f'<text x="{self.width - self.right}" y="{self.height - 2}" text-anchor="end">{_e(x_label)}</text>'
            )
        return "".join(parts)

    def svg(self, body: str, title: str) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" '
            f'width="{self.width}" height="{self.height}" role="img" aria-label="{_e(title)}">{body}</svg>'
        )


def _digits(value: float) -> int:
    magnitude = abs(value)
    return 0 if magnitude >= 100 or float(value).is_integer() else 1 if magnitude >= 10 else 2


def _range(values: Iterable[float], pad: float = 0.05) -> tuple[float, float]:
    ordered = sorted(values)
    if not ordered:
        return 0.0, 1.0
    low, high = ordered[0], ordered[-1]
    margin = (high - low) * pad or abs(low) * 0.1 or 1.0
    return low - margin, high + margin


def _scatter(
    points: Sequence[Sequence[Any]], y_label: str, title: str, *, also: Sequence[Sequence[Any]] = ()
) -> str:
    """Values over time, one dot per image coloured by its verdict; ``also``: grey dots behind."""
    if not points:
        return ""
    times = [p[0] for p in points] + [p[0] for p in also]
    frame = _Frame(_range(times, 0.01), _range([p[1] for p in points] + [p[1] for p in also]))
    radius = 2.2 if len(points) < 400 else 1.4
    dots = [
        f'<circle cx="{frame.px(p[0]):.1f}" cy="{frame.py(p[1]):.1f}" r="{radius + 0.6}" fill="{_TARGET}" fill-opacity=".5"/>'
        for p in also
    ]
    dots += [
        f'<circle cx="{frame.px(p[0]):.1f}" cy="{frame.py(p[1]):.1f}" r="{radius}" fill="{_COLOURS.get(str(p[2]) if len(p) > 2 else "", _UNJUDGED)}" fill-opacity=".85"/>'
        for p in points
    ]
    return frame.svg(frame.axes(y_label, time=True) + "".join(dots), title)


def _bars(timeline: Mapping[str, Any], judged: bool) -> str:
    """Acquisitions per time bucket, split into accepted, rejected and not judged."""
    bins, bucket, start = timeline.get("bins") or [], timeline.get("bucket"), timeline.get("start")
    if not bins or not bucket or start is None:
        return ""
    peak = max(b[0] for b in bins) or 1
    frame = _Frame((start, start + bucket * len(bins)), (0, peak * 1.1))
    width = max(0.6, (frame.px(start + bucket) - frame.px(start)) - 0.8)
    parts = []
    for k, (total, accepted, rejected) in enumerate(bins):
        x = frame.px(start + k * bucket) + 0.4
        base = 0.0
        for count, colour in (
            (accepted, _ACCEPTED),
            (rejected, _REJECTED),
            (total - accepted - rejected, _UNJUDGED),
        ):
            if count <= 0:
                continue
            y_top, y_bottom = frame.py(base + count), frame.py(base)
            parts.append(
                f'<rect x="{x:.1f}" y="{y_top:.1f}" width="{width:.1f}" height="{y_bottom - y_top:.1f}" fill="{colour}"/>'
            )
            base += count
    label = f"images par {_duration(bucket)}"
    return frame.svg(frame.axes(label, time=True) + "".join(parts), "Acquisitions dans le temps")


def _histogram(dist: Mapping[str, Any] | None, label: str, unit: str) -> str:
    hist = (dist or {}).get("histogram")
    if not hist or len(hist["counts"]) < 1:
        return ""
    edges, counts = hist["edges"], hist["counts"]
    if len(edges) < 2 or edges[-1] <= edges[0]:
        return f'<p class="note">{_e(label)} : toutes les images à {_num(edges[0])}{_unit(unit)}.</p>'
    frame = _Frame((edges[0], edges[-1]), (0, max(counts) * 1.1 or 1), height=150, width=330)
    frame.left = 40
    parts = [
        f'<rect x="{frame.px(edges[k]) + 0.5:.1f}" y="{frame.py(c):.1f}" '
        f'width="{max(0.5, frame.px(edges[k + 1]) - frame.px(edges[k]) - 1):.1f}" height="{frame.py(0) - frame.py(c):.1f}" fill="{_UNJUDGED}"/>'
        for k, c in enumerate(counts)
        if c
    ]
    outside = []
    if hist.get("below"):
        outside.append(f"{hist['below']} en dessous")
    if hist.get("above"):
        outside.append(f"{hist['above']} au-dessus")
    caption = f"{label}{f' ({unit})' if unit else ''}"
    note = f" — hors du graphique : {', '.join(outside)}" if outside else ""
    svg = frame.svg(frame.axes("images", time=False) + "".join(parts), label)
    return f'<div><div class="cap"><b>{_e(caption)}</b>{_e(note)}</div>{svg}</div>'


def _stage_map(points: Sequence[Sequence[Any]]) -> str:
    if not points:
        return ""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1 = _range(xs)
    y0, y1 = _range(ys)
    span = max(x1 - x0, y1 - y0)  # one scale on both axes
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    frame = _Frame((cx - span / 2, cx + span / 2), (cy - span / 2, cy + span / 2), height=420, width=460)
    dots = "".join(
        f'<circle cx="{frame.px(p[0]):.1f}" cy="{frame.py(p[1]):.1f}" r="2.4" fill="{_COLOURS.get(str(p[2]), _UNJUDGED)}" fill-opacity=".8"/>'
        for p in points
    )
    return frame.svg(frame.axes("y (µm)", time=False, x_label="x (µm)") + dots, "Positions de la platine")


def _legend(judged: bool, target: bool = False) -> str:
    items = [(_ACCEPTED, "acceptée"), (_REJECTED, "rejetée")] if judged else [(_UNJUDGED, "image")]
    if judged:
        items.append((_UNJUDGED, "non jugée"))
    if target:
        items.append((_TARGET, "défocus visé"))
    return (
        '<div class="legend">'
        + "".join(f'<span><i style="background:{c}"></i>{_e(t)}</span>' for c, t in items)
        + "</div>"
    )


# ── the page ─────────────────────────────────────────────────────────────────


def _table(head: Sequence[str], rows: Iterable[Sequence[str]], numeric: Sequence[int] = ()) -> str:
    """``rows`` hold HTML already escaped."""
    th = "".join(f"<th{' class=n' if k in numeric else ''}>{_e(h)}</th>" for k, h in enumerate(head))
    body = "".join(
        "<tr>"
        + "".join(f"<td{' class=n' if k in numeric else ''}>{cell}</td>" for k, cell in enumerate(row))
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _parameter_value(entry: Mapping[str, Any], key: str) -> tuple[str, str]:
    unit = entry.get("unit", "")
    if entry["kind"] == "continuous":
        s = entry.get("stats") or {}
        return (
            f"médiane {_num(s.get('median'))}{_unit(unit)} (de {_num(s.get('min'))} à {_num(s.get('max'))})",
            f"{entry['known']} images",
        )
    values = entry.get("values") or []
    shown = ", ".join(f"{_num(v['value'], 3)}{_unit(unit)} ({v['count']})" for v in values[:6])
    if entry.get("distinct", 0) > 6:
        shown += f" … ({entry['distinct']} valeurs)"
    if entry["kind"] == "targets":
        note = f"{entry.get('distinct', 0)} valeur(s) visée(s)"
    elif entry.get("constant"):
        note = "constant"
    else:
        changes = entry.get("changes") or []
        first = changes[0] if changes else None
        note = f"<b>a changé {entry.get('change_count', 0)} fois</b>"
        if first:
            note += (
                f" ; la première à {_when(first['at'])}, de {_num(first['from'], 3)} à {_num(first['to'], 3)}"
            )
    if entry.get("missing"):
        note += f" ; inconnu pour {entry['missing']} image(s)"
    return shown, note


def _card(card: Mapping[str, Any], thumbs: Mapping[int, bytes]) -> str:
    data = thumbs.get(int(card["id"]))
    image = (
        f'<img alt="" src="data:image/jpeg;base64,{base64.b64encode(data).decode("ascii")}">'
        if data
        else '<p class="note">vignette indisponible</p>'
    )
    verdict = card.get("verdict")
    mark = (
        ' · <span class="acc">acceptée</span>'
        if verdict == "accepted"
        else ' · <span class="rej">rejetée</span>'
        if verdict == "rejected"
        else ""
    )
    why = "".join(f'<div class="rej">{_e(w)}</div>' for w in card.get("why") or [])
    flags = ", ".join(FLAG_LABELS.get(f, f) for f in card.get("flags") or [])
    return (
        f'<div class="thumb">{image}<div class="name mono">{_e(card["name"])}</div>'
        f"<div>{_when(card.get('time'))}{mark}</div>"
        f"<div>CTF {_num(card.get('fit'), 1)} Å · défocus {_num(card.get('defocus'))} µm · glace {_num(card.get('ice'))}</div>"
        f"{f'<div>{_e(flags)}</div>' if flags else ''}{why}</div>"
    )


def render_html(report: Mapping[str, Any], thumbs: Mapping[int, bytes] | None = None) -> str:
    """The report as one self-contained page, in French. ``thumbs``: JPEG of the images shown,
    by exposure id."""
    thumbs = thumbs or {}
    session, head, counts = report["session"], report["headline"], report["counts"]
    quality, dist = report.get("quality"), report["distributions"]
    judged = quality is not None
    out: list[str] = []
    add = out.append
    generated = report["generated_at"]

    add(f'<h1>Rapport de qualité de la collecte</h1><p class="sub mono">{_e(session["path"])}</p>')
    add(
        f'<p class="sub">Images du {_when(session.get("first"))} au {_when(session.get("last"))} '
        f"({_duration(session.get('duration_s'))}) · heures en {_e(_zone(generated))}</p>"
        f'<p class="note">Rapport du {_when(generated)}, {_e(TRIGGER_LABELS.get(report["trigger"], report["trigger"]))}'
        f"{' · surveillance arrêtée le ' + _when(session['stopped_at']) if session.get('stopped_at') else ''}.</p>"
    )
    cards = [
        (str(head["exposures"]), "images acquises"),
        (_num(head.get("rate_per_hour"), 0), "images par heure, en moyenne"),
        (
            _share(head.get("accepted_share")) if judged else "—",
            "acceptées" if judged else "acceptées (aucun seuil)",
        ),
        (f"{_num(head.get('median_ctf_fit'), 1)} Å", "résolution médiane du CTF"),
        (f"{_num(head.get('median_defocus'))} µm", "défocus médian"),
        (_num(head.get("median_ice")), "glace relative médiane"),
        (
            f"{_num(head.get('median_motion'), 1)} Å" if head.get("median_motion") is not None else "—",
            "mouvement médian",
        ),
        (_share(head.get("ice_ring_share")), "glace cristalline"),
    ]
    add(
        '<div class="cards">'
        + "".join(f"<div class=card><b>{v}</b><span>{_e(t)}</span></div>" for v, t in cards)
        + "</div>"
    )

    if report["limits"]:
        add('<div class="callout"><b>À savoir avant de lire ces chiffres</b><ul>')
        add("".join(f"<li>{_e(limit['text'])}</li>" for limit in report["limits"]))
        add("</ul></div>")

    # ── over time ──
    add("<section><h2>La collecte dans le temps</h2>")
    add(_bars(report["timeline"], judged) + _legend(judged))
    rate = report["rate"]
    add(
        f"<p>{counts['exposures']} images, {_num(rate.get('per_hour_mean'), 0)} par heure en moyenne, "
        f"{_num(rate.get('per_hour_peak'), 0)} au plus fort.</p>"
    )
    if rate["pauses"]:
        add("<h3>Pauses</h3>")
        add(
            _table(
                ("Début", "Reprise", "Durée"),
                ((_when(p["start"]), _when(p["end"]), _duration(p["seconds"])) for p in rate["pauses"]),
                (2,),
            )
        )
    else:
        add('<p class="note">Aucune pause de plus de dix minutes.</p>')
    add("</section>")

    # ── quality ──
    add("<section><h2>Qualité</h2>")
    thresholds = report["thresholds"]
    set_ = [(k, v) for k, v in thresholds.items() if v is not None]
    if judged and quality is not None:
        add(
            f"<p><b>{quality['accepted']}</b> images acceptées sur {quality['judged']} jugées "
            f"({_share(quality.get('accepted_share'))}), <b>{quality['rejected']}</b> rejetées.</p>"
        )
        add(
            _table(
                ("Seuil", "Valeur"),
                ((_e(THRESHOLD_LABELS[k][0]), f"{_num(v)}{_unit(THRESHOLD_LABELS[k][1])}") for k, v in set_),
                (1,),
            )
        )
        if quality["reasons"]:
            add("<h3>Raisons des rejets</h3>")
            add(
                _table(
                    ("Raison", "Images"),
                    (
                        (_e(REASON_LABELS.get(code, code)), str(n))
                        for code, n in sorted(quality["reasons"].items(), key=lambda kv: -kv[1])
                    ),
                    (1,),
                )
            )
        add(
            "<p class=\"note\">Une image peut être rejetée pour plusieurs raisons. Une mesure qu'une image n'a pas ne compte pas contre elle.</p>"
        )
    else:
        add(
            "<p>Aucun seuil d'acceptation n'était réglé pour cette session : les images ne sont pas jugées.</p>"
        )
    add("</section>")

    # ── CTF ──
    add("<section><h2>CTF</h2>")
    targets = report["series"].get("target_defocus") or []
    add(_scatter(report["series"]["defocus"], "défocus mesuré (µm)", "Défocus dans le temps", also=targets))
    add(_legend(judged, target=bool(targets)))
    add(_scatter(report["series"]["ctf_fit"], "résolution du CTF (Å)", "Résolution du CTF dans le temps"))
    histograms = [
        _histogram(dist.get(key), *MEASURE_LABELS[key])
        for key in ("ctf_fit", "defocus", "defocus_error", "astigmatism")
        if dist.get(key)
    ]
    if histograms:
        add(
            '<div class="thumbs" style="grid-template-columns:1fr 1fr">'
            + "".join(f"<div>{h}</div>" for h in histograms)
            + "</div>"
        )
    settings = report["ctf_settings"]
    rows = []
    for key, label, unit in (
        ("kv", "Tension", "kV"),
        ("cs_mm", "Aberration sphérique", "mm"),
        ("amplitude", "Contraste d'amplitude", ""),
    ):
        value = settings.get(key) or {}
        rows.append(
            (
                _e(label),
                f"{_num(value.get('value'))}{_unit(unit)}",
                _e(SOURCE_LABELS.get(value.get("source", ""), "—")),
            )
        )
    add("<h3>Microscope utilisé pour estimer le CTF</h3>")
    add(_table(("Paramètre", "Valeur", "Origine"), rows, (1,)))
    if settings.get("kv_used"):
        used = ", ".join(
            f"{_num(u['kv'], 0)} kV ({_e(SOURCE_LABELS.get(u['from'], u['from']))}) : {u['count']} image(s)"
            for u in settings["kv_used"]
        )
        add(
            f'<p class="note">Tension réellement utilisée, image par image : {used}. Celle du fichier d\'acquisition passe avant les autres.</p>'
        )
    add("</section>")

    # ── ice and motion ──
    add("<section><h2>Glace et mouvement</h2>")
    ice = report["ice"]
    add(_scatter(report["series"]["ice"], "glace relative (ln I₀/I)", "Glace dans le temps"))
    if ice.get("calibrated"):
        add(f'<p class="note">Glace comparée aux coups d\'un trou vide ({_num(ice.get("reference"))}).</p>')
    elif ice.get("reference") is not None:
        add(
            '<p class="note">Glace comparée aux images les plus claires de la session : 0 pour les plus fines, pas une épaisseur.</p>'
        )
    histograms = [
        _histogram(dist.get(key), *MEASURE_LABELS[key])
        for key in ("ice", "ice_nm", "ice_ring", "motion", "early_motion", "ctf_thickness")
        if dist.get(key)
    ]
    if histograms:
        add(
            '<div class="thumbs" style="grid-template-columns:1fr 1fr">'
            + "".join(f"<div>{h}</div>" for h in histograms)
            + "</div>"
        )
    if not dist.get("motion"):
        add(
            '<p class="note">Le mouvement n\'est pas mesuré : ni alignement des films par le serveur, ni mesure de la dérive.</p>'
        )
    add("</section>")

    # ── acquisition parameters ──
    add("<section><h2>Paramètres d'acquisition</h2>")
    params = report["acquisition"]
    if params:
        add(
            _table(
                ("Paramètre", "Valeur(s) (nombre d'images)", "Au cours de la session"),
                (
                    (_e(PARAMETER_LABELS.get(key, key)), *_parameter_value(entry, key))
                    for key, entry in params.items()
                ),
            )
        )
        add(
            "<p class=\"note\">Lus dans les fichiers d'acquisition (XML d'EPU, mdoc de SerialEM). Un paramètre qui change en cours de session est signalé.</p>"
        )
    else:
        add("<p>Aucun fichier d'acquisition lu : les paramètres ne sont pas connus.</p>")
    add("</section>")

    # ── grid ──
    if report["squares"] or report["stage"]:
        add("<section><h2>La grille</h2>")
        squares = report["squares"]
        if squares:
            add(
                f"<p>{report['square_count']} carré(s) de grille. "
                + ("Les plus fournis en images : " if len(squares) > _MAX_SQUARE_ROWS else "")
                + "</p>"
            )
            shown = (
                sorted(squares, key=lambda s: -s["exposures"])[:_MAX_SQUARE_ROWS]
                if len(squares) > _MAX_SQUARE_ROWS
                else squares
            )
            add(
                _table(
                    (
                        "Carré",
                        "Images",
                        "Acceptées",
                        "CTF médian (Å)",
                        "Défocus médian (µm)",
                        "Glace médiane",
                    ),
                    (
                        (
                            f'<span class="mono">{_e(s["name"])}</span>',
                            str(s["exposures"]),
                            f"{s['accepted']}/{s['accepted'] + s['rejected']}" if judged else "—",
                            _num(s.get("median_fit"), 1),
                            _num(s.get("median_defocus")),
                            _num(s.get("median_ice")),
                        )
                        for s in shown
                    ),
                    (1, 2, 3, 4, 5),
                )
            )
        if report["stage"]:
            add("<h3>Positions de la platine</h3>" + _stage_map(report["stage"]) + _legend(judged))
        add("</section>")

    # ── images ──
    if report["best"] or report["worst"]:
        add("<section><h2>Images</h2>")
        if report["best"]:
            add("<h3>Les meilleures (anneaux de Thon les plus lointains)</h3>")
            add('<div class="thumbs">' + "".join(_card(c, thumbs) for c in report["best"]) + "</div>")
        if report["worst"]:
            add("<h3>" + ("Rejetées" if judged else "Les moins bonnes") + "</h3>")
            add('<div class="thumbs">' + "".join(_card(c, thumbs) for c in report["worst"]) + "</div>")
        add("</section>")

    # ── alerts, programs, all measures ──
    add("<section><h2>Alertes</h2>")
    if report["alerts"]:
        add(
            _table(
                ("Alerte", "Début", "Fin", "Durée"),
                (
                    (
                        _e(CONDITION_LABELS.get(a["condition"], a["condition"])),
                        _when(a["started"]),
                        _when(a.get("ended")) if a.get("ended") else "en cours",
                        _duration(a.get("seconds")),
                    )
                    for a in report["alerts"]
                ),
                (3,),
            )
        )
    else:
        add("<p>Aucune alerte n'a été annoncée pendant la surveillance.</p>")
    add("</section>")

    programs = report.get("programs") or {}
    if programs.get("available"):
        add("<section><h2>Programmes du serveur</h2>")
        steps = {"motion": "alignement des films", "ctf": "ajustement du CTF"}
        add(
            f"<p>{_e(', '.join(steps.get(s, s) for s in programs.get('steps', [])))} : "
            f"{'demandés' if programs.get('on') else 'non demandés'} pour cette session ; "
            f"{programs.get('done', 0)} image(s) traitée(s).</p></section>"
        )

    add("<section><h2>Toutes les mesures</h2>")
    add(
        _table(
            ("Mesure", "Images", "Médiane", "10 % – 90 %", "Min – max"),
            (
                (
                    _e(
                        MEASURE_LABELS[key][0]
                        + (f" ({MEASURE_LABELS[key][1]})" if MEASURE_LABELS[key][1] else "")
                    ),
                    str(d["n"]),
                    _num(d["median"]),
                    f"{_num(d['p10'])} – {_num(d['p90'])}",
                    f"{_num(d['min'])} – {_num(d['max'])}",
                )
                for key, d in dist.items()
                if d
            ),
            (1, 2, 3, 4),
        )
    )
    add("</section>")
    add(
        f"<footer>{_e(report['generated_by'])} · schéma {_e(report['schema'])} version {report['version']} · "
        "les mesures sont des indicateurs de suivi calculés sur chaque image ; elles ne remplacent pas le traitement. "
        "Le détail image par image est dans exposures.csv, et tous les chiffres dans report.json.</footer>"
    )
    title = f"Rapport de qualité · {session['path'].rsplit('/', 1)[-1]}"
    return (
        '<!doctype html>\n<html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        # the file fetches nothing and runs nothing, wherever it is opened
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'\">"
        f"<title>{_e(title)}</title><style>{_STYLE}</style></head><body>" + "".join(out) + "</body></html>\n"
    )


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False) + "\n"


_FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")
#: A sign that starts a formula, after a character some spreadsheet splits a line on.
_SPLIT_FORMULA: Final = re.compile(r"([;,\t\r\n])(\s*)([=+\-@])")


def _cell(value: Any) -> Any:
    """A text cell a spreadsheet would read as a formula is made plain text: a file name is
    whatever its author called it. Spaces before the sign do not save it (a spreadsheet may trim
    them on import), nor does a separator before it (one may split where this file does not)."""
    if isinstance(value, str):
        # a spreadsheet set to split on ";" (French settings) cuts inside a quoted cell: what
        # follows a ";" must not start a formula either. The exact name is in report.json.
        value = _SPLIT_FORMULA.sub(r"\1\2'\3", value)
        return "'" + value if value.lstrip(" ").startswith(_FORMULA) else value
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else value


def render_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    out = io.StringIO()
    # every text cell in quotes: a spreadsheet that splits on ";" (as French settings do) does not
    # cut "x;=2+6" into a cell of its own
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in CSV_COLUMNS])
    return out.getvalue()


def bundle(files: Mapping[str, str | bytes]) -> bytes:
    """The files of a report in one zip."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()
