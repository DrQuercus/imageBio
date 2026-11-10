"""What the files of an acquisition session are, from their names alone.

A live monitor shows *exposures*, not files: EPU writes for each exposure an integrated image,
its metadata, a preview and often a movie; SerialEM writes an image or a frame stack and an
``.mdoc``. The functions here give each file its role and the exposure it belongs to, and each
exposure its category in an EPU session. Nothing here touches the disk: classification must
cost nothing when a directory of 50 000 files is listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from imagebio.cryo.epu import exposure_name

__all__ = ["PRIMARY_ROLES", "Member", "Role", "category_of", "classify", "name_time", "primary_rank"]

Role = Literal["image", "movie", "preview", "xml", "mdoc"]

#: Roles a tile can be drawn from, best first: the integrated image, else the movie, else the
#: preview the acquisition program wrote.
PRIMARY_ROLES: Final[tuple[Role, ...]] = ("image", "movie", "preview")

_IMAGE: Final = frozenset({".mrc", ".mrcs", ".st", ".tif", ".tiff"})
_MOVIE: Final = frozenset({".eer"})
_PREVIEW: Final = frozenset({".jpg", ".jpeg", ".png"})
_MOVIE_STEM: Final = re.compile(r"_(fractions|eer)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Member:
    """A file as a member of an exposure."""

    role: Role
    exposure: str  # name shared by the files of the exposure


def classify(name: str) -> Member | None:
    """The role of file ``name``, or None for a file a monitor ignores (hidden files, session
    databases, gain references, text notes…)."""
    if name.startswith(".") or name.endswith("~"):
        return None
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        return None
    ext = f".{extension.lower()}"
    role: Role
    if ext == ".xml":
        role = "xml"
    elif ext == ".mdoc":
        role = "mdoc"
    elif ext in _PREVIEW:
        role = "preview"
    elif ext in _MOVIE:
        role = "movie"
    elif ext in _IMAGE:
        role = "movie" if _MOVIE_STEM.search(stem) else "image"
    else:
        return None
    return Member(role, exposure_name(name))


def primary_rank(role: str) -> int:
    """Lower is better; roles that cannot be shown rank last."""
    return next((i for i, known in enumerate(PRIMARY_ROLES) if known == role), len(PRIMARY_ROLES))


Category = Literal["data", "gridsquare", "foilhole", "atlas", "image"]


def category_of(directory: str, exposure: str) -> Category:
    """Where an exposure stands in an EPU session, from the directory it is in (relative, with
    ``/``) and its name. Anything that does not follow the EPU layout is a plain ``image``.

    * ``data``: an acquisition, ``…/Data/FoilHole_<hole>_Data_<…>``;
    * ``foilhole``: the overview of a hole, ``…/FoilHoles/FoilHole_<…>``;
    * ``gridsquare``: the overview of a square, ``…/GridSquare_<id>/GridSquare_<…>``;
    * ``atlas``: under an ``Atlas`` directory.
    """
    parts = [p for p in directory.split("/") if p]
    last = parts[-1] if parts else ""
    if any(p == "Atlas" for p in parts):
        return "atlas"
    if last == "Data" and exposure.startswith("FoilHole_") and "_Data_" in exposure:
        return "data"
    if last == "FoilHoles" and exposure.startswith("FoilHole_"):
        return "foilhole"
    if last.startswith("GridSquare_") and exposure.startswith("GridSquare_"):
        return "gridsquare"
    return "image"


_NAME_TIME: Final = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?!\d)")


def name_time(name: str) -> float | None:
    """The acquisition time written in an EPU file name (``…_20261110_101500…``), as seconds
    since the epoch, or None. The name holds the local time of the microscope PC; it is taken
    as the local time of this server. It orders the exposures of a session without a single
    ``stat``; the XML, when read, gives the exact time with its zone."""
    matches = _NAME_TIME.findall(name)
    if not matches:
        return None
    year, month, day, hour, minute, second = (
        int(part) for part in matches[-1]
    )  # the hole id may look like a date
    try:
        return datetime(year, month, day, hour, minute, second).timestamp()
    except ValueError:
        return None
