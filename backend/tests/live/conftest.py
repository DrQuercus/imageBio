"""Fixtures of the live tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fixtures.live import Watch, make_watch


@pytest.fixture
def watch(tmp_path: Path) -> Iterator[Watch]:
    w = make_watch(tmp_path)
    yield w
    w.index.close()
