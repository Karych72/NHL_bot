"""Run acceptance layer: benchmark gate and its reflection in run reports (UPDATE plan stage 12, Задача 66).

Reads completed training artifacts and reports only — no metric recomputation,
no PostgreSQL, no ``modeling.dataset_builder``. The only decision is the baseline gate
(``ok`` / ``failed_baseline_check``); it sets the exit code and gates ``latest`` promotion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from modeling.config import ResolvedConfig

RunStatus = Literal["ok", "failed_baseline_check"]

# Strict gate: model_ll + BASELINE_STRICT_EPS < benchmark (equality at 1e-12 → fail).
BASELINE_STRICT_EPS = 1e-12
# Family tie-break when pooled log losses are equal: earlier name wins (lgbm before logreg).
FAMILY_TIEBREAK_ORDER: tuple[str, ...] = ("lgbm", "logreg")


@dataclass(frozen=True)
class TaskModelEval:
    """Pooled-seasons log loss of one (task, model) pair and of the benchmark it must beat.

    ``benchmark`` is ``"elo"`` (``home_win``, Elo with the fitted curve) or ``"constant"``
    (``over_5_5``, share of "over 5.5" on the run's training seasons).
    """

    task: str
    model: str
    run_id: str
    reports_dir: Path
    model_log_loss: float
    benchmark_log_loss: float
    benchmark: str
    model_ece: float
    benchmark_ece: float


@dataclass(frozen=True)
class TaskBaselineVerdict:
    task: str
    winning_family: str
    model_log_loss: float
    benchmark_log_loss: float
    benchmark: str
    delta: float
    passed: bool
    reason: str


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
                    "benchmark_log_loss": v.benchmark_log_loss,
                    "benchmark": v.benchmark,
                    "delta": v.delta,
                    "passed": v.passed,
                    "reason": v.reason,
                }
                for v in self.per_task
            ],
        }


def _family_sort_key(model: str, log_loss: float) -> tuple[float, int]:
    """Lower log loss wins; tie-break prefers earlier entry in ``FAMILY_TIEBREAK_ORDER``."""
    try:
        tie_rank = FAMILY_TIEBREAK_ORDER.index(model)
    except ValueError:
        tie_rank = len(FAMILY_TIEBREAK_ORDER)
    return (log_loss, tie_rank)


def pick_winning_family(candidates: Sequence[TaskModelEval]) -> TaskModelEval:
    """Return the family with lowest pooled calibrated log loss (deterministic tie-break)."""
    if not candidates:
        raise ValueError("pick_winning_family requires at least one candidate")
    return min(
        candidates,
        key=lambda item: _family_sort_key(item.model, item.model_log_loss),
    )


def evaluate_baseline_gate(
    enabled_tasks: Sequence[str],
    eval_by_task: Mapping[str, Sequence[TaskModelEval]],
) -> BaselineGateResult:
    """Best calibrated pooled log loss must beat its benchmark (Elo / constant); vs Elo, ECE must also be no worse."""
    verdicts: list[TaskBaselineVerdict] = []
    all_passed = True

    for task in enabled_tasks:
        candidates = list(eval_by_task.get(task, ()))
        if not candidates:
            all_passed = False
            verdicts.append(
                TaskBaselineVerdict(
                    task=task,
                    winning_family="",
                    model_log_loss=float("nan"),
                    benchmark_log_loss=float("nan"),
                    benchmark="",
                    delta=float("nan"),
                    passed=False,
                    reason="no candidates",
                )
            )
            continue

        winner = pick_winning_family(candidates)
        benchmark_ll = winner.benchmark_log_loss
        model_ll = winner.model_log_loss
        reasons = []
        if not model_ll + BASELINE_STRICT_EPS < benchmark_ll:
            reasons.append(f"log_loss not below {winner.benchmark}")
        if winner.benchmark == "elo" and winner.model_ece > winner.benchmark_ece:
            reasons.append("ece above elo")
        passed = not reasons
        if not passed:
            all_passed = False
        verdicts.append(
            TaskBaselineVerdict(
                task=task,
                winning_family=winner.model,
                model_log_loss=model_ll,
                benchmark_log_loss=benchmark_ll,
                benchmark=winner.benchmark,
                delta=benchmark_ll - model_ll,
                passed=passed,
                reason="; ".join(reasons),
            )
        )

    status: RunStatus = "ok" if all_passed else "failed_baseline_check"
    return BaselineGateResult(status=status, per_task=tuple(verdicts))


def pair_run_status(*, task: str, baseline: BaselineGateResult) -> RunStatus:
    """Status of every ``(task, model)`` pair of ``task``: the task's gate verdict."""
    verdict = next((v for v in baseline.per_task if v.task == task), None)
    if verdict is None or not verdict.passed:
        return "failed_baseline_check"
    return "ok"


def format_baseline_summary_section(baseline: BaselineGateResult) -> str:
    lines = ["## Baseline gate (pooled test seasons, vs Elo|constant)", ""]
    for verdict in baseline.per_task:
        status_word = "PASS" if verdict.passed else "FAIL"
        lines.append(
            f"- **{verdict.task}**: {status_word} — best `{verdict.winning_family}` "
            f"log_loss={verdict.model_log_loss:.6f} vs {verdict.benchmark}={verdict.benchmark_log_loss:.6f} "
            f"(delta={verdict.delta:+.6f})" + (f" — {verdict.reason}" if verdict.reason else "")
        )
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
    run_id: str,
) -> None:
    """Write grep-able ``status:`` line (creates ``summary.md`` if absent for status output)."""
    if summary_path.exists():
        text = summary_path.read_text(encoding="utf-8")
    else:
        text = f"# Run report: {run_id}\n"
    if "## Baseline gate" not in text:
        text = text + "\n" + format_baseline_summary_section(baseline)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(apply_status_to_summary(text, status), encoding="utf-8")


