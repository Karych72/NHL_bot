"""Build canonical team-game long facts and enforce pair integrity."""

from __future__ import annotations

from typing import Dict, List, Tuple

import pandas as pd


def build_team_game_facts(
    raw_game_team_stats: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, List[dict]]]:
    report: Dict[str, List[dict]] = {"dropped_games": []}

    if raw_game_team_stats.empty:
        return raw_game_team_stats.copy(), report

    data = raw_game_team_stats.copy()
    data["game_id"] = data["game_id"].astype("int64")
    data["team_id"] = data["team_id"].astype("int64")
    data["home_team_id"] = data["home_team_id"].astype("int64")
    data["away_team_id"] = data["away_team_id"].astype("int64")
    data["day"] = pd.to_datetime(data["day"]).dt.normalize()

    counts = data.groupby("game_id")["team_id"].size().rename("team_rows").reset_index()
    broken = counts[counts["team_rows"] != 2]
    if not broken.empty:
        bad_ids = set(broken["game_id"].tolist())
        for row in broken.to_dict(orient="records"):
            report["dropped_games"].append(
                {
                    "reason": "expected_exactly_two_team_rows",
                    "game_id": int(row["game_id"]),
                    "actual_rows": int(row["team_rows"]),
                }
            )
        data = data[~data["game_id"].isin(bad_ids)].copy()

    if data.empty:
        return data, report

    left = data.copy()
    right = data.copy()
    merged = left.merge(right, on="game_id", suffixes=("", "_opp"))
    merged = merged[merged["team_id"] != merged["team_id_opp"]].copy()

    merged["opponent_team_id"] = merged["team_id_opp"]
    merged["is_home"] = (merged["team_id"] == merged["home_team_id"]).astype("int64")

    feature_pairs = [
        "goals",
        "shots",
        "pim",
        "power_play_percentage",
        "power_play_goals",
        "power_play_opportunities",
        "face_off_win_percentage",
        "blocked",
        "takeaways",
        "giveaways",
        "hits",
    ]
    for feature in feature_pairs:
        merged[f"{feature}_for"] = merged[feature]
        merged[f"{feature}_against"] = merged[f"{feature}_opp"]

    keep_columns = [
        "game_id",
        "day",
        "season_id",
        "team_id",
        "opponent_team_id",
        "is_home",
        "home_team_id",
        "away_team_id",
    ] + [f"{feature}_{suffix}" for feature in feature_pairs for suffix in ("for", "against")]
    return merged[keep_columns].copy(), report
