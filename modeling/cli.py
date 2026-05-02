"""CLI for NHL modeling operations."""

from __future__ import annotations

import argparse
from pathlib import Path

from modeling.dataset_builder import DatasetBuildConfig, build_dataset


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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "build-dataset":
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
        )
        artifacts = build_dataset(cfg)
        for key, path in artifacts.items():
            print(f"{key}: {path}")


if __name__ == "__main__":
    main()
