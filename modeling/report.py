"""Report assembly and serialization for modeling runs (UPDATE plan stage 5).

Writes ``artifacts/reports/<run_id>/{metrics.json, summary.md, reliability_<task>.png, run.log}``.
Metric formulas live in :mod:`modeling.metrics`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

import pandas as pd

from modeling.metrics import DEFAULT_ECE_BINS, DEFAULT_EPSILON

_REPORT_LOGGER_NAME = "modeling.report"
logger = logging.getLogger(_REPORT_LOGGER_NAME)

RUN_ID_PATTERN = re.compile(
    r"^[a-z0-9_]+_(logreg|lgbm)_[0-9a-f]{8}_\d{8}T\d{6}Z$"
)

_PLOT_FIGSIZE = (6.0, 6.0)
_PLOT_DPI = 100
_FOLD_BLOCK_KEYS = frozenset(
    {
        "k",
        "train_range",
        "test_range",
        "n_train",
        "n_test",
        "raw",
        "calibrated",
        "trivial_base_rate",
    }
)
_HOLDOUT_BLOCK_KEYS = _FOLD_BLOCK_KEYS - {"k"} | {"reliability_path"}
_RAW_METRIC_KEYS = frozenset({"log_loss", "brier", "ece"})
_TRIVIAL_BASE_RATE_KEYS = frozenset({"log_loss", "brier", "p"})
_DATE_RANGE_KEYS = frozenset({"start", "end"})


def _sort_dict_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sort_dict_keys(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return [_sort_dict_keys(item) for item in value]
    return value


def _records_to_list(frame: pd.DataFrame | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(frame, pd.DataFrame):
        # Columns are always str here (built from modeling.metrics DataFrames);
        # pandas-stubs types to_dict() keys as Hashable in general.
        return cast("list[dict[str, Any]]", frame.to_dict(orient="records"))
    return [dict(row) for row in frame]


def _validate_date_range(block: Mapping[str, Any], *, block_name: str, key: str) -> None:
    value = block.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{block_name} missing or invalid mapping {key!r}")
    missing = _DATE_RANGE_KEYS - value.keys()
    if missing:
        raise ValueError(f"{block_name}.{key} missing keys: {sorted(missing)}")


def _validate_raw_metrics(raw: Any, *, block_name: str) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{block_name} missing or invalid mapping 'raw'")
    missing = _RAW_METRIC_KEYS - raw.keys()
    if missing:
        raise ValueError(f"{block_name}.raw missing keys: {sorted(missing)}")


def _validate_trivial_base_rate(trivial: Any, *, block_name: str) -> None:
    if not isinstance(trivial, Mapping):
        raise ValueError(f"{block_name} missing or invalid mapping 'trivial_base_rate'")
    missing = _TRIVIAL_BASE_RATE_KEYS - trivial.keys()
    if missing:
        raise ValueError(f"{block_name}.trivial_base_rate missing keys: {sorted(missing)}")


def _validate_eval_block(
    block: Mapping[str, Any],
    *,
    block_name: str,
    required_keys: frozenset[str],
) -> None:
    missing = required_keys - block.keys()
    if missing:
        raise ValueError(f"{block_name} missing required keys: {sorted(missing)}")
    _validate_date_range(block, block_name=block_name, key="train_range")
    _validate_date_range(block, block_name=block_name, key="test_range")
    _validate_raw_metrics(block["raw"], block_name=block_name)
    _validate_trivial_base_rate(block["trivial_base_rate"], block_name=block_name)
    calibrated = block["calibrated"]
    if calibrated is not None and not isinstance(calibrated, Mapping):
        raise ValueError(f"{block_name}.calibrated must be a mapping or null")


def configure_run_logger(out_dir: Path, *, level: str = "INFO") -> logging.Logger:
    """Configure a module logger writing UTC timestamps to ``<out_dir>/run.log``."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    log_path = out_path / "run.log"

    run_logger = logging.getLogger(_REPORT_LOGGER_NAME)
    resolved = log_path.resolve()
    for handler in run_logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == resolved:
            return run_logger

    level_name = level.upper()
    numeric_level = logging._nameToLevel.get(level_name, logging.INFO)  # noqa: SLF001
    run_logger.setLevel(numeric_level)
    run_logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(numeric_level)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    run_logger.addHandler(handler)
    return run_logger


