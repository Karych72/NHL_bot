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
    # season_id is a groupby/filter key in features.py (compute_team_rolling_features's
    # groupby(["team_id", "season_id"]), _snapshot_side's right["__r_season_id"] ==
    # season_id) — cast alongside the other key columns so a NULL season_id can't
    # silently upcast the column to float64 and change grouping/filter behavior.
    data["season_id"] = data["season_id"].astype("int64")
    data["day"] = pd.to_datetime(data["day"]).dt.normalize()

    # power_play_percentage is stored NULL when a team had 0 PP opportunities
    # (0/0 undefined for display — docs/pipeline_nulls_and_explicit_null_tz.md:68;
    # safe_pct in pipeline/load_season_modern.py also returns None for missing
    # numerator/denominator, a distinct "unknown" case that must stay NaN and hit
    # the fail-fast NaN check below rather than being silently zeroed). Narrowed
    # to the documented 0-opportunities case only: as of 2026-09-12 all 168 NULL
    # rows in game_team_stats have power_play_opportunities == 0.
    data.loc[data["power_play_opportunities"] == 0, "power_play_percentage"] = 0.0

    # A handful of games (3 of 13120 rows as of 2026-09-12, all season 20232024:
    # game_id 2023020627/2023020788/2023021236) have power_play_percentage above
    # 100 — the play-by-play-derived opportunity count in
    # pipeline/load_season_modern.py undercounts distinct power plays relative to
    # power-play goals scored within them in rare cases. Fixing that counting is
    # out of scope here; clip to the meaningful upper bound so the rate feature
    # stays interpretable instead of failing the dataset builder's range check.
    # No lower bound: no code path produces a negative percentage.
    data["power_play_percentage"] = data["power_play_percentage"].clip(upper=100.0)

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
