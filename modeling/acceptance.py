"""Run acceptance layer: baseline gate and artifact diagnostics (UPDATE plan stage 12).

Reads completed training artifacts and reports only — no metric recomputation,
no PostgreSQL, no ``modeling.dataset_builder``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from modeling.config import ResolvedConfig

logger = logging.getLogger(__name__)

RunStatus = Literal["ok", "failed_baseline_check", "failed_artifact_check"]

# Strict baseline: model_ll + BASELINE_STRICT_EPS < trivial (equality at 1e-12 → fail).
BASELINE_STRICT_EPS = 1e-12
# Family tie-break when holdout log losses are equal: earlier name wins (lgbm before logreg).
FAMILY_TIEBREAK_ORDER: tuple[str, ...] = ("lgbm", "logreg")

_LIBRARY_VERSION_KEYS = frozenset(
    {"scikit-learn", "lightgbm", "pandas", "numpy"}
)
_METADATA_REQUIRED_TOP = frozenset(
    {
        "features_hash",
        "random_seed",
        "run_id",
        "git_commit",
        "library_versions",
        "train_days",
        "inner_val_days",
        "calibration_days",
        "test_days",
        "holdout_days",
        "n_rows_train",
        "n_rows_inner_val",
        "n_rows_calibration",
        "n_rows_test",
        "n_rows_holdout",
    }
)
_HOLDOUT_METRIC_KEYS = frozenset({"log_loss", "brier", "ece"})
_BOOTSTRAP_METRIC_KEYS = frozenset({"log_loss", "brier"})
_BOOTSTRAP_CI_KEYS = frozenset({"ci_low", "ci_high", "point"})


@dataclass(frozen=True)
class TaskModelHoldout:
    """Holdout metrics for one (task, model) pair — values from training / ``metrics.json``."""

    task: str
    model: str
    run_id: str
    reports_dir: Path
    model_log_loss: float
    trivial_log_loss: float


@dataclass(frozen=True)
class TaskBaselineVerdict:
    task: str
    winning_family: str
    model_log_loss: float
    trivial_log_loss: float
    delta: float
    passed: bool


@dataclass(frozen=True)
class BaselineGateResult:
    status: RunStatus
    per_task: tuple[TaskBaselineVerdict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "per_task": [
                {
                    "task": v.task,
                    "winning_family": v.winning_family,
                    "model_log_loss": v.model_log_loss,
                    "trivial_log_loss": v.trivial_log_loss,
                    "delta": v.delta,
                    "passed": v.passed,
                }
                for v in self.per_task
            ],
        }


@dataclass
class ArtifactCheckResult:
    ok: bool
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": list(self.issues)}


def _family_sort_key(model: str, log_loss: float) -> tuple[float, int]:
    """Lower log loss wins; tie-break prefers earlier entry in ``FAMILY_TIEBREAK_ORDER``."""
    try:
        tie_rank = FAMILY_TIEBREAK_ORDER.index(model)
    except ValueError:
        tie_rank = len(FAMILY_TIEBREAK_ORDER)
    return (log_loss, tie_rank)


def pick_winning_family(candidates: Sequence[TaskModelHoldout]) -> TaskModelHoldout:
    """Return the family with lowest holdout calibrated log loss (deterministic tie-break)."""
    if not candidates:
        raise ValueError("pick_winning_family requires at least one candidate")
    return min(
        candidates,
        key=lambda item: _family_sort_key(item.model, item.model_log_loss),
    )


def evaluate_baseline_gate(
    enabled_tasks: Sequence[str],
    holdout_by_task: Mapping[str, Sequence[TaskModelHoldout]],
) -> BaselineGateResult:
    """Compare best calibrated holdout log loss vs trivial baseline per enabled task."""
    verdicts: list[TaskBaselineVerdict] = []
    all_passed = True

    for task in enabled_tasks:
        candidates = list(holdout_by_task.get(task, ()))
        if not candidates:
            all_passed = False
            verdicts.append(
                TaskBaselineVerdict(
                    task=task,
                    winning_family="",
                    model_log_loss=float("nan"),
                    trivial_log_loss=float("nan"),
                    delta=float("nan"),
                    passed=False,
                )
            )
            continue

        winner = pick_winning_family(candidates)
        trivial = winner.trivial_log_loss
        model_ll = winner.model_log_loss
        passed = model_ll + BASELINE_STRICT_EPS < trivial
        if not passed:
            all_passed = False
        verdicts.append(
            TaskBaselineVerdict(
                task=task,
                winning_family=winner.model,
                model_log_loss=model_ll,
                trivial_log_loss=trivial,
                delta=trivial - model_ll,
                passed=passed,
            )
        )

    status: RunStatus = "ok" if all_passed else "failed_baseline_check"
    return BaselineGateResult(status=status, per_task=tuple(verdicts))


def holdout_metrics_from_json(metrics: Mapping[str, Any], *, reports_dir: Path) -> TaskModelHoldout:
    """Extract gate inputs from a written ``metrics.json`` (stage 5/10 output)."""
    holdout = metrics.get("holdout")
    if not isinstance(holdout, Mapping):
        raise ValueError("metrics.json missing holdout block")
    calibrated = holdout.get("calibrated")
    if not isinstance(calibrated, Mapping):
        raise ValueError("holdout.calibrated missing or not a mapping")
    trivial = holdout.get("trivial_base_rate")
    if not isinstance(trivial, Mapping):
        raise ValueError("holdout.trivial_base_rate missing or not a mapping")
    model_ll = calibrated.get("log_loss")
    trivial_ll = trivial.get("log_loss")
    if model_ll is None or trivial_ll is None:
        raise ValueError("holdout log_loss values missing for baseline gate")
    return TaskModelHoldout(
        task=str(metrics["task"]),
        model=str(metrics["model"]),
        run_id=str(metrics["run_id"]),
        reports_dir=reports_dir,
        model_log_loss=float(model_ll),
        trivial_log_loss=float(trivial_ll),
    )


def run_status(
    baseline: BaselineGateResult,
    artifacts: ArtifactCheckResult,
) -> RunStatus:
    """Combine baseline gate and artifact diagnostics into a single run status."""
    if not artifacts.ok:
        return "failed_artifact_check"
    return baseline.status


def _issue(issues: list[str], message: str) -> None:
    issues.append(message)


def _validate_metadata_file(
    path: Path,
    *,
    expected_features_hash: str,
    expected_run_id: str,
    issues: list[str],
) -> None:
    if not path.exists():
        _issue(issues, f"missing metadata.json: {path}")
        return
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _issue(issues, f"invalid JSON in {path}: {exc}")
        return
    if not isinstance(meta, dict):
        _issue(issues, f"metadata.json root must be object: {path}")
        return

    for key in sorted(_METADATA_REQUIRED_TOP):
        if key not in meta:
            _issue(issues, f"metadata.json missing field {key!r} in {path}")

    if meta.get("features_hash") != expected_features_hash:
        _issue(
            issues,
            f"metadata.json features_hash mismatch in {path}: "
            f"got {meta.get('features_hash')!r}, expected {expected_features_hash!r}",
        )

    if meta.get("run_id") != expected_run_id:
        _issue(
            issues,
            f"metadata.json run_id mismatch in {path}: "
            f"got {meta.get('run_id')!r}, expected {expected_run_id!r}",
        )

    if "git_commit" in meta and meta["git_commit"] is not None and not isinstance(meta["git_commit"], str):
        _issue(issues, f"metadata.json git_commit must be string or null in {path}")

    lib = meta.get("library_versions")
    if not isinstance(lib, Mapping):
        _issue(issues, f"metadata.json library_versions missing or invalid in {path}")
    else:
        missing_lib = _LIBRARY_VERSION_KEYS - lib.keys()
        if missing_lib:
            _issue(
                issues,
                f"metadata.json library_versions missing keys {sorted(missing_lib)} in {path}",
            )

    for day_key in ("train_days", "inner_val_days", "calibration_days", "holdout_days"):
        block = meta.get(day_key)
        if block is None:
            continue
        if not isinstance(block, Mapping) or "min" not in block or "max" not in block:
            _issue(issues, f"metadata.json {day_key} invalid in {path}")


def _validate_holdout_metrics_block(
    metrics: Mapping[str, Any],
    *,
    task: str,
    reports_dir: Path,
    issues: list[str],
) -> None:
    holdout = metrics.get("holdout")
    if not isinstance(holdout, Mapping):
        _issue(issues, f"metrics.json holdout block missing in {reports_dir}")
        return

    for block_name, block in (("raw", holdout.get("raw")), ("calibrated", holdout.get("calibrated"))):
        if not isinstance(block, Mapping):
            _issue(issues, f"holdout.{block_name} missing in {reports_dir / 'metrics.json'}")
            continue
        missing = _HOLDOUT_METRIC_KEYS - block.keys()
        if missing:
            _issue(
                issues,
                f"holdout.{block_name} missing keys {sorted(missing)} in {reports_dir / 'metrics.json'}",
            )

    trivial = holdout.get("trivial_base_rate")
    if not isinstance(trivial, Mapping) or "log_loss" not in trivial:
        _issue(issues, f"holdout.trivial_base_rate missing in {reports_dir / 'metrics.json'}")

    bootstrap = holdout.get("bootstrap")
    if not isinstance(bootstrap, Mapping):
        _issue(issues, f"holdout.bootstrap missing in {reports_dir / 'metrics.json'} (stage 6)")
        return
    for metric_name in _BOOTSTRAP_METRIC_KEYS:
        entry = bootstrap.get(metric_name)
        if not isinstance(entry, Mapping):
            _issue(
                issues,
                f"holdout.bootstrap.{metric_name} missing in {reports_dir / 'metrics.json'}",
            )
            continue
        missing_ci = _BOOTSTRAP_CI_KEYS - entry.keys()
        if missing_ci:
            _issue(
                issues,
                f"holdout.bootstrap.{metric_name} missing CI keys {sorted(missing_ci)}",
            )
        if entry.get("bootstrap.block_by_day") is not True:
            _issue(
                issues,
                f"holdout.bootstrap.{metric_name} must have bootstrap.block_by_day=true "
                f"(holdout block bootstrap by day)",
            )

    rel_name = holdout.get("reliability_path", f"reliability_{task}.png")
    rel_path = reports_dir / str(rel_name)
    if not rel_path.is_file() or rel_path.stat().st_size == 0:
        _issue(issues, f"reliability PNG missing or empty: {rel_path}")

    team_bd = metrics.get("team_breakdown")
    if not isinstance(team_bd, Mapping):
        _issue(issues, f"metrics.json team_breakdown missing in {reports_dir}")
    else:
        for col in ("home_team_id", "away_team_id"):
            if col not in team_bd:
                _issue(issues, f"team_breakdown missing {col!r} in {reports_dir / 'metrics.json'}")

    folds = metrics.get("folds")
    if not isinstance(folds, list) or len(folds) == 0:
        _issue(issues, f"metrics.json folds empty or missing in {reports_dir}")


def _validate_latest_symlink(
    *,
    task: str,
    model: str,
    run_id: str,
    artifacts_root: Path,
    expect_pointer: bool,
    issues: list[str],
) -> None:
    """Require ``latest`` symlink → ``<run_id>/final/`` (UPDATE plan DoD §5).

    ``train_runner.update_latest_symlink`` may write ``latest.txt`` when the host
    filesystem cannot create symlinks. That fallback is for production convenience
    only and **does not** satisfy acceptance DoD — phase-2 checks insist on a
    real symlink here.
    """
    if not expect_pointer:
        return
    base = artifacts_root / "models" / task / model
    link_path = base / "latest"
    if not link_path.is_symlink():
        txt = base / "latest.txt"
        if txt.is_file():
            _issue(
                issues,
                f"latest for {task}/{model} is latest.txt fallback, not a symlink "
                f"(DoD requires symlink → {run_id}/final/)",
            )
        else:
            _issue(
                issues,
                f"latest symlink missing for {task}/{model} (expected → {run_id}/final/)",
            )
        return
    resolved = (base / os.readlink(link_path)).resolve()
    if resolved != (base / run_id / "final").resolve():
        _issue(
            issues,
            f"latest symlink for {task}/{model} points to {resolved}, "
            f"expected {base / run_id / 'final'}",
        )


def verify_run_artifacts(
    *,
    config: ResolvedConfig,
    artifacts_root: Path,
    enabled_tasks: Sequence[str],
    models: Sequence[str],
    runs: Sequence[TaskModelHoldout],
) -> ArtifactCheckResult:
    """Verify DoD artifacts for each trained (task, model) pair (presence only).

    Symlink ``latest`` is **not** checked here — see :func:`verify_latest_symlinks`
    after ``train_runner.update_latest_symlink`` (two-phase layout in
    :func:`apply_acceptance_to_training_outcomes` + ``train_runner.run_training``).
    """
    issues: list[str] = []
    run_by_pair = {(r.task, r.model): r for r in runs}

    for task in enabled_tasks:
        for model in models:
            entry = run_by_pair.get((task, model))
            if entry is None:
                _issue(issues, f"no training result for task={task} model={model}")
                continue

            run_id = entry.run_id
            reports_dir = entry.reports_dir
            model_root = artifacts_root / "models" / task / model / run_id
            final_dir = model_root / "final"

            if not final_dir.is_dir():
                _issue(issues, f"missing final/ directory: {final_dir}")
            else:
                for name in ("model.joblib", "calibrator.joblib", "metadata.json"):
                    if not (final_dir / name).exists():
                        _issue(issues, f"missing {name} in {final_dir}")
                _validate_metadata_file(
                    final_dir / "metadata.json",
                    expected_features_hash=config.features_hash,
                    expected_run_id=run_id,
                    issues=issues,
                )

            if not list(model_root.glob("fold_*")):
                _issue(issues, f"no walk-forward fold directories under {model_root}")

            summary_path = reports_dir / "summary.md"
            if not summary_path.is_file() or summary_path.stat().st_size == 0:
                _issue(issues, f"missing or empty summary.md: {summary_path}")

            metrics_path = reports_dir / "metrics.json"
            if not metrics_path.is_file():
                _issue(issues, f"missing metrics.json: {metrics_path}")
            else:
                try:
                    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    _issue(issues, f"invalid metrics.json at {metrics_path}: {exc}")
                    metrics = {}
                if isinstance(metrics, dict):
                    _validate_holdout_metrics_block(
                        metrics,
                        task=task,
                        reports_dir=reports_dir,
                        issues=issues,
                    )

    return ArtifactCheckResult(ok=len(issues) == 0, issues=issues)


def _task_verdict_map(baseline: BaselineGateResult) -> dict[str, TaskBaselineVerdict]:
    return {v.task: v for v in baseline.per_task}


def pair_run_status(
    *,
    task: str,
    baseline: BaselineGateResult,
    artifacts: ArtifactCheckResult,
) -> RunStatus:
    if not artifacts.ok:
        return "failed_artifact_check"
    verdict = _task_verdict_map(baseline).get(task)
    if verdict is None or not verdict.passed:
        return "failed_baseline_check"
    return "ok"


def format_baseline_summary_section(baseline: BaselineGateResult) -> str:
    lines = ["## Baseline gate (holdout, calibrated model_final)", ""]
    for verdict in baseline.per_task:
        status_word = "PASS" if verdict.passed else "FAIL"
        lines.append(
            f"- **{verdict.task}**: {status_word} — best `{verdict.winning_family}` "
            f"log_loss={verdict.model_log_loss:.6f} vs trivial={verdict.trivial_log_loss:.6f} "
            f"(delta={verdict.delta:+.6f})"
        )
    lines.append("")
    return "\n".join(lines)


def format_artifact_summary_section(artifacts: ArtifactCheckResult) -> str:
    if artifacts.ok:
        return "## Artifact check\n\nAll required artifacts and report fields present.\n\n"
    lines = ["## Artifact check", "", "Failures:"]
    for issue in artifacts.issues:
        lines.append(f"- {issue}")
    lines.append("")
    return "\n".join(lines)


def apply_status_to_summary(summary_text: str, status: RunStatus) -> str:
    """Replace the grep-friendly status line at the top of ``summary.md``."""
    prefix = f"status: {status}"
    if summary_text.startswith("status:"):
        rest = summary_text.split("\n", 1)
        body = rest[1] if len(rest) > 1 else ""
        if body.startswith("\n"):
            return prefix + body
        return prefix + "\n\n" + body.lstrip("\n")
    return prefix + "\n\n" + summary_text


def _patch_summary_report(
    summary_path: Path,
    *,
    status: RunStatus,
    baseline: BaselineGateResult,
    artifacts: ArtifactCheckResult,
    run_id: str,
) -> None:
    """Write grep-able ``status:`` line (creates ``summary.md`` if absent for status output)."""
    if summary_path.exists():
        text = summary_path.read_text(encoding="utf-8")
    else:
        text = f"# Run report: {run_id}\n"
    if "## Baseline gate" not in text:
        text = text + "\n" + format_baseline_summary_section(baseline)
    if "## Artifact check" not in text:
        text = text + format_artifact_summary_section(artifacts)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(apply_status_to_summary(text, status), encoding="utf-8")


def patch_metrics_acceptance(
    metrics_path: Path,
    *,
    status: RunStatus,
    baseline: BaselineGateResult,
    artifacts: ArtifactCheckResult,
) -> None:
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    payload["acceptance"] = {
        "status": status,
        "baseline_gate": baseline.to_dict(),
        "artifact_check": artifacts.to_dict(),
    }
    metrics_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def apply_acceptance_to_training_outcomes(
    outcomes: Sequence[Any],
    *,
    config: ResolvedConfig,
    enabled_tasks: Sequence[str],
    models: Sequence[str],
    artifacts_root: Path,
) -> tuple[RunStatus, BaselineGateResult, ArtifactCheckResult]:
    """End-of-run hook: baseline gate, artifact verification, patch reports."""
    from modeling.report import configure_run_logger

    holdout_runs: list[TaskModelHoldout] = [
        TaskModelHoldout(
            task=item.task,
            model=item.model,
            run_id=item.result.run_id,
            reports_dir=item.result.reports_dir,
            model_log_loss=item.holdout_calibrated_log_loss,
            trivial_log_loss=item.holdout_trivial_log_loss,
        )
        for item in outcomes
    ]

    holdout_by_task: dict[str, list[TaskModelHoldout]] = {}
    for entry in holdout_runs:
        holdout_by_task.setdefault(entry.task, []).append(entry)

    baseline = evaluate_baseline_gate(enabled_tasks, holdout_by_task)

    # Phase 1: models/reports/metadata only (no ``latest`` — see phase 2 below).
    artifacts = verify_run_artifacts(
        config=config,
        artifacts_root=artifacts_root,
        enabled_tasks=enabled_tasks,
        models=models,
        runs=holdout_runs,
    )

    for item in outcomes:
        status = pair_run_status(task=item.task, baseline=baseline, artifacts=artifacts)
        item.result.status = status
        item.result.exit_code = 0 if status == "ok" else 1

        run_logger = configure_run_logger(item.result.reports_dir, level=config.compute.log_level)
        run_logger.info("Acceptance status=%s task=%s model=%s", status, item.task, item.model)
        run_logger.info("Baseline gate: %s", json.dumps(baseline.to_dict(), ensure_ascii=False))
        if not artifacts.ok:
            for issue in artifacts.issues:
                run_logger.warning("Artifact check: %s", issue)
        else:
            for verdict in baseline.per_task:
                run_logger.info(
                    "Baseline task=%s winner=%s model_ll=%.6f trivial_ll=%.6f passed=%s",
                    verdict.task,
                    verdict.winning_family,
                    verdict.model_log_loss,
                    verdict.trivial_log_loss,
                    verdict.passed,
                )

        _patch_summary_report(
            item.result.reports_dir / "summary.md",
            status=status,
            baseline=baseline,
            artifacts=artifacts,
            run_id=item.result.run_id,
        )

        metrics_path = item.result.reports_dir / "metrics.json"
        if metrics_path.exists():
            patch_metrics_acceptance(
                metrics_path,
                status=status,
                baseline=baseline,
                artifacts=artifacts,
            )

        final_meta = item.result.model_run_dir / "final" / "metadata.json"
        if final_meta.exists():
            meta = json.loads(final_meta.read_text(encoding="utf-8"))
            meta["status"] = status
            final_meta.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

    return run_status(baseline, artifacts), baseline, artifacts


def verify_latest_symlinks(
    outcomes: Sequence[Any],
    *,
    artifacts_root: Path,
) -> ArtifactCheckResult:
    """Phase 2: validate ``latest`` symlink after ``update_latest_symlink`` (stage 10)."""
    issues: list[str] = []
    for item in outcomes:
        if item.result.status != "ok":
            continue
        _validate_latest_symlink(
            task=item.task,
            model=item.model,
            run_id=item.result.run_id,
            artifacts_root=artifacts_root,
            expect_pointer=True,
            issues=issues,
        )
    return ArtifactCheckResult(ok=len(issues) == 0, issues=issues)


def apply_latest_symlink_check(
    outcomes: Sequence[Any],
    *,
    config: ResolvedConfig,
    enabled_tasks: Sequence[str],
    models: Sequence[str],
    artifacts_root: Path,
    baseline: BaselineGateResult,
) -> None:
    """Re-run acceptance patching when ``latest`` symlinks are missing after stage-10 update."""
    from modeling.report import configure_run_logger

    latest = verify_latest_symlinks(outcomes, artifacts_root=artifacts_root)
    if latest.ok:
        return

    holdout_runs = [
        TaskModelHoldout(
            task=item.task,
            model=item.model,
            run_id=item.result.run_id,
            reports_dir=item.result.reports_dir,
            model_log_loss=item.holdout_calibrated_log_loss,
            trivial_log_loss=item.holdout_trivial_log_loss,
        )
        for item in outcomes
    ]
    artifacts = verify_run_artifacts(
        config=config,
        artifacts_root=artifacts_root,
        enabled_tasks=enabled_tasks,
        models=models,
        runs=holdout_runs,
    )
    artifacts = ArtifactCheckResult(ok=False, issues=artifacts.issues + latest.issues)

    for item in outcomes:
        status = pair_run_status(task=item.task, baseline=baseline, artifacts=artifacts)
        item.result.status = status
        item.result.exit_code = 0 if status == "ok" else 1

        run_logger = configure_run_logger(item.result.reports_dir, level=config.compute.log_level)
        for issue in latest.issues:
            run_logger.warning("Latest symlink check: %s", issue)

        _patch_summary_report(
            item.result.reports_dir / "summary.md",
            status=status,
            baseline=baseline,
            artifacts=artifacts,
            run_id=item.result.run_id,
        )
        metrics_path = item.result.reports_dir / "metrics.json"
        if metrics_path.exists():
            patch_metrics_acceptance(
                metrics_path,
                status=status,
                baseline=baseline,
                artifacts=artifacts,
            )


__all__ = [
    "ArtifactCheckResult",
    "BaselineGateResult",
    "FAMILY_TIEBREAK_ORDER",
    "BASELINE_STRICT_EPS",
    "RunStatus",
    "TaskBaselineVerdict",
    "TaskModelHoldout",
    "apply_acceptance_to_training_outcomes",
    "apply_latest_symlink_check",
    "verify_latest_symlinks",
    "apply_status_to_summary",
    "evaluate_baseline_gate",
    "format_artifact_summary_section",
    "format_baseline_summary_section",
    "holdout_metrics_from_json",
    "pair_run_status",
    "patch_metrics_acceptance",
    "pick_winning_family",
    "run_status",
    "verify_run_artifacts",
]