def plot_reliability(
    reliability_df: pd.DataFrame,
    *,
    title: str,
    out_path: Path,
) -> None:
    """Save a reliability diagram PNG from a :func:`modeling.metrics.reliability_table` frame."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_file = Path(out_path)
    df = reliability_df.copy()
    centers = (df["bin_lower"] + df["bin_upper"]) / 2.0
    empty_mask = df["count"].eq(0)
    if empty_mask.any():
        logger.warning("Reliability plot has %d empty bin(s)", int(empty_mask.sum()))

    fig, ax = plt.subplots(figsize=_PLOT_FIGSIZE)
    weights = df["weight"].fillna(0.0).to_numpy()
    marker_sizes = 40.0 + 360.0 * weights

    valid = ~empty_mask
    if valid.any():
        sizes = marker_sizes[valid]
        ax.scatter(
            centers[valid],
            df.loc[valid, "mean_pred"],
            s=sizes,
            marker="o",
            label="mean_pred",
        )
        ax.scatter(
            centers[valid],
            df.loc[valid, "frac_positive"],
            s=sizes,
            marker="s",
            label="frac_positive",
        )

    ax.plot([0.0, 1.0], [0.0, 1.0], linestyle="--", color="gray", label="perfect calibration")
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)
    ax.set_xlabel("Bin center")
    ax.set_ylabel("Probability")
    ax.set_title(title)
    ax.legend(loc="best")
    fig.tight_layout()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_file, dpi=_PLOT_DPI)
    plt.close(fig)


def compose_metrics_json(
    *,
    run_id: str,
    task: str,
    model: str,
    features_hash: str,
    folds: Sequence[Mapping[str, Any]],
    holdout: Mapping[str, Any],
    team_breakdown: Mapping[str, Sequence[Mapping[str, Any]] | pd.DataFrame],
    evaluation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``metrics.json`` payload and validate ``run_id`` format."""
    if not RUN_ID_PATTERN.match(run_id):
        raise ValueError(
            "run_id must match "
            "<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>: "
            f"{run_id!r}"
        )
    if task not in {"home_win", "over_5_5"}:
        raise ValueError(f"unsupported task: {task!r}")
    if model not in {"logreg", "lgbm"}:
        raise ValueError(f"unsupported model: {model!r}")

    for index, fold in enumerate(folds):
        if not isinstance(fold, Mapping):
            raise ValueError(f"folds[{index}] must be a mapping")
        _validate_eval_block(fold, block_name=f"folds[{index}]", required_keys=_FOLD_BLOCK_KEYS)

    if not isinstance(holdout, Mapping):
        raise ValueError("holdout must be a mapping")
    _validate_eval_block(holdout, block_name="holdout", required_keys=_HOLDOUT_BLOCK_KEYS)

    eval_block = dict(evaluation or {"epsilon_clip": DEFAULT_EPSILON, "ece_bins": DEFAULT_ECE_BINS})
    team_block: dict[str, list[dict[str, Any]]] = {}
    for key in ("home_team_id", "away_team_id"):
        rows = team_breakdown.get(key, [])
        team_block[key] = _records_to_list(rows)

    return {
        "run_id": run_id,
        "task": task,
        "model": model,
        "features_hash": features_hash,
        "evaluation": eval_block,
        "folds": list(folds),
        "holdout": dict(holdout),
        "team_breakdown": team_block,
    }


