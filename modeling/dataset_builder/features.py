"""Rolling and match-level feature building with strict as-of joins."""

from __future__ import annotations

from typing import Sequence

import pandas as pd


ROLLING_BASE_FIELDS = (
    "goals_for",
    "goals_against",
    "shots_for",
    "shots_against",
    "pim_for",
    "pim_against",
    "power_play_percentage_for",
    "power_play_percentage_against",
)


def compute_team_rolling_features(
    team_game_facts: pd.DataFrame,
    rolling_windows: Sequence[int],
) -> pd.DataFrame:
    if team_game_facts.empty:
        return team_game_facts.copy()

    data = team_game_facts.sort_values(["team_id", "day", "game_id"]).copy()
    grp = data.groupby("team_id", sort=False)

    data["prev_day"] = grp["day"].shift(1)
    data["rest_days"] = (data["day"] - data["prev_day"]).dt.days.fillna(99).astype("int64")
    data["is_b2b"] = (data["rest_days"] <= 1).astype("int64")

    day_num = data["day"].map(pd.Timestamp.toordinal)
    data["_day_num"] = day_num
    data["games_last_7d"] = grp["_day_num"].transform(
        lambda s: s.apply(lambda v: int(((s < v) & (s >= v - 7)).sum()))
    )
    data["prior_games_count"] = grp.cumcount()

    for window in rolling_windows:
        for field in ROLLING_BASE_FIELDS:
            rolled = grp[field].transform(
                lambda s: s.shift(1).rolling(window=window, min_periods=1).mean()
            )
            data[f"{field}_roll_mean_{window}"] = rolled.astype("float64")

    data["goal_diff_roll_mean_5"] = (
        data.get("goals_for_roll_mean_5", 0.0) - data.get("goals_against_roll_mean_5", 0.0)
    ).astype("float64")
    data["pace_sum_roll_mean_5"] = (
        data.get("shots_for_roll_mean_5", 0.0) + data.get("shots_against_roll_mean_5", 0.0)
    ).astype("float64")
    return data.drop(columns=["prev_day", "_day_num"])


def _snapshot_side(
    targets: pd.DataFrame,
    team_features: pd.DataFrame,
    side_team_col: str,
    prefix: str,
) -> pd.DataFrame:
    left = targets[["game_id", "day", side_team_col]].copy()
    left = left.rename(columns={side_team_col: "team_id"})
    left = left.sort_values(["team_id", "day", "game_id"]).copy()
    right = team_features.sort_values(["team_id", "day", "game_id"]).copy()
    right["hist_day"] = right["day"]

    merged_parts = []
    for team_id, left_team in left.groupby("team_id", sort=False):
        left_team = left_team.sort_values(["day", "game_id"])
        right_team = right[right["team_id"] == team_id].sort_values(["day", "game_id"])
        if right_team.empty:
            part = left_team.copy()
            merged_parts.append(part)
            continue
        part = pd.merge_asof(
            left_team,
            right_team,
            on="day",
            allow_exact_matches=False,
            direction="backward",
        )
        merged_parts.append(part)

    merged = pd.concat(merged_parts, ignore_index=True)
    merged = merged.rename(columns={"game_id_x": "game_id", "game_id_y": f"{prefix}_hist_game_id", "hist_day": f"{prefix}_hist_day"})
    rename_columns = {
        col: f"{prefix}_{col}"
        for col in merged.columns
        if col not in ("game_id", "day", "team_id", f"{prefix}_hist_game_id", f"{prefix}_hist_day")
    }
    merged = merged.rename(columns=rename_columns)
    merged = merged.rename(columns={"team_id": f"{prefix}_team_id"})
    return merged


def build_match_feature_snapshots(
    target_games: pd.DataFrame,
    team_features: pd.DataFrame,
) -> pd.DataFrame:
    if target_games.empty:
        return target_games.copy()

    targets = target_games.copy()
    targets["day"] = pd.to_datetime(targets["day"]).dt.normalize()
    home = _snapshot_side(targets, team_features, "home_team_id", "home")
    away = _snapshot_side(targets, team_features, "away_team_id", "away")
    out = targets.merge(home, on=["game_id", "day"], how="left").merge(away, on=["game_id", "day"], how="left")
    return out
