"""Smoke tests for modeling CLI train command (stage 10)."""

from __future__ import annotations

import json
import os
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
    dry_run_training,
    format_dry_run_report,
    resolve_run_id,
    resolve_tasks,
    run_training,
    update_latest_symlink,
    validate_run_id_override,
)
from modeling.acceptance import evaluate_baseline_gate, TaskModelEval
from tests._modeling_fixtures import synthetic_season_id, write_synthetic_train_dataset
from tests.test_modeling_no_db_access import forbidden_imports_in_module

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
VALID_RUN_ID = "home_win_logreg_deadbeef_20260101T000000Z"

# UPDATE plan §11 target for ``train --dry-run``; CI assert uses headroom for cold start.
_DRY_RUN_TARGET_SECONDS = 5.0
_DRY_RUN_CI_BUDGET_SECONDS = 15.0


def _write_synthetic_dataset(tmp: Path, *, n_days: int = 2000, games_per_day: int = 1) -> tuple[Path, Path]:
    return write_synthetic_train_dataset(tmp, n_days=n_days, games_per_day=games_per_day)


def _test_config_yaml() -> dict:
    base = yaml.safe_load((ROOT / "configs" / "modeling_default.yaml").read_text(encoding="utf-8"))
    base["split"] = {
        "test_seasons": [synthetic_season_id(2), synthetic_season_id(3)],
        "inner_val_games": 100,
        "calibration_games": 100,
    }
    base["elo"]["grid"] = {
        "k": [8],
        "home_advantage": [35],
        "season_regression": [0.333],
        "mov": [True, False],
        "ot_win_weight": [1.0],
    }
    base["calibration"]["min_samples"] = 100
    base["models"]["lgbm"]["monotone"] = {"home_win": {}, "over_5_5": {}}
    return base


def _cli_train(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = [PY, "-m", "modeling.cli", "train", *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)


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
    def test_baseline_gate_marks_failed_when_not_strictly_better(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {
                "home_win": [
                    TaskModelEval(
                        task="home_win",
                        model="logreg",
                        run_id=VALID_RUN_ID,
                        reports_dir=Path("."),
                        model_log_loss=0.75,
                        benchmark_log_loss=0.70,
                        benchmark="elo",
                    )
                ]
            },
        )
        self.assertEqual(gate.status, "failed_baseline_check")

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
            for block in ("train=", "inner_val=", "calibration=", "test="):
                self.assertIn(block, proc.stdout)
            self.assertNotIn("holdout", proc.stdout)
            for season in _test_config_yaml()["split"]["test_seasons"]:
                self.assertIn(f"season {season}:", proc.stdout)

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
            for block in ("train=", "inner_val=", "calibration=", "test="):
                self.assertIn(block, text)
            self.assertNotIn("holdout", text)
            self.assertEqual(
                [row["season_id"] for row in payload["block_sizes"]["seasons"]],
                _test_config_yaml()["split"]["test_seasons"],
            )
            self.assertEqual(payload["block_sizes"]["final_retrain"]["test"], 0)

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
        hits = forbidden_imports_in_module(ROOT / "modeling" / "train_runner.py")
        self.assertEqual(hits, [], msg=f"forbidden imports in train_runner.py: {hits}")


class TestCliTrainMiniEndToEnd(unittest.TestCase):
    def test_train_creates_season_final_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, games_per_day=1)
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
            for season in _test_config_yaml()["split"]["test_seasons"]:
                self.assertTrue((model_root / f"season_{season}" / "model_raw.joblib").exists())
            self.assertFalse(list(model_root.glob("fold_*")))
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
                "test_seasons",
            ):
                self.assertIn(key, meta)
            for key in ("holdout_days", "n_rows_holdout", "test_days", "n_rows_test"):
                self.assertNotIn(key, meta)
            self.assertEqual(meta["test_seasons"], _test_config_yaml()["split"]["test_seasons"])
            self.assertEqual(meta["n_rows_train"] + meta["n_rows_calibration"], 2000)
            self.assertIn("scikit-learn", meta["library_versions"])
            latest = artifacts / "models" / "home_win" / "logreg" / "latest"
            latest_txt = artifacts / "models" / "home_win" / "logreg" / "latest.txt"
            self.assertTrue(latest.exists() or latest_txt.exists())
            self.assertTrue((artifacts / "reports" / run_id / "metrics.json").exists())
            self.assertTrue((artifacts / "reports" / run_id / "run.log").exists())


    def test_no_promote_keeps_latest_and_default_moves_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, games_per_day=1)
            cfg = _test_config_yaml()
            cfg["tasks"]["over_5_5"]["enabled"] = False
            cfg["models"]["logreg"]["grids"]["C"] = [1.0]
            cfg["evaluation"]["bootstrap_samples"] = 20
            cfg["compute"]["num_threads"] = 1
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            artifacts = tmp / "artifacts"
            resolved = load_config(cfg_path, metadata=load_metadata_json(meta_path))
            base = artifacts / "models" / "home_win" / "logreg"
            base.mkdir(parents=True)
            os.symlink(Path("previous_run") / "final", base / "latest")

            def _train(run_id: str, *, promote: bool) -> str:
                results = run_training(
                    resolved,
                    dataset_csv=csv_path,
                    metadata_path=meta_path,
                    task="home_win",
                    model="logreg",
                    run_id=run_id,
                    artifacts_root=artifacts,
                    promote=promote,
                )
                self.assertEqual(results[0].status, "ok")
                return results[0].run_id

            kept_run = _train(VALID_RUN_ID, promote=False)
            self.assertTrue((base / kept_run / "final" / "metadata.json").exists())
            self.assertEqual(os.readlink(base / "latest"), str(Path("previous_run") / "final"))

            moved_run = _train("home_win_logreg_deadbeef_20260102T000000Z", promote=True)
            self.assertEqual(os.readlink(base / "latest"), str(Path(moved_run) / "final"))


