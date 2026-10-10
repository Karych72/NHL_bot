"""Team Elo: the ``diff_elo`` dataset feature and the benchmark the models must beat.

One sequential pass over the full league history (``EloHistory.run``) serves both
users: ``dataset_builder.features`` takes pre-game ratings as a feature, and
``fit_elo_benchmark`` picks Elo parameters plus a Platt curve ``P = sigmoid(a + b*d)``
using only seasons before the checked one (Задача 66). Pure computation: no
PostgreSQL, no filesystem.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from modeling.metrics import DEFAULT_EPSILON, log_loss

ELO_START = 1500.0
# Margin-of-victory multiplier ln(|goal_diff|+1) * MOV_BASE / (MOV_HFA_WEIGHT * winner_edge + MOV_BASE).
ELO_MOV_BASE = 2.2
ELO_MOV_HFA_WEIGHT = 0.001
# Rating differences are scaled before the Platt fit so lbfgs converges on a unit-size feature.
_D_SCALE = 100.0


@dataclass(frozen=True)
class EloParams:
    """Elo hyper-parameters; the defaults are the constants of the ``diff_elo`` feature.

    ``k`` sizes each update; ``home_advantage`` is added to the home rating before the
    win expectation; ``season_regression`` shrinks every team toward the field mean at a
    season change; ``mov`` switches the goal-difference multiplier; ``ot_win_weight`` is the
    outcome credited to the winner of an OT/shootout game (1.0 = full win, 0.5 = draw).
    """

    k: float = 8.0
    home_advantage: float = 35.0
    season_regression: float = 1.0 / 3.0
    mov: bool = True
    ot_win_weight: float = 1.0

    def __post_init__(self) -> None:
        if not 0.5 <= self.ot_win_weight <= 1.0:
            raise ValueError(f"ot_win_weight must lie in [0.5, 1.0], got {self.ot_win_weight}")


class EloHistory:
    """Played games in ``(day, game_id)`` order with a cached Elo pass per parameter set.

    Args:
        games: one row per played game with ``game_id``, ``day``, ``season_id``,
            ``home_team_id``, ``away_team_id``, ``home_goals``, ``away_goals`` (order
            does not matter). Optional ``decision`` (``REG``/``OT``/``SO``; absent = all
            ``REG``). The winner is ``winner_id`` when the column exists, else the side
            with more goals. A played game with a missing result is a data bug and
            raises ``ValueError``.
    """

    def __init__(self, games: pd.DataFrame) -> None:
        frame = games.copy()
        frame["day"] = pd.to_datetime(frame["day"]).dt.normalize()
        frame = frame.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
        result_cols = ["home_goals", "away_goals"] + (["winner_id"] if "winner_id" in frame else [])
        if frame[result_cols].isna().any().any():
            raise ValueError("EloHistory: a played game is missing winner_id/goals")
        if "winner_id" in frame:
            home_won = (frame["winner_id"] == frame["home_team_id"]).tolist()
        else:
            if (frame["home_goals"] == frame["away_goals"]).any():
                raise ValueError("EloHistory: a played game ended level; Elo needs a winner")
            home_won = (frame["home_goals"] > frame["away_goals"]).tolist()
        decision = frame["decision"] if "decision" in frame else pd.Series("REG", index=frame.index)
        self.game_id = frame["game_id"].to_numpy(dtype="int64")
        self.season_id = frame["season_id"].to_numpy(dtype="int64")
        # Plain lists: the pass is a pure-Python loop run once per grid point.
        self._day = frame["day"].to_numpy().astype("datetime64[D]").astype("int64").tolist()
        self._season = self.season_id.tolist()
        self._home = frame["home_team_id"].astype("int64").tolist()
        self._away = frame["away_team_id"].astype("int64").tolist()
        self._home_won = home_won
        self.home_win = np.asarray(home_won, dtype=float)
        self._abs_goal_diff = (frame["home_goals"] - frame["away_goals"]).abs().astype(float).tolist()
        self._overtime = (decision != "REG").tolist()
        self._diffs: dict[EloParams, np.ndarray] = {}

    def run(self, params: EloParams) -> tuple[list[float], list[float], dict[int, float]]:
        """One Elo pass: pre-game home/away ratings per game and the final ratings.

        Ratings are frozen at the start of each calendar day (every game that day scores
        off the same snapshot, updates apply afterwards), so no game sees a same-day
        result. At a season change every rating is shrunk toward the field mean.
        """
        n = len(self._day)
        home_elo = [0.0] * n
        away_elo = [0.0] * n
        ratings: dict[int, float] = {}
        deltas: dict[int, float] = {}
        cur_day: int | None = None
        cur_season: int | None = None
        keep = 1.0 - params.season_regression
        for i in range(n):
            if self._day[i] != cur_day:
                for team, delta in deltas.items():
                    ratings[team] = ratings.get(team, ELO_START) + delta
                deltas = {}
                if cur_season is not None and self._season[i] != cur_season and ratings:
                    mean_rating = sum(ratings.values()) / len(ratings)
                    ratings = {t: mean_rating + keep * (r - mean_rating) for t, r in ratings.items()}
                cur_day = self._day[i]
                cur_season = self._season[i]
            home = self._home[i]
            away = self._away[i]
            home_rating = ratings.get(home, ELO_START)
            away_rating = ratings.get(away, ELO_START)
            home_elo[i] = home_rating
            away_elo[i] = away_rating

            diff = home_rating + params.home_advantage - away_rating
            expected_home = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
            win_outcome = params.ot_win_weight if self._overtime[i] else 1.0
            home_won = self._home_won[i]
            actual_home = win_outcome if home_won else 1.0 - win_outcome
            if params.mov:
                winner_edge = diff if home_won else -diff
                mov = math.log(self._abs_goal_diff[i] + 1) * ELO_MOV_BASE / (
                    ELO_MOV_HFA_WEIGHT * winner_edge + ELO_MOV_BASE
                )
            else:
                mov = 1.0
            delta = params.k * mov * (actual_home - expected_home)
            deltas[home] = deltas.get(home, 0.0) + delta
            deltas[away] = deltas.get(away, 0.0) - delta
        for team, delta in deltas.items():
            ratings[team] = ratings.get(team, ELO_START) + delta
        return home_elo, away_elo, ratings

    def diffs(self, params: EloParams) -> np.ndarray:
        """Pre-game ``d = R_home - R_away`` (no home bonus) per game, cached per ``params``."""
        if params not in self._diffs:
            home_elo, away_elo, _ = self.run(params)
            self._diffs[params] = np.asarray(home_elo) - np.asarray(away_elo)
        return self._diffs[params]


def compute_pregame_elo(
    played_games: pd.DataFrame,
    params: EloParams | None = None,
) -> tuple[pd.DataFrame, dict[int, float]]:
    """Pre-game team Elo for every played game, plus the final ratings.

    Used by the dataset builder for the ``diff_elo`` feature: Elo is one league-wide rating
    carried across seasons, so it makes its own pass over the whole history (see
    :class:`EloHistory` for the input contract and the per-day freeze).

    Args:
        played_games: played games, see :class:`EloHistory`.
        params: Elo parameters; default = the feature's constants.

    Returns:
        ``(per_game, final_ratings)``: ``per_game`` has ``game_id``, ``home_elo``,
        ``away_elo`` (ratings strictly before the game; ``d = home_elo - away_elo``);
        ``final_ratings`` maps ``team_id`` to its rating after the last game, which
        ``attach_pregame_elo`` gives to predict-mode games that have no result yet.
    """
    per_game_dtypes = {"game_id": "int64", "home_elo": "float64", "away_elo": "float64"}
    if played_games.empty:
        return pd.DataFrame(columns=list(per_game_dtypes)).astype(per_game_dtypes), {}
    history = EloHistory(played_games)
    home_elo, away_elo, ratings = history.run(params or EloParams())
    per_game = pd.DataFrame({"game_id": history.game_id, "home_elo": home_elo, "away_elo": away_elo})
    return per_game.astype(per_game_dtypes), ratings


def expand_elo_grid(grid: Mapping[str, Sequence[Any]]) -> list[EloParams]:
    """Cartesian product of the ``elo.grid`` config lists as :class:`EloParams` points."""
    names = ("k", "home_advantage", "season_regression", "mov", "ot_win_weight")
    return [
        EloParams(**dict(zip(names, values)))
        for values in itertools.product(*(grid[name] for name in names))
    ]


@dataclass(frozen=True)
class EloBenchmark:
    """Elo parameters and Platt curve chosen on seasons before ``test_season``.

    ``fit_log_loss`` is the in-sample log-loss of ``sigmoid(a + b*d)`` on the fit games;
    ``n_fit_games`` is how many games that was.
    """

    test_season: int
    params: EloParams
    a: float
    b: float
    fit_log_loss: float
    n_fit_games: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready ``elo_params`` block for ``metrics.json``."""
        return {
            "k": self.params.k,
            "home_advantage": self.params.home_advantage,
            "season_regression": self.params.season_regression,
            "mov": self.params.mov,
            "ot_win_weight": self.params.ot_win_weight,
            "a": self.a,
            "b": self.b,
            "fit_log_loss": self.fit_log_loss,
            "n_fit_games": self.n_fit_games,
        }


