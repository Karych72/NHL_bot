"""NHL dataset builder package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["DatasetBuildConfig", "build_dataset"]

if TYPE_CHECKING:
    from .base import DatasetBuildConfig as DatasetBuildConfig
    from .base import build_dataset as build_dataset


def __getattr__(name: str) -> Any:
    if name == "DatasetBuildConfig":
        from .base import DatasetBuildConfig

        return DatasetBuildConfig
    if name == "build_dataset":
        from .base import build_dataset

        return build_dataset
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
