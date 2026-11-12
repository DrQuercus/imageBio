"""Acceptance thresholds of a watched session: what its user calls a good image.

The thresholds are kept with the watch; the judgement itself is made where the images are
shown (every measurement an image has is in its item), so that changing a threshold costs no
request and no scan. A measurement an image does not have is not held against it.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from typing import Any, Final

from imagebio.core.errors import InvalidRequestError

__all__ = [
    "DARK_ABOVE",
    "EMPTY_BELOW",
    "ICE_RING_LIMIT",
    "QualityRules",
    "ctf_of",
    "drift_of",
    "image_flags",
    "rejections",
    "relative_ice",
    "verdict",
]

#: Above this ice-ring ratio the ice is probably crystalline (what cryoSPARC users reject at).
ICE_RING_LIMIT: Final = 1.2
#: Relative ice above which an image has a tenth of the counts of the reference: a grid bar,
#: or ice too thick to see through.
DARK_ABOVE: Final = math.log(10)
#: Relative ice under which an image is as bright as an empty hole (within 5 %): only said when
#: the reference is the counts of an empty hole.
EMPTY_BELOW: Final = 0.05

#: For each threshold: the bounds a value must lie in, and what it is called in an error.
_BOUNDS: Final[dict[str, tuple[float, float, str]]] = {
    "fit_max": (1.0, 50.0, "fit (Å)"),
    "ice_max": (0.01, 10.0, "ice (relative)"),
    "ring_max": (1.0, 100.0, "ring (ratio)"),
    "drift_max": (0.1, 1000.0, "drift (Å)"),
    "defocus_min": (0.0, 50.0, "defocus_min (µm)"),
    "defocus_max": (0.0, 50.0, "defocus_max (µm)"),
}


@dataclass(frozen=True, slots=True)
class QualityRules:
    """An image is rejected when one of its measurements is beyond a threshold that is set."""

    #: Thon rings must be found at least this far (Å): a larger resolution of the fit rejects.
    fit_max: float | None = None
    #: Relative ice, ln(I0 / I), above which the ice is too thick.
    ice_max: float | None = None
    #: Ice-ring ratio above which the ice is crystalline.
    ring_max: float | None = None
    #: Drift between the first and the last frames (Å).
    drift_max: float | None = None
    #: Measured defocus (µm, positive underfocus) must lie between these two.
    defocus_min: float | None = None
    defocus_max: float | None = None

    def __post_init__(self) -> None:
        for name, (low, high, label) in _BOUNDS.items():
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise InvalidRequestError(f"{label} must be a number")
            if not low <= value <= high:
                raise InvalidRequestError(f"{label} must be between {low:g} and {high:g}")
            object.__setattr__(self, name, float(value))
        if (
            self.defocus_min is not None
            and self.defocus_max is not None
            and self.defocus_min >= self.defocus_max
        ):
            raise InvalidRequestError("defocus_min must be smaller than defocus_max")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, text: str | None) -> QualityRules:
        if not text:
            return cls()
        data = json.loads(text)
        return cls(**{f.name: data.get(f.name) for f in fields(cls)})

    @property
    def any(self) -> bool:
        return any(getattr(self, f.name) is not None for f in fields(self))


# The judgement of an image, as the interface makes it (frontend/src/live/store.ts): the report
# of a session must say what its user saw. Both are tested on tests/fixtures/verdict_cases.json.


def _number(value: Any) -> float | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok else None


def _shown(value: float) -> str:
    """A threshold as the interface writes it (a JavaScript number): 5, 1.2, 0.05."""
    return str(int(value)) if value.is_integer() else repr(value)


def relative_ice(metrics: dict[str, Any] | None, reference: float | None) -> float | None:
    """ln(reference / mean counts): 0 for an image as bright as the reference."""
    mean = _number((metrics or {}).get("mean"))
    if mean is None or not mean > 0 or reference is None or not reference > 0:
        return None
    return math.log(reference / mean)


def ctf_of(metrics: dict[str, Any] | None, processing: dict[str, Any] | None) -> dict[str, Any] | None:
    """The CTF of an image: the fit of the program the server ran on it when there is one (it
    knows astigmatism), else the quick estimate made on every image."""
    fitted = (processing or {}).get("ctf")
    if isinstance(fitted, dict) and "error" not in fitted and _number(fitted.get("defocus")) is not None:
        return {
            "defocus": float(fitted["defocus"]),
            "fit": _number(fitted.get("fit")),
            "astigmatism": _number(fitted.get("astigmatism")),
            "source": "program",
        }
    quick = (metrics or {}).get("ctf")
    if isinstance(quick, dict) and _number(quick.get("defocus")) is not None:
        return {
            "defocus": float(quick["defocus"]),
            "fit": _number(quick.get("fit")),
            "astigmatism": None,
            "source": "estimate",
        }
    return None


def drift_of(metrics: dict[str, Any] | None, processing: dict[str, Any] | None) -> dict[str, Any] | None:
    """How far the specimen moved: the path followed by a motion correction when the server ran
    one, else the shift measured between the first and the last frames of the movie."""
    motion = (processing or {}).get("motion")
    if isinstance(motion, dict) and "error" not in motion and _number(motion.get("total")) is not None:
        return {
            "value": float(motion["total"]),
            "unit": "A" if motion.get("unit") == "A" else "px",
            "path": True,
        }
    drift = _number((metrics or {}).get("drift"))
    if drift is None:
        return None
    return {"value": drift, "unit": "A" if (metrics or {}).get("drift_unit") == "A" else "px", "path": False}


def rejections(
    metrics: dict[str, Any] | None,
    processing: dict[str, Any] | None,
    rules: QualityRules,
    reference: float | None,
) -> list[tuple[str, str]]:
    """Why an image is rejected: (what, sentence) for each threshold it is beyond. A measurement
    the image does not have is not held against it."""
    if not metrics:
        return []
    why: list[tuple[str, str]] = []
    ctf = ctf_of(metrics, processing)
    if (
        rules.fit_max is not None
        and ctf is not None
        and ctf["fit"] is not None
        and ctf["fit"] > rules.fit_max
    ):
        why.append(
            ("fit", f"anneaux de Thon jusqu'à {ctf['fit']:.1f} Å seulement (seuil {_shown(rules.fit_max)})")
        )
    ice = relative_ice(metrics, reference)
    if rules.ice_max is not None and ice is not None and ice > rules.ice_max:
        why.append(("ice", f"glace {ice:.2f} (seuil {_shown(rules.ice_max)})"))
    ring = _number(metrics.get("ice_ring"))
    if rules.ring_max is not None and ring is not None and ring > rules.ring_max:
        why.append(("ring", f"anneau de glace {ring:.2f} (seuil {_shown(rules.ring_max)})"))
    drift = drift_of(metrics, processing)
    if (
        rules.drift_max is not None
        and drift is not None
        and drift["unit"] == "A"
        and drift["value"] > rules.drift_max
    ):
        what = "mouvement" if drift["path"] else "dérive"
        why.append(("drift", f"{what} {drift['value']:.1f} Å (seuil {_shown(rules.drift_max)})"))
    if ctf is not None and (
        (rules.defocus_min is not None and ctf["defocus"] < rules.defocus_min)
        or (rules.defocus_max is not None and ctf["defocus"] > rules.defocus_max)
    ):
        low = _shown(rules.defocus_min) if rules.defocus_min is not None else "…"
        high = _shown(rules.defocus_max) if rules.defocus_max is not None else "…"
        why.append(("defocus", f"défocus {ctf['defocus']:.2f} µm hors de {low} à {high}"))
    return why


def verdict(
    state: str,
    metrics: dict[str, Any] | None,
    processing: dict[str, Any] | None,
    rules: QualityRules,
    reference: float | None,
) -> str | None:
    """``accepted`` or ``rejected``; None: nothing to judge it on (no threshold, not measured)."""
    if state != "complete" or _number((metrics or {}).get("mean")) is None or not rules.any:
        return None
    return "rejected" if rejections(metrics, processing, rules, reference) else "accepted"


def image_flags(
    metrics: dict[str, Any] | None, reference: float | None, calibrated: bool = False
) -> list[str]:
    """What is worth a mark on an image: crystalline ice, very dark, empty hole."""
    flags: list[str] = []
    ring = _number((metrics or {}).get("ice_ring"))
    if ring is not None and ring > ICE_RING_LIMIT:
        flags.append("ice_ring")
    ice = relative_ice(metrics, reference)
    if ice is not None and ice > DARK_ABOVE:
        flags.append("dark")
    if ice is not None and calibrated and ice < EMPTY_BELOW:
        flags.append("empty")
    return flags
