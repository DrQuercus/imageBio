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

__all__ = ["QualityRules"]

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