def _fit_platt(d: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Unregularised logistic fit ``P = sigmoid(a + b*d)``; returns ``(a, b)``."""
    lr = LogisticRegression(penalty=None, solver="lbfgs", max_iter=5000)
    lr.fit((d / _D_SCALE).reshape(-1, 1), y)
    return float(lr.intercept_[0]), float(lr.coef_[0, 0]) / _D_SCALE


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def fit_elo_benchmark(
    history: EloHistory,
    test_season: int,
    grid: Sequence[EloParams],
    *,
    epsilon: float = DEFAULT_EPSILON,
) -> EloBenchmark:
    """Choose Elo parameters and the Platt curve using only seasons before ``test_season``.

    For every grid point the (cached) Elo pass gives ``d`` per game; Platt
    ``P = sigmoid(a + b*d)`` is fitted on the games of seasons ``< test_season``, **excluding
    the first season in the data** (all teams start at 1500 there, so its ratings are
    warm-up). The point with the lowest log-loss on those same games wins.

    Args:
        history: full league history (all seasons, including ones after ``test_season``:
            the pass at a game depends only on earlier games, so they cannot leak).
        test_season: the checked season; its games never enter the fit.
        grid: parameter points to try.
        epsilon: probability clip for the log-loss criterion.

    Raises:
        ValueError: empty grid, or no season between the warm-up one and ``test_season``.
    """
    if not grid:
        raise ValueError("elo grid is empty")
    first_season = int(history.season_id.min())
    mask = (history.season_id < test_season) & (history.season_id > first_season)
    if not mask.any():
        raise ValueError(
            f"no seasons to fit Elo before {test_season}: the first season in the data "
            f"({first_season}) is warm-up only"
        )
    y = history.home_win[mask]
    best: EloBenchmark | None = None
    for params in grid:
        d = history.diffs(params)[mask]
        a, b = _fit_platt(d, y)
        loss = log_loss(y, _sigmoid(a + b * d), epsilon=epsilon)
        if best is None or loss < best.fit_log_loss:
            best = EloBenchmark(test_season, params, a, b, loss, int(mask.sum()))
    assert best is not None
    return best


def elo_season_predictions(history: EloHistory, benchmark: EloBenchmark) -> pd.DataFrame:
    """Elo forecasts for every game of ``benchmark.test_season``.

    Returns:
        ``game_id``, ``home_win`` (0/1), ``p_fitted`` (``sigmoid(a + b*d)``) and ``p_raw``
        (chess curve ``1/(1+10^(-(d+H)/400))`` with the chosen home bonus ``H``).
    """
    mask = history.season_id == benchmark.test_season
    if not mask.any():
        raise ValueError(f"season {benchmark.test_season} is not in the Elo history")
    d = history.diffs(benchmark.params)[mask]
    return pd.DataFrame(
        {
            "game_id": history.game_id[mask],
            "home_win": history.home_win[mask],
            "p_fitted": _sigmoid(benchmark.a + benchmark.b * d),
            "p_raw": 1.0 / (1.0 + 10.0 ** (-(d + benchmark.params.home_advantage) / 400.0)),
        }
    )
