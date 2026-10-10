# NHL Modeling Dataset Builder

## Purpose

This document describes the implemented dataset builder for NHL modeling in two modes:
- `train`: features + labels
- `predict`: the same feature schema without labels

Implementation lives in `modeling/dataset_builder/` and is exposed by CLI:
`python -m modeling.cli build-dataset ...`

**Training (stage 1):** consuming built artifacts for model fitting is documented below under [Training input contract (stage 1)](#training-input-contract-stage-1) (`modeling/train_input.py`).

**Training configuration (stage 2):** YAML config, metadata merge, and CLI flags — see [`modeling_training.md`](modeling_training.md).

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
  - `attach_pregame_elo`: attaches the pre-game team Elo rating (`home_elo`, `away_elo`); the
    Elo pass itself is `modeling/elo.py::compute_pregame_elo` — see
    [Elo team-strength feature](#elo-team-strength-feature) below.

- `modeling/dataset_builder/assemble.py`
  - Wide feature assembly: home/away absolute values, `diff_*`, `sum_*`.
  - Train labels:
    - `y_home_win`
    - `y_over_5_5`
  - Cold-start policy:
    - train: drop
    - predict: allow with flag (or drop, from config)
  - Since rolling windows and as-of snapshots reset at each season boundary
    (`features.py`, Task 30), the first game of every season now has
    `prior_games_count == 0` and NaN `*_roll_mean_*`. Train drops these rows via
    cold-start; **predict with the default `allow_with_flag` policy does not** — it
    keeps them and flags `low_history_confidence`, so a predict build for a
    season's first games fails `validate.py`'s NaN check (`numeric_nan_inf`)
    unless `--cold-start-policy-predict drop` is used or these games are handled
    upstream. This is the correct downstream consequence of the season-boundary
    fix, not a defect in it — see Task 15 in `plan/engineering/work_plan_2026-08-08.md`.

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
- rolling windows and as-of snapshots are grouped by `(team_id, season_id)`, not `team_id`
  alone (`features.py::compute_team_rolling_features`, `::_snapshot_side`) — a team's first
  game of a new season starts with an empty window and never snapshots a row from the prior
  season (fixed 2026-09-12, Task 30)

`_snapshot_side` renames *every* remaining joined column to `<home|away>_<name>`, which also
carries through columns that are not model features: `hist_day` / `hist_game_id` (the audit
pair above) and `opponent_team_id` / `home_team_id` / `away_team_id` (identifiers of the
*snapshot's own historical game*, not the current matchup — meaningless as an ordinal/numeric
signal, and prone to a train/predict dtype mismatch: `float64` when some row in the build has
no snapshot yet, `int64` once every row does). All six (`{home,away}_{hist_day,hist_game_id,
opponent_team_id,home_team_id,away_team_id}`) are listed in `feature_schema.AUDIT_COLUMNS` and
excluded from `feature_columns_from_df` — never part of `feature_manifest` / `features_hash` /
X, and dropped by `ordered_columns_for_output` before the CSV is written (fixed Задача 15,
first real-data train/predict run — see `plan/engineering/work_plan_2026-08-08.md`).

## `games_train.csv`

`build-dataset --mode train` writes, next to `dataset_train.csv`, `games_train.csv`: **every**
played game of the loaded seasons — including the games the cold-start policy dropped from the
dataset — with columns `game_id, day, season_id, home_team_id, away_team_id, home_goals,
away_goals, decision` (`decision` ∈ `REG`/`OT`/`SO`, from `games.is_overtime`/`is_shootouts`).
It is taken from the same `load_target_games(..., with_decision=True)` history that feeds the
Elo pass (no extra query). `train` reads it for the Elo benchmark (`modeling/elo.py`) and fails
loudly when `home_win` is trained without it. It is not part of `metadata_train.json` and not
of the feature table.

## Elo team-strength feature

Added Задача 40 (Task 3; tuned via a read-only diagnostic spike, 40a, before any code
was written — see the tuning and evidence notes below).
Unlike every other feature above, Elo is **cross-team and cross-season**: it is a single
`{team_id: rating}` state that a full pass over the played-game history mutates game by game
in `(day, game_id)` order (`modeling/elo.py::compute_pregame_elo`, attached to every target game by
`features.py::attach_pregame_elo`). `base.py` gets that history by calling `load_target_games`
itself with `mode="train"`/`season_ids=[]`/`target_day_from=None` (not a near-duplicate query)
— unfiltered by `--season-ids`/`--target-day-from`.

- **Formula** (Задача 66: the core moved to `modeling/elo.py` and takes an `EloParams`; the
  defaults `k=8, home_advantage=35, season_regression=1/3, mov=True, ot_win_weight=1.0` are the
  constants below, so `diff_elo` and `features_hash` did not change. The same core serves the
  Elo benchmark of the model gate with other parameters — see
  [`modeling_training.md`](modeling_training.md) §2a; `ELO_START`, `ELO_MOV_BASE`,
  `ELO_MOV_HFA_WEIGHT` stay constants):
  - new team starts at `ELO_START = 1500`.
  - expectancy `E_home = 1 / (1 + 10^(-(R_home + HFA - R_away)/400))`, `HFA = 35`.
  - update `R_home += K·M·(S - E_home)`, `R_away -= K·M·(S - E_home)`, `K = 8`.
  - `S = 1` if `winner_id == home_team_id` else `0` — OT/SO wins count as full wins
    (tuning picked `ot_s = 1`; `ot_win_weight` < 1 exists only for the benchmark, where a
    winner of an OT/SO game is credited `S = ot_win_weight`).
  - margin-of-victory multiplier `M = ln(|goal_diff|+1) · 2.2 / (0.001·winner_edge + 2.2)`,
    where `winner_edge` is the winning side's rating edge including HFA.
  - at a `season_id` change: every rating shrinks 1/3 toward the field mean
    (`R ← mean + (1 - 1/3)·(R - mean)`) instead of resetting — Elo is meant to persist
    across seasons, unlike the rolling/as-of features above.
- **Tuning:** grid search over `K ∈ {4, 6, 8, 10, 12, 16}`, `HFA ∈ {0, 20, 35, 50}`, season
  regression `∈ {0, 1/4, 1/3, 1/2}`, MOV on/off, and OT/SO-win weight `∈ {1, 0.75}`, scored by
  raw-Elo log-loss on games from 2022-10-01 up to (not including) the 2025-11-20 holdout
  start — i.e. only pre-holdout games; the holdout itself was never touched during tuning.
  The chosen point (`K=8, HFA=35`, season regression `1/3`, MOV on, OT/SO wins scored as full
  wins) was not a sharp optimum: the top 8 settings in that search were all within 0.0004
  log-loss of each other.
- **Evidence it carries signal:** per-season raw-Elo log-loss vs. that season's prior-rate
  constant — 2022-23: 0.6612 vs 0.6924 (Δ −0.0312), 2023-24: 0.6611 vs 0.6900 (Δ −0.0289),
  2024-25: 0.6670 vs 0.6870 (Δ −0.0200). Elo beats the constant by 0.020–0.031 log-loss in
  every one of the three seasons before 2025-26. In 2025-26 itself the gap nearly closes
  (0.6897 vs 0.6929, Δ −0.0032): that season's home-win rate (0.522) sits well off the
  training-era rate (0.541) and the season plays unusually close to a coin flip, so most of
  Elo's usual edge over a constant disappears there — a property of that season, not of the
  feature.
- **Columns:** `home_elo`, `away_elo` (raw ratings, no HFA baked in) and `diff_elo =
  home_elo - away_elo`, produced by the same `assemble.py::_wide_feature_columns` convention as
  every other `diff_*`/`sum_*` pair, except `sum_elo` is intentionally not produced (it is
  ≈2×the post-regression mean rating and carries no team-strength signal). This is a lighter
  special case than `goals_target`'s: `goals_target` is skipped before any `diff_`/`sum_`
  column is built for it, while `elo` still gets its `diff_elo` — only the `sum_elo` step is
  skipped.
- **As-of / no leakage:** a game's `home_elo`/`away_elo` are the ratings held strictly *before*
  that game — recorded before the update, mirroring `_snapshot_side`'s backward as-of join.
  Games sharing a calendar day never see each other's result: ratings are frozen at the start
  of the day and every game that day is scored off that same snapshot, with all of that day's
  updates applied together afterwards — the Elo analogue of `compute_team_rolling_features`'s
  `intra_day_prev` policy for rolling features (a same-day predecessor is masked out, not used).
- **Predict mode:** target games come from the `scheduled_games` table (upcoming games written
  by the loader on every run, Task 22A, only those whose both teams already exist in `teams`
  for the season; `games` only ever holds played games), and have no
  result, so they are never part of the play-order pass.
  They get the rating "as of after the last played game" — the final `{team_id: rating}` state
  once the whole history has been processed, with no season-transition regression applied for a
  season that has not actually been played into yet (`attach_pregame_elo`'s fallback path). This
  is different from every other feature in this document, which use a per-game as-of join.
- **No NaN, no cold-start row drop:** an unseen team defaults to `ELO_START`, so `home_elo`/
  `away_elo`/`diff_elo` are always populated — they are not subject to `apply_cold_start_policy`
  the way `*_roll_mean_*` is.
- **`games` columns used:** `game_id`, `day`, `season_id`, `home_team_id`, `away_team_id`,
  `winner_id`; goals for the MOV multiplier come from `game_team_stats` (`game_id`, `team_id`,
  `goals`).
- Bumped `feature_set_version` default `v1 → v2` (CLI and `DatasetBuildConfig`) — any feature
  change is supposed to bump it (`plan/deprecated_plan/nhl_dataset_build_plan.md` §5.3); this
  also changes `features_hash`, as intended.

## Schema Parity and Versioning

- Predict mode requires `--train-metadata-path` to load train manifest.
- Predict build fails if feature names/order/dtypes do not match train manifest.
- Predict build fails if `feature_set_version` differs from train metadata.
- Predict build fails on explicit `features_hash` mismatch with train metadata.
- A predict feature column's pandas dtype can legitimately differ from train's for a reason
  that has nothing to do with a real schema drift: dtype is a property of the *whole*
  target-game set assembled before cold-start filtering (any first-of-season row with no
  snapshot yet upcasts a column to `float64` for the entire train build), and a much smaller
  predict build can easily contain no such row, keeping pandas' narrower `int64` inference.
  `base.py::_align_predict_to_manifest` casts every predict feature column already present in
  the frame to the train manifest's recorded dtype before `assert_feature_parity` runs — for
  *any* row count, not only the empty-predict case it originally covered (fixed Задача 15,
  first real-data predict build). A manifest column genuinely **missing** from the predict
  frame is left uncast, so `assert_feature_parity` still rejects it loudly.
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

Train (multi-season — always pass `--season-ids` explicitly; an empty value builds over
every season in the DB and writes `seasons=all` into `data_snapshot_id`, so which seasons
actually went in cannot be recovered from the artifact afterwards):

```bash
python -m modeling.cli build-dataset \
  --mode train \
  --output-dir artifacts/datasets \
  --feature-set-version v2 \
  --rolling-windows 5,10,20 \
  --min-prior-games 5 \
  --season-ids 20212022,20222023,20232024,20242025,20252026
```

Predict (strict parity against train metadata):

```bash
python -m modeling.cli build-dataset \
  --mode predict \
  --output-dir artifacts/datasets \
  --feature-set-version v2 \
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
- `test_rolling_and_snapshot_reset_at_season_boundary` — rolling windows and as-of
  snapshots do not cross a season boundary (Task 30)
- `test_zero_pp_opportunities_null_percentage_becomes_zero`,
  `test_power_play_percentage_above_100_is_clipped` — `power_play_percentage` NULL
  (0 PP opportunities) and >100 (rare PBP opportunity-count artifact) are normalized to
  valid `[0, 100]` feature values instead of failing the dataset builder's fail-fast checks
- `tests/test_modeling_elo.py` (Задача 66): the default `EloParams` reproduce the pre-move
  feature values, OT-weight / MOV / per-day freeze / season shrink mechanics, benchmark leakage.
- `TestGamesTrainCsv` / `TestLoadTargetGamesSource.test_decision_column_only_when_requested`
  (Задача 66): `games_train.csv` content and that `decision` never reaches the feature table.
- `TestPregameElo` (Задача 40, Task 3): `test_hand_computed_pregame_ratings` (3-game
  mini example, expected ratings computed independently from the formula in "Elo
  team-strength feature" above), `test_asof_future_result_does_not_change_earlier_rating`,
  `test_same_day_games_do_not_see_each_others_result` (intra-day policy),
  `test_season_boundary_regresses_toward_mean`,
  `test_predict_mode_uses_rating_after_last_played_game_no_extra_regression`,
  `test_unseen_team_defaults_to_start_rating`, `test_missing_result_fails_fast`,
  `test_diff_elo_built_by_assemble_without_sum_elo`

File: `tests/test_modeling_train_input.py`

- Validates `modeling/train_input.py`: manifest-ordered `X`, keys/labels/service split, fail-fast on hash/metadata/schema mismatches.

Local run examples:

```bash
.venv/bin/python -m unittest tests.test_modeling_dataset_build -v
.venv/bin/python -m unittest tests.test_modeling_train_input -v
```

Current result: all tests pass.
