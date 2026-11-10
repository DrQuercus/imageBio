"""Reader registry: one decoder per format, chosen by extension and content.

A reader lives in its own module and exposes a module-level ``READER`` instance. Built-in
readers are listed in :data:`BUILTIN_READER_MODULES`; third-party ones register through the
``imagebio.readers`` entry-point group (the entry point resolves to a :class:`Reader` instance).
"""

from __future__ import annotations

import importlib
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Final

from imagebio.core.errors import CorruptFileError, ImageBioError, NotFoundError, UnsupportedFormatError
from imagebio.core.model import Dataset

log = logging.getLogger(__name__)

SNIFF_BYTES: Final = 4096
ENTRY_POINT_GROUP: Final = "imagebio.readers"
BUILTIN_READER_MODULES: Final = (
    "imagebio.io.readers.mrc",
    "imagebio.io.readers.eer",
    "imagebio.io.readers.tiff",
    "imagebio.io.readers.raster",
)


class Reader(ABC):
    """Opens one family of file formats.

    ``extensions`` are lower-case and include the dot. A higher ``priority`` is tried first
    among the readers that claim the same extension.
    """

    name: ClassVar[str]
    extensions: ClassVar[tuple[str, ...]]
    priority: ClassVar[int] = 0

    @abstractmethod
    def sniff(self, path: Path, head: bytes) -> bool:
        """Cheap content check on the first :data:`SNIFF_BYTES` bytes. Must not raise."""

    @abstractmethod
    def open(self, path: Path, options: Mapping[str, Any] | None = None) -> Dataset:
        """Open ``path``. Raise :class:`CorruptFileError` when the file is invalid, truncated
        or uses an unsupported variant of the format. ``options`` are reader-specific and
        unknown keys must be rejected with :class:`InvalidRequestError`."""


class Registry:
    def __init__(self, readers: Sequence[Reader] = ()) -> None:
        self._readers: list[Reader] = []
        for r in readers:
            self.register(r)

    def register(self, reader: Reader) -> None:
        if any(r.name == reader.name for r in self._readers):
            raise ValueError(f"a reader named {reader.name!r} is already registered")
        for ext in reader.extensions:
            if not (ext.startswith(".") and ext == ext.lower()):
                raise ValueError(f"reader {reader.name!r}: bad extension {ext!r}")
        self._readers.append(reader)

    @property
    def readers(self) -> tuple[Reader, ...]:
        return tuple(self._readers)

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset(ext for r in self._readers for ext in r.extensions)

    def supports(self, name: str) -> bool:
        """True when a reader claims the extension of ``name`` (used for directory listings)."""
        return _suffix(name) in self.extensions

    def candidates(self, path: Path, head: bytes) -> list[Reader]:
        """Readers to try, best first.

        1. readers that claim the extension and recognise the content;
        2. readers that recognise the content under another extension (mis-named files);
        3. readers that claim the extension but do not recognise the content: some formats
           (old MRC files) have no reliable magic number, so they still get a chance.
        """
        ext = _suffix(path.name)
        by_priority = sorted(self._readers, key=lambda r: -r.priority)
        sniffed = {r.name: _safe_sniff(r, path, head) for r in by_priority}
        claims = [r for r in by_priority if ext in r.extensions]
        first = [r for r in claims if sniffed[r.name]]
        second = [r for r in by_priority if sniffed[r.name] and r not in claims]
        third = [r for r in claims if not sniffed[r.name]]
        return first + second + third

    def open(self, path: str | Path, options: Mapping[str, Any] | None = None) -> Dataset:
        """Open ``path`` with the first reader that accepts it."""
        p = Path(path)
        try:
            with p.open("rb") as f:
                head = f.read(SNIFF_BYTES)
        except (FileNotFoundError, NotADirectoryError):
            raise NotFoundError(f"file not found: {p.name}") from None
        except IsADirectoryError:
            raise UnsupportedFormatError(f"{p.name} is a directory") from None
        candidates = self.candidates(p, head)
        if not candidates:
            raise UnsupportedFormatError(f"no reader for {p.name}")
        first_error: ImageBioError | None = None
        for reader in candidates:
            try:
                return reader.open(p, options)
            except CorruptFileError as e:
                # keep the most relevant diagnostic: the one of the best candidate
                first_error = first_error or e
                log.debug("reader %s rejected %s: %s", reader.name, p.name, e)
        assert first_error is not None
        raise first_error


def _suffix(name: str) -> str:
    return Path(name).suffix.lower()


def _safe_sniff(reader: Reader, path: Path, head: bytes) -> bool:
    try:
        return bool(reader.sniff(path, head))
    except Exception:
        log.exception("reader %s: sniff() raised on %s", reader.name, path.name)
        return False


# ─── Default registry ─────────────────────────────────────────────────────────

_default: Registry | None = None
_default_lock = threading.Lock()


def _load_builtin(module_name: str) -> Reader | None:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name == module_name:  # the reader module itself is absent (optional reader)
            log.warning("built-in reader module %s is missing", module_name)
            return None
        raise
    reader = module.READER
    if not isinstance(reader, Reader):
        raise TypeError(f"{module_name}.READER must be a Reader instance")
    return reader


def default_registry() -> Registry:
    """The process-wide registry with built-in and entry-point readers (created on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            reg = Registry()
            for module_name in BUILTIN_READER_MODULES:
                reader = _load_builtin(module_name)
                if reader is not None:
                    reg.register(reader)
            for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
                try:
                    plugin = ep.load()
                    if not isinstance(plugin, Reader):
                        raise TypeError("entry point must resolve to a Reader instance")
                    reg.register(plugin)
                except Exception:
                    log.exception("could not load reader plugin %s", ep.name)
            _default = reg
        return _default


def reset_default_registry() -> None:
    """Forget the default registry (tests)."""
    global _default
    with _default_lock:
        _default = None


def open_dataset(path: str | Path, options: Mapping[str, Any] | None = None) -> Dataset:
    """Open ``path`` with the default registry."""
    return default_registry().open(path, options)