class TestSeasonReportContents(unittest.TestCase):
    """What a real ``run_training`` writes to ``metrics.json`` under the per-season scheme."""

    def _train(self, tmp: Path, *, task: str) -> dict:
        csv_path, meta_path = _write_synthetic_dataset(tmp)
        cfg = _test_config_yaml()
        cfg["models"]["logreg"]["grids"]["C"] = [1.0]
        cfg["evaluation"]["bootstrap_samples"] = 30
        cfg["compute"]["num_threads"] = 1
        cfg_path = tmp / "cfg.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        run_id = f"{task}_logreg_cafebabe_20260101T000000Z"
        run_training(
            load_config(cfg_path, metadata=load_metadata_json(meta_path)),
            dataset_csv=csv_path,
            metadata_path=meta_path,
            task=task,
            model="logreg",
            run_id=run_id,
            artifacts_root=tmp / "artifacts",
        )
        return json.loads((tmp / "artifacts" / "reports" / run_id / "metrics.json").read_text(encoding="utf-8"))

    def test_home_win_report_compares_with_elo_on_the_same_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            metrics = self._train(tmp, task="home_win")
            seasons = metrics["seasons"]

            self.assertEqual([s["season_id"] for s in seasons], _test_config_yaml()["split"]["test_seasons"])
            for season in seasons:
                self.assertEqual(season["n_test"], 500)
                for key in ("model_raw", "model", "constant", "elo_raw", "elo"):
                    self.assertLessEqual({"log_loss", "brier", "ece"}, set(season[key]), msg=key)
                self.assertEqual(season["constant"]["p"], 0.5)
                self.assertEqual(
                    set(season["elo_params"]),
                    {"k", "home_advantage", "season_regression", "mov", "ot_win_weight", "a", "b",
                     "fit_log_loss", "n_fit_games"},
                )
                self.assertEqual(season["elo_full_season"]["n_games"], 500)
            # Elo is fitted on seasons before the checked one without the first (warm-up) season.
            self.assertEqual(seasons[0]["elo_params"]["n_fit_games"], 500)
            self.assertEqual(seasons[1]["elo_params"]["n_fit_games"], 1000)

            pooled = metrics["pooled"]
            self.assertEqual(pooled["n_test"], 1000)
            self.assertEqual(set(pooled["diff_ci"]), {"model_minus_elo", "model_minus_constant"})
            for ci in pooled["diff_ci"].values():
                self.assertLessEqual(ci["ci_low"], ci["point"])
                self.assertLessEqual(ci["point"], ci["ci_high"])
            self.assertAlmostEqual(
                pooled["diff_ci"]["model_minus_elo"]["point"],
                pooled["model"]["log_loss"] - pooled["elo"]["log_loss"],
                places=9,
            )
            self.assertEqual(set(metrics["calibration_table"]), {"model", "elo"})
            for rows in metrics["calibration_table"].values():
                self.assertTrue(rows)
                self.assertTrue(all(row["count"] > 0 for row in rows))
            self.assertEqual(metrics["slices"]["season_start"]["n"], 100)
            self.assertEqual(metrics["slices"]["post_olympic_break"], {"n": 0})
            self.assertIn("elo_log_loss", metrics["slices"]["season_start"])
            self.assertTrue((tmp / "artifacts" / "reports" / metrics["run_id"] / "reliability_home_win.png").exists())
            summary = (tmp / "artifacts" / "reports" / metrics["run_id"] / "summary.md").read_text(encoding="utf-8")
            self.assertIn("pooled test seasons, vs Elo|constant", summary)
            self.assertIn("| sum |", summary)

    def test_over_task_has_constant_benchmark_and_needs_no_games_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp)
            (tmp / "games_train.csv").unlink()
            cfg_path = tmp / "cfg.yaml"
            cfg = _test_config_yaml()
            cfg["models"]["logreg"]["grids"]["C"] = [1.0]
            cfg["evaluation"]["bootstrap_samples"] = 20
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            run_id = "over_5_5_logreg_cafebabe_20260101T000000Z"
            results = run_training(
                load_config(cfg_path, metadata=load_metadata_json(meta_path)),
                dataset_csv=csv_path,
                metadata_path=meta_path,
                task="over_5_5",
                model="logreg",
                run_id=run_id,
                artifacts_root=tmp / "artifacts",
            )
            metrics = json.loads((tmp / "artifacts" / "reports" / run_id / "metrics.json").read_text(encoding="utf-8"))

            self.assertEqual(results[0].status, "ok")
            self.assertEqual(set(metrics["pooled"]["diff_ci"]), {"model_minus_constant"})
            self.assertNotIn("elo", metrics["pooled"])
            self.assertNotIn("elo", metrics["calibration_table"])
            self.assertEqual(metrics["acceptance"]["baseline_gate"]["per_task"][0]["benchmark"], "constant")

    def test_home_win_fails_loudly_without_games_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp)
            (tmp / "games_train.csv").unlink()
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(_test_config_yaml()), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                run_training(
                    load_config(cfg_path, metadata=load_metadata_json(meta_path)),
                    dataset_csv=csv_path,
                    metadata_path=meta_path,
                    task="home_win",
                    model="logreg",
                    artifacts_root=tmp / "artifacts",
                )
            self.assertIn("games_train.csv", str(ctx.exception))
            self.assertFalse((tmp / "artifacts").exists())

    def test_gate_compares_with_elo_not_with_constant(self) -> None:
        # Predictable labels: the model beats Elo, so the home_win gate passes against Elo.
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            metrics = self._train(tmp, task="home_win")
            verdict = metrics["acceptance"]["baseline_gate"]["per_task"][0]
            self.assertEqual(verdict["benchmark"], "elo")
            self.assertAlmostEqual(verdict["benchmark_log_loss"], metrics["pooled"]["elo"]["log_loss"])
            self.assertAlmostEqual(verdict["model_log_loss"], metrics["pooled"]["model"]["log_loss"])
            self.assertTrue(verdict["passed"])


