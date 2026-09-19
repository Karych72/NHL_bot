"""Tests for the inference-side loading helpers added to modeling.artifacts (Задача 15)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import joblib

from modeling.artifacts import (
    check_features_hash_match,
    load_latest_calibrator,
    resolve_latest_model_dir,
)


class TestResolveLatestModelDir(unittest.TestCase):
    def test_resolves_real_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            base = tmp / "models" / "home_win" / "logreg"
            final_dir = base / "home_win_logreg_deadbeef_20260101T000000Z" / "final"
            final_dir.mkdir(parents=True)
            os.symlink(Path("home_win_logreg_deadbeef_20260101T000000Z") / "final", base / "latest")

            resolved = resolve_latest_model_dir(tmp, "home_win", "logreg")

            self.assertEqual(resolved, final_dir.resolve())

    def test_resolves_latest_txt_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            base = tmp / "models" / "over_5_5" / "lgbm"
            final_dir = base / "over_5_5_lgbm_cafebabe_20260101T000000Z" / "final"
            final_dir.mkdir(parents=True)
            (base / "latest.txt").write_text(
                "over_5_5_lgbm_cafebabe_20260101T000000Z/final\n", encoding="utf-8"
            )

            resolved = resolve_latest_model_dir(tmp, "over_5_5", "lgbm")

            self.assertEqual(resolved, final_dir.resolve())

    def test_raises_when_no_latest_pointer_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            with self.assertRaises(FileNotFoundError) as ctx:
                resolve_latest_model_dir(tmp, "home_win", "logreg")
            self.assertIn("home_win/logreg", str(ctx.exception))


class TestLoadLatestCalibrator(unittest.TestCase):
    def test_loads_dumped_estimator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            joblib.dump({"kind": "identity"}, tmp / "calibrator.joblib")

            loaded = load_latest_calibrator(tmp)

            self.assertEqual(loaded, {"kind": "identity"})

    def test_raises_file_not_found_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            with self.assertRaises(FileNotFoundError):
                load_latest_calibrator(tmp)


class TestCheckFeaturesHashMatch(unittest.TestCase):
    def test_matching_hashes_do_not_raise(self) -> None:
        check_features_hash_match({"features_hash": "abc123"}, {"features_hash": "abc123"})

    def test_mismatched_hashes_raise_with_both_values(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            check_features_hash_match(
                {"features_hash": "model_hash_aaa"},
                {"features_hash": "predict_hash_bbb"},
            )
        message = str(ctx.exception)
        self.assertIn("features_hash mismatch", message)
        self.assertIn("model_hash_aaa", message)
        self.assertIn("predict_hash_bbb", message)


if __name__ == "__main__":
    unittest.main()
