"""Rolling and match-level feature building with strict as-of joins.

Rolling windows and as-of history snapshots are grouped by (team_id, season_id),
not team_id alone: a team's first game of a new season starts with an empty
window, never carrying rolling stats or a snapshot row from the prior season.
Team Elo (below) is the one exception: it is a cross-team, cross-season
strength rating that persists between seasons (shrunk toward the mean, not
reset) — see ``compute_pregame_elo``.
"""

from __future__ import annotations

import math
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


# Задача 40 (Task 3, spike-findings.md §3): tuned pre-game team-strength Elo.
# K sizes each update, HFA is added to the home side before the win-expectancy
# calc, SEASON_REGRESSION shrinks every team 1/3 toward the field mean at a
# season_id change (Elo persists across seasons instead of resetting, unlike
# the rolling/as-of features above), START is the rating an unseen team gets,
# and the MOV_* pair implement the margin-of-victory multiplier
# ln(|goal_diff|+1)*MOV_BASE/(MOV_HFA_WEIGHT*winner_edge+MOV_BASE). Tuning
# picked ot_s=1 (OT/SO wins count as full wins, same as a regulation win), so
# there is no separate overtime branch here.
ELO_START = 1500.0
ELO_K = 8.0
ELO_HFA = 35.0
ELO_SEASON_REGRESSION = 1.0 / 3.0
ELO_MOV_BASE = 2.2
ELO_MOV_HFA_WEIGHT = 0.001


def compute_pregame_elo(played_games: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, float]]:
    """Pre-game team Elo rating for every played game, plus the final ratings.

    Why a separate pass: rolling/as-of features above are scoped to
    ``(team_id, season_id)`` and reset every season. Elo is the opposite by
    design (Задача 40a) — a single league-wide rating that carries across
    seasons, shrunk toward the mean rather than reset — so it cannot reuse
    that per-team grouping and instead makes one sequential pass over the
    full play history in ``(day, game_id)`` order, mutating a single
    ``{team_id: rating}`` dict as it goes.

    Args:
        played_games: one row per played game with ``game_id``, ``day``,
            ``season_id``, ``home_team_id``, ``away_team_id``, ``winner_id``,
            ``home_goals``, ``away_goals``. Order does not matter; this
            function sorts by ``(day, game_id)`` itself. Every row must have
            a non-null ``winner_id``/``home_goals``/``away_goals`` — a played
            game with no recorded result is a data bug, not a case to paper
            over silently (Global Constraint 4).

    Returns:
        ``(per_game, final_ratings)``:

        - ``per_game``: ``game_id``, ``home_elo``, ``away_elo`` — the rating
          each side held strictly *before* that game (mirroring
          ``compute_team_rolling_features``'s ``shift(1)``). Games sharing a
          calendar day never see each other's result: ratings are frozen at
          the start of the day and every game that day is scored off that
          same snapshot, with all of that day's updates applied together
          afterwards — the Elo analogue of this module's ``intra_day_prev``
          policy for rolling features (a same-day predecessor is masked out,
          not used). On real NHL data no team plays twice in a day, so this
          only matters for two different games sharing a day, and even then
          only changes the result when they also share a team.
        - ``final_ratings``: ``team_id -> rating`` after the very last
          processed game, unaffected by any season transition that has not
          actually been played through yet. ``base.py`` uses this to give
          predict-mode target games (no result yet) the rating "as of after
          the last played game" instead of a per-game as-of join.
    """
    per_game_dtypes = {"game_id": "int64", "home_elo": "float64", "away_elo": "float64"}
    if played_games.empty:
        return pd.DataFrame(columns=list(per_game_dtypes)).astype(per_game_dtypes), {}

    games = played_games.copy()
    games["day"] = pd.to_datetime(games["day"]).dt.normalize()
    games = games.sort_values(["day", "game_id"], kind="mergesort")
    if games[["winner_id", "home_goals", "away_goals"]].isna().any().any():
        raise ValueError("compute_pregame_elo: a played game is missing winner_id/goals")

    ratings: dict[int, float] = {}
    season_id: int | None = None
    out_game_id: list[int] = []
    out_home_elo: list[float] = []
    out_away_elo: list[float] = []

    for _, day_games in games.groupby("day"):
        day_season = int(day_games["season_id"].iloc[0])
        if season_id is not None and day_season != season_id and ratings:
            mean_rating = sum(ratings.values()) / len(ratings)
            ratings = {
                team: mean_rating + (1 - ELO_SEASON_REGRESSION) * (rating - mean_rating)
                for team, rating in ratings.items()
            }
        season_id = day_season

        pregame_snapshot = dict(ratings)  # frozen for the whole day: no intra-day leakage
        deltas: dict[int, float] = {}
        day_sorted = day_games.sort_values("game_id")
        # Plain Python lists, not itertuples: a namedtuple's per-column type is
        # the union of every dtype seen across this module's callers, which
        # mypy then refuses to subtract/compare/index a dict with.
        rows = zip(
            day_sorted["game_id"].tolist(),
            day_sorted["home_team_id"].tolist(),
            day_sorted["away_team_id"].tolist(),
            day_sorted["winner_id"].tolist(),
            day_sorted["home_goals"].tolist(),
            day_sorted["away_goals"].tolist(),
        )
        for game_id, home_team_id, away_team_id, winner_id, home_goals, away_goals in rows:
            home_team_id = int(home_team_id)
            away_team_id = int(away_team_id)
            home_rating = pregame_snapshot.get(home_team_id, ELO_START)
            away_rating = pregame_snapshot.get(away_team_id, ELO_START)
            out_game_id.append(int(game_id))
            out_home_elo.append(home_rating)
            out_away_elo.append(away_rating)

            diff = home_rating + ELO_HFA - away_rating
            expected_home = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
            home_won = int(winner_id) == home_team_id
            actual_home = 1.0 if home_won else 0.0
            winner_edge = diff if home_won else -diff
            abs_goal_diff = abs(float(home_goals) - float(away_goals))
            mov = math.log(abs_goal_diff + 1) * ELO_MOV_BASE / (ELO_MOV_HFA_WEIGHT * winner_edge + ELO_MOV_BASE)
            delta = ELO_K * mov * (actual_home - expected_home)
            deltas[home_team_id] = deltas.get(home_team_id, 0.0) + delta
            deltas[away_team_id] = deltas.get(away_team_id, 0.0) - delta

        for team, delta in deltas.items():
            ratings[team] = ratings.get(team, ELO_START) + delta

    per_game = pd.DataFrame({"game_id": out_game_id, "home_elo": out_home_elo, "away_elo": out_away_elo})
    return per_game.astype(per_game_dtypes), ratings


def attach_pregame_elo(
    target_games: pd.DataFrame,
    per_game_elo: pd.DataFrame,
    final_ratings: dict[int, float],
) -> pd.DataFrame:
    """Add ``home_elo``/``away_elo`` to every row of ``target_games``.

    Train-mode target games are themselves played games and are found by
    ``game_id`` in ``per_game_elo`` (the strictly-pre-game rating computed by
    ``compute_pregame_elo``). Predict-mode target games have no result yet
    and are never in ``per_game_elo``; they fall back to ``final_ratings`` —
    the rating each team held after the last played game, per this feature's
    as-of contract (see ``compute_pregame_elo``'s docstring). A team with no
    played history at all (predict fallback only — every real ``per_game_elo``
    row already defaults an unseen team the same way) gets ``ELO_START``.
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