class TestSliceMasks(unittest.TestCase):
    """Report slices on hand-built predictions (no training)."""

    def test_slices_cover_season_start_olympic_window_and_prior_games_buckets(self) -> None:
        import numpy as np
        import pandas as pd

        from modeling.splits import SeasonWindow
        from modeling.train_runner import _SeasonEval, _slice_blocks, _slice_masks

        days = pd.to_datetime(
            ["2025-10-10"] * 10 + ["2026-02-26", "2026-03-11", "2026-03-12", "2026-02-20", "2026-04-01"]
        )
        keys = pd.DataFrame({"day": days, "game_id": np.arange(len(days))})
        prior_home = np.array([5, 7, 8, 10, 11, 20, 21, 30, 5, 6, 8, 8, 8, 8, 8])
        X = pd.DataFrame(
            {"home_prior_games_count": prior_home, "away_prior_games_count": np.full(len(days), 40)}
        )
        X.loc[2, "away_prior_games_count"] = 4  # min(8, 4) = 4 -> the "<=7" bucket
        day_range = None  # unused by the masks
        window = SeasonWindow(
            season_id=20252026, train_idx=np.array([]), inner_val_idx=np.array([]),
            calibration_idx=np.array([]), test_idx=np.arange(len(days)),
            train_days=day_range, inner_val_days=day_range, calibration_days=day_range, test_days=day_range,  # type: ignore[arg-type]
            train_size=0, inner_val_size=0, calibration_size=0, test_size=len(days),
        )
        y = np.arange(len(days)) % 2
        ev = _SeasonEval(window, y, np.full(len(days), 0.5), np.full(len(days), 0.5), np.full(len(days), 0.5), None, None, None)

        masks = _slice_masks([ev], keys, X)

        self.assertEqual(int(masks["season_start"].sum()), 2)  # ceil(10% of 15)
        self.assertEqual(masks["post_olympic_break"].nonzero()[0].tolist(), [10, 11])
        self.assertEqual(masks["prior_games_<=7"].nonzero()[0].tolist(), [0, 1, 2, 8, 9])
        self.assertEqual(masks["prior_games_8-10"].nonzero()[0].tolist(), [3, 10, 11, 12, 13, 14])
        self.assertEqual(masks["prior_games_11-20"].nonzero()[0].tolist(), [4, 5])
        self.assertEqual(masks["prior_games_21+"].nonzero()[0].tolist(), [6, 7])

        blocks = _slice_blocks(masks, y, ev.p_cal, ev.p_const, None, epsilon=1e-15)
        self.assertEqual(blocks["season_start"]["n"], 2)
        self.assertAlmostEqual(blocks["season_start"]["model_log_loss"], float(-np.log(0.5)))
        self.assertNotIn("elo_log_loss", blocks["season_start"])

    def test_missing_prior_games_columns_fail_loudly(self):
        import numpy as np
        import pandas as pd

        from modeling.splits import SeasonWindow
        from modeling.train_runner import _SeasonEval, _slice_masks

        keys = pd.DataFrame({"day": pd.to_datetime(["2025-10-10"] * 4), "game_id": np.arange(4)})
        window = SeasonWindow(
            season_id=20252026, train_idx=np.array([]), inner_val_idx=np.array([]),
            calibration_idx=np.array([]), test_idx=np.arange(4),
            train_days=None, inner_val_days=None, calibration_days=None, test_days=None,  # type: ignore[arg-type]
            train_size=0, inner_val_size=0, calibration_size=0, test_size=4,
        )
        ev = _SeasonEval(window, np.zeros(4), np.full(4, 0.5), np.full(4, 0.5), np.full(4, 0.5), None, None, None)

        with self.assertRaises(ConfigError) as ctx:
            _slice_masks([ev], keys, pd.DataFrame({"f_a": np.zeros(4)}))
        self.assertIn("prior_games", str(ctx.exception))