def patch_metrics_acceptance(
    metrics_path: Path,
    *,
    status: RunStatus,
    baseline: BaselineGateResult,
) -> None:
    """Add the ``acceptance`` block (status and baseline gate verdicts) to ``metrics.json``."""
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    payload["acceptance"] = {
        "status": status,
        "baseline_gate": baseline.to_dict(),
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
) -> BaselineGateResult:
    """End-of-run hook: baseline gate, per-pair status and exit code, patch reports and metadata."""
    from modeling.report import configure_run_logger

    eval_by_task: dict[str, list[TaskModelEval]] = {}
    for item in outcomes:
        eval_by_task.setdefault(item.evaluation.task, []).append(item.evaluation)

    baseline = evaluate_baseline_gate(enabled_tasks, eval_by_task)

    for item in outcomes:
        status = pair_run_status(task=item.task, baseline=baseline)
        item.result.status = status
        item.result.exit_code = 0 if status == "ok" else 1

        run_logger = configure_run_logger(item.result.reports_dir, level=config.compute.log_level)
        run_logger.info("Acceptance status=%s task=%s model=%s", status, item.task, item.model)
        run_logger.info("Baseline gate: %s", json.dumps(baseline.to_dict(), ensure_ascii=False))
        for verdict in baseline.per_task:
            run_logger.info(
                "Baseline task=%s winner=%s model_ll=%.6f %s_ll=%.6f passed=%s",
                verdict.task,
                verdict.winning_family,
                verdict.model_log_loss,
                verdict.benchmark,
                verdict.benchmark_log_loss,
                verdict.passed,
            )

        _patch_summary_report(
            item.result.reports_dir / "summary.md",
            status=status,
            baseline=baseline,
            run_id=item.result.run_id,
        )

        metrics_path = item.result.reports_dir / "metrics.json"
        if metrics_path.exists():
            patch_metrics_acceptance(metrics_path, status=status, baseline=baseline)

        final_meta = item.result.model_run_dir / "final" / "metadata.json"
        if final_meta.exists():
            meta = json.loads(final_meta.read_text(encoding="utf-8"))
            meta["status"] = status
            final_meta.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

    return baseline


__all__ = [
    "BaselineGateResult",
    "FAMILY_TIEBREAK_ORDER",
    "BASELINE_STRICT_EPS",
    "RunStatus",
    "TaskBaselineVerdict",
    "TaskModelEval",
    "apply_acceptance_to_training_outcomes",
    "apply_status_to_summary",
    "evaluate_baseline_gate",
    "format_baseline_summary_section",
    "pair_run_status",
    "patch_metrics_acceptance",
    "pick_winning_family",
]
