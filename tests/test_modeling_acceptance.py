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
    TaskModelEval,
    apply_acceptance_to_training_outcomes,
    evaluate_baseline_gate,
    pick_winning_family,
    verify_latest_symlinks,
    verify_run_artifacts,
)
from modeling.train_runner import update_latest_symlink
from modeling.config import load_config, load_metadata_json
from modeling.train_runner import RunResult, _TaskModelOutcome, run_training
from modeling.report import compose_metrics_json
from tests._modeling_fixtures import (
    sample_compose_kwargs,
    synthetic_season_id,
    write_synthetic_train_dataset,
)
from tests.test_modeling_no_db_access import forbidden_imports_in_module

ROOT = Path(__file__).resolve().parent.parent
VALID_RUN_ID = "home_win_logreg_deadbeef_20260101T000000Z"


def _eval_entry(
    *, model_ll: float, benchmark_ll: float, model: str = "logreg", benchmark: str = "elo"
) -> TaskModelEval:
    return TaskModelEval(
        task="home_win",
        model=model,
        run_id=VALID_RUN_ID,
        reports_dir=Path("."),
        model_log_loss=model_ll,
        benchmark_log_loss=benchmark_ll,
        benchmark=benchmark,
    )


def _metrics_payload(*, task: str = "home_win", model: str = "logreg") -> dict:
    return compose_metrics_json(**sample_compose_kwargs(task=task, model=model))


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
        "test_seasons": [20232024, 20242025, 20252026],
        "n_rows_train": 100,
        "n_rows_inner_val": 50,
        "n_rows_calibration": 40,
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
) -> TaskModelEval:
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
    season_dir = model_root / f"season_{synthetic_season_id(2)}"
    season_dir.mkdir(parents=True, exist_ok=True)
    (season_dir / "metadata.json").write_text("{}", encoding="utf-8")
    if symlink_ok:
        base = tmp / "artifacts" / "models" / task / model
        base.mkdir(parents=True, exist_ok=True)
        link = base / "latest"
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(Path(run_id) / "final", link)
    benchmark = "elo" if task == "home_win" else "constant"
    return TaskModelEval(
        task=task,
        model=model,
        run_id=run_id,
        reports_dir=reports,
        model_log_loss=metrics_payload["pooled"]["model"]["log_loss"],
        benchmark_log_loss=metrics_payload["pooled"][benchmark]["log_loss"],
        benchmark=benchmark,
    )


