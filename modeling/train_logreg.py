"""Baseline logistic-regression classifier for prematch tasks (UPDATE plan stage 7).

Two independent models are trained (``home_win`` and ``over_5_5``).  All
shared logic lives in utility functions so neither task duplicates the other.

Pipeline composition (fixed by UPDATE plan §7):
    SimpleImputer(strategy="median")
        → StandardScaler()
        → LogisticRegression(penalty="l2", solver="lbfgs", max_iter=5000)

Usage pattern (orchestrated by CLI stage 10)::

    result = train_logreg_for_task(
        task="home_win",
        X_train=X_train_k, y_train=y_train_k,
        X_val=X_inner_val_k, y_val=y_inner_val_k,
        C_grid=[0.01, 0.1, 1.0, 10.0],
        class_weight=None,
        random_seed=42,
        log_loss_fn=modeling.metrics.log_loss,
    )
    save_model_artifact(path_dir, model=result.pipeline, metadata={...})

Constraints:
- fit() only on train_k; predict_proba() on val/cal/test/holdout.
- No n_jobs=-1, no os.cpu_count(), no shuffle-based CV splitters.
- No access to PostgreSQL or modeling.dataset_builder.*.
- No CalibratedClassifierCV (calibration is stage 9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from modeling.train_common import SUPPORTED_TASKS, TASK_LABEL_MAP, get_task_label

logger = logging.getLogger(__name__)

# Columns that identity-leak team information and must never appear in X for logreg v1.
FORBIDDEN_TEAM_ID_COLUMNS: frozenset[str] = frozenset({"home_team_id", "away_team_id"})


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class FitResult:
    """Result of :func:`train_logreg_for_task` for a single task.

    Attributes:
        task: Task name (``"home_win"`` or ``"over_5_5"``).
        pipeline: sklearn ``Pipeline`` fitted **only** on ``train_k``.
        chosen_C: Best ``C`` value selected by minimum log loss on ``inner_val_k``.
        inner_val_log_loss_by_C: Mapping ``{C: log_loss_on_inner_val}`` for all
            values in the grid (useful for logging / reports).
        chosen_inner_val_log_loss: Log loss of ``chosen_C`` on ``inner_val_k``.
        n_rows_train: Number of rows in the training block.
        n_rows_inner_val: Number of rows in the inner-validation block.

    Stage-10 CLI contract:
        Shared fields with :class:`modeling.train_lgbm.LgbmFitResult`: ``task``,
        ``chosen_inner_val_log_loss``, ``n_rows_train``, ``n_rows_inner_val``.
        Grid diagnostics: ``inner_val_log_loss_by_C`` maps scalar ``C`` values
        to inner-val log loss. LGBM uses
        ``inner_val_log_loss_by_config: list[tuple[dict, float]]`` instead;
        CLI stage 10 normalises both for reports.
    """

    task: str
    pipeline: Pipeline
    chosen_C: float
    inner_val_log_loss_by_C: dict[float, float] = field(default_factory=dict)
    chosen_inner_val_log_loss: float = float("nan")
    n_rows_train: int = 0
    n_rows_inner_val: int = 0


# ---------------------------------------------------------------------------
# Pipeline factory
# ---------------------------------------------------------------------------


def build_logreg_pipeline(
    C: float,
    *,
    class_weight: object = None,
    random_seed: int,
) -> Pipeline:
    """Return a fresh sklearn Pipeline for logistic regression.

    Composition (fixed by UPDATE plan §7):
        SimpleImputer(strategy="median")
            → StandardScaler()
            → LogisticRegression(penalty="l2", solver="lbfgs", max_iter=5000)

    Args:
        C: Inverse regularization strength (must be > 0).
        class_weight: Passed to ``LogisticRegression``.  Default is ``None``
            (no re-weighting).  ``"balanced"`` is reserved for stage 15 experiments
            and must **not** be passed here in production training runs.
        random_seed: Passed as ``random_state`` to ``LogisticRegression`` for
            reproducibility parity with other subsystems (even though lbfgs is
            deterministic, the seed is set explicitly).

    Returns:
        Unfitted ``Pipeline`` instance.
    """
    return Pipeline(
        steps=[
            ("simpleimputer", SimpleImputer(strategy="median")),
            ("standardscaler", StandardScaler()),
            (
                "logisticregression",
                LogisticRegression(
                    penalty="l2",
                    solver="lbfgs",
                    max_iter=5000,
                    C=C,
                    class_weight=class_weight,
                    random_state=random_seed,
                ),
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Feature guard
# ---------------------------------------------------------------------------


def assert_no_team_id_columns(columns: Iterable[str]) -> None:
    """Raise ``ValueError`` if any forbidden team-ID column appears in *columns*.

    Forbidden columns: ``home_team_id``, ``away_team_id``.  Their presence in
    the feature matrix would leak team identity into the linear model (logreg v1
    policy, UPDATE plan §3.7).

    Args:
        columns: Iterable of column names to check (e.g. ``X.columns``).

    Raises:
        ValueError: With a message listing the offending column names.
    """
    bad = sorted(FORBIDDEN_TEAM_ID_COLUMNS & set(columns))
    if bad:
        raise ValueError(
            f"Feature matrix X must not contain team-ID columns for logreg v1. "
            f"Offending column(s): {bad}. "
            f"Remove them from the feature manifest or use a model family that "
            f"supports categorical team embeddings."
        )


# ---------------------------------------------------------------------------
# C-grid search on inner_val
# ---------------------------------------------------------------------------


def select_C_by_inner_val(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    X_val: pd.DataFrame,
    y_val: np.ndarray,
    C_grid: list[float],
    *,
    class_weight: object = None,
    random_seed: int,
    log_loss_fn: Callable[[np.ndarray, np.ndarray], float],
) -> tuple[float, Pipeline, dict[float, float]]:
    """Fit one pipeline per C-value, evaluate on *inner_val*, return the best.

    Selection rule:
        - Choose ``C*`` with the **minimum** log loss on ``X_val`` / ``y_val``.
        - Tie-break: prefer the **smallest** ``C`` (strongest regularization).
          This rule is deterministic and documented here so tests can assert it.

    Args:
        X_train: Feature matrix for training block (fit step).
        y_train: Binary labels for training block.
        X_val: Feature matrix for inner-validation block (evaluate step only).
        y_val: Binary labels for inner-validation block.
        C_grid: Non-empty list of positive C values to try.
        class_weight: Passed to each :func:`build_logreg_pipeline` call.
        random_seed: Fixed seed for all pipelines (no per-C variation).
        log_loss_fn: Callable ``(y_true, y_pred_proba) -> float`` sourced from
            ``modeling.metrics`` so the ε-clipping matches final evaluation.
            **Not** ``sklearn.metrics.log_loss``.

    Returns:
        ``(chosen_C, fitted_pipeline, log_loss_by_C)`` where:
        - ``chosen_C`` is the best C found.
        - ``fitted_pipeline`` is the pipeline trained on ``X_train`` with ``chosen_C``.
        - ``log_loss_by_C`` maps every C in *C_grid* to its inner-val log loss.

    Raises:
        ValueError: If ``C_grid`` is empty.
    """
    if not C_grid:
        raise ValueError("C_grid must be non-empty")

    log_loss_by_C: dict[float, float] = {}
    best_pipeline: Pipeline | None = None
    best_C: float | None = None
    best_loss: float = float("inf")

    for c_val in C_grid:
        pipeline = build_logreg_pipeline(c_val, class_weight=class_weight, random_seed=random_seed)
        pipeline.fit(X_train, y_train)
        p_val = pipeline.predict_proba(X_val)[:, 1]
        loss = log_loss_fn(y_val, p_val)
        log_loss_by_C[c_val] = loss
        logger.debug("C=%.6g  inner_val_log_loss=%.6f", c_val, loss)

        # Strict improvement or tie-break by smaller C (stronger regularization).
        if loss < best_loss or (loss == best_loss and (best_C is None or c_val < best_C)):
            best_loss = loss
            best_C = c_val
            best_pipeline = pipeline

    logger.info(
        "Selected C*=%.6g (inner_val_log_loss=%.6f) from grid %s",
        best_C,
        best_loss,
        sorted(C_grid),
    )
    return best_C, best_pipeline, log_loss_by_C  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Per-task training entry point
# ---------------------------------------------------------------------------


def train_logreg_for_task(
    task: str,
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    X_val: pd.DataFrame,
    y_val: np.ndarray,
    *,
    C_grid: list[float],
    class_weight: object = None,
    random_seed: int,
    log_loss_fn: Callable[[np.ndarray, np.ndarray], float],
) -> FitResult:
    """Train a logistic-regression model for *task* using C-grid search on inner val.

    This function:
    1. Asserts that ``X_train`` contains no forbidden team-ID columns.
    2. Selects the best C via :func:`select_C_by_inner_val`.
    3. Returns a :class:`FitResult` with the fitted pipeline and diagnostics.

    The returned pipeline is fitted **only** on ``X_train`` / ``y_train``.
    Calibration, test-set evaluation, and holdout scoring are handled by
    later stages (9 and 10).

    Args:
        task: ``"home_win"`` or ``"over_5_5"``.
        X_train: Feature matrix — training block only.
        y_train: Binary label vector for training block.
        X_val: Feature matrix — inner-validation block (no fit, only evaluate).
        y_val: Binary label vector for inner-validation block.
        C_grid: List of positive C values to search over (from ``models.logreg.grids``).
            Default grid documented in UPDATE plan §7: ``[0.001, 0.01, 0.1, 1.0, 10.0, 100.0]``.
        class_weight: Passed through to the pipeline (default ``None``).
        random_seed: Fixed integer seed; no per-task or per-C variation.
        log_loss_fn: ``modeling.metrics.log_loss`` (or a compatible callable for tests).

    Returns:
        :class:`FitResult` instance.

    Raises:
        ValueError: Unknown task, forbidden team-ID columns, or empty C_grid.
    """
    if task not in SUPPORTED_TASKS:
        raise ValueError(f"Unknown task {task!r}. Supported: {sorted(SUPPORTED_TASKS)}")

    assert_no_team_id_columns(X_train.columns)

    logger.info(
        "Training logreg for task=%r  n_train=%d  n_val=%d  C_grid=%s",
        task,
        len(X_train),
        len(X_val),
        sorted(C_grid),
    )

    chosen_C, pipeline, log_loss_by_C = select_C_by_inner_val(
        X_train,
        np.asarray(y_train),
        X_val,
        np.asarray(y_val),
        C_grid,
        class_weight=class_weight,
        random_seed=random_seed,
        log_loss_fn=log_loss_fn,
    )

    return FitResult(
        task=task,
        pipeline=pipeline,
        chosen_C=chosen_C,
        inner_val_log_loss_by_C=log_loss_by_C,
        chosen_inner_val_log_loss=log_loss_by_C[chosen_C],
        n_rows_train=len(X_train),
        n_rows_inner_val=len(X_val),
    )


__all__ = [
    "FORBIDDEN_TEAM_ID_COLUMNS",
    "SUPPORTED_TASKS",
    "TASK_LABEL_MAP",
    "FitResult",
    "assert_no_team_id_columns",
    "build_logreg_pipeline",
    "get_task_label",
    "select_C_by_inner_val",
    "train_logreg_for_task",
]
