"""Shared synthetic fixtures for modeling tests (UPDATE plan stage 11).

All helpers build in-memory / tmp_path data only — no PostgreSQL, no real
``dataset_train.csv`` or production ``metadata_train.json``.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from modeling.feature_schema import features_hash

MODELING_SEED = 42


def synthetic_season_id(index: int) -> int:
    """``index`` 0 -> 20182019, 1 -> 20192020, ... (NHL ``season_id`` format)."""
    return (2018 + index) * 10000 + 2019 + index


def synthetic_calendar_keys(
    *,
    start: str = "2018-10-01",
    n_days: int = 1200,
    games_per_day: int | Sequence[int] = 1,
    days_per_season: int | None = None,
) -> pd.DataFrame:
    """Daily game calendar with ``day``, ``game_id`` and ``season_id`` columns.

    ``days_per_season`` cuts the calendar into consecutive seasons of that many
    days (``synthetic_season_id`` 0, 1, ...); ``None`` = one season.

    ``games_per_day`` is either a constant (every day has the same number of
    games) or a sequence of counts cycled day by day, to build an irregular
    real-calendar-shaped fixture (NHL game days have 3-11 games, never a
    constant). The cycled form is what exposes a block boundary landing
    inside a day — the scenario Задача 32 added, absent from every split
    fixture that predates it.
    """
    days = pd.date_range(start=start, periods=n_days, freq="D")
    counts: Sequence[int] = (
        [games_per_day] * n_days if isinstance(games_per_day, int) else games_per_day
    )
    counts_cycle = itertools.cycle(counts)
    rows: list[dict[str, object]] = []
    game_id = 1
    for day_index, day in enumerate(days):
        season_id = synthetic_season_id(day_index // days_per_season if days_per_season else 0)
        for _ in range(next(counts_cycle)):
            rows.append({"day": day, "game_id": game_id, "season_id": season_id})
            game_id += 1
    return pd.DataFrame(rows)


def minimal_feature_manifest() -> list[dict[str, str]]:
    return [
        {"name": "f_a", "dtype": "float64", "position": "0"},
        {"name": "f_b", "dtype": "float64", "position": "1"},
        {"name": "home_prior_games_count", "dtype": "int64", "position": "2"},
        {"name": "away_prior_games_count", "dtype": "int64", "position": "3"},
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
    n_days: int = 2000,
    games_per_day: int = 1,
    n_seasons: int = 4,
) -> tuple[Path, Path]:
    """Write ``dataset_train.csv``, ``games_train.csv`` + ``metadata_train.json`` under *tmp*.

    ``n_days`` are split into ``n_seasons`` equal seasons (``synthetic_season_id``). Labels
    are learnable from ``f_a`` (not from team ids); ``games_train.csv`` agrees with them (home wins iff
    ``y_home_win``) and cycles REG/OT/SO decisions.
    """
    days = pd.date_range("2018-10-01", periods=n_days, freq="D")
    days_per_season = max(1, n_days // n_seasons)
    rng = np.random.default_rng(MODELING_SEED)
    rows: list[dict[str, object]] = []
    games: list[dict[str, object]] = []
    game_id = 1
    for day_index, day in enumerate(days):
        season_id = synthetic_season_id(min(day_index // days_per_season, n_seasons - 1))
        for _ in range(games_per_day):
            # Teams are unrelated to the labels, so Elo has no signal to find.
            home = int(rng.integers(1, 9))
            away = int(rng.integers(1, 8))
            away += away >= home
            home_win = game_id % 2
            rows.append(
                {
                    "game_id": game_id,
                    "day": day.date().isoformat(),
                    "season_id": season_id,
                    "home_team_id": home,
                    "away_team_id": away,
                    "f_a": (game_id % 10) / 10.0,
                    "f_b": ((game_id + 3) % 10) / 10.0,
                    "home_prior_games_count": 5 + game_id % 30,
                    "away_prior_games_count": 5 + (game_id * 7) % 30,
                    "y_home_win": home_win,
                    "y_over_5_5": (game_id + 1) % 2,
                    "feature_set_version": "v1",
                    "dataset_built_at": "2024-01-01Z",
                    "source_snapshot_id": "snap",
                }
            )
            games.append(
                {
                    "game_id": game_id,
                    "day": day.date().isoformat(),
                    "season_id": season_id,
                    "home_team_id": home,
                    "away_team_id": away,
                    "home_goals": 3 if home_win else 2,
                    "away_goals": 2 if home_win else 3,
                    "decision": ("REG", "OT", "SO", "REG", "REG")[game_id % 5],
                }
            )
            game_id += 1
    frame = pd.DataFrame(rows)
    csv_path = tmp / "dataset_train.csv"
    meta_path = tmp / "metadata_train.json"
    frame.to_csv(csv_path, index=False)
    pd.DataFrame(games).to_csv(tmp / "games_train.csv", index=False)
    meta_path.write_text(json.dumps(synthetic_metadata(rows=len(frame)), indent=2), encoding="utf-8")
    return csv_path, meta_path


def sample_compose_kwargs(
    *,
    task: str = "home_win",
    model: str = "logreg",
    run_id: str | None = None,
    pooled_model_ll: float = 0.55,
) -> dict:
    """Keyword arguments for ``modeling.report.compose_metrics_json`` with plausible numbers.

    Pooled model log-loss is ``pooled_model_ll``; Elo (home_win only) is 0.67, constant 0.69.
    """
    run_id = run_id or f"{task}_{model}_deadbeef_20260101T000000Z"
    has_elo = task == "home_win"

    def metrics(ll: float) -> dict:
        return {"log_loss": ll, "brier": 0.21, "ece": 0.04}

    def ci(point: float) -> dict:
        return {"point": point, "ci_low": point - 0.01, "ci_high": point + 0.01}

    seasons = []
    for index in range(2):
        block = {
            "season_id": synthetic_season_id(index + 2),
            "n_train": 1000,
            "n_test": 100,
            "test_range": {"start": "2020-10-01", "end": "2021-04-30"},
            "model_raw": metrics(0.62),
            "model": metrics(pooled_model_ll),
            "constant": {"p": 0.54, **metrics(0.69)},
        }
        if has_elo:
            block.update(elo_raw=metrics(0.68), elo=metrics(0.67))
        seasons.append(block)
    pooled = {
        "n_test": 200,
        "model_raw": metrics(0.62),
        "model": metrics(pooled_model_ll),
        "constant": metrics(0.69),
        "diff_ci": {"model_minus_constant": ci(pooled_model_ll - 0.69)},
        "bootstrap": {
            name: {**ci(0.5), "bootstrap.N": 100, "bootstrap.block_by_day": True, "bootstrap.seed": 42}
            for name in ("log_loss", "brier")
        },
        "reliability_path": f"reliability_{task}.png",
    }
    calibration_row = {"bin_lower": 0.5, "bin_upper": 0.55, "count": 12, "mean_pred": 0.52, "frac_positive": 0.58}
    calibration_table = {"model": [calibration_row]}
    if has_elo:
        pooled.update(elo_raw=metrics(0.68), elo=metrics(0.67))
        pooled["diff_ci"]["model_minus_elo"] = ci(pooled_model_ll - 0.67)
        calibration_table["elo"] = [calibration_row]
    return {
        "run_id": run_id,
        "task": task,
        "model": model,
        "features_hash": "b334df68cab14a12056b7a41b324face3cc9cd835c30b738caffdef1b72f81a1",
        "seasons": seasons,
        "pooled": pooled,
        "calibration_table": calibration_table,
        "slices": {
            "season_start": {"n": 20, "model_log_loss": 0.6, "constant_log_loss": 0.69, "elo_log_loss": 0.68},
            "post_olympic_break": {"n": 0},
        },
        "team_breakdown": {
            "home_team_id": [
                {"team_id": 1, "n_games": 10, "log_loss": 0.8, "log_loss_minus_overall": 0.2},
                {"team_id": 2, "n_games": 12, "log_loss": 0.5, "log_loss_minus_overall": -0.1},
            ],
            "away_team_id": [],
        },
        "evaluation": {"epsilon_clip": 1e-15, "ece_bins": 10},
    }
