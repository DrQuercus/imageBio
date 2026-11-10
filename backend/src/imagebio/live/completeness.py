"""Is a file complete, or still being written?

The legacy monitor declared a file complete as soon as its size stopped changing for two
seconds: a pause of the microscope PC in the middle of a write produced half-read micrographs
(``docs/audit/AUDIT.md``, LIVE). Here a file is complete only when **all** of these hold
(``docs/PLAN.md``, principle 9):

1. its size and modification time did not change between two polls at least
   ``min_stable`` seconds apart (:func:`is_stable`);
2. it passes an integrity check of its format (:func:`check_integrity`): an MRC holds every
   section its header announces, the page chain of a TIFF or EER ends inside the file, an EER
   holds the fractions its metadata announce, a JPEG ends with its end marker, an XML parses;
3. when the acquisition program writes a metadata file last (EPU's ``.xml``), that file is
   there and complete too. That part needs to know about the other files of the exposure and
   lives in the scanner.

The integrity check reuses the readers: they already refuse truncated files, and what they
learn (size, pixel size, frame count) is kept for the index.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from imagebio.core.errors import CorruptFileError, ImageBioError, NotFoundError, UnsupportedFormatError
from imagebio.core.model import DatasetInfo
from imagebio.cryo.epu import EpuImageMetadata, read_epu_xml
from imagebio.io.registry import Registry

__all__ = ["Integrity", "check_integrity", "is_stable"]

_TAIL_BYTES: Final = 64


def is_stable(stable_since: float, polls: int, now: float, min_stable: float) -> bool:
    """Whether a file seen ``polls`` times with the same size and date, first at
    ``stable_since``, can be considered at rest: at least two polls, ``min_stable`` apart."""
    return polls >= 2 and now - stable_since >= min_stable


@dataclass(frozen=True, slots=True)
class Integrity:
    """Result of :func:`check_integrity`. ``reason`` is a public message (no server path)."""

    ok: bool
    reason: str | None = None
    #: What the reader found, for images and movies.
    info: DatasetInfo | None = None
    #: The parsed metadata, for an EPU XML.
    epu: EpuImageMetadata | None = None


def _tail(path: Path) -> bytes:
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - _TAIL_BYTES))
        return f.read(_TAIL_BYTES)


def _check_preview(path: Path) -> Integrity:
    suffix = path.suffix.lower()
    try:
        tail = _tail(path)
    except FileNotFoundError:
        return Integrity(False, f"{path.name}: the file disappeared")
    except OSError:
        return Integrity(False, f"{path.name}: cannot be read")
    if suffix in (".jpg", ".jpeg") and b"\xff\xd9" not in tail:
        return Integrity(
            False, f"{path.name}: the JPEG end marker is missing (truncated or still being written)"
        )
    if suffix == ".png" and b"IEND" not in tail:
        return Integrity(
            False, f"{path.name}: the PNG end chunk is missing (truncated or still being written)"
        )
    return Integrity(True)


def check_integrity(path: Path, role: str, registry: Registry) -> Integrity:
    """Check that ``path`` is a complete file of its kind. Never raises for a bad file: the
    answer says why it is not complete. ``role`` is the one given by ``live.layout.classify``."""
    if role == "mdoc":
        return Integrity(True)  # a text file that grows with its stack: nothing to check
    if role == "xml":
        try:
            return Integrity(True, epu=read_epu_xml(path))
        except CorruptFileError as e:
            return Integrity(False, e.public_message)
    if role == "preview":
        return _check_preview(path)
    try:
        dataset = registry.open(path)
    except CorruptFileError as e:
        return Integrity(False, e.public_message)
    except NotFoundError:
        return Integrity(False, f"{path.name}: the file disappeared")
    except UnsupportedFormatError:
        # nothing can read it: there is nothing to check either, a stable size has to do
        return Integrity(True, f"{path.name}: format not checked")
    except ImageBioError as e:
        return Integrity(False, e.public_message)
    except OSError:
        return Integrity(False, f"{path.name}: cannot be read")
    try:
        info = dataset.info
    finally:
        dataset.close()
    if info.metadata.get("complete") is False:
        # the reader says why among its warnings, next to others that are not about this
        told = (w for w in info.warnings if "truncated" in w or "being written" in w)
        reason = next(told, "holds fewer images than announced")
        return Integrity(False, reason if path.name in reason else f"{path.name}: {reason}", info)
    return Integrity(True, info=info)
