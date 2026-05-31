"""Primary LightGBM classifiers for prematch tasks (UPDATE plan stage 8).

Two independent models (``home_win`` and ``over_5_5``) are trained with a fixed
YAML hyperparameter grid, full determinism from ``random_seed``, and optional
``monotone_constraints`` aligned to ``feature_manifest`` column order.

Usage pattern (orchestrated by CLI stage 10)::

    result = train_lgbm_for_task(
        task="home_win",
        X_train=X_train_k, y_train=y_train_k,
        X_val=X_inner_val_k, y_val=y_inner_val_k,
        grid=expand_lgbm_grid(lgbm_grids_dict),
        monotone_constraints=build_monotone_constraints(X_train.columns, monotone_spec),
        random_seed=42,
        num_threads=4,
        early_stopping_rounds=50,
        num_boost_round=500,
        log_loss_fn=modeling.metrics.log_loss,
    )

Constraints:
- fit only on ``train_k``; early stopping and grid selection on ``inner_val_k``.
- No Optuna/Bayes search, no ``CalibratedClassifierCV`` (stage 9).
- No PostgreSQL or ``modeling.dataset_builder.*`` imports.
- Raw probabilities returned without clipping (clip is ``metrics`` / calibration).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import lightgbm as lgb
import numpy as np
import pandas as pd

from modeling.config import ConfigError
from modeling.train_common import (
    LGBM_GRID_KEYS,
    SUPPORTED_TASKS,
    build_monotone_constraints,
    expand_lgbm_grid,
    validate_num_threads,
)

logger = logging.getLogger(__name__)


@dataclass
class LgbmFitResult:
    """Result of :func:`train_lgbm_for_task` for a single task.

    Attributes:
        task: Task name (``"home_win"`` or ``"over_5_5"``).
        booster: LightGBM ``Booster`` fitted **only** on ``train_k``.
        chosen_params: Best hyperparameter dict from the grid.
        inner_val_log_loss_by_config: ``(params, log_loss)`` for every grid point.
        chosen_inner_val_log_loss: Log loss of the chosen config on ``inner_val_k``.
        best_iteration: Early-stopped iteration count for the chosen config.
        n_rows_train: Number of rows in the training block.
        n_rows_inner_val: Number of rows in the inner-validation block.
        eval_predictions: Raw probabilities on optional eval blocks keyed by block name.

    Stage-10 CLI contract:
        Shared fields with :class:`modeling.train_logreg.FitResult`: ``task``,
        ``chosen_inner_val_log_loss``, ``n_rows_train``, ``n_rows_inner_val``.
        Grid diagnostics differ by model family: logreg uses
        ``inner_val_log_loss_by_C: dict[float, float]``; LGBM uses
        ``inner_val_log_loss_by_config: list[tuple[dict, float]]`` because the
        grid is multi-dimensional. CLI stage 10 must normalise both into a
        common report/logging shape.
    """

    task: str
    booster: lgb.Booster
    chosen_params: dict[str, Any]
    inner_val_log_loss_by_config: list[tuple[dict[str, Any], float]] = field(default_factory=list)
    chosen_inner_val_log_loss: float = float("nan")
    best_iteration: int = 0
    n_rows_train: int = 0
    n_rows_inner_val: int = 0
    eval_predictions: dict[str, np.ndarray] = field(default_factory=dict)


def _as_numpy_matrix(X: pd.DataFrame | np.ndarray) -> np.ndarray:
    if isinstance(X, pd.DataFrame):
        return np.ascontiguousarray(X.to_numpy(dtype=np.float64))
    return np.ascontiguousarray(np.asarray(X, dtype=np.float64))


def build_lgbm_base_params(
    *,
    random_seed: int,
    num_threads: int,
    monotone_constraints: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Return fixed LightGBM params shared by every grid configuration."""
    validate_num_threads(num_threads)
    params: dict[str, Any] = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "seed": random_seed,
        "feature_fraction_seed": random_seed,
        "bagging_seed": random_seed,
        "data_random_seed": random_seed,
        "extra_seed": random_seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": num_threads,
        "bagging_freq": 1,
    }
    if monotone_constraints is not None:
        if len(monotone_constraints) == 0:
            raise ConfigError("monotone_constraints must be non-empty when provided")
        params["monotone_constraints"] = list(monotone_constraints)
    return params


