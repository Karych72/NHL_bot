"""Shared training utilities for prematch classifiers (logreg stage 7, LGBM stage 8).

Task labels, thread limits, hyperparameter grid expansion, and monotone-constraint
building are shared so ``train_logreg.py`` and ``train_lgbm.py`` do not duplicate
logic.
"""

from __future__ import annotations

import fnmatch
import itertools
import logging
from typing import Any, Mapping, Sequence

import numpy as np

from modeling.config import ConfigError

logger = logging.getLogger(__name__)

TASK_LABEL_MAP: dict[str, str] = {
    "home_win": "y_home_win",
    "over_5_5": "y_over_5_5",
}

SUPPORTED_TASKS: frozenset[str] = frozenset(TASK_LABEL_MAP)

LGBM_GRID_KEYS: tuple[str, ...] = (
    "num_leaves",
    "min_data_in_leaf",
    "feature_fraction",
    "bagging_fraction",
    "lambda_l1",
    "lambda_l2",
    "learning_rate",
)


def get_task_label(labels, task: str) -> np.ndarray:
    """Extract the label vector for *task* from the labels DataFrame.

    Args:
        labels: DataFrame with columns named per :data:`TASK_LABEL_MAP`.
        task: One of ``"home_win"``, ``"over_5_5"``.

    Returns:
        1-D numpy array of binary labels.

    Raises:
        ValueError: Unknown task or missing label column.
    """
    import pandas as pd

    if task not in TASK_LABEL_MAP:
        raise ValueError(f"Unknown task {task!r}. Supported: {sorted(SUPPORTED_TASKS)}")
    col = TASK_LABEL_MAP[task]
    if col not in labels.columns:
        raise ValueError(
            f"Label column {col!r} not found in labels DataFrame (columns: {list(labels.columns)})"
        )
    return labels[col].to_numpy()


def validate_num_threads(num_threads: int) -> int:
    """Return *num_threads* after validating the YAML ``compute.num_threads`` contract."""
    if not isinstance(num_threads, (int, np.integer)):
        raise ConfigError(f"num_threads must be an int, got {type(num_threads).__name__}")
    n = int(num_threads)
    if n <= 0:
        raise ConfigError(f"num_threads must be >= 1, got {n}")
    return n


def expand_param_grid(
    grid: Mapping[str, Sequence[Any]],
    *,
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    """Expand a Cartesian product over *keys* in deterministic YAML order."""
    missing = [key for key in keys if key not in grid]
    if missing:
        raise ConfigError(f"hyperparameter grid missing required key(s): {missing}")
    value_lists = [list(grid[key]) for key in keys]
    if any(not values for values in value_lists):
        raise ConfigError("hyperparameter grid must be non-empty for every dimension")
    return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


def expand_lgbm_grid(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Expand ``models.lgbm.grids`` into a flat list of parameter dicts."""
    return expand_param_grid(grid, keys=LGBM_GRID_KEYS)


def build_monotone_constraints(
    feature_names: Sequence[str],
    monotone_spec: Mapping[str, int] | None,
) -> list[int] | None:
    """Build a positional ``monotone_constraints`` vector for LightGBM.

    Patterns use :func:`fnmatch.fnmatch` (exact names and ``*`` wildcards). Every
    pattern in *monotone_spec* must match at least one column from
    *feature_names*; otherwise :class:`ConfigError` is raised.

    Args:
        feature_names: Ordered feature column names (``feature_manifest`` order).
        monotone_spec: Mapping ``{pattern: sign}`` with signs in ``{-1, 0, 1}``.
            ``None`` or empty → no constraints (``None`` returned).

    Returns:
        List of length ``len(feature_names)`` with ``0`` for unconstrained
        features, or ``None`` when *monotone_spec* is empty/absent.
    """
    if not monotone_spec:
        logger.info("No monotone constraints specified; training without monotone_constraints.")
        return None

    constraints = [0] * len(feature_names)
    for pattern, sign in monotone_spec.items():
        if sign not in (-1, 0, 1):
            raise ConfigError(
                f"monotone sign for pattern {pattern!r} must be one of {{-1, 0, 1}}, got {sign}"
            )
        matches = [i for i, name in enumerate(feature_names) if fnmatch.fnmatch(name, pattern)]
        if not matches:
            raise ConfigError(
                f"monotone pattern {pattern!r} matched no feature columns "
                f"(manifest has {len(feature_names)} features)"
            )
        for idx in matches:
            if constraints[idx] not in (0, sign):
                raise ConfigError(
                    f"conflicting monotone signs for feature {feature_names[idx]!r}: "
                    f"existing {constraints[idx]}, new {sign} from pattern {pattern!r}"
                )
            constraints[idx] = sign

    return constraints


__all__ = [
    "LGBM_GRID_KEYS",
    "SUPPORTED_TASKS",
    "TASK_LABEL_MAP",
    "build_monotone_constraints",
    "expand_lgbm_grid",
    "expand_param_grid",
    "get_task_label",
    "validate_num_threads",
]
