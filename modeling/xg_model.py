"""Expected goals (Задача 69): Fenwick-xG — the chance an unblocked shot attempt becomes a goal.

Input is ``shots_train.csv`` (``shot_attempts`` rows of played games plus ``season_id``, ``day``
and the two teams). :func:`prepare_shots` normalises the rink so every shooter attacks the net
at ``x = +89`` and builds the features; :func:`fit_xg` / :func:`predict_xg` are a logistic
regression on them (team xGF/xGA for the goal model — Задача 69B2 — come from these two).
``evaluate_xg_model`` checks it season by season (fit on earlier seasons). No PostgreSQL.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from modeling.metrics import log_loss

# Unblocked attempts. A blocked shot has no ``shotType`` and its coordinate is where it was blocked.
FENWICK_TYPES = ("shot-on-goal", "missed-shot", "goal")
# Net centre after normalisation; the blue line of the attacked zone is at x = +25.
GOAL_X = 89.0
BLUE_LINE_X = 25.0
# A rebound: the previous attempt of the period was by the same team at most this many seconds
# earlier. 2–3 s is the usual public cut (MoneyPuck, Evolving-Hockey): enough for the puck to
# come off the goalie or the boards and be shot again, short of a new set-up. Clock time has
# whole seconds, so 3 s keeps the 2-second second chances that a 2 s cut would lose to rounding.
REBOUND_SECONDS = 3
# Shot types rarer than this in training share one "infrequent" category; a type the training
# seasons never saw (the NHL adds types) goes there too instead of failing (or, if training
# had no rare type, to the base level); such shots are counted per season in the report.
MIN_SHOT_TYPE_SHOTS = 100
UNKNOWN_SHOT_TYPE = "unknown"

_NUMERIC = ["distance", "angle", "rebound"]
_CATEGORICAL = ["shot_type", "strength"]
_REQUIRED = [
    "game_id", "sort_order", "period", "time_in_period", "team_id", "event_type",
    "situation_code", "home_team_defending_side", "home_team_id", "away_team_id",
]


def prepare_shots(shots: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Fenwick attempts with model features, and counts of what was left out.

    Rebounds are found on *all* attempts (a blocked shot can be the first shot of a
    rebound) in ``sort_order`` within a game and period. Then only Fenwick attempts stay,
    without empty-net shots (the defending side has no goalie in ``situation_code``
    ``[away goalie, away skaters, home skaters, home goalie]``) and without coordinates.
    The rink is turned so the shooter attacks ``x = +89``: the home team attacks ``+x``
    when it defends the ``left`` side, which changes period to period.

    Args:
        shots: ``shots_train.csv`` rows. ``situation_code`` must be read as text (``"0651"``
            loses its leading zero as a number); a missing value in a column of
            ``_REQUIRED`` raises ``ValueError`` — nothing is guessed.

    Returns:
        ``(frame, counts)``: ``frame`` adds ``x_norm``, ``y_norm``, ``distance``, ``angle``
        (degrees off the net's axis), ``rebound``, ``shot_type`` (``unknown`` for NULL),
        ``strength`` (``even`` / ``pp`` / ``sh`` / ``other`` = own net empty) and ``is_goal``;
        ``counts`` — ``attempts``, ``fenwick``, ``excluded_empty_net``, ``excluded_no_coordinates``,
        ``kept``.
    """
    missing = [c for c in _REQUIRED if shots[c].isna().any()]
    if missing:
        raise ValueError(f"shot attempts with missing {missing}")
    frame = shots.sort_values(["game_id", "sort_order"], kind="mergesort").reset_index(drop=True)
    code = frame["situation_code"]
    if not code.map(lambda v: isinstance(v, str) and len(v) == 4 and v.isdigit()).all():
        raise ValueError("situation_code must be four digits read as text")
    is_home = frame["team_id"] == frame["home_team_id"]
    if not (is_home | (frame["team_id"] == frame["away_team_id"])).all():
        raise ValueError("an attempt's team_id is neither the home nor the away team")
    side = frame["home_team_defending_side"]
    if not side.isin(["left", "right"]).all():
        raise ValueError(f"unexpected home_team_defending_side {sorted(set(side) - {'left', 'right'})}")

    minutes, seconds = frame["time_in_period"].str.split(":", expand=True).astype(int).T.to_numpy()
    frame["clock"] = minutes * 60 + seconds
    by_period = frame.groupby(["game_id", "period"], sort=False)
    frame["rebound"] = (
        (by_period["team_id"].shift() == frame["team_id"])
        & (frame["clock"] - by_period["clock"].shift() <= REBOUND_SECONDS)
    ).astype(float)

    digits = {k: code.str[i].astype(int) for i, k in enumerate(["away_g", "away_s", "home_s", "home_g"])}
    own_goalie = digits["home_g"].where(is_home, digits["away_g"])
    opp_goalie = digits["away_g"].where(is_home, digits["home_g"])
    own_skaters = digits["home_s"].where(is_home, digits["away_s"])
    opp_skaters = digits["away_s"].where(is_home, digits["home_s"])
    frame["strength"] = np.select(
        [own_goalie == 0, own_skaters == opp_skaters, own_skaters > opp_skaters],
        ["other", "even", "pp"],
        default="sh",
    )

    fenwick = frame["event_type"].isin(FENWICK_TYPES)
    empty_net = fenwick & (opp_goalie == 0)
    no_coordinates = fenwick & ~empty_net & (frame["x_coord"].isna() | frame["y_coord"].isna())
    keep = fenwick & ~empty_net & ~no_coordinates
    counts = {
        "attempts": int(len(frame)),
        "fenwick": int(fenwick.sum()),
        "excluded_empty_net": int(empty_net.sum()),
        "excluded_no_coordinates": int(no_coordinates.sum()),
        "kept": int(keep.sum()),
    }

    out = frame[keep].copy()
    sign = np.where(is_home[keep] == (side[keep] == "left"), 1.0, -1.0)
    out["x_norm"] = sign * out["x_coord"].astype(float)
    out["y_norm"] = sign * out["y_coord"].astype(float)
    out["distance"] = np.hypot(GOAL_X - out["x_norm"], out["y_norm"])
    out["angle"] = np.degrees(np.arctan2(out["y_norm"].abs(), GOAL_X - out["x_norm"]))
    out["shot_type"] = out["shot_type"].fillna(UNKNOWN_SHOT_TYPE)
    out["is_goal"] = (out["event_type"] == "goal").astype(float)
    return out.reset_index(drop=True), counts