def train_single_lgbm(
    X_train: pd.DataFrame | np.ndarray,
    y_train: np.ndarray,
    X_val: pd.DataFrame | np.ndarray,
    y_val: np.ndarray,
    *,
    params: Mapping[str, Any],
    num_boost_round: int,
    early_stopping_rounds: int,
) -> lgb.Booster:
    """Train one LightGBM model with early stopping on the validation set."""
    if len(X_val) == 0:
        raise ValueError(
            "inner_val block is empty; early stopping requires a non-empty validation set"
        )
    if early_stopping_rounds <= 0:
        raise ValueError("early_stopping_rounds must be >= 1")
    if num_boost_round <= 0:
        raise ValueError("num_boost_round must be >= 1")

    x_train = _as_numpy_matrix(X_train)
    y_train_arr = np.asarray(y_train).ravel()
    x_val = _as_numpy_matrix(X_val)
    y_val_arr = np.asarray(y_val).ravel()

    train_data = lgb.Dataset(x_train, label=y_train_arr)
    val_data = lgb.Dataset(x_val, label=y_val_arr, reference=train_data)

    booster = lgb.train(
        dict(params),
        train_data,
        num_boost_round=num_boost_round,
        valid_sets=[val_data],
        valid_names=["inner_val"],
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
    )
    return booster


def predict_lgbm_proba(
    booster: lgb.Booster,
    X: pd.DataFrame | np.ndarray,
) -> np.ndarray:
    """Return raw positive-class probabilities in ``[0, 1]`` without clipping."""
    x = _as_numpy_matrix(X)
    iteration = booster.best_iteration if booster.best_iteration > 0 else booster.current_iteration()
    return np.asarray(
        booster.predict(x, num_iteration=iteration),
        dtype=float,
    ).ravel()


def select_lgbm_config_by_inner_val(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    X_val: pd.DataFrame,
    y_val: np.ndarray,
    grid: Sequence[Mapping[str, Any]],
    *,
    random_seed: int,
    num_threads: int,
    early_stopping_rounds: int,
    num_boost_round: int,
    monotone_constraints: Sequence[int] | None = None,
    log_loss_fn: Callable[[np.ndarray, np.ndarray], float],
) -> tuple[dict[str, Any], lgb.Booster, list[tuple[dict[str, Any], float]], int]:
    """Fit one model per grid point; pick the best by inner-val log loss.

    Tie-break: first configuration in *grid* order (deterministic YAML order).
    """
    if not grid:
        raise ConfigError("LGBM hyperparameter grid must be non-empty")

    base_params = build_lgbm_base_params(
        random_seed=random_seed,
        num_threads=num_threads,
        monotone_constraints=monotone_constraints,
    )

    losses: list[tuple[dict[str, Any], float]] = []
    best_booster: lgb.Booster | None = None
    best_params: dict[str, Any] | None = None
    best_loss = float("inf")
    best_iteration = 0

    for grid_params in grid:
        params = {**base_params, **dict(grid_params)}
        booster = train_single_lgbm(
            X_train,
            y_train,
            X_val,
            y_val,
            params=params,
            num_boost_round=num_boost_round,
            early_stopping_rounds=early_stopping_rounds,
        )
        p_val = predict_lgbm_proba(booster, X_val)
        loss = log_loss_fn(y_val, p_val)
        full_params = dict(grid_params)
        losses.append((full_params, loss))
        logger.debug("LGBM grid point %s  inner_val_log_loss=%.6f", full_params, loss)

        if loss < best_loss:
            best_loss = loss
            best_params = full_params
            best_booster = booster
            best_iteration = booster.best_iteration

    logger.info(
        "Selected LGBM config %r (inner_val_log_loss=%.6f, best_iteration=%d) from %d grid points",
        best_params,
        best_loss,
        best_iteration,
        len(grid),
    )
    return best_params, best_booster, losses, best_iteration  # type: ignore[return-value]


