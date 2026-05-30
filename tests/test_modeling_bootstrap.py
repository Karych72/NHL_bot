"""Tests for modeling bootstrap confidence intervals (stage 6)."""

from __future__ import annotations

import json
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from modeling.bootstrap import (
    BootstrapError,
    BootstrapResult,
    bootstrap_metrics,
    standard_metric_fns,
)
from modeling.metrics import DEFAULT_EPSILON, MetricsInputError, brier, log_loss

# Soft coverage threshold for synthetic CI tests (see test_coverage_synthetic).
_COVERAGE_MIN_FRACTION = 0.90
_COVERAGE_N_SEEDS = 30


def _metric_fns() -> dict:
    return standard_metric_fns(epsilon=DEFAULT_EPSILON)


def _run(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    seed: int = 42,
    block_by_day: bool = False,
    day=None,
    n_resamples: int = 200,
) -> dict[str, BootstrapResult]:
    return bootstrap_metrics(
        y_true,
        y_pred,
        day=day,
        n_resamples=n_resamples,
        block_by_day=block_by_day,
        seed=seed,
        metric_fns=_metric_fns(),
    )


def test_determinism_same_seed() -> None:
    y_true = np.array([0, 1, 0, 1, 1, 0, 1, 0])
    y_pred = np.array([0.2, 0.8, 0.3, 0.7, 0.6, 0.4, 0.9, 0.1])
    a = _run(y_true, y_pred, seed=123, n_resamples=100)
    b = _run(y_true, y_pred, seed=123, n_resamples=100)
    for name in ("log_loss", "brier"):
        np.testing.assert_allclose(a[name].point, b[name].point, atol=0)
        np.testing.assert_allclose(a[name].ci_low, b[name].ci_low, atol=0)
        np.testing.assert_allclose(a[name].ci_high, b[name].ci_high, atol=0)


def test_different_seed_different_ci() -> None:
    y_true = np.tile([0, 1], 50)
    y_pred = np.linspace(0.05, 0.95, 100)
    a = _run(y_true, y_pred, seed=1, n_resamples=300)
    b = _run(y_true, y_pred, seed=2, n_resamples=300)
    assert a["log_loss"].ci_low != b["log_loss"].ci_low or a["log_loss"].ci_high != b[
        "log_loss"
    ].ci_high


def test_point_matches_direct_metric() -> None:
    y_true = np.array([0, 1, 1, 0, 1])
    y_pred = np.array([0.1, 0.9, 0.7, 0.2, 0.6])
    fns = _metric_fns()
    out = _run(y_true, y_pred, seed=7, n_resamples=50)
    for name, fn in fns.items():
        np.testing.assert_allclose(out[name].point, fn(y_true, y_pred), atol=1e-12)


def test_coverage_synthetic() -> None:
    """True metric on Bernoulli(p) predictions should fall in 95% CI often enough."""
    p = 0.55
    n = 80
    fns = _metric_fns()
    hits = 0
    for seed in range(_COVERAGE_N_SEEDS):
        rng = np.random.default_rng(seed + 1000)
        y_true = rng.integers(0, 2, size=n)
        y_pred = np.full(n, p, dtype=float)
        true_ll = fns["log_loss"](y_true, y_pred)
        res = _run(y_true, y_pred, seed=seed + 5000, n_resamples=400)["log_loss"]
        if res.ci_low <= true_ll <= res.ci_high:
            hits += 1
    assert hits / _COVERAGE_N_SEEDS >= _COVERAGE_MIN_FRACTION


