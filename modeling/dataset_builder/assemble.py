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
        built.extend([home_col, away_col, f"diff_{feature}"])
        if feature in ("elo", "shot_share_ewm"):
            # Задача 40 (Task 3; see docs/modeling_dataset_builder.md, "Elo
            # team-strength feature"): home_elo+away_elo carries no signal
            # beyond what diff_elo already does (it is ~2*mean after season
            # regression, not a team-strength comparison) — skip it. Same for
            # shot_share_ewm (Задача 68): the sum of two shares carries no more
            # signal than their difference.
            continue
        data[f"sum_{feature}"] = data[home_col] + data[away_col]
        built.append(f"sum_{feature}")
    return data, built


def _assert_train_labels_match_facts(data: pd.DataFrame) -> None:
    """Train labels must match joined target-game facts before goals are dropped."""
    winner = pd.to_numeric(data["winner_id"], errors="coerce")
    home_tid = pd.to_numeric(data["home_team_id"], errors="coerce")
    y_win = pd.to_numeric(data["y_home_win"], errors="coerce")
    expected_win = (winner == home_tid).astype("float64")
    if not (y_win == expected_win).fillna(False).all():
        raise ValueError("y_home_win inconsistent with winner_id vs home_team_id")

    hg = pd.to_numeric(data["home_goals_target"], errors="coerce")
    ag = pd.to_numeric(data["away_goals_target"], errors="coerce")
    y_over = pd.to_numeric(data["y_over_5_5"], errors="coerce")
    expected_over = ((hg + ag) > 5.5).astype("float64")
    if not (y_over == expected_over).all():
        raise ValueError("y_over_5_5 inconsistent with home_goals_target + away_goals_target")


def apply_cold_start_policy(
    data: pd.DataFrame,
    mode: str,
    min_prior_games: int,
) -> ColdStartResult:
    out = data.copy()
    low_hist_mask = (
        out["home_prior_games_count"].astype("int64") < min_prior_games
    ) | (
        out["away_prior_games_count"].astype("int64") < min_prior_games
    )
    return ColdStartResult(
        data=out[~low_hist_mask].copy(),
        dropped_rows=int(low_hist_mask.sum()),
        reason=f"cold_start_policy_{mode}_drop",
    )


def attach_train_labels(data: pd.DataFrame) -> pd.DataFrame:
    """Add ``y_home_win`` and ``y_over_5_5`` to train rows.

    Raises ``ValueError`` when a finished game has no goals in ``game_team_stats``:
    a silent zero would mislabel it as under 5.5.
    """
    out = data.copy()
    out["y_home_win"] = (out["winner_id"] == out["home_team_id"]).astype("int64")
    goals = out[["home_goals_target", "away_goals_target"]]
    if goals.isna().any().any():
        bad = out.loc[goals.isna().any(axis=1), "game_id"].tolist()[:10]
        raise ValueError(f"train games without goals in game_team_stats (first game_ids: {bad})")
    total_goals = goals.sum(axis=1)
    out["y_over_5_5"] = (total_goals > 5.5).astype("int64")
    return out


def assemble_dataset(
    mode: str,
    snapshots: pd.DataFrame,
    min_prior_games: int,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    report: Dict[str, Any] = {"drops": []}
    data, built_feature_columns = _wide_feature_columns(snapshots)
    cold = apply_cold_start_policy(
        data=data,
        mode=mode,
        min_prior_games=min_prior_games,
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
