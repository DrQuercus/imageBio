"""The report as files: what a hostile file name can do to them, and what they hold."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from html.parser import HTMLParser

from imagebio.live.quality import QualityRules
from imagebio.live.report import CSV_COLUMNS, build_report, exposure_rows
from imagebio.live.report_render import bundle, render_csv, render_html, render_json
from live.test_report import T0, context, exposure, session

HOSTILE = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    '=HYPERLINK("http://evil.example","x")',
    "+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(A1:A2)",
    "javascript:alert(1)",
    "</style><style>*{display:none}",
    "a\u202eb",
]


class Tags(HTMLParser):
    """Every tag and attribute of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def hostile_session() -> list:
    images = session(len(HOSTILE))
    return [
        type(e)(
            **{**{f: getattr(e, f) for f in e.__slots__}, "name": name, "path": f"cryo/session/{name}.mrc"}
        )
        for e, name in zip(images, HOSTILE, strict=True)
    ]


def test_a_file_name_is_text_in_the_page_whatever_it_holds() -> None:
    images = hostile_session()
    report = build_report(images, context(rules=QualityRules(fit_max=3.5, ring_max=1.2)))
    page = render_html(report, {e.id: b"\xff\xd8 not really a jpeg" for e in images})
    tags = Tags()
    tags.feed(page)
    names = {tag for tag, _ in tags.tags}
    assert "script" not in names and "iframe" not in names and "object" not in names and "link" not in names
    assert sum(1 for t, _ in tags.tags if t == "style") == 1  # its own, and nothing a name could open
    for tag, attrs in tags.tags:
        for name, value in attrs.items():
            assert not name.startswith("on"), (tag, name)  # no event handler
            if name in ("src", "href"):
                assert (value or "").startswith("data:image/jpeg;base64,"), (
                    tag,
                    name,
                    value,
                )  # nothing fetched
    csp = [a for t, a in tags.tags if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
    assert csp and csp[0]["content"] == "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
    # the names are there, as text
    for name in (
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;",
        "&lt;/style&gt;",
    ):
        assert name in page
    assert "<script" not in page.lower() and "javascript:" not in page.replace("javascript:alert(1)", "")


def test_a_cell_a_spreadsheet_would_run_is_written_as_text() -> None:
    images = hostile_session()
    ctx = context(rules=QualityRules(fit_max=3.5))
    text = render_csv(exposure_rows(images, ctx))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == list(CSV_COLUMNS) and len(rows) == len(HOSTILE)
    by_name = {row["name"].lstrip("'"): row for row in rows}
    for name in HOSTILE:
        cell = next(r["name"] for r in rows if r["name"].lstrip("'") == name)
        if name[0] in "=+-@":
            assert cell == "'" + name  # a formula no more
        else:
            assert cell == name
    # numbers stay numbers, and say what was measured
    first = by_name[HOSTILE[0]]
    assert (
        float(first["ctf_fit_A"]) == 3.0
        and first["verdict"] == "accepted"
        and first["ctf_source"] == "estimate"
    )
    assert (
        float(first["stage_x_um"]) == 0.0
        and first["verified"] == "yes"
        and first["grid_square"] == "GridSquare_100"
    )
    rejected = next(r for r in rows if r["verdict"] == "rejected")
    assert rejected["reasons"].startswith("anneaux de Thon jusqu'à")


def test_the_page_holds_the_figures_and_the_charts() -> None:
    images = session(30)
    report = build_report(images, context(rules=QualityRules(fit_max=5.0, ring_max=1.2)))
    page = render_html(report, {0: b"\xff\xd8jpeg"})
    assert "Rapport de qualité de la collecte" in page and "cryo/session" in page
    assert "<b>20</b> images acceptées sur 30 jugées (67 %)" in page
    assert "<b>a changé 1 fois</b>" in page  # the magnification, image 20
    assert "130\u202f000" in page and "165\u202f000" in page  # written as French does
    assert page.count("<svg") >= 6 and page.count("<rect") > 30 and page.count("<circle") >= 90
    assert page.count('src="data:image/jpeg;base64,') == 1 and "vignette indisponible" in page
    assert "anneaux de Thon pas assez loin" in page and "glace cristalline" in page
    assert "Aucune alerte" in page and "Aucune pause de plus de dix minutes" in page
    assert "À savoir avant de lire ces chiffres" in page and "estimation rapide" in page
    assert re.search(r"Images du \d\d/\d\d/\d{4} \d\d:\d\d au", page)

    empty = render_html(build_report([], context()))
    assert "Aucune image acquise" in empty and "<svg" not in empty  # nothing drawn from nothing


def test_the_files_of_a_report_go_together() -> None:
    images = session(5)
    ctx = context()
    report = build_report(images, ctx)
    files = {
        "report.html": render_html(report),
        "report.json": render_json(report),
        "exposures.csv": render_csv(exposure_rows(images, ctx)),
    }
    archive = zipfile.ZipFile(io.BytesIO(bundle(files)))
    assert sorted(archive.namelist()) == ["exposures.csv", "report.html", "report.json"]
    assert json.loads(archive.read("report.json"))["session"]["first"] == T0
    assert archive.read("report.html").decode().startswith("<!doctype html>")
    assert exposure(0).name in archive.read("exposures.csv").decode()
