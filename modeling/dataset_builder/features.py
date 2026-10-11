"""Rolling and match-level feature building with strict as-of joins.

Rolling windows and as-of history snapshots are grouped by (team_id, season_id),
not team_id alone: a team's first game of a new season starts with an empty
window, never carrying rolling stats or a snapshot row from the prior season.
Team Elo (below) is the one exception: it is a cross-team, cross-season
strength rating that persists between seasons (shrunk toward the mean, not
reset) — see ``modeling.elo.compute_pregame_elo``. The smoothed shot share
(Задача 68, below) is likewise cross-season.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd

from modeling.elo import ELO_START


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
    """Compute per-team rolling/context features, reset at each season boundary.

    Args:
        team_game_facts: long team-game rows (one row per team per game) with
            at least team_id, season_id, day, game_id and the ROLLING_BASE_FIELDS.
        rolling_windows: window sizes (in games) for the trailing means.

    Windows, prior-game counts and rest-day features are grouped by
    (team_id, season_id) so a team's stats never leak across a season change.
    """
    if team_game_facts.empty:
        return team_game_facts.copy()

    data = team_game_facts.sort_values(["team_id", "season_id", "day", "game_id"]).copy()
    grp = data.groupby(["team_id", "season_id"], sort=False)

    data["prev_day"] = grp["day"].shift(1)
    intra_day_prev = (data["day"] == data["prev_day"]) & data["prev_day"].notna()
    rest = (data["day"] - data["prev_day"]).dt.days
    rest = rest.mask(intra_day_prev)
    data["rest_days"] = rest.fillna(99).astype("int64")
    data["is_b2b"] = (data["rest_days"] <= 1).astype("int64")

    day_num = data["day"].map(pd.Timestamp.toordinal)
    data["_day_num"] = day_num
    data["games_last_7d"] = grp["_day_num"].transform(
        lambda s: s.apply(lambda v: int(((s < v) & (s >= v - 7)).sum()))
    )
    data["prior_games_count"] = grp.cumcount()

    for window in rolling_windows:
        for field in ROLLING_BASE_FIELDS:
            shifted = grp[field].shift(1)
            shifted = shifted.mask(intra_day_prev)
            rolled = shifted.groupby(
                [data["team_id"], data["season_id"]], sort=False
            ).transform(lambda s: s.rolling(window=window, min_periods=1).mean())
            data[f"{field}_roll_mean_{window}"] = rolled.astype("float64")

    goals_for_rm = (
        data["goals_for_roll_mean_5"]
        if "goals_for_roll_mean_5" in data.columns
        else pd.Series(0.0, index=data.index, dtype="float64")
    )
    goals_against_rm = (
        data["goals_against_roll_mean_5"]
        if "goals_against_roll_mean_5" in data.columns
        else pd.Series(0.0, index=data.index, dtype="float64")
    )
    shots_for_rm = (
        data["shots_for_roll_mean_5"]
        if "shots_for_roll_mean_5" in data.columns
        else pd.Series(0.0, index=data.index, dtype="float64")
    )
    shots_against_rm = (
        data["shots_against_roll_mean_5"]
        if "shots_against_roll_mean_5" in data.columns
        else pd.Series(0.0, index=data.index, dtype="float64")
    )
    data["goal_diff_roll_mean_5"] = (goals_for_rm - goals_against_rm).astype("float64")
    data["pace_sum_roll_mean_5"] = (shots_for_rm + shots_against_rm).astype("float64")
    return data.drop(columns=["prev_day", "_day_num"])


def _snapshot_side(
    targets: pd.DataFrame,
    team_features: pd.DataFrame,
    side_team_col: str,
    prefix: str,
) -> pd.DataFrame:
    """As-of snapshot of one side's (home/away) most recent prior-game features.

    The as-of match (`merge_asof`, backward, no exact-day matches) is scoped to
    (team_id, season_id) pairs, not team_id alone: a team's first game of a new
    season must never snapshot a row from the previous season.
    """
    left = targets[["game_id", "day", "season_id", side_team_col]].copy()
    left = left.rename(columns={side_team_col: "team_id"})
    left = left.sort_values(["team_id", "season_id", "day", "game_id"]).copy()
    right = team_features.sort_values(["team_id", "season_id", "day", "game_id"]).copy()
    right["hist_day"] = right["day"]
    rightCols = [c for c in right.columns if c != "day"]
    right = right.rename(columns={c: f"__r_{c}" for c in rightCols})

    merged_parts = []
    for (team_id, season_id), left_team in left.groupby(["team_id", "season_id"], sort=False):
        left_team = left_team.sort_values(["day", "game_id"])
        right_team = right[
            (right["__r_team_id"] == team_id) & (right["__r_season_id"] == season_id)
        ].sort_values(["day", "__r_game_id"])
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
    merged = merged.drop(columns=["__r_team_id", "__r_season_id", "season_id"], errors="ignore")
    rename_out: dict[str, str] = {}
    for col in list(merged.columns):
        if col in ("game_id", "day", "team_id"):
            continue
        if col == "__r_game_id":
            rename_out[col] = f"{prefix}_hist_game_id"
        elif col == "__r_hist_day":
            rename_out[col] = f"{prefix}_hist_day"
        elif col.startswith("__r_"):
            rename_out[col] = f"{prefix}_{col[4:]}"
    merged = merged.rename(columns=rename_out)
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
    # targets already carry home_team_id / away_team_id; dropping avoids pandas merge suffixes (_x/_y).
    home = home.drop(columns=["home_team_id"], errors="ignore")
    away = away.drop(columns=["away_team_id"], errors="ignore")
    out = targets.merge(home, on=["game_id", "day"], how="left").merge(away, on=["game_id", "day"], how="left")
    return out


def attach_pregame_elo(
    target_games: pd.DataFrame,
    per_game_elo: pd.DataFrame,
    final_ratings: dict[int, float],
) -> pd.DataFrame:
    """Add ``home_elo``/``away_elo`` to every row of ``target_games``.

    A row found by ``game_id`` in ``per_game_elo`` gets its pre-game rating;
    a row not found there (predict mode: no result yet) falls back to
    ``final_ratings`` — see ``modeling.elo.compute_pregame_elo``'s docstring for the as-of
    contract behind both.
    """
    out = target_games.merge(per_game_elo, on="game_id", how="left")
    missing = out["home_elo"].isna()
    if missing.any():
        home_ids = out.loc[missing, "home_team_id"].astype("int64")
        away_ids = out.loc[missing, "away_team_id"].astype("int64")
        out.loc[missing, "home_elo"] = home_ids.map(final_ratings).fillna(ELO_START)
        out.loc[missing, "away_elo"] = away_ids.map(final_ratings).fillna(ELO_START)
    out["home_elo"] = out["home_elo"].astype("float64")
    out["away_elo"] = out["away_elo"].astype("float64")
    return out


# Задача 68: shot share of a team's games, smoothed over its games across seasons.
SHOT_SHARE_START = 0.5
SHOT_SHARE_HALFLIFE_GAMES = 20
SHOT_SHARE_ALPHA = 1.0 - 0.5 ** (1.0 / SHOT_SHARE_HALFLIFE_GAMES)
SHOT_SHARE_SEASON_KEEP = 0.5


def _shrink_share(share: float) -> float:
    """Pull a shot share toward ``SHOT_SHARE_START`` by ``1 - SHOT_SHARE_SEASON_KEEP``."""
    return SHOT_SHARE_START + SHOT_SHARE_SEASON_KEEP * (share - SHOT_SHARE_START)


def compute_pregame_shot_share(played_games: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, tuple[float, int]]]:
    """Pre-game exponentially smoothed shot share of both teams, plus each team's final state.

    The ``home_win`` model's shot-share feature (Задача 68). A team's share of the shots in a
    game, ``shots_for / (shots_for + shots_against)``, updates ``s += α·(x − s)`` (half-life
    ``SHOT_SHARE_HALFLIFE_GAMES``), starting at 0.5; at a team's first game of a new season it
    is shrunk toward 0.5 by ``SHOT_SHARE_SEASON_KEEP``. Like Elo it runs over the whole played
    history, not per season, and a day's games all read the state frozen at the start of that
    day.

    Args:
        played_games: one row per played game with ``game_id``, ``day``, ``season_id``,
            ``home_team_id``, ``away_team_id``, ``home_shots``, ``away_shots``.

    Returns:
        ``(per_game, final_state)``: ``game_id``, ``home_shot_share_ewm``,
        ``away_shot_share_ewm`` per game; ``{team_id: (share, season_id of its last game)}``.

    Raises:
        ValueError: a played game lacks shots, or both sides have zero shots.
    """
    games = played_games.assign(day=pd.to_datetime(played_games["day"]).dt.normalize())
    games = games.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    total = games["home_shots"] + games["away_shots"]
    bad = games["home_shots"].isna() | games["away_shots"].isna() | (total <= 0)
    if bad.any():
        raise ValueError(f"game {int(games.loc[bad, 'game_id'].iloc[0])} has missing or zero shots")
    home_x = (games["home_shots"] / total).tolist()
    ids = games["game_id"].astype("int64").tolist()
    days = games["day"].tolist()
    seasons = games["season_id"].astype("int64").tolist()
    homes = games["home_team_id"].astype("int64").tolist()
    aways = games["away_team_id"].astype("int64").tolist()

    state: dict[int, tuple[float, int]] = {}
    pending: list[tuple[int, float, int]] = []
    cur_day = None
    rows: list[tuple[int, float, float]] = []
    for i, game_id in enumerate(ids):
        if days[i] != cur_day:
            state.update({team: (share, season) for team, share, season in pending})
            pending = []
            cur_day = days[i]
        season = seasons[i]
        pre = []
        for team in (homes[i], aways[i]):
            share, last_season = state.get(team, (SHOT_SHARE_START, season))
            pre.append(_shrink_share(share) if last_season != season else share)
        rows.append((game_id, pre[0], pre[1]))
        pending.append((homes[i], pre[0] + SHOT_SHARE_ALPHA * (home_x[i] - pre[0]), season))
        pending.append((aways[i], pre[1] + SHOT_SHARE_ALPHA * (1.0 - home_x[i] - pre[1]), season))
    state.update({team: (share, season) for team, share, season in pending})
    per_game = pd.DataFrame(rows, columns=["game_id", "home_shot_share_ewm", "away_shot_share_ewm"])
    return per_game, state


def attach_pregame_shot_share(
    target_games: pd.DataFrame,
    per_game_share: pd.DataFrame,
    final_state: dict[int, tuple[float, int]],
) -> pd.DataFrame:
    """Add ``home_shot_share_ewm``/``away_shot_share_ewm`` to every row of ``target_games``.

    A row found by ``game_id`` in ``per_game_share`` gets its pre-game value; a row not found
    there (predict mode: no result yet) falls back to the team's ``final_state``, shrunk toward
    0.5 when the target's ``season_id`` is later than the state's. A team without state gets 0.5.
    """
    out = target_games.merge(per_game_share, on="game_id", how="left")
    missing = out["home_shot_share_ewm"].isna()
    for side in ("home", "away"):
        col = f"{side}_shot_share_ewm"
        for idx in out.index[missing]:
            share, last_season = final_state.get(int(out.at[idx, f"{side}_team_id"]), (SHOT_SHARE_START, 0))
            if int(out.at[idx, "season_id"]) > last_season:
                share = _shrink_share(share)
            out.at[idx, col] = share
        out[col] = out[col].astype("float64")
    return out
