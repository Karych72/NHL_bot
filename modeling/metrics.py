"""Offline metrics for prematch classifiers (UPDATE plan stage 5).

Pure functions over ``y_true``, predicted probabilities ``p``, and optional
team identifiers. No filesystem, database, or feature-manifest access.

Report serialization lives in :mod:`modeling.report` (``metrics.json`` keys,
``summary.md``, reliability PNG, ``run.log``).
"""

from __future__ import annotations

from typing import Literal, Union

import numpy as np
import pandas as pd

DEFAULT_EPSILON: float = 1e-15
DEFAULT_ECE_BINS: int = 10

TeamBreakdownBy = Literal["home_team_id", "away_team_id"]


class MetricsInputError(ValueError):
    """Invalid shapes or values for metric inputs."""


def _as_1d_float_array(values: Union[np.ndarray, pd.Series, list, tuple], name: str) -> np.ndarray:
    arr = np.asarray(values, dtype=float).ravel()
    if arr.ndim != 1:
        raise MetricsInputError(f"{name} must be one-dimensional")
    return arr


def validate_metric_inputs(
    y_true: Union[np.ndarray, pd.Series, list, tuple],
    p: Union[np.ndarray, pd.Series, list, tuple],
) -> tuple[np.ndarray, np.ndarray]:
    """Validate binary labels and probabilities; raise :class:`MetricsInputError` on failure."""
    y = _as_1d_float_array(y_true, "y_true")
    p_arr = _as_1d_float_array(p, "p")
    if y.shape != p_arr.shape:
        raise MetricsInputError("y_true and p must have the same length")
    if np.isnan(p_arr).any():
        raise MetricsInputError("p must not contain NaN")
    if (p_arr < 0.0).any() or (p_arr > 1.0).any():
        raise MetricsInputError("p must lie in [0, 1]")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise MetricsInputError("y_true must contain only 0 and 1")
    return y, p_arr


def log_loss(
    y_true: Union[np.ndarray, pd.Series, list, tuple],
    p: Union[np.ndarray, pd.Series, list, tuple],
    *,
    epsilon: float = DEFAULT_EPSILON,
) -> float:
    """Binary log loss with probability clip ``p ∈ [ε, 1−ε]``."""
    y, p_arr = validate_metric_inputs(y_true, p)
    p_clip = np.clip(p_arr, epsilon, 1.0 - epsilon)
    losses = -(y * np.log(p_clip) + (1.0 - y) * np.log(1.0 - p_clip))
    return float(np.mean(losses))


def brier(
    y_true: Union[np.ndarray, pd.Series, list, tuple],
    p: Union[np.ndarray, pd.Series, list, tuple],
) -> float:
    """Brier score ``mean((p − y)²)`` without clipping ``p``."""
    y, p_arr = validate_metric_inputs(y_true, p)
    return float(np.mean((p_arr - y) ** 2))


def _bin_indices(p: np.ndarray, n_bins: int) -> np.ndarray:
    clipped = np.clip(p, 0.0, 1.0)
    # Map p=1.0 into the last bin; equal-width bins on [0, 1].
    idx = np.floor(clipped * n_bins).astype(int)
    return np.clip(idx, 0, n_bins - 1)


def ece(
    y_true: Union[np.ndarray, pd.Series, list, tuple],
    p: Union[np.ndarray, pd.Series, list, tuple],
    *,
    n_bins: int = DEFAULT_ECE_BINS,
) -> float:
    """Expected calibration error on equal-width bins in ``[0, 1]``."""
    if n_bins < 2:
        raise MetricsInputError("n_bins must be >= 2")
    y, p_arr = validate_metric_inputs(y_true, p)
    if y.size == 0:
        return float("nan")

    n = y.size
    bin_idx = _bin_indices(p_arr, n_bins)
    total = 0.0
    for b in range(n_bins):
        mask = bin_idx == b
        count = int(mask.sum())
        if count == 0:
            continue
        weight = count / n
        conf = float(np.mean(p_arr[mask]))
        acc = float(np.mean(y[mask]))
        total += weight * abs(conf - acc)
    return float(total)