def test_block_bootstrap_wider_than_iid() -> None:
    """Block resampling preserves day-level prevalence; i.i.d. mixes matches more."""
    n_days = 24
    per_day = 6
    n = n_days * per_day
    days = np.repeat(pd.date_range("2020-01-01", periods=n_days, freq="D"), per_day)
    y_true = np.zeros(n, dtype=int)
    y_pred = np.zeros(n, dtype=float)
    for i in range(n_days):
        mask = slice(i * per_day, (i + 1) * per_day)
        if i < n_days // 2:
            y_true[mask] = 0
            y_pred[mask] = 0.15
        else:
            y_true[mask] = 1
            y_pred[mask] = 0.85

    iid = _run(y_true, y_pred, block_by_day=False, seed=99, n_resamples=600)
    block = _run(
        y_true,
        y_pred,
        block_by_day=True,
        day=days,
        seed=99,
        n_resamples=600,
    )
    iid_width = iid["brier"].ci_high - iid["brier"].ci_low
    block_width = block["brier"].ci_high - block["brier"].ci_low
    assert block_width > iid_width * 1.2


def test_block_single_day_raises() -> None:
    y_true = np.array([0, 1, 0])
    y_pred = np.array([0.2, 0.8, 0.3])
    day = pd.to_datetime(["2020-01-01"] * 3)
    with pytest.raises(BootstrapError, match="D=1"):
        _run(y_true, y_pred, block_by_day=True, day=day)


def test_block_nat_in_day_raises() -> None:
    y_true = np.array([0, 1, 0, 1])
    y_pred = np.array([0.2, 0.8, 0.3, 0.7])
    day = pd.Series(["2020-01-01", "2020-01-02", pd.NaT, "2020-01-02"])
    with pytest.raises(BootstrapError, match="NaT"):
        _run(y_true, y_pred, block_by_day=True, day=day)


def test_block_without_day_raises() -> None:
    with pytest.raises(BootstrapError, match="day is required"):
        _run(np.array([0, 1]), np.array([0.3, 0.7]), block_by_day=True)


def test_n_resamples_zero_raises() -> None:
    with pytest.raises(BootstrapError, match="positive"):
        bootstrap_metrics(
            np.array([0, 1]),
            np.array([0.3, 0.7]),
            n_resamples=0,
            seed=1,
            metric_fns=_metric_fns(),
        )


def test_length_mismatch_raises() -> None:
    with pytest.raises(BootstrapError, match="length"):
        _run(np.array([0, 1, 0]), np.array([0.3, 0.7]))


def test_invalid_y_true_raises() -> None:
    with pytest.raises(MetricsInputError):
        _run(np.array([0, 2]), np.array([0.3, 0.7]))


def test_no_global_numpy_random_state() -> None:
    state_before = np.random.get_state()
    _run(np.array([0, 1, 0, 1]), np.array([0.2, 0.8, 0.3, 0.7]), seed=11)
    state_after = np.random.get_state()
    assert state_before[0] == state_after[0]
    np.testing.assert_array_equal(state_before[1], state_after[1])
    assert state_before[2] == state_after[2]


def test_result_metadata_json_serializable() -> None:
    out = _run(np.array([0, 1, 0, 1]), np.array([0.2, 0.8, 0.3, 0.7]), seed=5)[
        "brier"
    ]
    payload = out.to_dict()
    assert set(payload.keys()) >= {
        "metric_name",
        "point",
        "ci_low",
        "ci_high",
        "bootstrap.N",
        "bootstrap.block_by_day",
        "bootstrap.seed",
    }
    meta_keys = {"bootstrap.N", "bootstrap.block_by_day", "bootstrap.seed"}
    assert meta_keys <= set(payload.keys())
    json.dumps(payload)
    plain = asdict(out)
    assert "bootstrap.N" not in plain
    assert payload["bootstrap.N"] == out.n_resamples
    assert payload["bootstrap.block_by_day"] is False
    assert payload["bootstrap.seed"] == 5


def test_standard_metric_fns_match_metrics_module() -> None:
    y_true = np.array([0, 1, 1, 0])
    y_pred = np.array([0.1, 0.9, 0.7, 0.2])
    fns = _metric_fns()
    assert fns["log_loss"](y_true, y_pred) == log_loss(y_true, y_pred)
    assert fns["brier"](y_true, y_pred) == brier(y_true, y_pred)
