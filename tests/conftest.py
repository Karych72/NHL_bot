"""Shared pytest fixtures for modeling tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._modeling_fixtures import write_synthetic_train_dataset


@pytest.fixture(scope="session")
def matplotlib_agg_backend() -> None:
    """Pay matplotlib import cost once for PNG-related tests."""
    import matplotlib

    matplotlib.use("Agg", force=True)


@pytest.fixture
def synthetic_train_dataset(tmp_path: Path) -> tuple[Path, Path]:
    """Mini ``dataset_train.csv`` + ``metadata_train.json`` in *tmp_path*."""
    return write_synthetic_train_dataset(tmp_path)
