"""What RELION says of the classes of a 2D classification, to label a gallery of averages.

``run_it025_classes.mrcs`` holds the class averages; next to it ``run_it025_model.star`` has a
table ``data_model_classes`` with, for each of them, the share of the particles it received
(``rlnClassDistribution``) and its estimated resolution.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Final

from imagebio.core.errors import CorruptFileError
from imagebio.cryo.star import find_table

__all__ = ["model_star_name", "read_classes"]

_SUFFIX: Final = "_classes.mrcs"
_MAX_CLASSES: Final = 5000
_MAX_BYTES: Final = 64 * 2**20


def model_star_name(stack_name: str) -> str | None:
    """Name of the model file that describes the class averages ``stack_name``, if it is one."""
    if not stack_name.lower().endswith(_SUFFIX):
        return None
    return stack_name[: -len(_SUFFIX)] + "_model.star"


def _finite(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_classes(model: Path) -> list[dict[str, Any]]:
    """The classes of a RELION model file: ``index`` (0-based plane of the averages),
    ``distribution`` (share of the particles, 0 to 1) and ``resolution`` (Å, None when RELION
    gives none: an empty class)."""
    if model.stat().st_size > _MAX_BYTES:
        raise CorruptFileError("the model file is too large to be one")
    table = find_table(model, ("rlnReferenceImage", "rlnClassDistribution"), block="model_classes")
    if table is None:
        raise CorruptFileError("no class table (data_model_classes) in this model file")
    columns = ["rlnReferenceImage", "rlnClassDistribution"]
    resolved = table.has("rlnEstimatedResolution")
    if resolved:
        columns.append("rlnEstimatedResolution")
    classes: list[dict[str, Any]] = []
    for row in table.rows(columns):
        number, at, _ = row[0].partition("@")  # "000003@Class2D/job008/run_it025_classes.mrcs"
        share = _finite(row[1])
        if not at or not number.isdigit() or int(number) < 1 or share is None or len(classes) >= _MAX_CLASSES:
            raise CorruptFileError("the class table of the model file cannot be read")
        classes.append(
            {
                "index": int(number) - 1,
                "distribution": share,
                "resolution": _finite(row[2]) if resolved else None,
            }
        )
    return classes
