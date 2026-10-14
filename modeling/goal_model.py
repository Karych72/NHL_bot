"""Goal model (Задача 67): regulation goals of each side -> score grid -> P(total over a line).

A Poisson GLM on stacked team-games (two rows per game) gives the expected regulation goals
``λ_home``, ``λ_away``; their outer product is the score grid of 60 minutes, whose diagonal
(ties) is rescaled to the league's tracked tie rate. The total follows the bookmakers' rule:
a tie after 60 minutes adds exactly one goal (OT to the first goal, or +1 for the shootout).
``evaluate_goal_model`` checks it season by season against the constant, like Задача 66
checks the win models. Input is ``games_train.csv``; no PostgreSQL, no filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import poisson
from sklearn.linear_model import PoissonRegressor

from modeling.bootstrap import bootstrap_mean_diff
from modeling.metrics import calibration_bins, log_loss_per_game, metric_block

MAX_GOALS = 14
GATE_LINE = 5.5
TIE_TOLERANCE = 0.02
# Bins of the forecast regulation total λ_home + λ_away for the report.
LAMBDA_SUM_EDGES = (5.5, 6.0, 6.5)

_GOALS = np.arange(MAX_GOALS + 1)
_HOME_GOALS, _AWAY_GOALS = np.meshgrid(_GOALS, _GOALS, indexing="ij")
_IS_TIE = _HOME_GOALS == _AWAY_GOALS
# Settled total: regulation goals plus one for a tie (OT goal or shootout +1).
_SETTLED_TOTAL = _HOME_GOALS + _AWAY_GOALS + _IS_TIE


def prepare_games(games: pd.DataFrame) -> pd.DataFrame:
    """Sort played games by ``(day, game_id)`` and add the per-game inputs of the model.

    Adds ``home_b2b`` / ``away_b2b`` (the team also played the day before), ``reg_tie``
    (level after 60 minutes) and ``over`` (settled total above :data:`GATE_LINE`). The
    back-to-back flags look at the previous game in this frame, so it must hold the whole
    history, not one season.

    Args:
        games: ``games_train.csv`` rows; needs ``game_id``, ``day``, ``season_id``,
            ``home_team_id``, ``away_team_id``, ``home_reg_goals``, ``away_reg_goals``.
    """
    if games[["home_reg_goals", "away_reg_goals"]].isna().any().any():
        raise ValueError("a played game is missing its regulation goals")
    frame = _with_b2b(games)
    home, away = frame["home_reg_goals"], frame["away_reg_goals"]
    frame["reg_tie"] = (home == away).astype(float)
    frame["over"] = (home + away + frame["reg_tie"] > GATE_LINE).astype(float)
    return frame


def _with_b2b(games: pd.DataFrame) -> pd.DataFrame:
    """``games`` sorted by ``(day, game_id)`` plus ``home_b2b`` / ``away_b2b`` (the team also played the day before)."""
    frame = games.copy()
    frame["day"] = pd.to_datetime(frame["day"]).dt.normalize()
    frame = frame.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    side = pd.concat(
        [
            frame[["game_id", "day", "home_team_id"]].rename(columns={"home_team_id": "team_id"}).assign(home=True),
            frame[["game_id", "day", "away_team_id"]].rename(columns={"away_team_id": "team_id"}).assign(home=False),
        ]
    ).sort_values(["team_id", "day"], kind="mergesort")
    side["b2b"] = side.groupby("team_id")["day"].diff() == pd.Timedelta(days=1)
    b2b = side.set_index(["game_id", "home"])["b2b"]
    frame["home_b2b"] = b2b.xs(True, level="home").reindex(frame["game_id"]).to_numpy(dtype=float)
    frame["away_b2b"] = b2b.xs(False, level="home").reindex(frame["game_id"]).to_numpy(dtype=float)
    return frame


def tracked_tie_rate(games: pd.DataFrame, halflife_days: float) -> np.ndarray:
    """Share of regulation ties before each game's day, older days weighted ``0.5 ** (Δdays / halflife)``.

    Tracks the league's tie rate through the season (2025/26 went from ~21% to 25%). Games of
    the first day have no history and get ``NaN``.

    Args:
        games: output of :func:`prepare_games` (the whole history).
        halflife_days: half-life of the day weights (config ``goals.tie_halflife_days``).
    """
    daily = games.groupby("day")["reg_tie"].agg(["sum", "count"])
    days = daily.index.to_numpy().astype("datetime64[D]").astype("int64")
    rate = np.full(len(days), np.nan)
    for i in range(1, len(days)):
        weight = 0.5 ** ((days[i] - days[:i]) / halflife_days)
        rate[i] = (weight * daily["sum"].to_numpy()[:i]).sum() / (weight * daily["count"].to_numpy()[:i]).sum()
    return pd.Series(rate, index=daily.index).reindex(games["day"]).to_numpy()


@dataclass(frozen=True)
class GoalModel:
    """Fitted GLM: ``log λ = β0 + attack_team − defence_opponent + home + own/opponent back-to-back``.

    ``teams`` are the teams seen in training; a team without games (a new franchise) gets
    zero attack and defence — the league average, which is also where the ridge pulls
    every team with few games.
    """

    regressor: PoissonRegressor
    teams: tuple[int, ...]

    def lambdas(self, games: pd.DataFrame) -> np.ndarray:
        """Expected regulation goals ``(n, 2)``: column 0 home, column 1 away."""
        mu = self.regressor.predict(_design(games, self.teams))
        return mu.reshape(-1, 2)


def _design(games: pd.DataFrame, teams: Sequence[int]) -> np.ndarray:
    """Stacked design: row ``2i`` — the home side scores in game ``i``, row ``2i + 1`` — the away side."""
    index = {team: k for k, team in enumerate(teams)}
    n_teams = len(teams)
    x = np.zeros((2 * len(games), 2 * n_teams + 3))
    rows = np.arange(len(games))
    sides = (
        (2 * rows, games["home_team_id"], games["away_team_id"], games["home_b2b"], games["away_b2b"], 1.0),
        (2 * rows + 1, games["away_team_id"], games["home_team_id"], games["away_b2b"], games["home_b2b"], 0.0),
    )
    for row, scorer, opponent, own_b2b, opp_b2b, home in sides:
        for r, team, opp in zip(row, scorer, opponent):
            if team in index:
                x[r, index[team]] = 1.0
            if opp in index:
                x[r, n_teams + index[opp]] = -1.0
        x[row, 2 * n_teams] = home
        x[row, 2 * n_teams + 1] = own_b2b.to_numpy()
        x[row, 2 * n_teams + 2] = opp_b2b.to_numpy()
    return x


def fit_goal_model(games: pd.DataFrame, alpha: float) -> GoalModel:
    """Fit the GLM on regulation goals of ``games``.

    Args:
        games: training games (output of :func:`prepare_games`), all with equal weight.
        alpha: ridge strength of ``PoissonRegressor``; pulls every team toward the average.
    """
    teams = tuple(sorted(set(games["home_team_id"]) | set(games["away_team_id"])))
    y = np.column_stack([games["home_reg_goals"], games["away_reg_goals"]]).ravel()
    regressor = PoissonRegressor(alpha=alpha, max_iter=1000).fit(_design(games, teams), y)
    return GoalModel(regressor, teams)


def score_grid(lambdas: np.ndarray, tie_rate: float) -> np.ndarray:
    """Score grids of 60 minutes for one day's games, ``(n, MAX_GOALS + 1, MAX_GOALS + 1)``.

    Independent Poisson pmfs, then one multiplier ``1 + θ`` on every diagonal cell and
    renormalisation; ``θ`` is solved so the mean tie probability of these games equals
    ``tie_rate`` (independent Poisson gives ~17% ties, the league has 21–25%).

    Args:
        lambdas: ``(n, 2)`` expected regulation goals, home then away.
        tie_rate: target mean P(tie) of these games; ``NaN`` (no history) raises ``ValueError``.
    """
    home = poisson.pmf(_GOALS[None, :], lambdas[:, :1])
    away = poisson.pmf(_GOALS[None, :], lambdas[:, 1:])
    base = home[:, :, None] * away[:, None, :]

    def inflated(theta: float) -> np.ndarray:
        grid = base * np.where(_IS_TIE, 1.0 + theta, 1.0)
        return grid / grid.sum(axis=(1, 2), keepdims=True)

    theta = brentq(lambda t: inflated(t)[:, _IS_TIE].sum(axis=1).mean() - tie_rate, -0.99, 50.0)
    return inflated(theta)


def p_over(grid: np.ndarray, line: float) -> np.ndarray:
    """P(settled total > ``line``) per grid — the bookmakers' total on any line.

    Args:
        grid: score grids from :func:`score_grid`.
        line: total line, e.g. 5.5; a tie after 60 minutes counts one extra goal.
    """
    return grid[:, _SETTLED_TOTAL > line].sum(axis=1)


def forecast(model: GoalModel, games: pd.DataFrame, tie_rate: np.ndarray) -> pd.DataFrame:
    """Forecast of ``games``, one score grid per game day.

    Args:
        model: fitted :class:`GoalModel`.
        games: games to forecast (output of :func:`prepare_games`, any subset).
        tie_rate: tracked tie rate per row of ``games`` (:func:`tracked_tie_rate`).

    Returns:
        ``lambda_home``, ``lambda_away``, ``p_tie`` and ``p_over`` (over :data:`GATE_LINE`),
        indexed like ``games``.
    """
    lambdas = model.lambdas(games)
    p_tie = np.zeros(len(games))
    p_over_line = np.zeros(len(games))
    for _, idx in games.groupby("day").indices.items():
        grid = score_grid(lambdas[idx], float(tie_rate[idx[0]]))
        p_tie[idx] = grid[:, _IS_TIE].sum(axis=1)
        p_over_line[idx] = p_over(grid, GATE_LINE)
    return pd.DataFrame(
        {"lambda_home": lambdas[:, 0], "lambda_away": lambdas[:, 1], "p_tie": p_tie, "p_over": p_over_line},
        index=games.index,
    )


def _season_forecast(
    games: pd.DataFrame, tie_rate: np.ndarray, season: int, alpha: float
) -> pd.DataFrame:
    """Fit on every game of seasons before ``season`` and forecast all of ``season`` (summer fit)."""
    model = fit_goal_model(games[games["season_id"] < season], alpha)
    mask = (games["season_id"] == season).to_numpy()
    return forecast(model, games[mask], tie_rate[mask])


def _pick_alpha(
    games: pd.DataFrame, tie_rate: np.ndarray, test_season: int, alphas: Sequence[float], epsilon: float
) -> float:
    """Ridge with the lowest over-5.5 log-loss on the seasons strictly between the first and ``test_season``."""
    seasons = sorted(games["season_id"].unique())
    tune = [s for s in seasons if seasons[0] < s < test_season]
    if not tune:
        raise ValueError(f"no season to tune the goal model before {test_season}")
    y = np.concatenate([games.loc[games["season_id"] == s, "over"].to_numpy() for s in tune])
    losses = {
        alpha: log_loss_per_game(
            y,
            np.concatenate([_season_forecast(games, tie_rate, s, alpha)["p_over"].to_numpy() for s in tune]),
            epsilon=epsilon,
        ).mean()
        for alpha in alphas
    }
    return min(losses, key=lambda a: losses[a])


def fit_for_upcoming(
    games: pd.DataFrame, *, alphas: Sequence[float], tie_halflife_days: float, epsilon: float
) -> tuple[GoalModel, float]:
    """Fit the model the bot publishes (Задача 73): on every played game, ridge picked as in the check.

    The ridge is chosen like :func:`evaluate_goal_model` chooses it for a season after the
    last one in ``games``, then the GLM is fitted once on all of ``games``.

    Args:
        games: ``games_train.csv`` rows (every played game).
        alphas: ridge grid (config ``goals.alpha``).
        tie_halflife_days: half-life of the tracked tie rate (config ``goals.tie_halflife_days``).
        epsilon: log-loss clip (config ``evaluation.epsilon_clip``).

    Returns:
        The fitted model and its ridge.
    """
    games = prepare_games(games)
    tie_rate = tracked_tie_rate(games, tie_halflife_days)
    alpha = _pick_alpha(games, tie_rate, int(games["season_id"].max()) + 1, alphas, epsilon)
    return fit_goal_model(games, alpha), alpha


def forecast_upcoming(
    model: GoalModel, played: pd.DataFrame, upcoming: pd.DataFrame, tie_halflife_days: float
) -> pd.DataFrame:
    """P(over :data:`GATE_LINE`) of not yet played games.

    Back-to-back flags see the played history and the other upcoming games; the tie rate of
    an upcoming day is tracked from the played games before it.

    Args:
        model: fitted :class:`GoalModel` (:func:`fit_for_upcoming`).
        played: ``games_train.csv`` rows (every played game).
        upcoming: games to forecast; needs ``game_id``, ``day``, ``home_team_id``, ``away_team_id``.
        tie_halflife_days: half-life the model was fitted with.

    Returns:
        ``game_id`` and ``p_over``, one row per upcoming game.
    """
    if upcoming.empty:  # sklearn refuses 0 samples
        return pd.DataFrame({"game_id": pd.Series(dtype="int64"), "p_over": pd.Series(dtype="float64")})
    keys = ["game_id", "day", "home_team_id", "away_team_id"]
    frame = _with_b2b(pd.concat([played, upcoming[keys]], ignore_index=True))
    is_upcoming = frame["game_id"].isin(upcoming["game_id"]).to_numpy()
    # NaN for upcoming games: their days add nothing to the tracked rate.
    frame["reg_tie"] = (frame["home_reg_goals"] == frame["away_reg_goals"]).astype(float).where(~is_upcoming)
    tie_rate = tracked_tie_rate(frame, tie_halflife_days)
    target = frame[is_upcoming]
    return pd.DataFrame(
        {"game_id": target["game_id"], "p_over": forecast(model, target, tie_rate[is_upcoming])["p_over"]}
    ).reset_index(drop=True)


def _lambda_sum_table(checked: pd.DataFrame) -> list[dict[str, Any]]:
    """Forecast vs actual regulation total and over-5.5 share by bins of ``λ_home + λ_away``."""
    lam = checked["lambda_home"] + checked["lambda_away"]
    edges = (-np.inf, *LAMBDA_SUM_EDGES, np.inf)
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        part = checked[(lam >= lo) & (lam < hi)]
        if part.empty:
            continue
        rows.append(
            {
                "lambda_sum_from": None if np.isinf(lo) else lo,
                "lambda_sum_to": None if np.isinf(hi) else hi,
                "count": int(len(part)),
                "mean_lambda_sum": float((part["lambda_home"] + part["lambda_away"]).mean()),
                "mean_reg_total": float((part["home_reg_goals"] + part["away_reg_goals"]).mean()),
                "mean_p_over": float(part["p_over"].mean()),
                "over_share": float(part["over"].mean()),
            }
        )
    return rows


def evaluate_goal_model(
    games: pd.DataFrame,
    *,
    test_seasons: Sequence[int],
    alphas: Sequence[float],
    tie_halflife_days: float,
    ece_bins: int,
    epsilon: float,
    n_resamples: int,
    seed: int,
) -> dict[str, Any]:
    """Season-by-season check of the over-5.5 forecast against the constant (Задача 66 scheme).

    For each checked season ``s``: the ridge is picked on earlier seasons (each forecast by a
    fit on the seasons before it), the model is fitted once on every game before ``s`` and
    forecasts all of ``s``; the tie rate is tracked day by day from games before that day.
    The constant is the over-5.5 share of all games before ``s``.

    Returns:
        ``metrics.json`` payload: ``seasons[]``, ``pooled`` (with the paired block-by-day
        bootstrap CI of ``model_minus_constant``), ``calibration_table``, ``lambda_sum_table``
        and ``status`` — ``ok`` only if the CI lies below zero and every season's mean
        ``p_tie`` is within :data:`TIE_TOLERANCE` of the actual tie share.
    """
    games = prepare_games(games)
    tie_rate = tracked_tie_rate(games, tie_halflife_days)
    seasons: list[dict[str, Any]] = []
    checked_parts: list[pd.DataFrame] = []
    for season in test_seasons:
        alpha = _pick_alpha(games, tie_rate, season, alphas, epsilon)
        part = pd.concat(
            [games[games["season_id"] == season], _season_forecast(games, tie_rate, season, alpha)], axis=1
        )
        part["p_const"] = games.loc[games["season_id"] < season, "over"].mean()
        y = part["over"].to_numpy()
        seasons.append(
            {
                "season_id": int(season),
                "n_test": int(len(part)),
                "alpha": alpha,
                "model": metric_block(y, part["p_over"].to_numpy(), epsilon=epsilon, ece_bins=ece_bins),
                "constant": {
                    "p": float(part["p_const"].iloc[0]),
                    **metric_block(y, part["p_const"].to_numpy(), epsilon=epsilon, ece_bins=ece_bins),
                },
                "tie": {"predicted": float(part["p_tie"].mean()), "actual": float(part["reg_tie"].mean())},
                "reg_total": {
                    "predicted": float((part["lambda_home"] + part["lambda_away"]).mean()),
                    "actual": float((part["home_reg_goals"] + part["away_reg_goals"]).mean()),
                },
            }
        )
        checked_parts.append(part)
    checked = pd.concat(checked_parts, ignore_index=True)
    y = checked["over"].to_numpy()
    diff = bootstrap_mean_diff(
        log_loss_per_game(y, checked["p_over"].to_numpy(), epsilon=epsilon),
        log_loss_per_game(y, checked["p_const"].to_numpy(), epsilon=epsilon),
        checked["day"],
        n_resamples=n_resamples,
        seed=seed,
    )
    ties_ok = all(abs(s["tie"]["predicted"] - s["tie"]["actual"]) <= TIE_TOLERANCE for s in seasons)
    return {
        "line": GATE_LINE,
        "tie_halflife_days": tie_halflife_days,
        "seasons": seasons,
        "pooled": {
            "n_test": int(len(checked)),
            "model": metric_block(y, checked["p_over"].to_numpy(), epsilon=epsilon, ece_bins=ece_bins),
            "constant": metric_block(y, checked["p_const"].to_numpy(), epsilon=epsilon, ece_bins=ece_bins),
            "diff_ci": {"model_minus_constant": {"point": diff.point, "ci_low": diff.ci_low, "ci_high": diff.ci_high}},
        },
        "calibration_table": calibration_bins(y, checked["p_over"].to_numpy()),
        "lambda_sum_table": _lambda_sum_table(checked),
        "status": "ok" if diff.ci_high < 0 and ties_ok else "failed_baseline_check",
    }


def compose_goal_summary(metrics: dict[str, Any]) -> str:
    """``summary.md`` of :func:`evaluate_goal_model`: status, per-season table, CI, calibration."""
    lines = [
        f"status: {metrics['status']}",
        "",
        f"# Goal model: over {metrics['line']} vs constant (tie half-life {metrics['tie_halflife_days']} days)",
        "",
        "| season | n | alpha | model LL | constant LL | diff | ties pred / actual | reg total pred / actual |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in metrics["seasons"]:
        model_ll, const_ll = s["model"]["log_loss"], s["constant"]["log_loss"]
        lines.append(
            f"| {s['season_id']} | {s['n_test']} | {s['alpha']} | {model_ll:.4f} | {const_ll:.4f} "
            f"| {model_ll - const_ll:+.4f} | {s['tie']['predicted']:.3f} / {s['tie']['actual']:.3f} "
            f"| {s['reg_total']['predicted']:.2f} / {s['reg_total']['actual']:.2f} |"
        )
    pooled = metrics["pooled"]
    ci = pooled["diff_ci"]["model_minus_constant"]
    lines += [
        f"| pooled | {pooled['n_test']} | — | {pooled['model']['log_loss']:.4f} "
        f"| {pooled['constant']['log_loss']:.4f} | {ci['point']:+.4f} | — | — |",
        "",
        f"model − constant, 95% CI (paired block-by-day bootstrap): [{ci['ci_low']:+.4f}; {ci['ci_high']:+.4f}]",
        "",
        "## Calibration of P(over) by 5 pp bins",
        "",
        "| bin | n | mean forecast | actual |",
        "|---|---|---|---|",
    ]
    for row in metrics["calibration_table"]:
        lines.append(
            f"| {row['bin_lower']:.2f}–{row['bin_upper']:.2f} | {row['count']} "
            f"| {row['mean_pred']:.3f} | {row['frac_positive']:.3f} |"
        )
    lines += [
        "",
        "## By forecast regulation total λ_home + λ_away",
        "",
        "| λ sum | n | mean λ sum | actual reg total | mean P(over) | over share |",
        "|---|---|---|---|---|---|",
    ]
    for row in metrics["lambda_sum_table"]:
        lo = "" if row["lambda_sum_from"] is None else f"{row['lambda_sum_from']}"
        hi = "" if row["lambda_sum_to"] is None else f"{row['lambda_sum_to']}"
        lines.append(
            f"| {lo}–{hi} | {row['count']} | {row['mean_lambda_sum']:.2f} | {row['mean_reg_total']:.2f} "
            f"| {row['mean_p_over']:.3f} | {row['over_share']:.3f} |"
        )
    return "\n".join(lines) + "\n"