class TestBaselineGate(unittest.TestCase):
    def test_gate_passes_when_strictly_better(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {"home_win": [_eval_entry(model_ll=0.5, benchmark_ll=0.7)]},
        )
        self.assertEqual(gate.status, "ok")
        self.assertTrue(gate.per_task[0].passed)

    def test_gate_fails_when_not_strictly_better(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {"home_win": [_eval_entry(model_ll=0.75, benchmark_ll=0.70)]},
        )
        self.assertEqual(gate.status, "failed_baseline_check")

    def test_gate_fails_on_equality_within_eps(self) -> None:
        ll = 0.693147
        gate = evaluate_baseline_gate(
            ["home_win"],
            {"home_win": [_eval_entry(model_ll=ll, benchmark_ll=ll)]},
        )
        self.assertEqual(gate.status, "failed_baseline_check")
        self.assertGreaterEqual(BASELINE_STRICT_EPS, 0)

    def test_pick_winning_family_prefers_lgbm_on_tie(self) -> None:
        ll = 0.55
        winner = pick_winning_family(
            [
                _eval_entry(model_ll=ll, benchmark_ll=0.9, model="logreg"),
                _eval_entry(model_ll=ll, benchmark_ll=0.9, model="lgbm"),
            ]
        )
        self.assertEqual(winner.model, "lgbm")
        gate = evaluate_baseline_gate(
            ["home_win"],
            {
                "home_win": [
                    _eval_entry(model_ll=ll, benchmark_ll=0.9, model="logreg"),
                    _eval_entry(model_ll=ll, benchmark_ll=0.9, model="lgbm"),
                ]
            },
        )
        self.assertEqual(gate.per_task[0].winning_family, "lgbm")

    def test_disabled_task_not_in_gate(self) -> None:
        gate = evaluate_baseline_gate(
            ["home_win"],
            {
                "over_5_5": [_eval_entry(model_ll=9.0, benchmark_ll=0.1)],
                "home_win": [_eval_entry(model_ll=0.5, benchmark_ll=0.7)],
            },
        )
        self.assertEqual(gate.status, "ok")

    def test_gate_summary_names_pooled_scheme_and_benchmark(self) -> None:
        from modeling.acceptance import format_baseline_summary_section

        gate = evaluate_baseline_gate(
            ["home_win"], {"home_win": [_eval_entry(model_ll=0.60, benchmark_ll=0.66)]}
        )
        text = format_baseline_summary_section(gate)
        self.assertIn("pooled test seasons, vs Elo|constant", text)
        self.assertIn("elo=0.660000", text)
        self.assertEqual(gate.to_dict()["per_task"][0]["benchmark"], "elo")


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

    def _issues_after_breaking_metrics(self, mutate) -> list[str]:
        run = _write_valid_run_layout(self.tmp, features_hash=self.config.features_hash)
        metrics_path = run.reports_dir / "metrics.json"
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        mutate(payload)
        metrics_path.write_text(json.dumps(payload), encoding="utf-8")
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertFalse(result.ok)
        return result.issues

    def test_verify_fails_missing_bootstrap(self) -> None:
        issues = self._issues_after_breaking_metrics(lambda m: m["pooled"].pop("bootstrap"))
        self.assertTrue(any("pooled.bootstrap" in issue for issue in issues), msg=issues)

    def test_verify_fails_bootstrap_not_by_day(self) -> None:
        def mutate(m: dict) -> None:
            m["pooled"]["bootstrap"]["log_loss"]["bootstrap.block_by_day"] = False

        issues = self._issues_after_breaking_metrics(mutate)
        self.assertTrue(any("block_by_day" in issue for issue in issues), msg=issues)

    def test_verify_fails_missing_diff_vs_elo(self) -> None:
        issues = self._issues_after_breaking_metrics(lambda m: m["pooled"]["diff_ci"].pop("model_minus_elo"))
        self.assertTrue(any("model_minus_elo" in issue for issue in issues), msg=issues)

    def test_verify_fails_missing_elo_metrics(self) -> None:
        issues = self._issues_after_breaking_metrics(lambda m: m["pooled"].pop("elo"))
        self.assertTrue(any("pooled.elo" in issue for issue in issues), msg=issues)

    def test_verify_fails_missing_pooled_block(self) -> None:
        issues = self._issues_after_breaking_metrics(lambda m: m.pop("pooled"))
        self.assertTrue(any("pooled block missing" in issue for issue in issues), msg=issues)

    def test_verify_fails_missing_season_run_dirs(self) -> None:
        run = _write_valid_run_layout(self.tmp, features_hash=self.config.features_hash)
        for season_dir in (self.tmp / "artifacts" / "models" / "home_win" / "logreg" / run.run_id).glob("season_*"):
            (season_dir / "metadata.json").unlink()
            season_dir.rmdir()
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertTrue(any("per-season" in issue for issue in result.issues), msg=result.issues)

    def test_verify_fails_old_metadata_without_test_seasons(self) -> None:
        run_id = "home_win_logreg_deadbeef_20260101T000000Z"
        meta = _metadata_dict(run_id=run_id, features_hash=self.config.features_hash)
        del meta["test_seasons"]
        run = _write_valid_run_layout(self.tmp, features_hash=self.config.features_hash, metadata=meta)
        result = verify_run_artifacts(
            config=self.config,
            artifacts_root=self.tmp / "artifacts",
            enabled_tasks=["home_win"],
            models=["logreg"],
            runs=[run],
        )
        self.assertTrue(any("test_seasons" in issue for issue in result.issues), msg=result.issues)

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

    def _make_outcome(self, run: TaskModelEval, *, status: str = "ok") -> _TaskModelOutcome:
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
            pooled_model_log_loss=run.model_log_loss,
            benchmark_log_loss=run.benchmark_log_loss,
            benchmark=run.benchmark,
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
                pooled_model_log_loss=run.model_log_loss,
                benchmark_log_loss=run.benchmark_log_loss,
                benchmark=run.benchmark,
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


class TestAcceptanceEndToEndSmoke(unittest.TestCase):
    def test_train_writes_status_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = write_synthetic_train_dataset(tmp)
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
            self.assertEqual([s["season_id"] for s in metrics["seasons"]], _test_config_yaml()["split"]["test_seasons"])
            self.assertIn("bootstrap", metrics["pooled"])
            self.assertIn("model_minus_elo", metrics["pooled"]["diff_ci"])
            self.assertNotIn("holdout", metrics)
            self.assertNotIn("folds", metrics)

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
                pooled_model_log_loss=0.99,
                benchmark_log_loss=0.50,
                benchmark="elo",
            )
            (run.reports_dir / "summary.md").write_text("status: ok\n", encoding="utf-8")
            apply_acceptance_to_training_outcomes(
                [outcome],
                config=config,
                enabled_tasks=["home_win"],
                models=["logreg"],
                artifacts_root=tmp / "artifacts",
            )
            model_base = tmp / "artifacts" / "models" / "home_win" / "logreg"
            (model_base / "latest").unlink()
            os.symlink("previous_run/final", model_base / "latest")
            update_latest_symlink(
                "home_win",
                "logreg",
                run.run_id,
                artifacts_root=tmp / "artifacts",
                status=outcome.result.status,
            )
            self.assertEqual(outcome.result.status, "failed_baseline_check")
            self.assertEqual(outcome.result.exit_code, 1)
            self.assertEqual(os.readlink(model_base / "latest"), "previous_run/final")
            self.assertFalse((model_base / "latest.txt").exists())
            self.assertTrue((run.reports_dir / "summary.md").exists())
            self.assertTrue((run.reports_dir / "metrics.json").exists())
            summary = (run.reports_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("status: failed_baseline_check", summary)

    def test_no_fail_on_baseline_flag_keeps_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = write_synthetic_train_dataset(tmp)
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
