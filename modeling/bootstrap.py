"""Bootstrap confidence intervals for log loss and Brier (UPDATE plan stage 6).

Percentile 95% CIs on resampled metric values. Holdout callers should pass
``block_by_day=True`` (block resample whole game days); walk-forward ``test_k``
callers should pass ``block_by_day=False`` (i.i.d. match resampling).

All randomness uses a single ``numpy.random.Generator`` from ``seed``
(``bootstrap_seed = random_seed`` in YAML). Independent per-fold or per-metric
seeds are forbidden. Global ``np.random.seed`` is never used.

Default ``n_resamples=1000`` matches ``evaluation.bootstrap_samples``.
Default ``num_threads=1`` matches ``compute.num_threads``; parallel resampling
is optional and only when ``num_threads > 1``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from modeling.metrics import validate_metric_inputs

DEFAULT_N_RESAMPLES: int = 1000

MetricFn = Callable[[np.ndarray, np.ndarray], float]


class BootstrapError(ValueError):
    """Invalid bootstrap inputs or configuration."""


@dataclass(frozen=True)
class BootstrapResult:
    """Bootstrap CI for one metric on one fold or holdout."""

    metric_name: str
    point: float
    ci_low: float
    ci_high: float
    n_resamples: int
    block_by_day: bool
    seed: int

    def to_dict(self) -> dict[str, Any]:
        """JSON mapping for stage-10 logging and ``metrics.json`` / artifact metadata.

        Uses UPDATE-plan keys ``bootstrap.N``, ``bootstrap.block_by_day``,
        ``bootstrap.seed``. For flat field names (``n_resamples``, …) use
        :func:`dataclasses.asdict` on the dataclass instance instead — both
        are JSON-serializable; only ``to_dict()`` matches the run-metadata contract.
        """
        return {
            "metric_name": self.metric_name,
            "point": self.point,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "bootstrap.N": self.n_resamples,
            "bootstrap.block_by_day": self.block_by_day,
            "bootstrap.seed": self.seed,
        }


def _validate_n_resamples(n_resamples: int) -> int:
    if not isinstance(n_resamples, (int, np.integer)):
        raise BootstrapError(f"n_resamples must be an int, got {type(n_resamples).__name__}")
    n = int(n_resamples)
    if n <= 0:
        raise BootstrapError(f"n_resamples must be positive, got {n}")
    return n


def _validate_num_threads(num_threads: int) -> int:
    if not isinstance(num_threads, (int, np.integer)):
        raise BootstrapError(f"num_threads must be an int, got {type(num_threads).__name__}")
    n = int(num_threads)
    if n < 1:
        raise BootstrapError(f"num_threads must be >= 1, got {n}")
    return n


def _normalize_day(
    day: pd.Series | np.ndarray | list | tuple | None,
    n: int,
) -> np.ndarray:
    if day is None:
        raise BootstrapError("day is required when block_by_day=True")
    series = day if isinstance(day, pd.Series) else pd.Series(day)
    if len(series) != n:
        raise BootstrapError(
            f"day length {len(series)} does not match y_true length {n}"
        )
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.isna().any():
        raise BootstrapError("day contains NaT or invalid date values")
    return parsed.to_numpy()


def _build_day_blocks(day: np.ndarray) -> tuple[list[Any], dict[Any, np.ndarray], int]:
    frame = pd.DataFrame({"day": day, "idx": np.arange(day.size)})
    day_to_idx: dict[Any, np.ndarray] = {
        d: grp["idx"].to_numpy(dtype=int)
        for d, grp in frame.groupby("day", sort=False)
    }
    sorted_days = sorted(day_to_idx.keys())
    d_count = len(sorted_days)
    if d_count < 2:
        raise BootstrapError(
            f"недостаточно дней для block bootstrap (D={d_count})"
        )
    return sorted_days, day_to_idx, d_count


def _iid_resample_indices(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.integers(0, n, size=n)


def _block_resample_indices(
    rng: np.random.Generator,
    sorted_days: list[Any],
    day_to_idx: dict[Any, np.ndarray],
    d_count: int,
) -> np.ndarray:
    day_choice = rng.integers(0, d_count, size=d_count)
    blocks = [day_to_idx[sorted_days[i]] for i in day_choice]
    return np.concatenate(blocks)


def _compute_resamples(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    metric_fn: MetricFn,
    indices_list: list[np.ndarray],
    num_threads: int,
) -> np.ndarray:
    def _one(idx: np.ndarray) -> float:
        return metric_fn(y_true[idx], y_pred[idx])

    if num_threads == 1:
        return np.array([_one(idx) for idx in indices_list], dtype=float)

    from joblib import Parallel, delayed

    values = Parallel(n_jobs=num_threads)(
        delayed(_one)(idx) for idx in indices_list
    )
    return np.asarray(values, dtype=float)


def _bootstrap_one_metric(
    name: str,
    metric_fn: MetricFn,
    y_true: np.ndarray,
    y_pred: np.ndarray,
    indices_list: list[np.ndarray],
    *,
    n_resamples: int,
    block_by_day: bool,
    seed: int,
    num_threads: int,
) -> BootstrapResult:
    point = metric_fn(y_true, y_pred)
    resamples = _compute_resamples(
        y_true, y_pred, metric_fn, indices_list, num_threads
    )
    if resamples.size != n_resamples:
        raise BootstrapError(
            f"expected {n_resamples} resamples for {name!r}, got {resamples.size}"
        )
    if np.isnan(resamples).any():
        raise BootstrapError(f"bootstrap resamples for {name!r} contain NaN")
    ci_low = float(np.quantile(resamples, 0.025))
    ci_high = float(np.quantile(resamples, 0.975))
    return BootstrapResult(
        metric_name=name,
        point=float(point),
        ci_low=ci_low,
        ci_high=ci_high,
        n_resamples=n_resamples,
        block_by_day=block_by_day,
        seed=seed,
    )


def bootstrap_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    day: pd.Series | np.ndarray | list | tuple | None = None,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    block_by_day: bool = False,
    seed: int,
    metric_fns: Mapping[str, MetricFn],
    num_threads: int = 1,
) -> dict[str, BootstrapResult]:
    """Compute percentile 95% bootstrap CIs for each metric in ``metric_fns``.

    Parameters
    ----------
    y_true, y_pred
        Aligned length-``n`` arrays. Labels in ``{0, 1}``; probabilities in
        ``[0, 1]``. Clipping for log loss is done inside ``metric_fns`` (stage 5).
    day
        Game day per row. Required when ``block_by_day=True`` (holdout).
        Ignored when ``block_by_day=False`` (walk-forward ``test_k``).
    n_resamples
        Number of bootstrap draws (``evaluation.bootstrap_samples``, default 1000).
    block_by_day
        If True, block-bootstrap whole days (holdout). If False, i.i.d. match
        resampling (``test_k``). Callers choose the mode per fold; this function
        does not override ``block_by_day`` based on fold name.
    seed
        ``random_seed`` from YAML (``bootstrap_seed``); no derived seeds.
    metric_fns
        At least ``log_loss`` and ``brier`` callables from ``modeling.metrics``.
    num_threads
        Worker limit from ``compute.num_threads`` (default 1, sequential).
    """
    n_resamples = _validate_n_resamples(n_resamples)
    num_threads = _validate_num_threads(num_threads)

    y = np.asarray(y_true, dtype=float).ravel()
    p = np.asarray(y_pred, dtype=float).ravel()
    if y.shape != p.shape:
        raise BootstrapError(
            f"y_true length {y.size} != y_pred length {p.size}"
        )
    validate_metric_inputs(y, p)

    if not metric_fns:
        raise BootstrapError("metric_fns must not be empty")

    n = y.size
    rng = np.random.default_rng(seed)

    if block_by_day:
        day_arr = _normalize_day(day, n)
        sorted_days, day_to_idx, d_count = _build_day_blocks(day_arr)
        indices_list = [
            _block_resample_indices(rng, sorted_days, day_to_idx, d_count)
            for _ in range(n_resamples)
        ]
    else:
        if n == 0:
            raise BootstrapError("cannot bootstrap with zero samples")
        indices_list = [_iid_resample_indices(rng, n) for _ in range(n_resamples)]

    results: dict[str, BootstrapResult] = {}
    for name, fn in metric_fns.items():
        results[name] = _bootstrap_one_metric(
            name,
            fn,
            y,
            p,
            indices_list,
            n_resamples=n_resamples,
            block_by_day=block_by_day,
            seed=seed,
            num_threads=num_threads,
        )
    return results


def standard_metric_fns(
    *,
    epsilon: float | None = None,
) -> dict[str, MetricFn]:
    """Default ``log_loss`` / ``brier`` callables from :mod:`modeling.metrics`.

    ``epsilon`` is the clip bound passed to :func:`modeling.metrics.log_loss`
    (Python name ``epsilon``). YAML/config uses ``evaluation.epsilon_clip``;
    stage-10 CLI maps that field to this argument.
    """
    from modeling.metrics import DEFAULT_EPSILON, brier, log_loss

    eps = DEFAULT_EPSILON if epsilon is None else epsilon
    return {
        "log_loss": lambda yt, yp: log_loss(yt, yp, epsilon=eps),
        "brier": lambda yt, yp: brier(yt, yp),
    }


__all__ = [
    "DEFAULT_N_RESAMPLES",
    "BootstrapError",
    "BootstrapResult",
    "bootstrap_metrics",
    "standard_metric_fns",
]
