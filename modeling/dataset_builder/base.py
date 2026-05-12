"""Dataset builder orchestration for train/predict modes."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd
import psycopg2

from .assemble import assemble_dataset
from .features import build_match_feature_snapshots, compute_team_rolling_features
from .schema import (
    assert_feature_parity,
    build_feature_manifest,
    feature_columns_from_df,
    features_hash,
    ordered_columns_for_output,
)
from .team_game_facts import build_team_game_facts
from .validate import validate_or_raise, write_report


@dataclass
class DatasetBuildConfig:
    mode: str
    output_dir: Path
    feature_set_version: str = "v1"
    rolling_windows: Sequence[int] = (5, 10, 20)
    min_prior_games: int = 5
    cold_start_policy_predict: str = "allow_with_flag"
    min_rows_threshold: int = 0
    season_ids: Sequence[int] = field(default_factory=list)
    target_day_from: Optional[str] = None
    target_day_to: Optional[str] = None
    train_metadata_path: Optional[Path] = None
    validate_only: bool = False
    source_snapshot_id: Optional[str] = None
    allow_empty_predict: bool = True


def _git_commit_hash() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return "unknown"


def _season_where_clause(config: DatasetBuildConfig) -> str:
    if not config.season_ids:
        return ""
    ids = ",".join(str(int(v)) for v in config.season_ids)
    return f" AND g.season_id IN ({ids})"


def _day_where_clause(config: DatasetBuildConfig) -> str:
    if config.target_day_from and config.target_day_to:
        return f" AND g.day BETWEEN '{config.target_day_from}' AND '{config.target_day_to}'"
    if config.target_day_from:
        return f" AND g.day >= '{config.target_day_from}'"
    if config.target_day_to:
        return f" AND g.day <= '{config.target_day_to}'"
    return ""


def load_target_games(conn, config: DatasetBuildConfig) -> pd.DataFrame:
    mode_predicate = "g.winner_id IS NOT NULL" if config.mode == "train" else "g.winner_id IS NULL"
    query = f"""
        SELECT
            g.game_id::bigint AS game_id,
            g.day::date AS day,
            g.season_id::bigint AS season_id,
            g.home_team_id::bigint AS home_team_id,
            g.away_team_id::bigint AS away_team_id,
            g.winner_id::bigint AS winner_id,
            hs.goals::double precision AS home_goals_target,
            aws.goals::double precision AS away_goals_target
        FROM games g
        LEFT JOIN game_team_stats hs
            ON hs.game_id = g.game_id
           AND hs.team_id::bigint = g.home_team_id::bigint
        LEFT JOIN game_team_stats aws
            ON aws.game_id = g.game_id
           AND aws.team_id::bigint = g.away_team_id::bigint
        WHERE {mode_predicate}
          AND g.day IS NOT NULL
          AND g.home_team_id IS NOT NULL
          AND g.away_team_id IS NOT NULL
          AND g.home_team_id <> g.away_team_id
          {_season_where_clause(config)}
          {_day_where_clause(config)}
        ORDER BY g.day, g.game_id
    """
    return pd.read_sql_query(query, conn)


def load_history_team_stats(conn, config: DatasetBuildConfig) -> pd.DataFrame:
    max_day_clause = ""
    if config.target_day_to:
        max_day_clause = f" AND g.day <= '{config.target_day_to}'"
    query = f"""
        SELECT
            g.game_id::bigint AS game_id,
            g.day::date AS day,
            g.season_id::bigint AS season_id,
            g.home_team_id::bigint AS home_team_id,
            g.away_team_id::bigint AS away_team_id,
            s.team_id::bigint AS team_id,
            s.goals::double precision AS goals,
            s.shots::double precision AS shots,
            s.pim::double precision AS pim,
            s.power_play_percentage::double precision AS power_play_percentage,
            s.power_play_goals::double precision AS power_play_goals,
            s.power_play_opportunities::double precision AS power_play_opportunities,
            s.face_off_win_percentage::double precision AS face_off_win_percentage,
            s.blocked::double precision AS blocked,
            s.takeaways::double precision AS takeaways,
            s.giveaways::double precision AS giveaways,
            s.hits::double precision AS hits
        FROM game_team_stats s
        JOIN games g ON g.game_id = s.game_id
        WHERE g.winner_id IS NOT NULL
          AND g.day IS NOT NULL
          {_season_where_clause(config)}
          {max_day_clause}
        ORDER BY g.day, g.game_id, s.team_id
    """
    return pd.read_sql_query(query, conn)


def _load_train_metadata(path: Path) -> Dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    manifest = data.get("feature_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Invalid train metadata: feature_manifest missing")
    feature_hash = data.get("features_hash")
    if not isinstance(feature_hash, str) or not feature_hash:
        raise ValueError("Invalid train metadata: features_hash missing")
    return data


def _compose_data_snapshot_id(config: DatasetBuildConfig, built_at_iso: str) -> str:
    parts = ["db:games+game_team_stats"]
    if config.season_ids:
        parts.append("seasons=" + ",".join(str(int(s)) for s in config.season_ids))
    else:
        parts.append("seasons=all")
    if config.target_day_from:
        parts.append("day_from=" + config.target_day_from)
    if config.target_day_to:
        parts.append("day_to=" + config.target_day_to)
    parts.append("mode=" + config.mode)
    parts.append("built_at=" + built_at_iso)
    return "|".join(parts)


def _metadata_dict(
    config: DatasetBuildConfig,
    feature_manifest: List[Dict[str, str]],
    feature_hash: str,
    dataset_rows: int,
    data_snapshot_id: str,
) -> Dict[str, object]:
    return {
        "mode": config.mode,
        "feature_set_version": config.feature_set_version,
        "features_hash": feature_hash,
        "feature_manifest": feature_manifest,
        "rolling_windows": list(config.rolling_windows),
        "cold_start_policy_predict": config.cold_start_policy_predict,
        "min_prior_games": config.min_prior_games,
        "dataset_rows": dataset_rows,
        "code_version": _git_commit_hash(),
        "data_snapshot_id": data_snapshot_id,
        "dataset_built_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }


def _empty_series_for_dtype(dtype_name: str) -> pd.Series:
    normalized = dtype_name.lower()
    if normalized.startswith("int"):
        return pd.Series(dtype="int64")
    if normalized.startswith("float"):
        return pd.Series(dtype="float64")
    if normalized.startswith("datetime64"):
        return pd.Series(dtype="datetime64[ns]")
    if normalized in {"bool", "boolean"}:
        return pd.Series(dtype="bool")
    return pd.Series(dtype="object")


def _align_empty_predict_to_manifest(
    assembled: pd.DataFrame,
    train_manifest: Sequence[Dict[str, str]],
) -> pd.DataFrame:
    if not assembled.empty:
        return assembled
    out = assembled.copy()
    expected_features = [item["name"] for item in train_manifest]
    for item in train_manifest:
        name = item["name"]
        dtype = item["dtype"]
        if name not in out.columns:
            out[name] = _empty_series_for_dtype(dtype)
        else:
            out[name] = out[name].astype(dtype)
    extra_features = [col for col in feature_columns_from_df(out) if col not in expected_features]
    if extra_features:
        out = out.drop(columns=extra_features)
    key_cols = [col for col in ("game_id", "day", "season_id", "home_team_id", "away_team_id") if col in out.columns]
    service_cols = [col for col in out.columns if col not in key_cols and col not in expected_features]
    out = out[key_cols + expected_features + service_cols]
    return out


def _connect_from_env():
    telegram_bot_dir = Path(__file__).resolve().parents[2] / "telegram_bot"
    if str(telegram_bot_dir) not in os.sys.path:
        os.sys.path.insert(0, str(telegram_bot_dir))
    import config  # noqa: WPS433

    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        user=config.PG_USER,
        database=config.PG_DATABASE,
    )


def build_dataset(config: DatasetBuildConfig) -> Dict[str, Path]:
    if config.mode not in {"train", "predict"}:
        raise ValueError("mode must be train|predict")

    built_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    data_snapshot_id = config.source_snapshot_id or _compose_data_snapshot_id(config, built_at)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = config.output_dir / f"dataset_{config.mode}.csv"
    metadata_path = config.output_dir / f"metadata_{config.mode}.json"
    quality_path = config.output_dir / "data_quality_report.json"

    if config.mode == "predict" and config.train_metadata_path is None:
        raise ValueError("predict mode requires --train-metadata-path for strict schema parity")
    train_metadata: Optional[Dict[str, object]] = None
    train_manifest: Optional[List[Dict[str, str]]] = None
    if config.mode == "predict" and config.train_metadata_path is not None:
        train_metadata = _load_train_metadata(config.train_metadata_path)
        manifest = train_metadata.get("feature_manifest")
        if not isinstance(manifest, list):
            raise ValueError("Invalid train metadata: feature_manifest missing")
        train_manifest = manifest
        train_feature_set_version = train_metadata.get("feature_set_version")
        if (
            isinstance(train_feature_set_version, str)
            and train_feature_set_version
            and train_feature_set_version != config.feature_set_version
        ):
            raise ValueError(
                "feature_set_version mismatch: "
                f"train={train_feature_set_version}, predict={config.feature_set_version}"
            )

    with _connect_from_env() as conn:
        target_games = load_target_games(conn, config)
        history = load_history_team_stats(conn, config)

    team_facts, team_facts_report = build_team_game_facts(history)
    rolling = compute_team_rolling_features(team_facts, config.rolling_windows)
    snapshots = build_match_feature_snapshots(target_games, rolling)
    assembled, assemble_report = assemble_dataset(
        mode=config.mode,
        snapshots=snapshots,
        min_prior_games=config.min_prior_games,
        cold_start_policy_predict=config.cold_start_policy_predict,
    )

    assembled["dataset_built_at"] = built_at
    assembled["feature_set_version"] = config.feature_set_version
    if config.mode == "train":
        assembled["source_snapshot_id"] = data_snapshot_id

    if config.mode == "predict" and train_manifest is not None:
        if assembled.empty and not config.allow_empty_predict:
            raise ValueError(
                "predict dataset is empty (no target games). "
                "Pass allow_empty_predict=True when this is expected, or widen day/season filters."
            )
        assembled = _align_empty_predict_to_manifest(assembled, train_manifest)
        assert_feature_parity(assembled, train_manifest)

    if config.mode == "predict" and train_manifest is not None:
        feature_cols = [item["name"] for item in train_manifest]
    else:
        feature_cols = feature_columns_from_df(assembled)

    quality_report = validate_or_raise(
        mode=config.mode,
        df=assembled,
        feature_columns=feature_cols,
        min_rows_threshold=config.min_rows_threshold,
    )
    quality_report["team_game_facts"] = team_facts_report
    quality_report["assemble"] = assemble_report

    if config.mode == "train":
        feature_manifest = build_feature_manifest(assembled, feature_cols)
    else:
        feature_manifest = train_manifest or build_feature_manifest(assembled, feature_cols)
    feature_hash = features_hash(
        feature_manifest=feature_manifest,
        rolling_windows=config.rolling_windows,
        cold_start_policy=f"train:drop|predict:{config.cold_start_policy_predict}",
        feature_set_version=config.feature_set_version,
    )
    if config.mode == "predict" and train_metadata is not None:
        train_feature_hash = train_metadata.get("features_hash")
        if train_feature_hash != feature_hash:
            raise ValueError(
                "features_hash mismatch: "
                f"train={train_feature_hash}, predict={feature_hash}"
            )
    metadata = _metadata_dict(config, feature_manifest, feature_hash, len(assembled), data_snapshot_id)

    ordered = ordered_columns_for_output(config.mode, assembled, train_manifest=feature_manifest)
    assembled = assembled[ordered].copy()

    if not config.validate_only:
        assembled.to_csv(dataset_path, index=False)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8")
    write_report(quality_path, quality_report)

    return {
        "dataset_path": dataset_path,
        "metadata_path": metadata_path,
        "quality_report_path": quality_path,
    }
