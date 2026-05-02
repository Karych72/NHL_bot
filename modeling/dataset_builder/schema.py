"""Schema manifest and parity helpers for dataset artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Sequence

import pandas as pd

LABEL_COLUMNS = ("y_home_win", "y_over_5_5")
KEY_COLUMNS = ("game_id", "day", "season_id", "home_team_id", "away_team_id")
SERVICE_COLUMNS = (
    "feature_set_version",
    "dataset_built_at",
    "source_snapshot_id",
    "low_history_confidence",
    "quality_warnings",
)


def _normalize_dtype(value: str) -> str:
    if value.startswith("int"):
        return "int64"
    if value.startswith("float"):
        return "float64"
    if value.startswith("datetime64"):
        return "datetime64[ns]"
    return value


def feature_columns_from_df(df: pd.DataFrame) -> List[str]:
    return [
        column
        for column in df.columns
        if column not in KEY_COLUMNS
        and column not in LABEL_COLUMNS
        and column not in SERVICE_COLUMNS
    ]


def build_feature_manifest(df: pd.DataFrame, feature_columns: Sequence[str]) -> List[Dict[str, str]]:
    return [
        {"name": column, "dtype": _normalize_dtype(str(df[column].dtype)), "position": str(idx)}
        for idx, column in enumerate(feature_columns)
    ]


def features_hash(
    feature_manifest: Sequence[Dict[str, str]],
    rolling_windows: Sequence[int],
    cold_start_policy: str,
    feature_set_version: str,
) -> str:
    payload = {
        "manifest": list(feature_manifest),
        "rolling_windows": list(rolling_windows),
        "cold_start_policy": cold_start_policy,
        "feature_set_version": feature_set_version,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_feature_parity(
    predict_df: pd.DataFrame,
    train_manifest: Sequence[Dict[str, str]],
) -> None:
    train_columns = [item["name"] for item in train_manifest]
    predict_columns = feature_columns_from_df(predict_df)
    if predict_columns != train_columns:
        raise ValueError(
            "Predict feature schema mismatch: expected "
            f"{train_columns}, got {predict_columns}"
        )

    for item in train_manifest:
        expected = _normalize_dtype(item["dtype"])
        actual = _normalize_dtype(str(predict_df[item["name"]].dtype))
        if actual != expected:
            raise ValueError(
                f"Predict dtype mismatch for {item['name']}: expected {expected}, got {actual}"
            )


def ordered_columns_for_output(
    mode: str,
    df: pd.DataFrame,
    train_manifest: Iterable[Dict[str, str]] | None = None,
) -> List[str]:
    feature_cols = (
        [item["name"] for item in train_manifest]
        if train_manifest is not None
        else feature_columns_from_df(df)
    )
    out = [col for col in KEY_COLUMNS if col in df.columns]
    out.extend(feature_cols)
    if mode == "train":
        out.extend([col for col in LABEL_COLUMNS if col in df.columns])
        out.extend([col for col in ("feature_set_version", "dataset_built_at", "source_snapshot_id") if col in df.columns])
    else:
        out.extend(
            [col for col in ("feature_set_version", "dataset_built_at", "low_history_confidence", "quality_warnings") if col in df.columns]
        )
    return out
