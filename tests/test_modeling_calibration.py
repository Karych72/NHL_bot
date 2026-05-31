"""Tests for modeling/calibrate.py (UPDATE plan stage 11)."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from modeling.calibrate import (
    CalibrationError,
    apply_calibrator,
    fit_calibrator,
    load_calibration_artifact,
    save_calibration_artifact,
)
from modeling.metrics import brier, ece


RNG = np.random.default_rng(42)


def _miscalibrated_probs(y: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Synthetic raw probabilities: compressed toward 0.5 (poor calibration)."""
    noise = rng.uniform(-0.05, 0.05, size=y.size)
    raw = 0.35 + 0.3 * y + noise
    return np.clip(raw, 0.0, 1.0)


def _make_calibration_data(
    n: int = 800,
    rng: np.random.Generator | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    _rng = rng or RNG
    y = _rng.integers(0, 2, size=n)
    p = _miscalibrated_probs(y, _rng)
    return p, y


class TestFitCalibratorOnlyOnCalibrationBlock(unittest.TestCase):
    """fit_calibrator accepts only (raw_p_cal, y_cal) — no test/holdout API."""

    def test_signature_has_only_calibration_arrays(self) -> None:
        sig = inspect.signature(fit_calibrator)
        positional = [
            name
            for name, param in sig.parameters.items()
            if param.default is inspect.Parameter.empty
            and param.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual(positional, ["raw_p_cal", "y_cal"])

    def test_result_depends_only_on_calibration_inputs(self) -> None:
        p_cal, y_cal = _make_calibration_data(n=600)
        fit_a = fit_calibrator(
            p_cal, y_cal, method="isotonic", min_samples=500, seed=7
        )

        # Perturbing a held-out "test" slice cannot affect fit — no such parameter exists.
        p_test = RNG.uniform(0.0, 1.0, size=200)
        y_test = RNG.integers(0, 2, size=200)
        fit_b = fit_calibrator(
            p_cal, y_cal, method="isotonic", min_samples=500, seed=7
        )
        self.assertEqual(fit_a.to_dict(), fit_b.to_dict())
        p_apply = np.linspace(0.05, 0.95, 50)
        np.testing.assert_array_equal(
            apply_calibrator(fit_a, p_apply),
            apply_calibrator(fit_b, p_apply),
        )
        # Sanity: test data exists but was never passed to fit_calibrator.
        self.assertEqual(p_test.size, 200)
        self.assertEqual(y_test.size, 200)

    def test_isotonic_fit_receives_only_calibration_slice(self) -> None:
        """Marked arrays: only calibration_k indices may reach the sklearn fit call."""
        n_cal, n_test = 600, 400
        p_all = RNG.uniform(0.0, 1.0, size=n_cal + n_test)
        y_all = RNG.integers(0, 2, size=n_cal + n_test)
        p_cal, y_cal = p_all[:n_cal], y_all[:n_cal]
        p_test, y_test = p_all[n_cal:], y_all[n_cal:]

        seen_x: list[np.ndarray] = []
        seen_y: list[np.ndarray] = []
        original_fit = IsotonicRegression.fit

        def _recording_fit(self, X, y, sample_weight=None):  # type: ignore[no-untyped-def]
            seen_x.append(np.asarray(X, dtype=float).copy())
            seen_y.append(np.asarray(y, dtype=float).copy())
            return original_fit(self, X, y, sample_weight=sample_weight)

        with patch.object(IsotonicRegression, "fit", _recording_fit):
            fit_calibrator(p_cal, y_cal, method="isotonic", min_samples=500, seed=7)

        self.assertEqual(len(seen_x), 1)
        np.testing.assert_array_equal(seen_x[0], p_cal)
        np.testing.assert_array_equal(seen_y[0], y_cal)
        # Hold-out slice exists but must never be passed to fit_calibrator.
        self.assertEqual(p_test.size, n_test)
        self.assertEqual(y_test.size, n_test)


class TestCalibrationSkipped(unittest.TestCase):
    def test_skipped_returns_identity(self) -> None:
        p_cal = RNG.uniform(0.0, 1.0, size=100)
        y_cal = RNG.integers(0, 2, size=100)
        fit = fit_calibrator(
            p_cal, y_cal, method="isotonic", min_samples=500, seed=0
        )
        self.assertTrue(fit.calibration_skipped)
        self.assertEqual(type(fit.calibrator).__name__, "_IdentityCalibrator")

        raw_p = RNG.uniform(0.0, 1.0, size=50)
        np.testing.assert_array_equal(apply_calibrator(fit, raw_p), raw_p)


class TestIsotonicImprovesCalibration(unittest.TestCase):
    def test_ece_not_worse_than_raw_on_miscalibrated_slice(self) -> None:
        # Soft threshold: isotonic should improve (or at least not worsen) ECE on
        # deliberately miscalibrated probabilities.
        y = RNG.integers(0, 2, size=1000)
        raw_p = _miscalibrated_probs(y, RNG)

        fit = fit_calibrator(
            raw_p[:600], y[:600], method="isotonic", min_samples=500, seed=1
        )
        self.assertFalse(fit.calibration_skipped)

        cal_p = apply_calibrator(fit, raw_p[600:])
        y_eval = y[600:]

        ece_raw = ece(y_eval, raw_p[600:])
        ece_cal = ece(y_eval, cal_p)
        self.assertLessEqual(ece_cal, ece_raw + 1e-9)

        brier_raw = brier(y_eval, raw_p[600:])
        brier_cal = brier(y_eval, cal_p)
        self.assertLessEqual(brier_cal, brier_raw + 1e-9)


class TestPlattWorks(unittest.TestCase):
    def setUp(self) -> None:
        p_cal, y_cal = _make_calibration_data(n=600)
        self.fit = fit_calibrator(
            p_cal, y_cal, method="platt", min_samples=500, seed=3, num_threads=1
        )
        self.grid = np.linspace(0.01, 0.99, 100)

    def test_output_in_unit_interval(self) -> None:
        out = apply_calibrator(self.fit, self.grid)
        self.assertTrue(np.all(out >= 0.0))
        self.assertTrue(np.all(out <= 1.0))

    def test_no_nan(self) -> None:
        out = apply_calibrator(self.fit, self.grid)
        self.assertFalse(np.any(np.isnan(out)))

    def test_monotone_non_decreasing(self) -> None:
        out = apply_calibrator(self.fit, self.grid)
        self.assertTrue(np.all(np.diff(out) >= -1e-12))


class TestNoCalibratedClassifierCV(unittest.TestCase):
    def test_calibrate_module_has_no_calibrated_classifier_cv(self) -> None:
        path = Path(__file__).resolve().parent.parent / "modeling" / "calibrate.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "CalibratedClassifierCV" in {a.name for a in node.names}:
                    hits.append(f"import from {node.module}")
            elif isinstance(node, ast.Name) and node.id == "CalibratedClassifierCV":
                hits.append(f"name ref line {node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "CalibratedClassifierCV":
                hits.append(f"attr ref line {node.lineno}")
        self.assertEqual(hits, [])


class TestDeterminism(unittest.TestCase):
    def test_identical_fits_with_same_seed(self) -> None:
        p_cal, y_cal = _make_calibration_data(n=600)
        fit_a = fit_calibrator(
            p_cal, y_cal, method="platt", min_samples=500, seed=99, num_threads=1
        )
        fit_b = fit_calibrator(
            p_cal, y_cal, method="platt", min_samples=500, seed=99, num_threads=1
        )
        probe = np.linspace(0.1, 0.9, 20)
        np.testing.assert_array_equal(
            apply_calibrator(fit_a, probe),
            apply_calibrator(fit_b, probe),
        )


class TestErrorContract(unittest.TestCase):
    def setUp(self) -> None:
        self.p = RNG.uniform(0.0, 1.0, size=50)
        self.y = RNG.integers(0, 2, size=50)

    def test_unknown_method(self) -> None:
        with self.assertRaises(CalibrationError):
            fit_calibrator(self.p, self.y, method="sigmoid", min_samples=10, seed=0)

    def test_length_mismatch(self) -> None:
        with self.assertRaises(CalibrationError):
            fit_calibrator(self.p, self.y[:30], method="isotonic", min_samples=10, seed=0)

    def test_invalid_labels(self) -> None:
        bad_y = self.y.astype(float)
        bad_y[0] = 2.0
        with self.assertRaises(CalibrationError):
            fit_calibrator(self.p, bad_y, method="isotonic", min_samples=10, seed=0)

    def test_raw_p_out_of_range(self) -> None:
        bad_p = self.p.copy()
        bad_p[0] = 1.5
        with self.assertRaises(CalibrationError):
            fit_calibrator(bad_p, self.y, method="isotonic", min_samples=10, seed=0)

    def test_raw_p_nan(self) -> None:
        bad_p = self.p.copy()
        bad_p[0] = np.nan
        with self.assertRaises(CalibrationError):
            fit_calibrator(bad_p, self.y, method="isotonic", min_samples=10, seed=0)

    def test_min_samples_non_positive(self) -> None:
        with self.assertRaises(CalibrationError):
            fit_calibrator(self.p, self.y, method="isotonic", min_samples=0, seed=0)


class TestArtifactRoundTrip(unittest.TestCase):
    def test_save_load_round_trip(self) -> None:
        p_cal, y_cal = _make_calibration_data(n=600)
        fit = fit_calibrator(
            p_cal, y_cal, method="isotonic", min_samples=500, seed=5
        )
        dummy_model = {"family": "logreg", "C": 1.0}
        metadata = {
            "features_hash": "abc123",
            "seed": 5,
            "train_days": {"min": "2020-01-01", "max": "2023-06-01"},
            "calibration_days": {"min": "2023-06-02", "max": "2023-09-01"},
            "n_rows_train": 10000,
            "n_rows_calibration": 600,
        }

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            save_calibration_artifact(
                out, model_raw=dummy_model, calibrator_fit=fit, metadata=metadata
            )
            self.assertTrue((out / "model_raw.joblib").exists())
            self.assertTrue((out / "calibrator.joblib").exists())
            meta_text = (out / "metadata.json").read_text(encoding="utf-8")
            json.dumps(json.loads(meta_text))

            loaded_model, loaded_fit, loaded_meta = load_calibration_artifact(out)

        self.assertEqual(loaded_model, dummy_model)
        self.assertEqual(loaded_fit.to_dict(), fit.to_dict())
        self.assertEqual(loaded_meta["features_hash"], "abc123")
        self.assertIn("library_versions", loaded_meta)
        self.assertIn("scikit-learn", loaded_meta["library_versions"])

        probe = np.linspace(0.05, 0.95, 30)
        np.testing.assert_allclose(
            apply_calibrator(fit, probe),
            apply_calibrator(loaded_fit, probe),
        )


class TestRawToCalibratorChain(unittest.TestCase):
    def test_apply_matches_direct_isotonic(self) -> None:
        p_cal, y_cal = _make_calibration_data(n=600)
        fit = fit_calibrator(
            p_cal, y_cal, method="isotonic", min_samples=500, seed=2
        )
        raw_p = RNG.uniform(0.0, 1.0, size=40)
        expected = np.clip(fit.calibrator.predict(raw_p), 0.0, 1.0)
        np.testing.assert_allclose(apply_calibrator(fit, raw_p), expected)

    def test_apply_matches_direct_platt(self) -> None:
        p_cal, y_cal = _make_calibration_data(n=600)
        fit = fit_calibrator(
            p_cal, y_cal, method="platt", min_samples=500, seed=2, num_threads=1
        )
        raw_p = RNG.uniform(0.0, 1.0, size=40)
        assert isinstance(fit.calibrator, LogisticRegression)
        expected = fit.calibrator.predict_proba(raw_p.reshape(-1, 1))[:, 1]
        np.testing.assert_allclose(apply_calibrator(fit, raw_p), expected)

    def test_skipped_chain_is_identity(self) -> None:
        p_cal = RNG.uniform(0.0, 1.0, size=50)
        y_cal = RNG.integers(0, 2, size=50)
        fit = fit_calibrator(
            p_cal, y_cal, method="platt", min_samples=500, seed=0
        )
        raw_p = RNG.uniform(0.0, 1.0, size=20)
        np.testing.assert_array_equal(apply_calibrator(fit, raw_p), raw_p)


class TestNoDbAccessInCalibrate(unittest.TestCase):
    def test_no_forbidden_imports(self) -> None:
        from tests.test_modeling_no_db_access import forbidden_imports_in_module

        path = Path(__file__).resolve().parent.parent / "modeling" / "calibrate.py"
        hits = forbidden_imports_in_module(path)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
