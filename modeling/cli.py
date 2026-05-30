"""CLI for NHL modeling operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from modeling.config import (
    ConfigError,
    load_config,
    load_metadata_json,
    parse_override,
    resolved_config_to_yaml,
)


def _parse_windows(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("rolling windows must not be empty")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NHL modeling CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-dataset", help="Build train or predict dataset")
    build.add_argument("--mode", choices=("train", "predict"), required=True)
    build.add_argument("--output-dir", default="artifacts/datasets")
    build.add_argument("--feature-set-version", default="v1")
    build.add_argument("--rolling-windows", type=_parse_windows, default=[5, 10, 20])
    build.add_argument("--min-prior-games", type=int, default=5)
    build.add_argument(
        "--cold-start-policy-predict",
        choices=("allow_with_flag", "drop"),
        default="allow_with_flag",
    )
    build.add_argument("--min-rows-threshold", type=int, default=0)
    build.add_argument("--season-ids", default="")
    build.add_argument("--target-day-from")
    build.add_argument("--target-day-to")
    build.add_argument("--train-metadata-path")
    build.add_argument("--validate-only", action="store_true")
    build.add_argument(
        "--source-snapshot-id",
        help="Override auto-composed data_snapshot_id (default: from seasons/day range + built_at)",
    )
    build.add_argument(
        "--fail-on-empty-predict",
        action="store_true",
        help="Raise when predict mode finds no games (default: allow empty predict aligned to train manifest)",
    )

    train = sub.add_parser("train", help="Train classifiers (config resolution in stage 2)")
    train.add_argument("--config", required=True, help="Path to training YAML config")
    train.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override config value via dotted path; value parsed as YAML literal",
    )
    train.add_argument(
        "--print-resolved-config",
        action="store_true",
        help="Print merged config to stdout and exit without training",
    )
    train.add_argument(
        "--metadata",
        default="artifacts/datasets/metadata_train.json",
        help="Path to metadata_train.json for metadata truth merge (default: artifacts/datasets/metadata_train.json)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "build-dataset":
        # Lazy import: train path must not require psycopg2 (see modeling/dataset_builder/__init__.py).
        from modeling.dataset_builder import DatasetBuildConfig, build_dataset

        season_ids = [int(item.strip()) for item in args.season_ids.split(",") if item.strip()]
        cfg = DatasetBuildConfig(
            mode=args.mode,
            output_dir=Path(args.output_dir),
            feature_set_version=args.feature_set_version,
            rolling_windows=args.rolling_windows,
            min_prior_games=args.min_prior_games,
            cold_start_policy_predict=args.cold_start_policy_predict,
            min_rows_threshold=args.min_rows_threshold,
            season_ids=season_ids,
            target_day_from=args.target_day_from,
            target_day_to=args.target_day_to,
            train_metadata_path=Path(args.train_metadata_path) if args.train_metadata_path else None,
            validate_only=args.validate_only,
            source_snapshot_id=args.source_snapshot_id,
            allow_empty_predict=not args.fail_on_empty_predict,
        )
        artifacts = build_dataset(cfg)
        for key, path in artifacts.items():
            print(f"{key}: {path}")
        return

    if args.command == "train":
        try:
            overrides = [parse_override(item) for item in args.set]
            metadata = load_metadata_json(args.metadata)
            resolved = load_config(args.config, overrides=overrides, metadata=metadata)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        if args.print_resolved_config:
            print(resolved_config_to_yaml(resolved), end="")
            return

        raise NotImplementedError("stage 10")


if __name__ == "__main__":
    main()
