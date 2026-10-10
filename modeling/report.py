"""Report assembly and serialization for modeling runs (UPDATE plan stage 5, Задача 66).

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
_SEASON_BLOCK_KEYS = frozenset({"season_id", "n_test", "test_range", "model_raw", "model", "constant"})
_POOLED_BLOCK_KEYS = frozenset({"n_test", "model_raw", "model", "constant", "diff_ci", "bootstrap", "reliability_path"})
_METRIC_KEYS = frozenset({"log_loss", "brier", "ece"})
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


def _validate_metrics(metrics: Any, *, block_name: str, key: str) -> None:
    if not isinstance(metrics, Mapping):
        raise ValueError(f"{block_name} missing or invalid mapping {key!r}")
    missing = _METRIC_KEYS - metrics.keys()
    if missing:
        raise ValueError(f"{block_name}.{key} missing keys: {sorted(missing)}")


def _validate_eval_block(
    block: Mapping[str, Any],
    *,
    block_name: str,
    required_keys: frozenset[str],
) -> None:
    missing = required_keys - block.keys()
    if missing:
        raise ValueError(f"{block_name} missing required keys: {sorted(missing)}")
    for key in ("model_raw", "model", "constant"):
        _validate_metrics(block[key], block_name=block_name, key=key)
    test_range = block.get("test_range")
    if test_range is not None and _DATE_RANGE_KEYS - test_range.keys():
        raise ValueError(f"{block_name}.test_range missing keys: {sorted(_DATE_RANGE_KEYS)}")


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
    seasons: Sequence[Mapping[str, Any]],
    pooled: Mapping[str, Any],
    calibration_table: Mapping[str, Sequence[Mapping[str, Any]]],
    slices: Mapping[str, Mapping[str, Any]],
    team_breakdown: Mapping[str, Sequence[Mapping[str, Any]] | pd.DataFrame],
    evaluation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``metrics.json`` payload and validate ``run_id`` format and block shapes.

    ``seasons`` has one block per checked season, ``pooled`` the same metrics over all of
    them plus ``diff_ci`` / ``bootstrap`` / ``reliability_path``; ``calibration_table`` maps
    ``model`` (and ``elo`` for ``home_win``) to 5 pp bins; ``slices`` maps slice name to
    ``n`` and log-losses. Each metrics mapping carries ``log_loss``, ``brier``, ``ece``.
    """
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
    if not seasons:
        raise ValueError("seasons must not be empty")

    for index, season in enumerate(seasons):
        _validate_eval_block(season, block_name=f"seasons[{index}]", required_keys=_SEASON_BLOCK_KEYS)
    _validate_eval_block(pooled, block_name="pooled", required_keys=_POOLED_BLOCK_KEYS)

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
        "seasons": [dict(season) for season in seasons],
        "pooled": dict(pooled),
        "calibration_table": {name: list(rows) for name, rows in calibration_table.items()},
        "slices": {name: dict(block) for name, block in slices.items()},
        "team_breakdown": team_block,
    }


def _format_metric(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _log_loss_of(block: Mapping[str, Any], key: str) -> Any:
    metrics = block.get(key)
    return metrics.get("log_loss") if isinstance(metrics, Mapping) else None


def _eval_row(block: Mapping[str, Any], *, label: str) -> str:
    model = block.get("model") or {}
    return (
        f"| {label} | {block.get('n_test', '—')} | "
        f"{_format_metric(_log_loss_of(block, 'model_raw'))} | {_format_metric(model.get('log_loss'))} | "
        f"{_format_metric(model.get('brier'))} | {_format_metric(model.get('ece'))} | "
        f"{_format_metric(_log_loss_of(block, 'constant'))} | "
        f"{_format_metric(_log_loss_of(block, 'elo_raw'))} | {_format_metric(_log_loss_of(block, 'elo'))} |"
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


def _calibration_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["| bin | n | mean_pred | frac_positive |", "|-----|---|-----------|---------------|"]
    for row in rows:
        lines.append(
            f"| {row['bin_lower']:.2f}–{row['bin_upper']:.2f} | {row['count']} | "
            f"{_format_metric(row['mean_pred'])} | {_format_metric(row['frac_positive'])} |"
        )
    return lines


def compose_summary_md(metrics_json: Mapping[str, Any]) -> str:
    """Render human-readable ``summary.md`` from a ``metrics.json`` dict."""
    run_id = metrics_json["run_id"]
    task = metrics_json["task"]
    model = metrics_json["model"]
    pooled = metrics_json["pooled"]
    lines: list[str] = [
        f"# Run report: {run_id}",
        "",
        f"Task: `{task}` · Model: `{model}`",
        "",
        "## Checked seasons (log_loss; model_cal = calibrated model)",
        "",
        "| season | n_test | model_raw | model_cal | brier_cal | ece_cal | constant | elo_raw | elo |",
        "|--------|--------|-----------|-----------|-----------|---------|----------|---------|-----|",
    ]
    for season in metrics_json["seasons"]:
        lines.append(_eval_row(season, label=str(season["season_id"])))
    lines.append(_eval_row(pooled, label="sum"))
    lines.extend(
        [
            "",
            f"> Reliability plot (sum of seasons): `{pooled['reliability_path']}`",
            "",
            "## Log-loss differences, sum of seasons (negative = model better; paired block bootstrap by game day)",
            "",
            "| comparison | point | ci_low | ci_high |",
            "|------------|-------|--------|---------|",
        ]
    )
    for name, ci in pooled["diff_ci"].items():
        lines.append(
            f"| {name} | {_format_metric(ci['point'])} | {_format_metric(ci['ci_low'])} | "
            f"{_format_metric(ci['ci_high'])} |"
        )
    for name, rows in (metrics_json.get("calibration_table") or {}).items():
        lines.extend(["", f"## Calibration ({name}, 5 pp bins, sum of seasons)", ""])
        lines.extend(_calibration_lines(rows))
    lines.extend(
        [
            "",
            "## Slices (sum of seasons)",
            "",
            "| slice | n | model_cal | elo | constant |",
            "|-------|---|-----------|-----|----------|",
        ]
    )
    for name, block in (metrics_json.get("slices") or {}).items():
        lines.append(
            f"| {name} | {block['n']} | {_format_metric(block.get('model_log_loss'))} | "
            f"{_format_metric(block.get('elo_log_loss'))} | {_format_metric(block.get('constant_log_loss'))} |"
        )
    lines.extend(["", "## Team breakdown (worst vs best by log_loss_minus_overall)", "", "### Worst (home_team_id)"])
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
