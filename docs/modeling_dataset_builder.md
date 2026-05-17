# NHL Modeling Dataset Builder

## Purpose

This document describes the implemented dataset builder for NHL modeling in two modes:
- `train`: features + labels
- `predict`: the same feature schema without labels

Implementation lives in `modeling/dataset_builder/` and is exposed by CLI:
`python -m modeling.cli build-dataset ...`

**Training (stage 1):** consuming built artifacts for model fitting is documented below under [Training input contract (stage 1)](#training-input-contract-stage-1) (`modeling/train_input.py`).

## Implemented Components

- `modeling/cli.py`
  - CLI entrypoint with `build-dataset` command.
  - Supports mode selection, date and season filters, rolling windows, cold-start policy, and `--validate-only`.

- `modeling/dataset_builder/base.py`
  - End-to-end orchestration of dataset building.
  - Reads data from PostgreSQL (`games`, `game_team_stats`).
  - Builds artifacts:
    - `dataset_train.csv` or `dataset_predict.csv`
    - `metadata_train.json` or `metadata_predict.json`
    - `data_quality_report.json`

- `modeling/dataset_builder/team_game_facts.py`
  - Canonical long layer (team-game rows).
  - Enforces "exactly two teams per game" rule with controlled drop and report logging.
  - Produces symmetric `*_for` and `*_against` fields.

- `modeling/dataset_builder/features.py`
  - Rolling features with mandatory `shift(1)` logic.
  - Context features: `rest_days`, `is_b2b`, `games_last_7d`, `prior_games_count`.
  - As-of snapshots for home/away teams per target game.

- `modeling/dataset_builder/assemble.py`
  - Wide feature assembly: home/away absolute values, `diff_*`, `sum_*`.
  - Train labels:
    - `y_home_win`
    - `y_over_5_5`
  - Cold-start policy:
    - train: drop
    - predict: allow with flag (or drop, from config)

- `modeling/dataset_builder/schema.py`
  - Feature manifest generation (name, dtype, position).
  - `features_hash` calculation.
  - Strict train/predict schema parity check (no silent schema correction).
  - Output column ordering.

- `modeling/dataset_builder/validate.py`
  - Fail-fast quality validation.
  - Leakage checks and consistency checks.
  - Writes `data_quality_report.json`.

- `modeling/train_input.py`
  - Validates `dataset_train.csv` + `metadata_train.json` for training (manifest-ordered **X**, keys/labels/service aligned with `schema.py`).

## Anti-Leakage Contract

The builder enforces:
- `hist_day < target_day` (strictly less)
- no use of current `game_id` as history

Technical enforcement:
- rolling metrics are built with `shift(1)`, excluding current game
- as-of merge uses backward snapshots with exact-day matches disabled
- validation fails if `home_hist_day` or `away_hist_day` is not strictly earlier than target day
- validation fails if `home_hist_game_id == game_id` or `away_hist_game_id == game_id`

## Schema Parity and Versioning

- Predict mode requires `--train-metadata-path` to load train manifest.
- Predict build fails if feature names/order/dtypes do not match train manifest.
- Predict build fails if `feature_set_version` differs from train metadata.
- Predict build fails on explicit `features_hash` mismatch with train metadata.
- Metadata includes:
  - `feature_set_version`
  - `features_hash`
  - `feature_manifest`
  - `rolling_windows`
  - `cold_start_policy_predict`
  - `min_prior_games`
  - `dataset_rows`
  - `code_version`
  - `data_snapshot_id`
  - `dataset_built_at`

## Data Quality and Fail-Fast Checks

Implemented checks:
- uniqueness of `game_id` in final dataset
- required key columns (`game_id`, `day`, `home_team_id`, `away_team_id`) are present and non-null
- NaN/Inf in numeric features
- range checks for `power_play_percentage*` in `[0, 100]`
- non-negative `rest_days` features
- anti-leakage checks (`hist_day` and historical `game_id`)
- forbidden target/label fields in predict mode
- train label contract (`y_home_win`, `y_over_5_5` must exist, be non-null, and binary)

Any violation raises an exception and marks the run as failed.

## CLI Usage

Train:

```bash
python -m modeling.cli build-dataset \
  --mode train \
  --output-dir artifacts/datasets \
  --feature-set-version v1 \
  --rolling-windows 5,10,20 \
  --min-prior-games 5
```

Predict (strict parity against train metadata):

```bash
python -m modeling.cli build-dataset \
  --mode predict \
  --output-dir artifacts/datasets \
  --feature-set-version v1 \
  --rolling-windows 5,10,20 \
  --min-prior-games 5 \
  --train-metadata-path artifacts/datasets/metadata_train.json
```

Validation only (no dataset file write):

```bash
python -m modeling.cli build-dataset --mode train --validate-only
```

## Training input contract (stage 1)

Downstream training code should consume the same artifacts the builder writes:

- `dataset_train.csv` — primary tabular format for v1 training (Parquet is not read by the loader until explicitly supported alongside CSV).
- `metadata_train.json` — required companion file in the same output directory.

**Mandatory metadata keys for the training loader** (must match builder output):

- `feature_manifest` — ordered list of objects with at least `name` and `dtype`; defines the exact columns and order of **X**.
- `features_hash` — must equal `schema.features_hash(...)` recomputed from manifest + rolling/policy/version fields below (fail-fast on mismatch).
- `feature_set_version` — semantic tag; included in `features_hash`.

**Additional keys required so `features_hash` can be validated** (same composition as `dataset_builder/base.py`):

- `rolling_windows`
- `cold_start_policy_predict`

Any inconsistency between the CSV and the manifest (missing/extra feature columns, wrong feature column order for parity checks, dtype mismatch) raises a predictable error (`TrainSchemaError`). Unknown columns outside `KEY_COLUMNS ∪ LABEL_COLUMNS ∪ SERVICE_COLUMNS ∪ manifest names` also fail fast (`TrainSchemaError`). Invalid or incomplete metadata raises `TrainMetadataError`.

The module `modeling/train_input.py` is the single entrypoint for validating that pair and splitting columns **without guessing features by name prefixes**: the feature matrix **X** uses exactly the ordered names from `feature_manifest`, matching `ordered_columns_for_output` semantics from `schema.py`. Keys, labels (`y_home_win`, `y_over_5_5`), and service columns follow `KEY_COLUMNS`, `LABEL_COLUMNS`, and `SERVICE_COLUMNS` in `modeling/dataset_builder/schema.py`.

Public helpers:

- `load_training_table(csv_path, metadata_path)` — fail-fast validation (`features_hash` recomputed like `dataset_builder/base.py`, dtypes checked via `assert_feature_parity`) and returns the full frame.
- `load_training_table_split(...)` — returns `(X, keys, labels, service, metadata)` for pipelines.

Example paths after `build-dataset --mode train --output-dir artifacts/datasets`:

- `artifacts/datasets/dataset_train.csv`
- `artifacts/datasets/metadata_train.json`

Tests: `tests/test_modeling_train_input.py`.

## Tests Added

File: `tests/test_modeling_dataset_build.py`

Implemented tests:
- `test_no_same_game_leakage`
- `test_strict_past_only_by_day`
- `test_train_predict_feature_parity`
- `test_two_team_rows_or_drop`
- `test_cold_start_policy`
- `test_features_hash_detects_cold_start_drift`
- `test_validate_fails_on_nan_keys`

File: `tests/test_modeling_train_input.py`

- Validates `modeling/train_input.py`: manifest-ordered `X`, keys/labels/service split, fail-fast on hash/metadata/schema mismatches.

Local run examples:

```bash
.venv/bin/python -m unittest tests.test_modeling_dataset_build -v
.venv/bin/python -m unittest tests.test_modeling_train_input -v
```

Current result: all tests pass.