def reliability_table(
    y_true: Union[np.ndarray, pd.Series, list, tuple],
    p: Union[np.ndarray, pd.Series, list, tuple],
    *,
    n_bins: int = DEFAULT_ECE_BINS,
) -> pd.DataFrame:
    """Per-bin reliability table (includes empty bins with NaN statistics)."""
    if n_bins < 2:
        raise MetricsInputError("n_bins must be >= 2")
    y, p_arr = validate_metric_inputs(y_true, p)
    n = y.size
    bin_idx = _bin_indices(p_arr, n_bins) if n > 0 else np.array([], dtype=int)

    rows: list[dict[str, float | int]] = []
    for b in range(n_bins):
        lower = b / n_bins
        upper = (b + 1) / n_bins
        if n == 0:
            count = 0
        else:
            count = int((bin_idx == b).sum())
        if count == 0:
            rows.append(
                {
                    "bin_lower": lower,
                    "bin_upper": upper,
                    "count": 0,
                    "weight": 0.0,
                    "mean_pred": float("nan"),
                    "frac_positive": float("nan"),
                }
            )
            continue
        mask = bin_idx == b
        rows.append(
            {
                "bin_lower": lower,
                "bin_upper": upper,
                "count": count,
                "weight": count / n,
                "mean_pred": float(np.mean(p_arr[mask])),
                "frac_positive": float(np.mean(y[mask])),
            }
        )
    return pd.DataFrame(rows)


def team_breakdown(
    y_true: Union[np.ndarray, pd.Series, list, tuple],
    p: Union[np.ndarray, pd.Series, list, tuple],
    *,
    team_ids: Union[np.ndarray, pd.Series, list, tuple],
    by: TeamBreakdownBy,
    epsilon: float = DEFAULT_EPSILON,
) -> pd.DataFrame:
    """Average log loss by team group (home or away).

    ``by`` documents which team column ``team_ids`` represents; grouping uses
    ``team_ids`` directly. Pass the home or away id vector matching ``by``.
    """
    if by not in ("home_team_id", "away_team_id"):
        raise MetricsInputError(
            f"by must be 'home_team_id' or 'away_team_id', got {by!r}"
        )
    y, p_arr = validate_metric_inputs(y_true, p)
    teams = np.asarray(team_ids)
    if teams.shape != y.shape:
        raise MetricsInputError("team_ids must have the same length as y_true")

    overall = log_loss(y, p_arr, epsilon=epsilon)
    frame = pd.DataFrame({"team_id": teams, "y": y, "p": p_arr})
    rows: list[dict[str, float | int]] = []
    for team_id, group in frame.groupby("team_id", sort=True):
        n_games = int(len(group))
        if n_games == 0:
            continue
        ll = log_loss(group["y"].to_numpy(), group["p"].to_numpy(), epsilon=epsilon)
        assert isinstance(team_id, (np.integer, int)), (
            f"team_id must be int-like, got {type(team_id).__name__}"
        )
        rows.append(
            {
                "team_id": int(team_id),
                "n_games": n_games,
                "log_loss": ll,
                "log_loss_minus_overall": ll - overall,
            }
        )
    return pd.DataFrame(rows)


def trivial_baseline(
    y_train: Union[np.ndarray, pd.Series, list, tuple],
    y_test: Union[np.ndarray, pd.Series, list, tuple],
    *,
    epsilon: float = DEFAULT_EPSILON,
) -> dict[str, float]:
    """Constant predictor ``p ≡ mean(y_train)`` evaluated on ``y_test``."""
    y_tr = _as_1d_float_array(y_train, "y_train")
    y_te = _as_1d_float_array(y_test, "y_test")
    if not np.all(np.isin(y_tr, (0.0, 1.0))):
        raise MetricsInputError("y_train must contain only 0 and 1")
    if not np.all(np.isin(y_te, (0.0, 1.0))):
        raise MetricsInputError("y_test must contain only 0 and 1")

    p_const = float(np.mean(y_tr))
    p_pred = np.full(y_te.shape, p_const, dtype=float)
    return {
        "p": p_const,
        "log_loss": log_loss(y_te, p_pred, epsilon=epsilon),
        "brier": brier(y_te, p_pred),
    }


__all__ = [
    "DEFAULT_ECE_BINS",
    "DEFAULT_EPSILON",
    "MetricsInputError",
    "TeamBreakdownBy",
    "brier",
    "ece",
    "log_loss",
    "reliability_table",
    "team_breakdown",
    "trivial_baseline",
    "validate_metric_inputs",
]
