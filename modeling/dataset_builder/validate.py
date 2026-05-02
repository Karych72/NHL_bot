"""Fail-fast data quality and anti-leakage validation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd


def _as_plain(value):
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def build_quality_report(
    mode: str,
    df: pd.DataFrame,
    feature_columns: Iterable[str],
    min_rows_threshold: int,
) -> Dict[str, object]:
    checks: List[Dict[str, object]] = []
    errors: List[str] = []

    required_key_columns = ["game_id", "day", "home_team_id", "away_team_id"]
    for col in required_key_columns:
        if col not in df.columns:
            errors.append(f"missing required key column: {col}")
            continue
        if df[col].isna().any():
            errors.append(f"NaN in key column: {col}")
    if "home_team_id" in df.columns and "away_team_id" in df.columns:
        same_team = (df["home_team_id"] == df["away_team_id"]).fillna(False)
        if same_team.any():
            errors.append("home_team_id must not equal away_team_id")

    if df.empty and mode == "predict":
        checks.append({"name": "non_empty_predict", "status": "ok", "note": "no games for prediction"})
    elif len(df) <= min_rows_threshold:
        errors.append(f"row_count={len(df)} <= min_rows_threshold={min_rows_threshold}")

    if "game_id" in df.columns:
        unique_game_ids = int(df["game_id"].nunique())
        if unique_game_ids != len(df):
            errors.append("game_id must be unique")
        checks.append({"name": "unique_game_id", "status": "ok" if unique_game_ids == len(df) else "failed"})

    numeric_features = [col for col in feature_columns if col in df.columns and pd.api.types.is_numeric_dtype(df[col])]
    if numeric_features:
        nan_cols = [col for col in numeric_features if df[col].isna().any()]
        inf_cols = [
            col
            for col in numeric_features
            if df[col].dropna().map(lambda v: math.isinf(float(v))).any()
        ]
        if nan_cols:
            errors.append(f"NaN in numeric features: {nan_cols}")
        if inf_cols:
            errors.append(f"Infinity in numeric features: {inf_cols}")
        checks.append({"name": "numeric_nan_inf", "status": "ok" if not nan_cols and not inf_cols else "failed"})

    if any(col.endswith("rest_days") for col in df.columns):
        rest_cols = [
            col
            for col in df.columns
            if col.endswith("rest_days") and (col.startswith("home_") or col.startswith("away_"))
        ]
        for col in rest_cols:
            if (df[col].fillna(0) < 0).any():
                errors.append(f"{col} has negative values")

    for col in [
        c
        for c in df.columns
        if "power_play_percentage" in c and (c.startswith("home_") or c.startswith("away_"))
    ]:
        valid = df[col].dropna().between(0, 100).all()
        if not valid:
            errors.append(f"{col} out of [0,100] range")

    leakage_columns = {
        "home": ("home_hist_day", "home_hist_game_id"),
        "away": ("away_hist_day", "away_hist_game_id"),
    }
    if "day" in df.columns:
        target_day = pd.to_datetime(df["day"])
        for side, (hist_day_col, hist_game_col) in leakage_columns.items():
            if hist_day_col in df.columns:
                hist_day = pd.to_datetime(df[hist_day_col], errors="coerce")
                mask = hist_day.notna() & (hist_day >= target_day)
                if mask.any():
                    errors.append(f"anti-leakage violated: {side} hist_day must be < target day")
            if hist_game_col in df.columns and "game_id" in df.columns:
                hist_game = pd.to_numeric(df[hist_game_col], errors="coerce")
                game_id = pd.to_numeric(df["game_id"], errors="coerce")
                same_game = hist_game.notna() & game_id.notna() & (hist_game == game_id)
                if same_game.any():
                    errors.append(f"anti-leakage violated: {side} hist_game_id == target game_id")

    if mode == "predict":
        forbidden = [col for col in ("winner_id", "y_home_win", "y_over_5_5") if col in df.columns]
        if forbidden:
            errors.append(f"predict contains forbidden columns: {forbidden}")
    else:
        for label in ("y_home_win", "y_over_5_5"):
            if label not in df.columns:
                errors.append(f"train missing label column: {label}")
                continue
            if df[label].isna().any():
                errors.append(f"NaN in label column: {label}")
            values = set(pd.to_numeric(df[label], errors="coerce").dropna().astype("int64").tolist())
            if not values.issubset({0, 1}):
                errors.append(f"{label} must be binary 0/1")

    checks.append({"name": "fail_fast", "status": "failed" if errors else "ok"})
    report = {
        "mode": mode,
        "row_count": int(len(df)),
        "checks": checks,
        "errors": errors,
        "sample_game_ids": [_as_plain(v) for v in df["game_id"].head(5).tolist()] if "game_id" in df.columns else [],
        "status": "FAILED" if errors else "OK",
    }
    return report


def validate_or_raise(
    mode: str,
    df: pd.DataFrame,
    feature_columns: Iterable[str],
    min_rows_threshold: int = 0,
) -> Dict[str, object]:
    report = build_quality_report(
        mode=mode,
        df=df,
        feature_columns=feature_columns,
        min_rows_threshold=min_rows_threshold,
    )
    if report["errors"]:
        raise ValueError(json.dumps(report, ensure_ascii=True, indent=2))
    return report


def write_report(path: Path, report: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
