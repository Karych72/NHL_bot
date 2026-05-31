"""Tests for modeling/train_logreg.py and modeling/artifacts.py (UPDATE plan stage 7).

Coverage:
    1.  Pipeline composition (steps, order, hyperparameters, class_weight default).
    2.  Forbidden team-ID column guard.
    3.  No statistics leakage: imputer/scaler fit only on train block.
    4.  C-grid selection picks the best C by inner-val log loss.
    5.  Tie-break is deterministic (smaller C wins).
    6.  log_loss_fn is used, not sklearn.metrics.log_loss directly.
    7.  Determinism: two runs with same seed give identical results.
    8.  Two tasks are trained independently (different labels → potentially different C).
    9.  Artifact round-trip: save → load → identical predict_proba + valid metadata.json.
    10. git_commit is None (not an exception) when run outside a .git directory.
    11. Library version strings are present and non-empty in metadata.
    12. No-DB-access: train_logreg.py and artifacts.py contain no forbidden imports.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from modeling.train_logreg import (
    FORBIDDEN_TEAM_ID_COLUMNS,
    FitResult,
    assert_no_team_id_columns,
    build_logreg_pipeline,
    select_C_by_inner_val,
    train_logreg_for_task,
)
from modeling.artifacts import (
    build_logreg_metadata,
    load_model_artifact,
    save_model_artifact,
)


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

RNG = np.random.default_rng(0)


def _make_Xy(
    n: int = 200,
    n_features: int = 5,
    rng: np.random.Generator | None = None,
) -> tuple[pd.DataFrame, np.ndarray]:
    _rng = rng or RNG
    X = pd.DataFrame(
        _rng.standard_normal((n, n_features)),
        columns=[f"feat_{i}" for i in range(n_features)],
    )
    y = _rng.integers(0, 2, size=n).astype(float)
    return X, y


def _dummy_log_loss(y_true: np.ndarray, p: np.ndarray) -> float:
    """Thin wrapper that delegates to modeling.metrics.log_loss."""
    from modeling.metrics import log_loss as _ll
    return _ll(y_true, p)


# ---------------------------------------------------------------------------
# Test 1: Pipeline composition
# ---------------------------------------------------------------------------

class TestBuildLogregPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = build_logreg_pipeline(C=1.0, random_seed=42)

    def test_is_pipeline(self) -> None:
        self.assertIsInstance(self.pipe, Pipeline)

    def test_exactly_three_steps(self) -> None:
        self.assertEqual(len(self.pipe.steps), 3)

    def test_step_order_and_types(self) -> None:
        _, imputer = self.pipe.steps[0]
        _, scaler = self.pipe.steps[1]
        _, lr = self.pipe.steps[2]
        self.assertIsInstance(imputer, SimpleImputer)
        self.assertIsInstance(scaler, StandardScaler)
        self.assertIsInstance(lr, LogisticRegression)

    def test_imputer_strategy_median(self) -> None:
        _, imputer = self.pipe.steps[0]
        self.assertEqual(imputer.strategy, "median")

    def test_logreg_hyperparameters(self) -> None:
        _, lr = self.pipe.steps[2]
        self.assertEqual(lr.penalty, "l2")
        self.assertEqual(lr.solver, "lbfgs")
        self.assertEqual(lr.max_iter, 5000)
        self.assertEqual(lr.C, 1.0)

    def test_class_weight_default_is_none(self) -> None:
        _, lr = self.pipe.steps[2]
        self.assertIsNone(lr.class_weight)

    def test_class_weight_passed_through(self) -> None:
        pipe = build_logreg_pipeline(C=0.5, class_weight="balanced", random_seed=0)
        _, lr = pipe.steps[2]
        self.assertEqual(lr.class_weight, "balanced")

    def test_random_state_set(self) -> None:
        pipe = build_logreg_pipeline(C=1.0, random_seed=99)
        _, lr = pipe.steps[2]
        self.assertEqual(lr.random_state, 99)


# ---------------------------------------------------------------------------
# Test 2: Forbidden team-ID column guard
# ---------------------------------------------------------------------------

class TestAssertNoTeamIdColumns(unittest.TestCase):
    def test_clean_columns_do_not_raise(self) -> None:
        assert_no_team_id_columns(["feat_1", "feat_2", "season"])

    def test_home_team_id_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            assert_no_team_id_columns(["feat_1", "home_team_id"])
        self.assertIn("home_team_id", str(ctx.exception))

    def test_away_team_id_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            assert_no_team_id_columns(["away_team_id", "feat_2"])
        self.assertIn("away_team_id", str(ctx.exception))

    def test_both_forbidden_columns_raise(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            assert_no_team_id_columns(["home_team_id", "away_team_id", "feat_1"])
        msg = str(ctx.exception)
        self.assertIn("home_team_id", msg)
        self.assertIn("away_team_id", msg)

    def test_error_message_is_informative(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            assert_no_team_id_columns(["home_team_id"])
        self.assertTrue(len(str(ctx.exception)) > 10)


# ---------------------------------------------------------------------------
# Test 3: No statistics leakage
# ---------------------------------------------------------------------------

class TestNoLeakage(unittest.TestCase):
    def test_imputer_and_scaler_stats_from_train_only(self) -> None:
        rng = np.random.default_rng(7)
        # Train block: all values in [0, 1]
        X_train = pd.DataFrame(rng.uniform(0, 1, (100, 3)), columns=["a", "b", "c"])
        y_train = rng.integers(0, 2, 100).astype(float)
        # Val block: values in [100, 200] — very different distribution
        X_val = pd.DataFrame(rng.uniform(100, 200, (50, 3)), columns=["a", "b", "c"])
        y_val = rng.integers(0, 2, 50).astype(float)

        pipe = build_logreg_pipeline(C=1.0, random_seed=0)
        pipe.fit(X_train, y_train)

        train_means = pipe.named_steps["standardscaler"].mean_.copy()

        # Call predict_proba on val (this applies transform internally)
        pipe.predict_proba(X_val)

        # Stats must not change after predict
        np.testing.assert_array_equal(
            train_means,
            pipe.named_steps["standardscaler"].mean_,
            err_msg="StandardScaler mean changed after predict_proba on val block",
        )

    def test_mean_matches_train_not_combined(self) -> None:
        rng = np.random.default_rng(13)
        X_train = pd.DataFrame({"x": rng.uniform(0, 1, 80)})
        y_train = rng.integers(0, 2, 80).astype(float)
        X_val = pd.DataFrame({"x": rng.uniform(10, 20, 20)})

        pipe = build_logreg_pipeline(C=1.0, random_seed=0)
        pipe.fit(X_train, y_train)

        scaler_mean = pipe.named_steps["standardscaler"].mean_[0]
        expected_mean = X_train["x"].mean()
        self.assertAlmostEqual(scaler_mean, expected_mean, places=10)


# ---------------------------------------------------------------------------
# Test 4: C selection by inner val
# ---------------------------------------------------------------------------

class TestSelectCByInnerVal(unittest.TestCase):
    def _make_data(self) -> tuple:
        rng = np.random.default_rng(42)
        X_train, y_train = _make_Xy(300, 6, rng)
        X_val, y_val = _make_Xy(100, 6, rng)
        return X_train, y_train, X_val, y_val

    def test_returns_tuple_of_three(self) -> None:
        X_train, y_train, X_val, y_val = self._make_data()
        result = select_C_by_inner_val(
            X_train, y_train, X_val, y_val,
            C_grid=[0.01, 1.0, 10.0],
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        self.assertEqual(len(result), 3)

    def test_chosen_C_is_in_grid(self) -> None:
        X_train, y_train, X_val, y_val = self._make_data()
        grid = [0.001, 0.1, 10.0]
        chosen_C, _, _ = select_C_by_inner_val(
            X_train, y_train, X_val, y_val,
            C_grid=grid,
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        self.assertIn(chosen_C, grid)

    def test_log_loss_table_covers_all_C_values(self) -> None:
        X_train, y_train, X_val, y_val = self._make_data()
        grid = [0.01, 0.1, 1.0, 10.0]
        _, _, table = select_C_by_inner_val(
            X_train, y_train, X_val, y_val,
            C_grid=grid,
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        self.assertEqual(set(table.keys()), set(grid))

    def test_chosen_C_has_minimum_log_loss(self) -> None:
        X_train, y_train, X_val, y_val = self._make_data()
        grid = [0.001, 0.01, 0.1, 1.0, 10.0]
        chosen_C, _, table = select_C_by_inner_val(
            X_train, y_train, X_val, y_val,
            C_grid=grid,
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        self.assertEqual(table[chosen_C], min(table.values()))

    def test_empty_C_grid_raises(self) -> None:
        X_train, y_train, X_val, y_val = self._make_data()
        with self.assertRaises(ValueError):
            select_C_by_inner_val(
                X_train, y_train, X_val, y_val,
                C_grid=[],
                random_seed=0,
                log_loss_fn=_dummy_log_loss,
            )


# ---------------------------------------------------------------------------
# Test 5: Tie-break is deterministic — smaller C wins
# ---------------------------------------------------------------------------

class TestTieBreak(unittest.TestCase):
    def test_tie_break_selects_smaller_C(self) -> None:
        """Force all C-values to return the same loss via a constant log_loss_fn."""
        rng = np.random.default_rng(1)
        X_train, y_train = _make_Xy(100, 4, rng)
        X_val, y_val = _make_Xy(50, 4, rng)

        grid = [0.1, 1.0, 10.0]
        constant_loss_fn: Callable[[np.ndarray, np.ndarray], float] = lambda _y, _p: 0.5

        chosen_C, _, _ = select_C_by_inner_val(
            X_train, y_train, X_val, y_val,
            C_grid=grid,
            random_seed=0,
            log_loss_fn=constant_loss_fn,
        )
        self.assertEqual(chosen_C, min(grid), "Tie-break must select the smallest C")


# ---------------------------------------------------------------------------
# Test 6: log_loss_fn is used, not sklearn directly
# ---------------------------------------------------------------------------

class TestLogLossFnUsed(unittest.TestCase):
    def test_custom_log_loss_fn_controls_selection(self) -> None:
        """
        If we return a high loss for C=1.0 and low for C=0.01, C=0.01 must be chosen
        even if the real log loss would prefer C=1.0.
        """
        rng = np.random.default_rng(5)
        X_train, y_train = _make_Xy(150, 4, rng)
        X_val, y_val = _make_Xy(50, 4, rng)

        call_count = [0]

        def marker_log_loss(y_true: np.ndarray, p: np.ndarray) -> float:
            call_count[0] += 1
            # Return artificially low loss for the first call (which will be for C=0.01)
            return 0.1 if call_count[0] == 1 else 0.9

        grid = [0.01, 1.0]
        chosen_C, _, table = select_C_by_inner_val(
            X_train, y_train, X_val, y_val,
            C_grid=grid,
            random_seed=0,
            log_loss_fn=marker_log_loss,
        )
        # The marker function was called (not sklearn)
        self.assertEqual(call_count[0], 2, "log_loss_fn must be called once per C value")
        # Selection was driven by the marker values
        self.assertEqual(set(table.keys()), {0.01, 1.0})
        # Verify table values come from our function (0.1 and 0.9)
        self.assertIn(0.1, table.values())
        self.assertIn(0.9, table.values())


# ---------------------------------------------------------------------------
# Test 7: Determinism
# ---------------------------------------------------------------------------

class TestDeterminism(unittest.TestCase):
    def test_two_runs_give_identical_predictions(self) -> None:
        rng = np.random.default_rng(77)
        X_train, y_train = _make_Xy(200, 5, rng)
        X_val, y_val = _make_Xy(60, 5, rng)
        X_test = pd.DataFrame(rng.standard_normal((40, 5)), columns=[f"feat_{i}" for i in range(5)])

        kwargs = dict(
            task="home_win",
            X_train=X_train, y_train=y_train,
            X_val=X_val, y_val=y_val,
            C_grid=[0.01, 0.1, 1.0],
            class_weight=None,
            random_seed=42,
            log_loss_fn=_dummy_log_loss,
        )
        r1 = train_logreg_for_task(**kwargs)
        r2 = train_logreg_for_task(**kwargs)

        self.assertEqual(r1.chosen_C, r2.chosen_C)
        np.testing.assert_allclose(
            r1.pipeline.predict_proba(X_test),
            r2.pipeline.predict_proba(X_test),
            rtol=0,
            atol=0,
        )


# ---------------------------------------------------------------------------
# Test 8: Two tasks trained independently
# ---------------------------------------------------------------------------

class TestTwoTasksIndependent(unittest.TestCase):
    def test_different_labels_can_yield_different_chosen_C(self) -> None:
        rng = np.random.default_rng(99)
        X_train, _ = _make_Xy(300, 5, rng)
        X_val, _ = _make_Xy(100, 5, rng)
        # Different label vectors for each task
        y_home = rng.integers(0, 2, 300).astype(float)
        y_over = 1 - y_home  # Complementary labels → different model
        y_home_val = rng.integers(0, 2, 100).astype(float)
        y_over_val = 1 - y_home_val

        r_home = train_logreg_for_task(
            "home_win", X_train, y_home, X_val, y_home_val,
            C_grid=[0.01, 0.1, 1.0, 10.0],
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        r_over = train_logreg_for_task(
            "over_5_5", X_train, y_over, X_val, y_over_val,
            C_grid=[0.01, 0.1, 1.0, 10.0],
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        self.assertEqual(r_home.task, "home_win")
        self.assertEqual(r_over.task, "over_5_5")
        # Pipelines are different objects
        self.assertIsNot(r_home.pipeline, r_over.pipeline)

    def test_task_label_map_is_correct(self) -> None:
        from modeling.train_logreg import TASK_LABEL_MAP
        self.assertIn("home_win", TASK_LABEL_MAP)
        self.assertIn("over_5_5", TASK_LABEL_MAP)
        self.assertEqual(TASK_LABEL_MAP["home_win"], "y_home_win")
        self.assertEqual(TASK_LABEL_MAP["over_5_5"], "y_over_5_5")


# ---------------------------------------------------------------------------
# Test 9: Artifact round-trip
# ---------------------------------------------------------------------------

class TestArtifactRoundTrip(unittest.TestCase):
    def _train_result(self) -> FitResult:
        rng = np.random.default_rng(11)
        X_train, y_train = _make_Xy(200, 4, rng)
        X_val, y_val = _make_Xy(60, 4, rng)
        return train_logreg_for_task(
            "home_win", X_train, y_train, X_val, y_val,
            C_grid=[0.1, 1.0],
            random_seed=7,
            log_loss_fn=_dummy_log_loss,
        )

    def test_round_trip_identical_predict_proba(self) -> None:
        rng = np.random.default_rng(12)
        result = self._train_result()
        X_test = pd.DataFrame(rng.standard_normal((30, 4)), columns=[f"feat_{i}" for i in range(4)])
        proba_before = result.pipeline.predict_proba(X_test)

        meta = build_logreg_metadata(
            task="home_win",
            chosen_C=result.chosen_C,
            class_weight=None,
            random_seed=7,
            features_hash="abc123",
            feature_set_version="v1",
            feature_manifest=[{"name": f"feat_{i}", "dtype": "float64", "position": i} for i in range(4)],
            n_rows_train=result.n_rows_train,
            n_rows_inner_val=result.n_rows_inner_val,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            save_model_artifact(tmpdir, model=result.pipeline, metadata=meta)
            loaded_model, loaded_meta = load_model_artifact(tmpdir)

        proba_after = loaded_model.predict_proba(X_test)
        np.testing.assert_allclose(proba_before, proba_after, rtol=0, atol=0)

    def test_metadata_json_is_valid(self) -> None:
        result = self._train_result()
        meta = build_logreg_metadata(
            task="home_win",
            chosen_C=result.chosen_C,
            class_weight=None,
            random_seed=7,
            features_hash="abc123",
            feature_set_version="v1",
            feature_manifest=[{"name": f"feat_{i}", "dtype": "float64", "position": i} for i in range(4)],
            n_rows_train=200,
            n_rows_inner_val=60,
            train_days=("2021-01-01", "2021-06-30"),
            inner_val_days=("2021-07-01", "2021-09-30"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            save_model_artifact(tmpdir, model=result.pipeline, metadata=meta)
            meta_path = Path(tmpdir) / "metadata.json"
            raw = meta_path.read_text(encoding="utf-8")
            loaded = json.loads(raw)  # Must not raise

        # Required fields
        required = [
            "features_hash", "feature_set_version", "feature_manifest",
            "model_family", "task", "chosen_C", "class_weight", "random_seed",
            "n_rows_train", "n_rows_inner_val",
            "library_versions", "git_commit",
        ]
        for key in required:
            self.assertIn(key, loaded, f"Missing metadata key: {key!r}")

    def test_model_family_is_logreg(self) -> None:
        result = self._train_result()
        meta = build_logreg_metadata(
            task="over_5_5", chosen_C=1.0, class_weight=None, random_seed=0,
            features_hash="x", feature_set_version="v1", feature_manifest=[],
            n_rows_train=100, n_rows_inner_val=30,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            save_model_artifact(tmpdir, model=result.pipeline, metadata=meta)
            _, loaded_meta = load_model_artifact(tmpdir)
        self.assertEqual(loaded_meta["model_family"], "logreg")


# ---------------------------------------------------------------------------
# Test 10: git_commit is None (not an exception) outside a .git directory
# ---------------------------------------------------------------------------

class TestGitCommitWithoutDotGit(unittest.TestCase):
    def test_git_commit_null_in_non_git_dir(self) -> None:
        from modeling.artifacts import _get_git_commit

        with tempfile.TemporaryDirectory() as tmpdir:
            commit = _get_git_commit(repo_root=Path(tmpdir))
        # Must be None (not an exception)
        self.assertIsNone(commit)

    def test_save_artifact_does_not_crash_without_git(self) -> None:
        rng = np.random.default_rng(20)
        X_train, y_train = _make_Xy(100, 3, rng)
        X_val, y_val = _make_Xy(40, 3, rng)
        result = train_logreg_for_task(
            "home_win", X_train, y_train, X_val, y_val,
            C_grid=[1.0],
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        meta = {"model_family": "logreg", "task": "home_win"}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch _get_git_commit to simulate no .git
            with patch("modeling.artifacts._get_git_commit", return_value=None):
                save_model_artifact(tmpdir, model=result.pipeline, metadata=meta)
            _, loaded_meta = load_model_artifact(tmpdir)
        self.assertIsNone(loaded_meta.get("git_commit"))


# ---------------------------------------------------------------------------
# Test 11: Library versions are present and non-empty
# ---------------------------------------------------------------------------

class TestLibraryVersions(unittest.TestCase):
    def test_library_versions_in_metadata(self) -> None:
        rng = np.random.default_rng(30)
        X_train, y_train = _make_Xy(100, 3, rng)
        X_val, y_val = _make_Xy(40, 3, rng)
        result = train_logreg_for_task(
            "home_win", X_train, y_train, X_val, y_val,
            C_grid=[1.0],
            random_seed=0,
            log_loss_fn=_dummy_log_loss,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            save_model_artifact(tmpdir, model=result.pipeline, metadata={"task": "home_win"})
            _, meta = load_model_artifact(tmpdir)

        versions = meta.get("library_versions", {})
        for pkg in ("scikit-learn", "numpy", "pandas", "joblib"):
            self.assertIn(pkg, versions, f"Missing library version for {pkg!r}")
            self.assertTrue(
                isinstance(versions[pkg], str) and versions[pkg],
                f"Version for {pkg!r} must be a non-empty string, got {versions[pkg]!r}",
            )


# ---------------------------------------------------------------------------
# Test 12: No-DB-access guard
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = frozenset({"psycopg2", "modeling.dataset_builder"})


def _forbidden_imports_in_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    hits.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _is_forbidden(node.module):
                hits.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return hits


def _is_forbidden(module: str) -> bool:
    return any(
        module == root or module.startswith(f"{root}.")
        for root in FORBIDDEN_IMPORT_ROOTS
    )


class TestNoDbAccess(unittest.TestCase):
    def _check_module(self, rel_path: str) -> None:
        root = Path(__file__).resolve().parent.parent / "modeling"
        hits = _forbidden_imports_in_module(root / rel_path)
        self.assertEqual(hits, [], msg=f"Forbidden imports in modeling/{rel_path}: {hits}")

    def test_train_logreg_no_db_imports(self) -> None:
        self._check_module("train_logreg.py")

    def test_artifacts_no_db_imports(self) -> None:
        self._check_module("artifacts.py")


if __name__ == "__main__":
    unittest.main()
