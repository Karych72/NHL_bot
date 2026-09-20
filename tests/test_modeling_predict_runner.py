"""End-to-end tests for modeling.predict_runner (Задача 15).

Builds a tiny real logreg model + calibrator on synthetic data, writes them as a
``final/`` artifact with a ``latest`` symlink (mirroring what
``train_runner.run_training`` produces), and scores a synthetic predict dataset
against it. No PostgreSQL, no real training run.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from modeling.artifacts import save_model_artifact
from modeling.calibrate import fit_calibrator
from modeling.feature_schema import features_hash
from modeling.predict_runner import run_predict
from modeling.train_logreg import build_logreg_pipeline

MANIFEST = [
    {"name": "f_a", "dtype": "float64", "position": "0"},
    {"name": "f_b", "dtype": "float64", "position": "1"},
]
ROLLING_WINDOWS = [5, 10]
FEATURE_SET_VERSION = "v1"


def _features_hash(cold_start_policy_predict: str) -> str:
    return features_hash(
        feature_manifest=MANIFEST,
        rolling_windows=ROLLING_WINDOWS,
        cold_start_policy=f"train:drop|predict:{cold_start_policy_predict}",
        feature_set_version=FEATURE_SET_VERSION,
    )


def _dataset_metadata(*, mode: str, cold_start_policy_predict: str, rows: int) -> dict:
    return {
        "mode": mode,
        "feature_set_version": FEATURE_SET_VERSION,
        "features_hash": _features_hash(cold_start_policy_predict),
        "feature_manifest": MANIFEST,
        "rolling_windows": ROLLING_WINDOWS,
        "cold_start_policy_predict": cold_start_policy_predict,
        "min_prior_games": 5,
        "dataset_rows": rows,
        "code_version": "test",
        "data_snapshot_id": "test-snap",
        "dataset_built_at": "2020-01-01T00:00:00Z",
    }


def _write_predict_dataset(tmp: Path, *, cold_start_policy_predict: str = "drop") -> tuple[Path, Path]:
    rows = pd.DataFrame(
        {
            "game_id": [101, 102, 103],
            "day": ["2026-04-14", "2026-04-15", "2026-04-15"],
            "season_id": [20252026, 20252026, 20252026],
            "home_team_id": [10, 20, 30],
            "away_team_id": [11, 21, 31],
            "f_a": [0.1, 0.5, 0.9],
            "f_b": [0.9, 0.5, 0.1],
            "feature_set_version": [FEATURE_SET_VERSION] * 3,
            "dataset_built_at": ["2026-04-16T00:00:00Z"] * 3,
            "low_history_confidence": [0, 0, 0],
            "quality_warnings": ["", "", ""],
        }
    )
    csv_path = tmp / "dataset_predict.csv"
    meta_path = tmp / "metadata_predict.json"
    rows.to_csv(csv_path, index=False)
    meta_path.write_text(
        json.dumps(
            _dataset_metadata(mode="predict", cold_start_policy_predict=cold_start_policy_predict, rows=len(rows)),
            indent=2,
        ),
        encoding="utf-8",
    )
    return csv_path, meta_path


def _write_latest_model(
    artifacts_root: Path,
    *,
    task: str = "home_win",
    model_family: str = "logreg",
    run_id: str = "home_win_logreg_deadbeef_20260101T000000Z",
    cold_start_policy_predict: str = "drop",
) -> Path:
    """Fit a tiny real pipeline + calibrator and write them as train_runner would."""
    rng = np.random.default_rng(0)
    X_train = pd.DataFrame({"f_a": rng.uniform(size=40), "f_b": rng.uniform(size=40)})
    y_train = (X_train["f_a"] > X_train["f_b"]).astype(int).to_numpy()

    pipeline = build_logreg_pipeline(C=1.0, random_seed=42)
    pipeline.fit(X_train, y_train)

    p_cal = pipeline.predict_proba(X_train)[:, 1]
    calibrator_fit = fit_calibrator(p_cal, y_train, method="platt", min_samples=1, seed=42)

    base = artifacts_root / "models" / task / model_family
    final_dir = base / run_id / "final"
    final_dir.mkdir(parents=True)

    save_model_artifact(
        final_dir,
        model=pipeline,
        metadata={
            "run_id": run_id,
            "task": task,
            "model_family": model_family,
            "status": "ok",
            "features_hash": _features_hash(cold_start_policy_predict),
            "feature_set_version": FEATURE_SET_VERSION,
            "feature_manifest": MANIFEST,
            "method": calibrator_fit.method,
            "calibration_skipped": calibrator_fit.calibration_skipped,
            "n_calibration": calibrator_fit.n_calibration,
            "seed": calibrator_fit.seed,
        },
    )
    joblib.dump(calibrator_fit.calibrator, final_dir / "calibrator.joblib")
    os.symlink(Path(run_id) / "final", base / "latest")
    return final_dir


class TestRunPredictHappyPath(unittest.TestCase):
    def test_scores_predict_dataset_with_latest_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            artifacts_root = tmp / "artifacts"
            final_dir = _write_latest_model(artifacts_root)
            csv_path, meta_path = _write_predict_dataset(tmp)
            output_path = tmp / "predictions" / "home_win_logreg_predictions.csv"

            result = run_predict(
                task="home_win",
                model="logreg",
                predict_dataset=csv_path,
                predict_metadata=meta_path,
                artifacts_root=artifacts_root,
                output_path=output_path,
            )

            self.assertEqual(result.run_id, "home_win_logreg_deadbeef_20260101T000000Z")
            self.assertEqual(result.model_dir, final_dir.resolve())
            self.assertEqual(len(result.predictions), 3)
            self.assertEqual(
                list(result.predictions.columns),
                ["game_id", "day", "season_id", "home_team_id", "away_team_id", "probability"],
            )
            self.assertTrue(result.predictions["probability"].between(0.0, 1.0).all())
            self.assertTrue(output_path.exists())

            on_disk = pd.read_csv(output_path)
            self.assertEqual(list(on_disk["game_id"]), [101, 102, 103])

    def test_raises_when_no_latest_model_trained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_predict_dataset(tmp)
            with self.assertRaises(FileNotFoundError):
                run_predict(
                    task="home_win",
                    model="logreg",
                    predict_dataset=csv_path,
                    predict_metadata=meta_path,
                    artifacts_root=tmp / "artifacts",
                    output_path=tmp / "out.csv",
                )


class TestRunPredictFeaturesHashMismatch(unittest.TestCase):
    def test_raises_when_model_and_predict_dataset_hashes_disagree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            artifacts_root = tmp / "artifacts"
            # Model trained with cold_start_policy_predict="drop" (its own self-consistent hash)...
            _write_latest_model(artifacts_root, cold_start_policy_predict="drop")
            # ...but the predict dataset was built with a different (still self-consistent)
            # policy value, so its features_hash differs from the model's.
            csv_path, meta_path = _write_predict_dataset(tmp, cold_start_policy_predict="allow_with_flag")

            with self.assertRaises(ValueError) as ctx:
                run_predict(
                    task="home_win",
                    model="logreg",
                    predict_dataset=csv_path,
                    predict_metadata=meta_path,
                    artifacts_root=artifacts_root,
                    output_path=tmp / "out.csv",
                )
            self.assertIn("features_hash mismatch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
