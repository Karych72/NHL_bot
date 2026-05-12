"""Assemble final train/predict datasets from feature snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import pandas as pd


@dataclass
class ColdStartResult:
    data: pd.DataFrame
    dropped_rows: int
    reason: str


def _wide_feature_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    data = df.copy()
    home_columns = [col for col in data.columns if col.startswith("home_") and "team_id" not in col and "hist_" not in col]
    built: List[str] = []
    for home_col in home_columns:
        feature = home_col.removeprefix("home_")
        if feature == "goals_target":
            continue
        away_col = f"away_{feature}"
        if away_col not in data.columns:
            continue
        data[f"diff_{feature}"] = data[home_col] - data[away_col]
        data[f"sum_{feature}"] = data[home_col] + data[away_col]
        built.extend([home_col, away_col, f"diff_{feature}", f"sum_{feature}"])
    return data, built


def _assert_train_labels_match_facts(data: pd.DataFrame) -> None:
    """Train labels must match joined target-game facts before goals are dropped."""
    winner = pd.to_numeric(data["winner_id"], errors="coerce")
    home_tid = pd.to_numeric(data["home_team_id"], errors="coerce")
    y_win = pd.to_numeric(data["y_home_win"], errors="coerce")
    expected_win = (winner == home_tid).astype("float64")
    if not (y_win == expected_win).fillna(False).all():
        raise ValueError("y_home_win inconsistent with winner_id vs home_team_id")

    hg = pd.to_numeric(data["home_goals_target"], errors="coerce").fillna(0)
    ag = pd.to_numeric(data["away_goals_target"], errors="coerce").fillna(0)
    y_over = pd.to_numeric(data["y_over_5_5"], errors="coerce")
    expected_over = ((hg + ag) > 5.5).astype("float64")
    if not (y_over == expected_over).all():
        raise ValueError("y_over_5_5 inconsistent with home_goals_target + away_goals_target")


def apply_cold_start_policy(
    data: pd.DataFrame,
    mode: str,
    min_prior_games: int,
    cold_start_policy_predict: str,
) -> ColdStartResult:
    out = data.copy()
    home_prior = out["home_prior_games_count"] if "home_prior_games_count" in out.columns else pd.Series(0, index=out.index)
    away_prior = out["away_prior_games_count"] if "away_prior_games_count" in out.columns else pd.Series(0, index=out.index)
    low_hist_mask = (
        home_prior.fillna(0).astype("int64") < min_prior_games
    ) | (
        away_prior.fillna(0).astype("int64") < min_prior_games
    )

    if mode == "train":
        dropped = int(low_hist_mask.sum())
        return ColdStartResult(
            data=out[~low_hist_mask].copy(),
            dropped_rows=dropped,
            reason="cold_start_policy_train_drop",
        )

    out["low_history_confidence"] = low_hist_mask.astype("int64")
    out["quality_warnings"] = out["low_history_confidence"].map(
        lambda v: "LOW_HISTORY" if int(v) == 1 else ""
    )
    if cold_start_policy_predict == "drop":
        dropped = int(low_hist_mask.sum())
        out = out[~low_hist_mask].copy()
        return ColdStartResult(data=out, dropped_rows=dropped, reason="cold_start_policy_predict_drop")
    return ColdStartResult(data=out, dropped_rows=0, reason="cold_start_policy_predict_allow_with_flag")


def attach_train_labels(data: pd.DataFrame) -> pd.DataFrame:
    out = data.copy()
    out["y_home_win"] = (out["winner_id"] == out["home_team_id"]).astype("int64")
    total_goals = out["home_goals_target"].fillna(0) + out["away_goals_target"].fillna(0)
    out["y_over_5_5"] = (total_goals > 5.5).astype("int64")
    return out


def assemble_dataset(
    mode: str,
    snapshots: pd.DataFrame,
    min_prior_games: int,
    cold_start_policy_predict: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    report: Dict[str, Any] = {"drops": []}
    data, built_feature_columns = _wide_feature_columns(snapshots)
    cold = apply_cold_start_policy(
        data=data,
        mode=mode,
        min_prior_games=min_prior_games,
        cold_start_policy_predict=cold_start_policy_predict,
    )
    data = cold.data
    if cold.dropped_rows > 0:
        report["drops"].append({"reason": cold.reason, "rows": cold.dropped_rows})

    if mode == "train":
        data = attach_train_labels(data)
        _assert_train_labels_match_facts(data)
        data = data.drop(columns=["winner_id", "home_goals_target", "away_goals_target"], errors="ignore")
    else:
        data = data.drop(columns=["winner_id", "home_goals_target", "away_goals_target"], errors="ignore")

    # Keep compact, deterministic ordering for downstream manifest generation.
    key_cols = ["game_id", "day", "season_id", "home_team_id", "away_team_id"]
    present_key_cols = [col for col in key_cols if col in data.columns]
    trailing = [col for col in data.columns if col not in present_key_cols]
    ordered = present_key_cols + trailing
    data = data[ordered].copy()
    report["built_feature_columns"] = built_feature_columns
    return data, report
