"""Tests for modeling acceptance layer (UPDATE plan stage 12)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from modeling.acceptance import (
    BASELINE_STRICT_EPS,
    BaselineGateResult,
    TaskModelEval,
    apply_acceptance_to_training_outcomes,
    evaluate_baseline_gate,
    pick_winning_family,
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
    *,
    model_ll: float,
    benchmark_ll: float,
    model: str = "logreg",
    benchmark: str = "elo",
    model_ece: float = 0.01,
    benchmark_ece: float = 0.01,
) -> TaskModelEval:
    return TaskModelEval(
        task="home_win",
        model=model,
        model_log_loss=model_ll,
        benchmark_log_loss=benchmark_ll,
        benchmark=benchmark,
        model_ece=model_ece,
        benchmark_ece=benchmark_ece,
    )


def _metrics_payload(*, task: str = "home_win", model: str = "logreg") -> dict:
    return compose_metrics_json(**sample_compose_kwargs(task=task, model=model))


def _write_run_layout(
    tmp: Path, *, task: str = "home_win", model: str = "logreg"
) -> tuple[str, Path, TaskModelEval]:
    """Write reports and final metadata of one run; return ``(run_id, reports_dir, evaluation)``."""
    run_id = f"{task}_{model}_deadbeef_20260101T000000Z"
    reports = tmp / "artifacts" / "reports" / run_id
    reports.mkdir(parents=True, exist_ok=True)
    metrics_payload = _metrics_payload(task=task, model=model)
    (reports / "metrics.json").write_text(json.dumps(metrics_payload, indent=2), encoding="utf-8")
    (reports / "summary.md").write_text("status: ok\n\n# placeholder\n", encoding="utf-8")
    final_dir = tmp / "artifacts" / "models" / task / model / run_id / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "metadata.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    benchmark = "elo" if task == "home_win" else "constant"
    return run_id, reports, TaskModelEval(
        task=task,
        model=model,
        model_log_loss=metrics_payload["pooled"]["model"]["log_loss"],
        benchmark_log_loss=metrics_payload["pooled"][benchmark]["log_loss"],
        benchmark=benchmark,
        model_ece=metrics_payload["pooled"]["model"]["ece"],
        benchmark_ece=metrics_payload["pooled"][benchmark]["ece"],
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

    def test_home_win_gate_ece_not_above_elo(self) -> None:
        def verdict(model_ll: float = 0.5, **kw: float):
            entry = _eval_entry(model_ll=model_ll, benchmark_ll=0.7, **kw)
            return evaluate_baseline_gate(["home_win"], {"home_win": [entry]}).per_task[0]

        equal = verdict(model_ece=0.02, benchmark_ece=0.02)
        self.assertTrue(equal.passed)
        self.assertEqual(equal.reason, "")
        worse = verdict(model_ece=0.03, benchmark_ece=0.02)
        self.assertFalse(worse.passed)
        self.assertEqual(worse.reason, "ece above elo")
        both = verdict(model_ll=0.8, model_ece=0.03, benchmark_ece=0.02)
        self.assertEqual(both.reason, "log_loss not below elo; ece above elo")

    def test_constant_benchmark_ignores_ece(self) -> None:
        entry = _eval_entry(
            model_ll=0.5, benchmark_ll=0.7, benchmark="constant", model_ece=0.5, benchmark_ece=0.0
        )
        verdict = evaluate_baseline_gate(["home_win"], {"home_win": [entry]}).per_task[0]
        self.assertTrue(verdict.passed)

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
        failing = evaluate_baseline_gate(
            ["home_win"], {"home_win": [_eval_entry(model_ll=0.6, benchmark_ll=0.66, model_ece=0.1)]}
        )
        self.assertIn("ece above elo", format_baseline_summary_section(failing))
        self.assertEqual(gate.to_dict()["per_task"][0]["benchmark"], "elo")


class TestApplyAcceptanceIntegration(unittest.TestCase):
    def test_apply_updates_summary_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, meta_path = write_synthetic_train_dataset(tmp, n_days=20)
            config = load_config(ROOT / "configs" / "modeling_default.yaml", metadata=load_metadata_json(meta_path))
            run_id, reports_dir, run = _write_run_layout(tmp)
            result = RunResult(
                run_id=run_id,
                task="home_win",
                model="logreg",
                status="ok",
                exit_code=0,
                reports_dir=reports_dir,
                model_run_dir=tmp / "artifacts" / "models" / "home_win" / "logreg" / run_id,
            )
            outcome = _TaskModelOutcome(
                task="home_win",
                model="logreg",
                result=result,
                evaluation=run,
            )
            (reports_dir / "summary.md").write_text("status: ok\n\n# body\n", encoding="utf-8")
            apply_acceptance_to_training_outcomes([outcome], config=config, enabled_tasks=["home_win"])
            self.assertEqual((outcome.result.status, outcome.result.exit_code), ("ok", 0))
            summary = (reports_dir / "summary.md").read_text(encoding="utf-8")
            self.assertTrue(summary.startswith("status:"))
            metrics = json.loads((reports_dir / "metrics.json").read_text(encoding="utf-8"))
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
    base["models"]["lgbm"]["monotone"] = {"over_5_5": {}}
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
            self.assertRegex(summary, r"^status: (ok|failed_baseline_check)")
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
            run_id, reports_dir, run = _write_run_layout(tmp)
            result = RunResult(
                run_id=run_id,
                task="home_win",
                model="logreg",
                status="ok",
                exit_code=0,
                reports_dir=reports_dir,
                model_run_dir=tmp / "artifacts" / "models" / "home_win" / "logreg" / run_id,
            )
            outcome = _TaskModelOutcome(
                task="home_win",
                model="logreg",
                result=result,
                evaluation=replace(
                    run, model_log_loss=0.99, benchmark_log_loss=0.50, benchmark="elo", model_ece=0.01, benchmark_ece=0.01
                ),
            )
            (reports_dir / "summary.md").write_text("status: ok\n", encoding="utf-8")
            apply_acceptance_to_training_outcomes([outcome], config=config, enabled_tasks=["home_win"])
            model_base = tmp / "artifacts" / "models" / "home_win" / "logreg"
            os.symlink("previous_run/final", model_base / "latest")
            update_latest_symlink(
                "home_win",
                "logreg",
                run_id,
                artifacts_root=tmp / "artifacts",
                status=outcome.result.status,
            )
            self.assertEqual(outcome.result.status, "failed_baseline_check")
            self.assertEqual(outcome.result.exit_code, 1)
            self.assertEqual(os.readlink(model_base / "latest"), "previous_run/final")
            self.assertTrue((reports_dir / "summary.md").exists())
            self.assertTrue((reports_dir / "metrics.json").exists())
            summary = (reports_dir / "summary.md").read_text(encoding="utf-8")
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
                return BaselineGateResult(status="failed_baseline_check", per_task=())

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