def train_lgbm_for_task(
    task: str,
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    X_val: pd.DataFrame,
    y_val: np.ndarray,
    *,
    grid: Sequence[Mapping[str, Any]],
    random_seed: int,
    num_threads: int,
    early_stopping_rounds: int,
    num_boost_round: int,
    log_loss_fn: Callable[[np.ndarray, np.ndarray], float],
    monotone_constraints: Sequence[int] | None = None,
    eval_blocks: Mapping[str, tuple[pd.DataFrame, np.ndarray]] | None = None,
) -> LgbmFitResult:
    """Train a LightGBM model for *task* using grid search on inner val.

    Args:
        task: ``"home_win"`` or ``"over_5_5"``.
        X_train: Feature matrix — training block only.
        y_train: Binary labels for training block.
        X_val: Inner-validation block (early stopping + grid selection).
        y_val: Labels for inner validation.
        grid: Flat list of hyperparameter dicts (from :func:`expand_lgbm_grid`).
        random_seed: Single YAML seed for all LightGBM seed fields.
        num_threads: ``compute.num_threads`` from YAML.
        early_stopping_rounds: Patience for early stopping on ``inner_val_k``.
        num_boost_round: Upper bound on boosting rounds.
        log_loss_fn: ``modeling.metrics.log_loss`` (or compatible callable).
        monotone_constraints: Positional constraint vector or ``None``.
        eval_blocks: Optional ``{block_name: (X, y)}`` for raw predictions only.

    Returns:
        :class:`LgbmFitResult` with fitted booster and diagnostics.
    """
    if task not in SUPPORTED_TASKS:
        raise ValueError(f"Unknown task {task!r}. Supported: {sorted(SUPPORTED_TASKS)}")

    if monotone_constraints is not None and len(monotone_constraints) != X_train.shape[1]:
        raise ValueError(
            f"monotone_constraints length ({len(monotone_constraints)}) "
            f"does not match number of features ({X_train.shape[1]})"
        )

    logger.info(
        "Training LGBM for task=%r  n_train=%d  n_val=%d  grid_size=%d",
        task,
        len(X_train),
        len(X_val),
        len(grid),
    )

    chosen_params, booster, losses, best_iteration = select_lgbm_config_by_inner_val(
        X_train,
        np.asarray(y_train),
        X_val,
        np.asarray(y_val),
        grid,
        random_seed=random_seed,
        num_threads=num_threads,
        early_stopping_rounds=early_stopping_rounds,
        num_boost_round=num_boost_round,
        monotone_constraints=monotone_constraints,
        log_loss_fn=log_loss_fn,
    )

    eval_predictions: dict[str, np.ndarray] = {}
    if eval_blocks:
        for name, (x_block, _y_block) in eval_blocks.items():
            eval_predictions[name] = predict_lgbm_proba(booster, x_block)

    chosen_loss = next(loss for params, loss in losses if params == chosen_params)

    return LgbmFitResult(
        task=task,
        booster=booster,
        chosen_params=chosen_params,
        inner_val_log_loss_by_config=losses,
        chosen_inner_val_log_loss=chosen_loss,
        best_iteration=best_iteration,
        n_rows_train=len(X_train),
        n_rows_inner_val=len(X_val),
        eval_predictions=eval_predictions,
    )


__all__ = [
    "LGBM_GRID_KEYS",
    "LgbmFitResult",
    "build_lgbm_base_params",
    "build_monotone_constraints",
    "expand_lgbm_grid",
    "predict_lgbm_proba",
    "select_lgbm_config_by_inner_val",
    "train_lgbm_for_task",
    "train_single_lgbm",
]
