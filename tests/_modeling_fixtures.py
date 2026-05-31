"""Shared synthetic fixtures for modeling tests (UPDATE plan stage 11).

All helpers build in-memory / tmp_path data only — no PostgreSQL, no real
``dataset_train.csv`` or production ``metadata_train.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from modeling.feature_schema import features_hash

MODELING_SEED = 42


def synthetic_calendar_keys(
    *,
    start: str = "2018-10-01",
    n_days: int = 1200,
    games_per_day: int = 1,
) -> pd.DataFrame:
    """Daily game calendar with ``day`` and ``game_id`` columns."""
    days = pd.date_range(start=start, periods=n_days, freq="D")
    rows: list[dict[str, object]] = []
    game_id = 1
    for day in days:
        for _ in range(games_per_day):
            rows.append({"day": day, "game_id": game_id})
            game_id += 1
    return pd.DataFrame(rows)


def minimal_feature_manifest() -> list[dict[str, str]]:
    return [
        {"name": "f_a", "dtype": "float64", "position": "0"},
        {"name": "f_b", "dtype": "float64", "position": "1"},
    ]


def synthetic_metadata(*, rows: int) -> dict:
    """Mock ``metadata_train.json`` consistent with ``minimal_feature_manifest``."""
    manifest = minimal_feature_manifest()
    rolling = [5, 10]
    cold_predict = "allow_with_flag"
    fsv = "v1"
    fh = features_hash(
        feature_manifest=manifest,
        rolling_windows=rolling,
        cold_start_policy=f"train:drop|predict:{cold_predict}",
        feature_set_version=fsv,
    )
    return {
        "mode": "train",
        "feature_set_version": fsv,
        "features_hash": fh,
        "feature_manifest": manifest,
        "rolling_windows": rolling,
        "cold_start_policy_predict": cold_predict,
        "min_prior_games": 5,
        "dataset_rows": rows,
        "code_version": "test",
        "data_snapshot_id": "test-snap",
        "dataset_built_at": "2020-01-01T00:00:00Z",
    }


def write_synthetic_train_dataset(
    tmp: Path,
    *,
    n_days: int = 6000,
    games_per_day: int = 1,
) -> tuple[Path, Path]:
    """Write ``dataset_train.csv`` + ``metadata_train.json`` under *tmp*."""
    days = pd.date_range("2018-10-01", periods=n_days, freq="D")
    rows: list[dict[str, object]] = []
    game_id = 1
    for day in days:
        for _ in range(games_per_day):
            rows.append(
                {
                    "game_id": game_id,
                    "day": day.date().isoformat(),
                    "f_a": (game_id % 10) / 10.0,
                    "f_b": ((game_id + 3) % 10) / 10.0,
                    "y_home_win": game_id % 2,
                    "y_over_5_5": (game_id + 1) % 2,
                    "feature_set_version": "v1",
                    "dataset_built_at": "2024-01-01Z",
                    "source_snapshot_id": "snap",
                }
            )
            game_id += 1
    frame = pd.DataFrame(rows)
    csv_path = tmp / "dataset_train.csv"
    meta_path = tmp / "metadata_train.json"
    frame.to_csv(csv_path, index=False)
    meta_path.write_text(json.dumps(synthetic_metadata(rows=len(frame)), indent=2), encoding="utf-8")
    return csv_path, meta_path
