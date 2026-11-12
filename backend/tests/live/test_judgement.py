"""The server judges an image as the interface does: the report of a session says what its user
saw on the tiles. The cases are shared with frontend/tests/live.test.ts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imagebio.live.quality import QualityRules, image_flags, rejections, verdict

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "verdict_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_judgement_of_an_image(case: dict) -> None:
    item, rules = case["item"], QualityRules(**case["rules"])
    got = verdict(item["state"], item["metrics"], item["processing"], rules, case["reference"])
    assert got == case["verdict"]
    if got is not None:
        reasons = rejections(item["metrics"], item["processing"], rules, case["reference"])
        assert [code for code, _ in reasons] == case["reasons"]
    assert image_flags(item["metrics"], case["reference"], case["calibrated"]) == case["flags"]


def test_the_reasons_are_written_as_the_interface_writes_them() -> None:
    rules = QualityRules(
        fit_max=5, ice_max=0.1, ring_max=1.2, drift_max=10, defocus_min=0.5, defocus_max=1.25
    )
    metrics = {
        "mean": 50.0,
        "ice_ring": 1.456,
        "ctf": {"defocus": 1.876, "fit": 6.04, "score": 9},
        "drift": 12.34,
        "drift_unit": "A",
    }
    assert [text for _, text in rejections(metrics, None, rules, 100.0)] == [
        "anneaux de Thon jusqu'à 6.0 Å seulement (seuil 5)",
        "glace 0.69 (seuil 0.1)",
        "anneau de glace 1.46 (seuil 1.2)",
        "dérive 12.3 Å (seuil 10)",
        "défocus 1.88 µm hors de 0.5 à 1.25",
    ]
    only_low = rejections(
        {"mean": 1, "ctf": {"defocus": 0.2, "fit": 3}}, None, QualityRules(defocus_min=0.5), None
    )
    assert only_low == [("defocus", "défocus 0.20 µm hors de 0.5 à …")]
