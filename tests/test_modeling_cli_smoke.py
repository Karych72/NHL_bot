"""Smoke tests for modeling CLI train command (stage 10)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from modeling.config import ConfigError, build_run_id, load_config, load_metadata_json
from modeling.train_runner import (
    RunResult,
    dry_run_training,
    format_dry_run_report,
    resolve_run_id,
    resolve_tasks,
    run_training,
    update_latest_symlink,
    validate_run_id_override,
)
from modeling.train_runner import _apply_task_baseline_gate, _TaskModelOutcome
from tests._modeling_fixtures import write_synthetic_train_dataset

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
VALID_RUN_ID = "home_win_logreg_deadbeef_20260101T000000Z"

# UPDATE plan §11 target for ``train --dry-run``; CI assert uses headroom for cold start.
_DRY_RUN_TARGET_SECONDS = 5.0
_DRY_RUN_CI_BUDGET_SECONDS = 15.0


def _write_synthetic_dataset(tmp: Path, *, n_days: int = 6000, games_per_day: int = 1) -> tuple[Path, Path]:
    return write_synthetic_train_dataset(tmp, n_days=n_days, games_per_day=games_per_day)


def _test_config_yaml() -> dict:
    base = yaml.safe_load((ROOT / "configs" / "modeling_default.yaml").read_text(encoding="utf-8"))
    base["split"] = {
        "method": "fixed_games",
        "n_test_windows": 5,
        "inner_val_games": 300,
        "calibration_games": 300,
        "outer_block_games": 601,
        "holdout": {"fraction": 0.15, "date_range": {"from": None, "to": None}},
    }
    base["models"]["lgbm"]["monotone"] = {"home_win": {}, "over_5_5": {}}
    return base


def _cli_train(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = [PY, "-m", "modeling.cli", "train", *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)


def _forbidden_imports_in_module(path: Path) -> list[str]:
    roots = frozenset({"psycopg2", "modeling.dataset_builder"})
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []

    def is_forbidden(module: str) -> bool:
        return any(module == root or module.startswith(f"{root}.") for root in roots)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_forbidden(alias.name):
                    hits.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and is_forbidden(node.module):
            hits.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return hits


class TestRunIdResolution(unittest.TestCase):
    def test_build_run_id_format(self) -> None:
        ts = datetime(2026, 5, 30, 14, 30, 22, tzinfo=timezone.utc)
        run_id = build_run_id("home_win", "logreg", "b334df68" + "0" * 56, ts)
        self.assertEqual(run_id, "home_win_logreg_b334df68_20260530T143022Z")

    def test_override_run_id(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(
            resolve_run_id(
                task="home_win",
                model="logreg",
                features_hash="abc",
                run_start_utc=ts,
                override=VALID_RUN_ID,
            ),
            VALID_RUN_ID,
        )


class TestTaskFlagConflict(unittest.TestCase):
    def test_disabled_task_via_flag_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_synthetic_dataset(tmp, n_days=100)
            cfg_path = tmp / "cfg.yaml"
            cfg = _test_config_yaml()
            cfg["tasks"]["home_win"]["enabled"] = False
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            resolved = load_config(cfg_path, metadata=load_metadata_json(tmp / "metadata_train.json"))
            with self.assertRaises(ConfigError) as ctx:
                resolve_tasks("home_win", resolved)
            self.assertIn("home_win", str(ctx.exception))
            self.assertIn("disabled", str(ctx.exception))

    def test_cli_disabled_task_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_synthetic_dataset(tmp, n_days=100)
            cfg_path = tmp / "cfg.yaml"
            cfg = _test_config_yaml()
            cfg["tasks"]["home_win"]["enabled"] = False
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            proc = _cli_train(
                [
                    "--dry-run",
                    "--task",
                    "home_win",
                    "--config",
                    str(cfg_path),
                    "--metadata",
                    str(tmp / "metadata_train.json"),
                    "--dataset",
                    str(tmp / "dataset_train.csv"),
                ]
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("disabled", proc.stderr)


class TestRunIdOverrideValidation(unittest.TestCase):
    def test_run_id_rejected_for_multiple_pairs(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            validate_run_id_override(
                ["home_win", "over_5_5"],
                ["logreg", "lgbm"],
                "fixed_run_id",
            )
        self.assertIn("--run-id", str(ctx.exception))
        self.assertIn("4", str(ctx.exception))

    def test_run_id_allowed_for_single_pair(self) -> None:
        validate_run_id_override(["home_win"], ["logreg"], VALID_RUN_ID)


class TestBaselineGate(unittest.TestCase):
    def test_apply_task_baseline_gate_marks_failed(self) -> None:
        result = RunResult(
            run_id=VALID_RUN_ID,
            task="home_win",
            model="logreg",
            status="ok",
            exit_code=0,
            reports_dir=Path("."),
            model_run_dir=Path("."),
            holdout_calibrated_log_loss=0.75,
        )
        outcome = _TaskModelOutcome(
            task="home_win",
            model="logreg",
            result=result,
            holdout_calibrated_log_loss=0.75,
            holdout_trivial_log_loss=0.70,
        )
        _apply_task_baseline_gate([outcome])
        self.assertEqual(outcome.result.status, "failed_baseline_check")
        self.assertEqual(outcome.result.exit_code, 1)

    def test_latest_symlink_skipped_on_failed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            update_latest_symlink(
                "home_win",
                "logreg",
                VALID_RUN_ID,
                artifacts_root=tmp,
                status="failed_baseline_check",
            )
            base = tmp / "models" / "home_win" / "logreg"
            self.assertFalse((base / "latest").exists())
            self.assertFalse((base / "latest.txt").exists())


class TestCliTrainSmoke(unittest.TestCase):
    def test_dry_run_within_five_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_synthetic_dataset(tmp)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(_test_config_yaml()), encoding="utf-8")
            meta_path = tmp / "metadata_train.json"

            t0 = time.monotonic()
            proc = _cli_train(
                [
                    "--dry-run",
                    "--config",
                    str(cfg_path),
                    "--metadata",
                    str(meta_path),
                    "--dataset",
                    str(tmp / "dataset_train.csv"),
                ]
            )
            elapsed = time.monotonic() - t0

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertLessEqual(
                elapsed,
                _DRY_RUN_CI_BUDGET_SECONDS,
                msg=(
                    f"dry-run took {elapsed:.2f}s "
                    f"(target ≤ {_DRY_RUN_TARGET_SECONDS}s per UPDATE plan §11)"
                ),
            )
            for block in ("train=", "inner_val=", "calibration=", "test=", "holdout="):
                self.assertIn(block, proc.stdout)

    def test_dry_run_does_not_create_model_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_synthetic_dataset(tmp)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(_test_config_yaml()), encoding="utf-8")
            meta_path = tmp / "metadata_train.json"
            artifacts = tmp / "artifacts"

            proc = _cli_train(
                [
                    "--dry-run",
                    "--config",
                    str(cfg_path),
                    "--metadata",
                    str(meta_path),
                    "--dataset",
                    str(tmp / "dataset_train.csv"),
                ]
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertFalse(artifacts.exists())
            for pattern in ("**/model.joblib", "**/model_raw.joblib", "**/calibrator.joblib"):
                self.assertEqual(list(tmp.glob(pattern)), [])

    def test_dry_run_block_sizes_via_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(_test_config_yaml()), encoding="utf-8")
            resolved = load_config(cfg_path, metadata=load_metadata_json(meta_path))
            payload = dry_run_training(
                resolved,
                dataset_csv=csv_path,
                metadata_path=meta_path,
            )
            text = format_dry_run_report(payload)
            for block in ("train=", "inner_val=", "calibration=", "test=", "holdout="):
                self.assertIn(block, text)

    def test_print_resolved_config_exits_zero_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_synthetic_dataset(tmp, n_days=10)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(_test_config_yaml()), encoding="utf-8")
            meta_path = tmp / "metadata_train.json"
            proc = _cli_train(
                [
                    "--print-resolved-config",
                    "--config",
                    str(cfg_path),
                    "--metadata",
                    str(meta_path),
                ]
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("random_seed:", proc.stdout)
            self.assertIn("num_threads:", proc.stdout)
            self.assertIn("split:", proc.stdout)
            self.assertFalse((tmp / "artifacts").exists())

    def test_importing_cli_does_not_load_dataset_builder(self) -> None:
        code = (
            "import importlib\n"
            "importlib.import_module('modeling.cli')\n"
            "import sys\n"
            "assert 'modeling.dataset_builder' not in sys.modules\n"
            "assert 'psycopg2' not in sys.modules\n"
        )
        proc = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    def test_cli_train_handler_ast_has_no_top_level_db_imports(self) -> None:
        """build-dataset uses lazy import inside its handler; train path must not."""
        hits = _forbidden_imports_in_module(ROOT / "modeling" / "train_runner.py")
        self.assertEqual(hits, [], msg=f"forbidden imports in train_runner.py: {hits}")


class TestCliTrainMiniEndToEnd(unittest.TestCase):
    def test_train_creates_fold_final_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, n_days=6500, games_per_day=1)
            cfg = _test_config_yaml()
            cfg["tasks"]["over_5_5"]["enabled"] = False
            cfg["models"]["logreg"]["grids"]["C"] = [0.1, 1.0]
            cfg["evaluation"]["bootstrap_samples"] = 50
            cfg["compute"]["num_threads"] = 1
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            artifacts = tmp / "artifacts"
            resolved = load_config(cfg_path, metadata=load_metadata_json(meta_path))
            results = run_training(
                resolved,
                dataset_csv=csv_path,
                metadata_path=meta_path,
                task="home_win",
                model="logreg",
                run_id=VALID_RUN_ID,
                artifacts_root=artifacts,
            )
            self.assertEqual(len(results), 1)
            run_id = results[0].run_id
            model_root = artifacts / "models" / "home_win" / "logreg" / run_id
            self.assertTrue((model_root / "fold_1" / "model_raw.joblib").exists())
            final_dir = model_root / "final"
            self.assertTrue((final_dir / "model.joblib").exists())
            self.assertTrue((final_dir / "calibrator.joblib").exists())
            meta = json.loads((final_dir / "metadata.json").read_text(encoding="utf-8"))
            for key in (
                "features_hash",
                "random_seed",
                "run_id",
                "status",
                "bootstrap",
                "library_versions",
                "n_rows_test",
                "test_days",
            ):
                self.assertIn(key, meta)
            self.assertIsNone(meta["test_days"])
            self.assertEqual(meta["n_rows_test"], 0)
            self.assertIn("scikit-learn", meta["library_versions"])
            latest = artifacts / "models" / "home_win" / "logreg" / "latest"
            latest_txt = artifacts / "models" / "home_win" / "logreg" / "latest.txt"
            self.assertTrue(latest.exists() or latest_txt.exists())
            self.assertTrue((artifacts / "reports" / run_id / "metrics.json").exists())
            self.assertTrue((artifacts / "reports" / run_id / "run.log").exists())


class TestCalibrationSkipped(unittest.TestCase):
    def test_high_min_samples_skips_calibration_in_fold_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, n_days=6500, games_per_day=1)
            cfg = _test_config_yaml()
            cfg["tasks"]["over_5_5"]["enabled"] = False
            cfg["models"]["logreg"]["grids"]["C"] = [1.0]
            cfg["calibration"]["min_samples"] = 999_999
            cfg["evaluation"]["bootstrap_samples"] = 20
            cfg["compute"]["num_threads"] = 1
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            artifacts = tmp / "artifacts"
            resolved = load_config(cfg_path, metadata=load_metadata_json(meta_path))
            results = run_training(
                resolved,
                dataset_csv=csv_path,
                metadata_path=meta_path,
                task="home_win",
                model="logreg",
                run_id="home_win_logreg_cafebabe_20260101T000000Z",
                artifacts_root=artifacts,
            )
            self.assertEqual(results[0].status, "ok")
            fold_meta = json.loads(
                (
                    artifacts
                    / "models"
                    / "home_win"
                    / "logreg"
                    / results[0].run_id
                    / "fold_1"
                    / "metadata.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(fold_meta["calibration_skipped"])


class TestCliTrainLgbmEndToEnd(unittest.TestCase):
    def test_lgbm_train_creates_final_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, n_days=6500, games_per_day=1)
            cfg = _test_config_yaml()
            cfg["tasks"]["over_5_5"]["enabled"] = False
            cfg["models"]["logreg"]["grids"]["C"] = [1.0]
            lgbm_grid = cfg["models"]["lgbm"]["grids"]
            for key in lgbm_grid:
                lgbm_grid[key] = [lgbm_grid[key][0]]
            cfg["evaluation"]["bootstrap_samples"] = 20
            cfg["compute"]["num_threads"] = 1
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            artifacts = tmp / "artifacts"
            resolved = load_config(cfg_path, metadata=load_metadata_json(meta_path))
            results = run_training(
                resolved,
                dataset_csv=csv_path,
                metadata_path=meta_path,
                task="home_win",
                model="lgbm",
                run_id="home_win_lgbm_cafebabe_20260101T000000Z",
                artifacts_root=artifacts,
            )
            self.assertEqual(len(results), 1)
            final_dir = (
                artifacts / "models" / "home_win" / "lgbm" / results[0].run_id / "final"
            )
            self.assertTrue((final_dir / "model.joblib").exists())
            meta = json.loads((final_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["model_family"], "lgbm")


if __name__ == "__main__":
    unittest.main()