def fit_xg(shots: pd.DataFrame) -> Pipeline:
    """Fit the xG logistic regression on prepared shots.

    Distance and angle are standardised (the ridge of ``LogisticRegression`` treats them
    alike), shot type and strength are one-hot.

    Args:
        shots: output of :func:`prepare_shots` for the training seasons.
    """
    columns = ColumnTransformer(
        [
            ("numeric", StandardScaler(), _NUMERIC),
            (
                "categorical",
                OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=MIN_SHOT_TYPE_SHOTS),
                _CATEGORICAL,
            ),
        ]
    )
    model = make_pipeline(columns, LogisticRegression(max_iter=1000))
    return model.fit(shots[_NUMERIC + _CATEGORICAL], shots["is_goal"])


def predict_xg(model: Pipeline, shots: pd.DataFrame) -> np.ndarray:
    """xG of each prepared shot — P(goal) from :func:`fit_xg`.

    Args:
        model: fitted pipeline from :func:`fit_xg`.
        shots: output of :func:`prepare_shots`, any seasons.
    """
    return model.predict_proba(shots[_NUMERIC + _CATEGORICAL])[:, 1]


def normalization_check(shots: pd.DataFrame) -> dict[str, float]:
    """Does the side normalisation hold on the data? Shares to read before trusting the model.

    ``goals_beyond_blue_line`` — goals with ``x_norm > 25`` (should be ~1: goals from
    the own half are rare); ``zone_code_agreement`` — attempts whose API ``zone_code == 'O'``
    agrees with ``x_norm > 25`` (should be ~1; the API zone is the shooter's).

    Args:
        shots: output of :func:`prepare_shots`.
    """
    goals = shots[shots["is_goal"] == 1.0]
    attacking = shots["x_norm"] > BLUE_LINE_X
    return {
        "goals_beyond_blue_line": float((goals["x_norm"] > BLUE_LINE_X).mean()),
        "zone_code_agreement": float(((shots["zone_code"] == "O") == attacking).mean()),
    }


