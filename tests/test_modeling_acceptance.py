"""Tests for modeling acceptance layer (UPDATE plan stage 12)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from modeling.acceptance import (
    ArtifactCheckResult,
    BASELINE_STRICT_EPS,
    BaselineGateResult,
    TaskModelHoldout,
    apply_acceptance_to_training_outcomes,
    apply_status_to_summary,
    evaluate_baseline_gate,
    holdout_metrics_from_json,
    pick_winning_family,
    verify_latest_symlinks,
    verify_run_artifacts,
)
from modeling.train_runner import update_latest_symlink
from modeling.config import load_config, load_metadata_json
from modeling.train_runner import RunResult, _TaskModelOutcome, run_training
from tests._modeling_fixtures import write_synthetic_train_dataset
from tests.test_modeling_metrics import _metric_block, _sample_metrics_json
from tests.test_modeling_no_db_access import forbidden_imports_in_module

ROOT = Path(__file__).resolve().parent.parent
VALID_RUN_ID = "home_win_logreg_deadbeef_20260101T000000Z"


def _holdout_entry(*, model_ll: float, trivial_ll: float, model: str = "logreg") -> TaskModelHoldout:
    return TaskModelHoldout(
        task="home_win",
        model=model,
        run_id=VALID_RUN_ID,
        reports_dir=Path("."),
        model_log_loss=model_ll,
        trivial_log_loss=trivial_ll,
    )


def _bootstrap_block() -> dict:
    template = {
        "point": 0.5,
        "ci_low": 0.4,
        "ci_high": 0.6,
        "bootstrap.N": 100,
        "bootstrap.block_by_day": True,
        "bootstrap.seed": 42,
    }
    return {"log_loss": dict(template), "brier": dict(template)}


def _metrics_payload(*, task: str = "home_win", model: str = "logreg") -> dict:
    holdout = _metric_block(n_test=50)
    holdout["calibrated"] = {"log_loss": 0.55, "brier": 0.20, "ece": 0.03}
    holdout["reliability_path"] = f"reliability_{task}.png"
    holdout["bootstrap"] = _bootstrap_block()
    fold = _metric_block(k=1, n_test=40)
    fold["calibrated"] = {"log_loss": 0.56, "brier": 0.21, "ece": 0.04}
    return {
        "run_id": f"{task}_{model}_deadbeef_20260101T000000Z",
        "task": task,
        "model": model,
        "features_hash": "b334df68cab14a12056b7a41b324face3cc9cd835c30b738caffdef1b72f81a1",
        "evaluation": {"epsilon_clip": 1e-15, "ece_bins": 10},
        "folds": [fold],
        "holdout": holdout,
        "team_breakdown": {"home_team_id": [], "away_team_id": []},
    }


def _metadata_dict(*, run_id: str, features_hash: str) -> dict:
    return {
        "features_hash": features_hash,
        "random_seed": 42,
        "run_id": run_id,
        "git_commit": None,
        "library_versions": {
            "scikit-learn": "1.0",
            "lightgbm": "4.0",
            "pandas": "2.0",
            "numpy": "1.0",
        },
        "train_days": {"min": "2018-01-01", "max": "2019-01-01"},
        "inner_val_days": {"min": "2019-01-02", "max": "2019-02-01"},
        "calibration_days": {"min": "2019-02-02", "max": "2019-03-01"},
        "test_days": None,
        "holdout_days": {"min": "2019-03-02", "max": "2019-04-01"},
        "n_rows_train": 100,
        "n_rows_inner_val": 50,
        "n_rows_calibration": 40,
        "n_rows_test": 0,
        "n_rows_holdout": 30,
    }


def _write_valid_run_layout(
    tmp: Path,
    *,
    task: str = "home_win",
    model: str = "logreg",
    features_hash: str,
    symlink_ok: bool = True,
    metadata: dict | None = None,
    metrics: dict | None = None,
    skip_reliability: bool = False,
) -> TaskModelHoldout:
    run_id = f"{task}_{model}_deadbeef_20260101T000000Z"
    reports = tmp / "artifacts" / "reports" / run_id
    reports.mkdir(parents=True, exist_ok=True)
    metrics_payload = metrics if metrics is not None else _metrics_payload(task=task, model=model)
    (reports / "metrics.json").write_text(
        json.dumps(metrics_payload, indent=2),
        encoding="utf-8",
    )
    (reports / "summary.md").write_text("status: ok\n\n# placeholder\n", encoding="utf-8")
    if not skip_reliability:
        (reports / f"reliability_{task}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    model_root = tmp / "artifacts" / "models" / task / model / run_id
    final_dir = model_root / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "model.joblib").write_bytes(b"model")
    (final_dir / "calibrator.joblib").write_bytes(b"cal")
    meta = metadata if metadata is not None else _metadata_dict(run_id=run_id, features_hash=features_hash)
    (final_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    fold_dir = model_root / "fold_1"
    fold_dir.mkdir(parents=True, exist_ok=True)
    (fold_dir / "metadata.json").write_text("{}", encoding="utf-8")
    if symlink_ok:
        base = tmp / "artifacts" / "models" / task / model
        base.mkdir(parents=True, exist_ok=True)
        link = base / "latest"
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(Path(run_id) / "final", link)
    holdout = metrics_payload["holdout"]
    calibrated = holdout.get("calibrated") or {}
    trivial = holdout.get("trivial_base_rate") or {}
    return TaskModelHoldout(
        task=task,
        model=model,
        run_id=run_id,
        reports_dir=reports,
        model_log_loss=float(calibrated.get("log_loss", 0.55)),
        trivial_log_loss=float(trivial.get("log_loss", 0.69)),
    )


class TestBaselineGate(unittest.TestCase):
    def test_gate_passes_when_strictly_better(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {"home_win": [_holdout_entry(model_ll=0.5, trivial_ll=0.7)]},
        )
        self.assertEqual(gate.status, "ok")
        self.assertTrue(gate.per_task[0].passed)

    def test_gate_fails_when_not_strictly_better(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {"home_win": [_holdout_entry(model_ll=0.75, trivial_ll=0.70)]},
        )
        self.assertEqual(gate.status, "failed_baseline_check")

    def test_gate_fails_on_equality_within_eps(self) -> None:
        ll = 0.693147
        gate = evaluate_baseline_gate(
            ["home_win"],
            {"home_win": [_holdout_entry(model_ll=ll, trivial_ll=ll)]},
        )
        self.assertEqual(gate.status, "failed_baseline_check")
        self.assertGreaterEqual(BASELINE_STRICT_EPS, 0)

    def test_pick_winning_family_prefers_lgbm_on_tie(self) -> None:
        ll = 0.55
        winner = pick_winning_family(
            [
                _holdout_entry(model_ll=ll, trivial_ll=0.9, model="logreg"),
                _holdout_entry(model_ll=ll, trivial_ll=0.9, model="lgbm"),
            ]
        )
        self.assertEqual(winner.model, "lgbm")
        gate = evaluate_baseline_gate(
            ["home_win"],
            {
                "home_win": [
                    _holdout_entry(model_ll=ll, trivial_ll=0.9, model="logreg"),
                    _holdout_entry(model_ll=ll, trivial_ll=0.9, model="lgbm"),
                ]
            },
        )
        self.assertEqual(gate.per_task[0].winning_family, "lgbm")

    def test_disabled_task_not_in_gate(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {
                "over_5_5": [_holdout_entry(model_ll=9.0, trivial_ll=0.1)],
                "home_win": [_holdout_entry(model_ll=0.5, trivial_ll=0.7)],
            },
        )
        self.assertEqual(gate.status, "ok")

    def test_holdout_metrics_from_json(self) -> None:
        payload = _metrics_payload()
        entry = holdout_metrics_from_json(payload, reports_dir=Path("/tmp/r"))
        self.assertAlmostEqual(entry.model_log_loss, 0.55)
        self.assertAlmostEqual(entry.trivial_log_loss, 0.69)


class TestVerifyRunArtifacts(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.meta_path = self.tmp / "metadata_train.json"
        _, meta_path = write_synthetic_train_dataset(self.tmp, n_days=50)
        self.meta_path = meta_path
        self.config = load_config(
            ROOT / "configs" / "modeling_default.yaml",
            metadata=load_metadata_json(meta_path),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_verify_passes_on_valid_layout(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertTrue(result.ok, msg=result.issues)

    def test_verify_fails_missing_final(self) -> None:
        run = _write_valid_run_layout(self.tmp, features_hash=self.config.features_hash)
        final = self.tmp / "artifacts" / "models" / "home_win" / "logreg" / run.run_id / "final"
        for child in final.iterdir():
            child.unlink()
        final.rmdir()
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("final" in issue for issue in result.issues))

    def test_verify_accepts_git_commit_null(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metadata=_metadata_dict(
                run_id="home_win_logreg_deadbeef_20260101T000000Z",
                features_hash=self.config.features_hash,
            ),
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertTrue(result.ok, msg=result.issues)

    def test_verify_fails_missing_random_seed(self) -> None:
        run_id = "home_win_logreg_deadbeef_20260101T000000Z"
        meta = {
            k: v
            for k, v in _metadata_dict(run_id=run_id, features_hash=self.config.features_hash).items()
            if k != "random_seed"
        }
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metadata=meta,
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("random_seed" in i for i in result.issues))

    def test_verify_fails_missing_reliability_png(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            skip_reliability=True,
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)

    def test_verify_fails_missing_bootstrap(self) -> None:
        holdout = _metric_block(n_test=10)
        holdout["calibrated"] = {"log_loss": 0.55, "brier": 0.20, "ece": 0.03}
        holdout["reliability_path"] = "reliability_home_win.png"
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metrics={**_metrics_payload(), "holdout": holdout},
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)

    def test_verify_fails_missing_trivial_base_rate(self) -> None:
        holdout = {k: v for k, v in _metric_block(n_test=10).items() if k != "trivial_base_rate"}
        holdout["calibrated"] = {"log_loss": 0.55, "brier": 0.20, "ece": 0.03}
        holdout["reliability_path"] = "reliability_home_win.png"
        holdout["bootstrap"] = _bootstrap_block()
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metrics={**_metrics_payload(), "holdout": holdout},
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)

    def test_verify_fails_features_hash_mismatch(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metadata=_metadata_dict(
                run_id="home_win_logreg_deadbeef_20260101T000000Z",
                features_hash="0" * 64,
            ),
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("features_hash" in issue for issue in result.issues))

    def test_verify_fails_missing_library_version_key(self) -> None:
        run_id = "home_win_logreg_deadbeef_20260101T000000Z"
        meta = _metadata_dict(run_id=run_id, features_hash=self.config.features_hash)
        meta["library_versions"] = {"numpy": "1.0", "pandas": "2.0", "lightgbm": "4.0"}
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metadata=meta,
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("scikit-learn" in issue for issue in result.issues))

    def test_verify_fails_missing_summary_md(self) -> None:
        run = _write_valid_run_layout(self.tmp, features_hash=self.config.features_hash)
        (run.reports_dir / "summary.md").unlink()
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("summary.md" in issue for issue in result.issues))

    def test_verify_fails_missing_team_breakdown(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            metrics={**_metrics_payload(), "team_breakdown": {}},
        )
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)

    def test_patch_summary_creates_file_when_missing(self) -> None:
        from modeling.acceptance import _patch_summary_report

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            path = tmp / "summary.md"
            baseline = BaselineGateResult(status="ok", per_task=())
            artifacts = ArtifactCheckResult(ok=True)
            _patch_summary_report(
                path,
                status="ok",
                baseline=baseline,
                artifacts=artifacts,
                run_id="home_win_logreg_deadbeef_20260101T000000Z",
            )
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("status: ok"))


class TestVerifyLatestSymlinks(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _, meta_path = write_synthetic_train_dataset(self.tmp, n_days=50)
        self.config = load_config(
            ROOT / "configs" / "modeling_default.yaml",
            metadata=load_metadata_json(meta_path),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_outcome(self, run: TaskModelHoldout, *, status: str = "ok") -> _TaskModelOutcome:
        result = RunResult(
            run_id=run.run_id,
            task="home_win",
            model="logreg",
            status=status,
            exit_code=0,
            reports_dir=run.reports_dir,
            model_run_dir=self.tmp / "artifacts" / "models" / "home_win" / "logreg" / run.run_id,
        )
        return _TaskModelOutcome(
            task="home_win",
            model="logreg",
            result=result,
            holdout_calibrated_log_loss=run.model_log_loss,
            holdout_trivial_log_loss=run.trivial_log_loss,
        )

    def test_verify_latest_passes_on_valid_symlink(self) -> None:
        run = _write_valid_run_layout(self.tmp, features_hash=self.config.features_hash)
        latest = verify_latest_symlinks(
            [self._make_outcome(run)],
            artifacts_root=self.tmp / "artifacts",
        )
        self.assertTrue(latest.ok, msg=latest.issues)

    def test_verify_latest_fails_wrong_symlink_target(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            symlink_ok=False,
        )
        base = self.tmp / "artifacts" / "models" / "home_win" / "logreg"
        os.symlink("other_run/final", base / "latest")
        latest = verify_latest_symlinks(
            [self._make_outcome(run)],
            artifacts_root=self.tmp / "artifacts",
        )
        self.assertFalse(latest.ok)

    def test_verify_latest_rejects_latest_txt_fallback(self) -> None:
        run = _write_valid_run_layout(
            self.tmp,
            features_hash=self.config.features_hash,
            symlink_ok=False,
        )
        base = self.tmp / "artifacts" / "models" / "home_win" / "logreg"
        (base / "latest.txt").write_text(f"{run.run_id}/final\n", encoding="utf-8")
        latest = verify_latest_symlinks(
            [self._make_outcome(run)],
            artifacts_root=self.tmp / "artifacts",
        )
        self.assertFalse(latest.ok)
        self.assertTrue(any("latest.txt" in issue for issue in latest.issues))


class TestApplyAcceptanceIntegration(unittest.TestCase):
    def test_apply_updates_summary_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, meta_path = write_synthetic_train_dataset(tmp, n_days=20)
            config = load_config(ROOT / "configs" / "modeling_default.yaml", metadata=load_metadata_json(meta_path))
            run = _write_valid_run_layout(tmp, features_hash=config.features_hash)
            result = RunResult(
                run_id=run.run_id,
                task="home_win",
                model="logreg",
                status="ok",
                exit_code=0,
                reports_dir=run.reports_dir,
                model_run_dir=tmp / "artifacts" / "models" / "home_win" / "logreg" / run.run_id,
            )
            outcome = _TaskModelOutcome(
                task="home_win",
                model="logreg",
                result=result,
                holdout_calibrated_log_loss=run.model_log_loss,
                holdout_trivial_log_loss=run.trivial_log_loss,
            )
            (run.reports_dir / "summary.md").write_text("status: ok\n\n# body\n", encoding="utf-8")
            combined, _, _ = apply_acceptance_to_training_outcomes(
                [outcome],
                config=config,
                enabled_tasks=["home_win"],
                models=["logreg"],
                artifacts_root=tmp / "artifacts",
            )
            self.assertIn(combined, ("ok", "failed_baseline_check", "failed_artifact_check"))
            summary = (run.reports_dir / "summary.md").read_text(encoding="utf-8")
            self.assertTrue(summary.startswith("status:"))
            metrics = json.loads((run.reports_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertIn("acceptance", metrics)


class TestAcceptanceNoDbAccess(unittest.TestCase):
    def test_acceptance_module_ast_clean(self) -> None:
        hits = forbidden_imports_in_module(ROOT / "modeling" / "acceptance.py")
        self.assertEqual(hits, [])


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


class TestAcceptanceEndToEndSmoke(unittest.TestCase):
    def test_train_writes_status_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = write_synthetic_train_dataset(tmp, n_days=6500, games_per_day=1)
            cfg = _test_config_yaml()
            cfg["tasks"]["over_5_5"]["enabled"] = False
            cfg["models"]["logreg"]["grids"]["C"] = [0.1]
            cfg["evaluation"]["bootstrap_samples"] = 30
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
            summary = (artifacts / "reports" / VALID_RUN_ID / "summary.md").read_text(encoding="utf-8")
            self.assertRegex(summary, r"^status: (ok|failed_baseline_check|failed_artifact_check)")
            metrics = json.loads((artifacts / "reports" / VALID_RUN_ID / "metrics.json").read_text())
            self.assertIn("acceptance", metrics)
            self.assertIn("holdout", metrics)
            self.assertIn("bootstrap", metrics["holdout"])

    def test_failed_baseline_sets_nonzero_exit_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, meta_path = write_synthetic_train_dataset(tmp, n_days=20)
            config = load_config(
                ROOT / "configs" / "modeling_default.yaml",
                metadata=load_metadata_json(meta_path),
            )
            run = _write_valid_run_layout(tmp, features_hash=config.features_hash)
            result = RunResult(
                run_id=run.run_id,
                task="home_win",
                model="logreg",
                status="ok",
                exit_code=0,
                reports_dir=run.reports_dir,
                model_run_dir=tmp / "artifacts" / "models" / "home_win" / "logreg" / run.run_id,
            )
            outcome = _TaskModelOutcome(
                task="home_win",
                model="logreg",
                result=result,
                holdout_calibrated_log_loss=0.99,
                holdout_trivial_log_loss=0.50,
            )
            (run.reports_dir / "summary.md").write_text("status: ok\n", encoding="utf-8")
            apply_acceptance_to_training_outcomes(
                [outcome],
                config=config,
                enabled_tasks=["home_win"],
                models=["logreg"],
                artifacts_root=tmp / "artifacts",
            )
            update_latest_symlink(
                "home_win",
                "logreg",
                run.run_id,
                artifacts_root=tmp / "artifacts",
                status=outcome.result.status,
            )
            self.assertEqual(outcome.result.status, "failed_baseline_check")
            self.assertEqual(outcome.result.exit_code, 1)
            self.assertTrue((run.reports_dir / "summary.md").exists())
            self.assertTrue((run.reports_dir / "metrics.json").exists())
            summary = (run.reports_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("status: failed_baseline_check", summary)

    def test_no_fail_on_baseline_flag_keeps_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = write_synthetic_train_dataset(tmp, n_days=6500)
            cfg = _test_config_yaml()
            cfg["tasks"]["over_5_5"]["enabled"] = False
            cfg["models"]["logreg"]["grids"]["C"] = [0.1]
            cfg["evaluation"]["bootstrap_samples"] = 20
            cfg["compute"]["num_threads"] = 1
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            def _mark_baseline_failed(outcomes: list, **kwargs: object) -> tuple:
                for item in outcomes:
                    item.result.status = "failed_baseline_check"
                    item.result.exit_code = 1
                return (
                    "failed_baseline_check",
                    BaselineGateResult(status="failed_baseline_check", per_task=()),
                    ArtifactCheckResult(ok=True),
                )

            with mock.patch(
                "modeling.train_runner.apply_acceptance_to_training_outcomes",
                side_effect=_mark_baseline_failed,
            ):
                results = run_training(
                    load_config(cfg_path, metadata=load_metadata_json(meta_path)),
                    dataset_csv=csv_path,
                    metadata_path=meta_path,
                    task="home_win",
                    model="logreg",
                    run_id=VALID_RUN_ID,
                    artifacts_root=tmp / "artifacts",
                    fail_on_baseline=False,
                )
            self.assertEqual(results[0].exit_code, 0)


if __name__ == "__main__":
    unittest.main()
