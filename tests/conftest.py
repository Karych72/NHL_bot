"""Shared pytest fixtures for modeling tests."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def matplotlib_agg_backend() -> None:
    """Pay matplotlib import cost once for PNG-related tests."""
    import matplotlib

    matplotlib.use("Agg", force=True)
