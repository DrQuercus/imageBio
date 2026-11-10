"""Shared pytest configuration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``tests/fixtures`` importable as ``fixtures`` from every test module.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Performance budgets only run when asked for (``pytest -m perf``)."""
    if config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="performance budget: run with -m perf")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip)