def _format_metric(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _block_metric_row(block: Mapping[str, Any], *, label: str) -> str:
    raw = block.get("raw") or {}
    cal = block.get("calibrated")
    trivial = block.get("trivial_base_rate") or {}
    cal_ll = cal.get("log_loss") if isinstance(cal, Mapping) else None
    return (
        f"| {label} | {block.get('n_test', '—')} | "
        f"{_format_metric(raw.get('log_loss'))} | {_format_metric(raw.get('brier'))} | "
        f"{_format_metric(raw.get('ece'))} | {_format_metric(cal_ll)} | "
        f"{_format_metric(trivial.get('log_loss'))} |"
    )


def _team_rank_lines(
    team_rows: Sequence[Mapping[str, Any]],
    *,
    ascending: bool,
    limit: int = 5,
) -> list[str]:
    if not team_rows:
        return ["_(no teams)_"]
    frame = pd.DataFrame(team_rows)
    if frame.empty or "log_loss_minus_overall" not in frame.columns:
        return ["_(no teams)_"]
    ordered = frame.sort_values("log_loss_minus_overall", ascending=ascending).head(limit)
    lines: list[str] = []
    for _, row in ordered.iterrows():
        lines.append(
            f"- team {row['team_id']}: log_loss={row['log_loss']:.6f}, "
            f"delta={row['log_loss_minus_overall']:+.6f}, n={int(row['n_games'])}"
        )
    return lines


def compose_summary_md(metrics_json: Mapping[str, Any]) -> str:
    """Render human-readable ``summary.md`` from a ``metrics.json`` dict."""
    run_id = metrics_json["run_id"]
    task = metrics_json["task"]
    model = metrics_json["model"]
    lines: list[str] = [
        f"# Run report: {run_id}",
        "",
        f"Task: `{task}` · Model: `{model}`",
        "",
        "## Walk-forward folds",
        "",
        "| k | n_test | log_loss_raw | brier_raw | ece_raw | log_loss_cal | trivial_log_loss |",
        "|---|--------|--------------|-----------|---------|--------------|------------------|",
    ]

    for fold in metrics_json.get("folds", []):
        lines.append(_block_metric_row(fold, label=str(fold.get("k", "?"))))
        trivial = fold.get("trivial_base_rate") or {}
        lines.append(
            f"> Trivial baseline (train base rate p={_format_metric(trivial.get('p'))}): "
            f"log_loss={_format_metric(trivial.get('log_loss'))}, "
            f"brier={_format_metric(trivial.get('brier'))}."
        )

    holdout = metrics_json.get("holdout") or {}
    rel_path = holdout.get("reliability_path", f"reliability_{task}.png")
    lines.extend(
        [
            "",
            "## Holdout",
            "",
            "| block | n_test | log_loss_raw | brier_raw | ece_raw | log_loss_cal | trivial_log_loss |",
            "|-------|--------|--------------|-----------|---------|--------------|------------------|",
            _block_metric_row(holdout, label="holdout"),
            f"> Trivial baseline (train base rate p={_format_metric((holdout.get('trivial_base_rate') or {}).get('p'))}): "
            f"log_loss={_format_metric((holdout.get('trivial_base_rate') or {}).get('log_loss'))}, "
            f"brier={_format_metric((holdout.get('trivial_base_rate') or {}).get('brier'))}.",
            f"> Reliability plot: `{rel_path}`",
            "",
            "## Team breakdown (worst vs best by log_loss_minus_overall)",
            "",
            "### Worst (home_team_id)",
        ]
    )
    home_rows = (metrics_json.get("team_breakdown") or {}).get("home_team_id", [])
    lines.extend(_team_rank_lines(home_rows, ascending=False))
    lines.extend(["", "### Best (home_team_id)"])
    lines.extend(_team_rank_lines(home_rows, ascending=True))
    lines.extend(["", "### Worst (away_team_id)"])
    away_rows = (metrics_json.get("team_breakdown") or {}).get("away_team_id", [])
    lines.extend(_team_rank_lines(away_rows, ascending=False))
    lines.extend(["", "### Best (away_team_id)"])
    lines.extend(_team_rank_lines(away_rows, ascending=True))
    lines.append("")
    return "\n".join(lines)


def write_report(
    out_dir: Path,
    *,
    metrics_json: Mapping[str, Any],
    reliability_pngs: Mapping[str, pd.DataFrame],
    summary_md: str,
) -> None:
    """Write ``metrics.json``, ``summary.md``, and reliability PNGs into ``out_dir``."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    metrics_path = out_path / "metrics.json"
    if metrics_path.exists():
        logger.warning("Overwriting existing file: %s", metrics_path)
    sorted_payload = _sort_dict_keys(dict(metrics_json))
    metrics_path.write_text(
        json.dumps(sorted_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary_path = out_path / "summary.md"
    if summary_path.exists():
        logger.warning("Overwriting existing file: %s", summary_path)
    summary_path.write_text(summary_md, encoding="utf-8")

    for rel_task, rel_df in reliability_pngs.items():
        png_path = out_path / f"reliability_{rel_task}.png"
        if png_path.exists():
            logger.warning("Overwriting existing file: %s", png_path)
        plot_reliability(
            rel_df,
            title=f"Reliability — {rel_task}",
            out_path=png_path,
        )


__all__ = [
    "RUN_ID_PATTERN",
    "compose_metrics_json",
    "compose_summary_md",
    "configure_run_logger",
    "plot_reliability",
    "write_report",
]