def evaluate_xg_model(shots: pd.DataFrame, *, test_seasons: Sequence[int], epsilon: float) -> dict[str, Any]:
    """Season-by-season check: for each ``s`` fit on every season before it, score all of ``s``.

    The constant is the goal share of the training shots. Status ``ok`` only if every
    season's log-loss is below the constant's.

    Returns:
        ``metrics.json`` payload: ``rebound_seconds``, ``counts`` (see :func:`prepare_shots`),
        ``normalization_check``, ``seasons[]`` (``n_train``, ``n_test``, ``auc``, ``log_loss``,
        ``constant``, ``xg_sum``, ``goals``, ``unseen_shot_types``) and ``status``.
    """
    prepared, counts = prepare_shots(shots)
    seasons: list[dict[str, Any]] = []
    for season in test_seasons:
        train = prepared[prepared["season_id"] < season]
        test = prepared[prepared["season_id"] == season]
        if train.empty or test.empty:
            raise ValueError(f"season {season}: no training or no checked shots")
        p = predict_xg(fit_xg(train), test)
        y = test["is_goal"].to_numpy()
        const = float(train["is_goal"].mean())
        unseen = test.loc[~test["shot_type"].isin(set(train["shot_type"])), "shot_type"].value_counts()
        seasons.append(
            {
                "season_id": int(season),
                "n_train": int(len(train)),
                "n_test": int(len(test)),
                "auc": float(roc_auc_score(y, p)),
                "log_loss": log_loss(y, p, epsilon=epsilon),
                "constant": {"p": const, "log_loss": log_loss(y, np.full(len(y), const), epsilon=epsilon)},
                "xg_sum": float(p.sum()),
                "goals": int(y.sum()),
                "unseen_shot_types": {str(k): int(v) for k, v in unseen.items()},
            }
        )
    ok = all(s["log_loss"] < s["constant"]["log_loss"] for s in seasons)
    return {
        "rebound_seconds": REBOUND_SECONDS,
        "counts": counts,
        "normalization_check": normalization_check(prepared),
        "seasons": seasons,
        "status": "ok" if ok else "failed_baseline_check",
    }


def compose_xg_summary(metrics: dict[str, Any]) -> str:
    """``summary.md`` of :func:`evaluate_xg_model`: status, exclusions, normalisation, per-season table."""
    counts, check = metrics["counts"], metrics["normalization_check"]
    lines = [
        f"status: {metrics['status']}",
        "",
        f"# Fenwick xG (rebound ≤ {metrics['rebound_seconds']} s)",
        "",
        f"attempts {counts['attempts']}, Fenwick {counts['fenwick']}, excluded: empty net "
        f"{counts['excluded_empty_net']}, no coordinates {counts['excluded_no_coordinates']}; kept {counts['kept']}",
        "",
        f"normalisation: goals beyond the blue line {check['goals_beyond_blue_line']:.4f}, "
        f"zone_code 'O' agreement {check['zone_code_agreement']:.4f}",
        "",
        "| season | n train | n test | AUC | log-loss | constant LL | xG sum | goals | xG / goals |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in metrics["seasons"]:
        lines.append(
            f"| {s['season_id']} | {s['n_train']} | {s['n_test']} | {s['auc']:.4f} | {s['log_loss']:.4f} "
            f"| {s['constant']['log_loss']:.4f} | {s['xg_sum']:.1f} | {s['goals']} | {s['xg_sum'] / s['goals']:.3f} |"
        )
    unseen = {s["season_id"]: s["unseen_shot_types"] for s in metrics["seasons"] if s["unseen_shot_types"]}
    if unseen:
        lines += ["", f"shot types unseen in training: {unseen}"]
    return "\n".join(lines) + "\n"
