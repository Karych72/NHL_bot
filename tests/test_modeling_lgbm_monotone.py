"""Tests for modeling/train_lgbm.py and monotone constraints (UPDATE plan 11)."""

from __future__ import annotations

import importlib.util
import unittest

import numpy as np
import pandas as pd
import pytest

HAS_LIGHTGBM = importlib.util.find_spec("lightgbm") is not None
pytestmark = pytest.mark.skipif(
    not HAS_LIGHTGBM,
    reason="lightgbm not installed (see requirements-modeling.txt)",
)

from modeling.config import ConfigError
from modeling.metrics import log_loss
from modeling.train_common import build_monotone_constraints, validate_num_threads
from modeling.train_lgbm import (
    build_lgbm_base_params,
    expand_lgbm_grid,
    predict_lgbm_proba,
    select_lgbm_config_by_inner_val,
    train_lgbm_for_task,
    train_single_lgbm,
)


RNG = np.random.default_rng(0)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _make_monotone_data(
    n: int = 400,
    *,
    coef: float = 2.0,
    rng: np.random.Generator | None = None,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Synthetic data: y ~ Bernoulli(sigmoid(coef * x0)), other features noise."""
    _rng = rng or RNG
    x0 = _rng.uniform(-2.0, 2.0, size=n)
    x1 = _rng.standard_normal(n)
    X = pd.DataFrame({"feat_mono": x0, "feat_noise": x1})
    p = _sigmoid(coef * x0)
    y = (_rng.uniform(size=n) < p).astype(float)
    return X, y


def _tiny_grid(**overrides: list) -> list[dict]:
    base = {
        "num_leaves": [4],
        "min_data_in_leaf": [5],
        "feature_fraction": [1.0],
        "bagging_fraction": [1.0],
        "lambda_l1": [0.0],
        "lambda_l2": [0.0],
        "learning_rate": [0.2],
    }
    base.update(overrides)
    return expand_lgbm_grid(base)


class TestMonotonePredictionScan(unittest.TestCase):
    """Trained LGBM with +1 / -1 constraints must not violate sign on feature scan."""

    # Allow tiny numerical noise from histogram splits on a coarse grid.
    _TOL = 1e-6

    def _scan_monotonicity(
        self,
        sign: int,
        *,
        coef: float = 2.5,
        seed: int = 7,
    ) -> None:
        X, y = _make_monotone_data(n=500, coef=coef, rng=np.random.default_rng(seed))
        n_train = 350
        X_train, y_train = X.iloc[:n_train], y[:n_train]
        X_val, y_val = X.iloc[n_train:], y[n_train:]

        constraints = build_monotone_constraints(X.columns, {"feat_mono": sign})
        params = build_lgbm_base_params(
            random_seed=42,
            num_threads=1,
            monotone_constraints=constraints,
        )
        params.update(_tiny_grid()[0])

        booster = train_single_lgbm(
            X_train,
            y_train,
            X_val,
            y_val,
            params=params,
            num_boost_round=80,
            early_stopping_rounds=10,
        )

        grid_vals = np.linspace(X["feat_mono"].min(), X["feat_mono"].max(), 40)
        median_noise = float(X["feat_noise"].median())
        preds = [
            float(
                predict_lgbm_proba(
                    booster,
                    pd.DataFrame({"feat_mono": [v], "feat_noise": [median_noise]}),
                )[0]
            )
            for v in grid_vals
        ]

        diffs = np.diff(preds)
        if sign == 1:
            self.assertTrue(
                np.all(diffs >= -self._TOL),
                msg=f"+1 constraint violated: min diff={diffs.min()}",
            )
        else:
            self.assertTrue(
                np.all(diffs <= self._TOL),
                msg=f"-1 constraint violated: max diff={diffs.max()}",
            )

    def test_positive_constraint_non_decreasing(self) -> None:
        self._scan_monotonicity(+1)

    def test_negative_constraint_non_increasing(self) -> None:
        self._scan_monotonicity(-1, coef=-2.5)


class TestDeterminism(unittest.TestCase):
    def test_identical_predictions_same_seed(self) -> None:
        X, y = _make_monotone_data(n=300, rng=np.random.default_rng(1))
        X_train, y_train = X.iloc[:200], y[:200]
        X_val, y_val = X.iloc[200:], y[200:]
        grid = _tiny_grid()

        kwargs = dict(
            task="home_win",
            X_train=X_train,
            y_train=y_train,
            X_val=X_val,
            y_val=y_val,
            grid=grid,
            random_seed=99,
            num_threads=1,
            early_stopping_rounds=5,
            num_boost_round=40,
            log_loss_fn=log_loss,
            monotone_constraints=build_monotone_constraints(X.columns, {"feat_mono": 1}),
        )
        r1 = train_lgbm_for_task(**kwargs)
        r2 = train_lgbm_for_task(**kwargs)

        p1 = predict_lgbm_proba(r1.booster, X_val)
        p2 = predict_lgbm_proba(r2.booster, X_val)
        np.testing.assert_allclose(p1, p2, atol=0)


class TestMonotoneConstraintsByName(unittest.TestCase):
    def test_positional_mapping_survives_column_reorder(self) -> None:
        names_a = ["alpha", "beta", "gamma"]
        names_b = ["gamma", "alpha", "beta"]
        spec = {"alpha": 1, "gamma": -1}

        c_a = build_monotone_constraints(names_a, spec)
        c_b = build_monotone_constraints(names_b, spec)

        self.assertEqual(c_a, [1, 0, -1])
        self.assertEqual(c_b, [-1, 1, 0])

    def test_unmatched_pattern_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            build_monotone_constraints(["feat_a", "feat_b"], {"missing_*": 1})
        self.assertIn("missing_*", str(ctx.exception))


class TestGridSelection(unittest.TestCase):
    def test_selects_lower_inner_val_log_loss(self) -> None:
        X, y = _make_monotone_data(n=400, rng=np.random.default_rng(2))
        X_train, y_train = X.iloc[:250], y[:250]
        X_val, y_val = X.iloc[250:], y[250:]

        grid = _tiny_grid(learning_rate=[0.05, 0.3])
        _, booster, losses, _ = select_lgbm_config_by_inner_val(
            X_train,
            y_train,
            X_val,
            y_val,
            grid,
            random_seed=11,
            num_threads=1,
            early_stopping_rounds=5,
            num_boost_round=30,
            log_loss_fn=log_loss,
        )

        best_lr = min(losses, key=lambda item: item[1])[0]["learning_rate"]
        chosen_lr = next(params["learning_rate"] for params, loss in losses if loss == min(l for _, l in losses))
        self.assertEqual(best_lr, chosen_lr)
        self.assertIsNotNone(booster)

    def test_tie_break_prefers_first_grid_point(self) -> None:
        X, y = _make_monotone_data(n=200, rng=np.random.default_rng(3))
        X_train, y_train = X.iloc[:120], y[:120]
        X_val, y_val = X.iloc[120:], y[120:]

        grid = [
            {"num_leaves": 4, "min_data_in_leaf": 5, "feature_fraction": 1.0,
             "bagging_fraction": 1.0, "lambda_l1": 0.0, "lambda_l2": 0.0, "learning_rate": 0.1},
            {"num_leaves": 8, "min_data_in_leaf": 5, "feature_fraction": 1.0,
             "bagging_fraction": 1.0, "lambda_l1": 0.0, "lambda_l2": 0.0, "learning_rate": 0.1},
        ]

        def tied_log_loss(_y_true: np.ndarray, _p: np.ndarray) -> float:
            return 0.42

        chosen_params, _, losses, _ = select_lgbm_config_by_inner_val(
            X_train,
            y_train,
            X_val,
            y_val,
            grid,
            random_seed=5,
            num_threads=1,
            early_stopping_rounds=5,
            num_boost_round=20,
            log_loss_fn=tied_log_loss,
        )

        self.assertEqual(losses[0][1], losses[1][1])
        self.assertEqual(chosen_params["num_leaves"], 4)


class TestInnerValRequired(unittest.TestCase):
    def test_empty_inner_val_raises(self) -> None:
        X, y = _make_monotone_data(n=50)
        params = build_lgbm_base_params(random_seed=1, num_threads=1)
        params.update(_tiny_grid()[0])
        with self.assertRaises(ValueError) as ctx:
            train_single_lgbm(
                X.iloc[:40],
                y[:40],
                X.iloc[:0],
                y[:0],
                params=params,
                num_boost_round=10,
                early_stopping_rounds=3,
            )
        self.assertIn("inner_val", str(ctx.exception).lower())


class TestEmptyGrid(unittest.TestCase):
    def test_empty_grid_raises_config_error(self) -> None:
        X, y = _make_monotone_data(n=100)
        with self.assertRaises(ConfigError):
            train_lgbm_for_task(
                task="home_win",
                X_train=X.iloc[:60],
                y_train=y[:60],
                X_val=X.iloc[60:],
                y_val=y[60:],
                grid=[],
                random_seed=1,
                num_threads=1,
                early_stopping_rounds=3,
                num_boost_round=10,
                log_loss_fn=log_loss,
            )


class TestNumThreads(unittest.TestCase):
    def test_non_positive_num_threads_raises(self) -> None:
        with self.assertRaises(ConfigError):
            validate_num_threads(0)
        with self.assertRaises(ConfigError):
            validate_num_threads(-1)

    def test_base_params_use_explicit_num_threads(self) -> None:
        params = build_lgbm_base_params(random_seed=1, num_threads=3)
        self.assertEqual(params["num_threads"], 3)
        self.assertNotEqual(params["num_threads"], -1)


if __name__ == "__main__":
    unittest.main()