class TestPromoteCommand(unittest.TestCase):
    """``modeling.cli promote`` against a real artifacts tree in a temp cwd."""

    def _write_run(self, tmp: Path, run_id: str, *, status: str | None) -> None:
        final_dir = tmp / "artifacts" / "models" / "home_win" / "logreg" / run_id / "final"
        final_dir.mkdir(parents=True)
        if status is not None:
            (final_dir / "metadata.json").write_text(json.dumps({"status": status}), encoding="utf-8")

    def _promote(self, tmp: Path, run_id: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        cmd = [PY, "-m", "modeling.cli", "promote", "--task", "home_win", "--model", "logreg", "--run-id", run_id]
        return subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, check=False)

    def test_promote_ok_run_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            self._write_run(tmp, "run_old", status="ok")
            self._write_run(tmp, "run_new", status="ok")
            latest = tmp / "artifacts" / "models" / "home_win" / "logreg" / "latest"

            proc = self._promote(tmp, "run_new")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("latest: home_win/logreg -> run_new/final", proc.stdout)
            self.assertEqual(os.readlink(latest), str(Path("run_new") / "final"))

            proc = self._promote(tmp, "run_old")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(os.readlink(latest), str(Path("run_old") / "final"))

    def test_promote_refuses_non_ok_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            self._write_run(tmp, "run_good", status="ok")
            self._write_run(tmp, "run_bad", status="failed_baseline_check")
            self.assertEqual(self._promote(tmp, "run_good").returncode, 0)
            latest = tmp / "artifacts" / "models" / "home_win" / "logreg" / "latest"

            proc = self._promote(tmp, "run_bad")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("failed_baseline_check", proc.stderr)
            self.assertEqual(os.readlink(latest), str(Path("run_good") / "final"))

    def test_promote_missing_metadata_fails_and_keeps_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            self._write_run(tmp, "run_good", status="ok")
            self._write_run(tmp, "run_empty", status=None)
            self.assertEqual(self._promote(tmp, "run_good").returncode, 0)
            latest = tmp / "artifacts" / "models" / "home_win" / "logreg" / "latest"

            for run_id in ("run_empty", "run_absent"):
                proc = self._promote(tmp, run_id)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("metadata.json", proc.stderr)
                self.assertEqual(os.readlink(latest), str(Path("run_good") / "final"))


class TestCalibrationSkipped(unittest.TestCase):
    def test_high_min_samples_skips_calibration_in_season_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, games_per_day=1)
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
            season_meta = json.loads(
                (
                    artifacts
                    / "models"
                    / "home_win"
                    / "logreg"
                    / results[0].run_id
                    / f"season_{synthetic_season_id(2)}"
                    / "metadata.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(season_meta["calibration_skipped"])


class TestCliTrainLgbmEndToEnd(unittest.TestCase):
    def test_lgbm_train_creates_final_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_synthetic_dataset(tmp, games_per_day=1)
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
