# NHL Modeling Training Configuration

This document describes the **training configuration** contract introduced in modeling stage 2. Training hyperparameters, split settings, model grids, calibration, and evaluation options live in a single YAML file validated by typed Python models in `modeling/config.py`.

Default config: [`configs/modeling_default.yaml`](../configs/modeling_default.yaml).

Training input (CSV + metadata) is documented in [`modeling_dataset_builder.md`](modeling_dataset_builder.md#training-input-contract-stage-1).

## Walk-forward temporal splits (stage 4)

Split geometry is implemented in [`modeling/splits.py`](../modeling/splits.py). Training stages 5–10 consume its output; the splitter does **not** train models or touch PostgreSQL.

### Timeline layout

One outer window `k` (expanding train, fixed val/cal/test tail):

```text
[ train_k ... | inner_val_k | calibration_k | test_k ]    ...    [ holdout ]
```

- **Holdout** is cut **first** from the timeline tail (by day fraction or explicit date range) and is excluded from all walk-forward windows.
- **`train_k`**: all rows strictly before `inner_val_k` (expanding window; train blocks may overlap across `k`).
- **`inner_val_k`**, **`calibration_k`**, **`test_k`**: three consecutive, non-overlapping blocks immediately after `train_k`.
- **Embargo is not used** — rolling features use `shift(1)`; see the module comment in `modeling/splits.py` and UPDATE plan stage 4.

Index fields (`train_idx`, `inner_val_idx`, …) are **positional** indices into keys sorted by `(day, game_id)`.

### Required YAML fields (`split.*`)

| Field | Constraint | Notes |
|-------|------------|-------|
| `split.method` | `"month"` or `"fixed_games"` | Calendar month vs fixed game blocks for `test_k` |
| `split.n_test_windows` | integer **≥ 5** | Outer evaluation windows (bootstrap stage 6 needs ≥ 5) |
| `split.inner_val_games` | integer **≥ 300** | Inner validation / early stopping block |
| `split.calibration_games` | integer **≥ 300** | Calibration block (stage 9) |
| `split.holdout.fraction` | `0 < fraction < 0.5` | **Or** explicit `split.holdout.date_range.from` / `.to` (mutually exclusive with `fraction`) |
| `split.outer_block_games` | required when `method=fixed_games` | Must be ≥ `inner_val_games + calibration_games + 1` |

Default values live in [`configs/modeling_default.yaml`](../configs/modeling_default.yaml).

Example:

```python
from modeling.config import load_config, load_metadata_json
from modeling.splits import build_walk_forward_splits
from modeling.train_input import load_training_table_split

meta = load_metadata_json("artifacts/datasets/metadata_train.json")
cfg = load_config("configs/modeling_default.yaml", metadata=meta)
_, keys, _, _, _ = load_training_table_split(
    "artifacts/datasets/dataset_train.csv",
    "artifacts/datasets/metadata_train.json",
)
splits = build_walk_forward_splits(keys, cfg.split, metadata=meta)
```

Validation errors: `ConfigError` (config / metadata parity), `SplitError` (geometry / insufficient history).

## Metrics and run report format (stage 5)

Offline metric functions live in [`modeling/metrics.py`](../modeling/metrics.py) (`log_loss`, `brier`, `ece`, `reliability_table`, `team_breakdown`, `trivial_baseline`). Report assembly and file output live in [`modeling/report.py`](../modeling/report.py).

Each training run writes:

```text
artifacts/reports/<run_id>/{metrics.json, summary.md, reliability_<task>.png, run.log}
```

Fixed top-level keys in `metrics.json`: `run_id`, `task`, `model`, `features_hash`, `evaluation`, `folds`, `holdout`, `team_breakdown`. Each fold and the holdout block include `raw`, `calibrated` (or `null`), and `trivial_base_rate` (`log_loss`, `brier`, `p` — base rate from **train**, not test). Holdout also includes `reliability_path`.

Run logging for reports uses `configure_run_logger(out_dir, level=...)` in `modeling/report.py` (UTC timestamps in `run.log`). Stage 10 CLI wires this together with `write_report()`.

## Bootstrap confidence intervals (stage 6)

Bootstrap CIs live in [`modeling/bootstrap.py`](../modeling/bootstrap.py). They call [`modeling/metrics.py`](../modeling/metrics.py) (`log_loss`, `brier`) via `metric_fns` so point estimates and resamples use the same formulas and `evaluation.epsilon_clip` clipping as stage 5.

### Percentile 95% CI

For each metric, draw `N = evaluation.bootstrap_samples` bootstrap replicates (default **1000**), compute the metric on each replicate, then:

- `ci_low` = 2.5% quantile of replicates
- `ci_high` = 97.5% quantile of replicates
- `point` = metric on the original `(y_true, y_pred)` (no resample)

v1 uses the **percentile method only** (no BCa, t-bootstrap, or normal approximation).

### Resampling modes

| Split | Mode | `block_by_day` |
|-------|------|----------------|
| Walk-forward `test_k` | i.i.d. resample matches with replacement (`idx = rng.integers(0, n, size=n)`) | `False` (caller responsibility) |
| `holdout` | Block bootstrap by `day`: resample `D` whole days with replacement (`D` = number of unique days); all games from a chosen day stay together | `True` (YAML `evaluation.bootstrap_block_by_day`) |

If `block_by_day=True` but fewer than two unique days exist, bootstrap raises `BootstrapError` (no silent fallback to i.i.d.).

### Reproducibility and threads

- **Seed:** `bootstrap.seed = random_seed` from YAML. One `numpy.random.Generator` per call; independent per-fold or per-metric seeds are forbidden. Global `np.random.seed` is never used.
- **Threads:** `num_threads` defaults to **1** (sequential). When `compute.num_threads > 1`, resamples may run in parallel via `joblib` with `n_jobs=num_threads` only — never `os.cpu_count()` or env vars.

Run metadata fields (also on each `BootstrapResult.to_dict()`): `bootstrap.N`, `bootstrap.block_by_day`, `bootstrap.seed`. Stage 10 CLI copies these into `metrics.json` / artifact `metadata.json`.

**Serialization:** `BootstrapResult.to_dict()` is the API for run logging (dotted `bootstrap.*` keys). `dataclasses.asdict(result)` keeps Python field names (`n_resamples`, `block_by_day`, `seed`) — useful for tests and in-process use, not for the metadata contract.

**Epsilon naming:** YAML `evaluation.epsilon_clip` maps to the `epsilon=` argument of `log_loss()` / `standard_metric_fns(epsilon=...)` in `modeling/metrics.py` (stage 5). Bootstrap does not re-clip probabilities; clipping stays inside the metric callables passed as `metric_fns`.

## LightGBM training (stage 8)

Primary classifiers live in [`modeling/train_lgbm.py`](../modeling/train_lgbm.py). Shared task labels, grid expansion, and monotone-constraint building are in [`modeling/train_common.py`](../modeling/train_common.py).

### Fixed hyperparameters

- `objective='binary'`, `metric='binary_logloss'`
- Early stopping on **`inner_val_k`** only (not test/holdout/calibration)
- `early_stopping_rounds` and `num_boost_round` are function parameters (wired from YAML in stage 10 CLI)
- Full Cartesian product over `models.lgbm.grids.*` (`num_leaves`, `min_data_in_leaf`, `feature_fraction`, `bagging_fraction`, `lambda_l1`, `lambda_l2`, `learning_rate`)
- Best config chosen by minimum `binary_logloss` on `inner_val_k`; tie-break = first grid point in YAML order
- **No Optuna / Bayes search** in v1

### Determinism

All LightGBM seed fields (`seed`, `feature_fraction_seed`, `bagging_seed`, `data_random_seed`, `extra_seed`) are set to YAML `random_seed`. Also: `deterministic=True`, `force_row_wise=True`, `num_threads = compute.num_threads` (must be ≥ 1; never `-1` or `os.cpu_count()`).

### Monotone constraints

Signs are defined in YAML `models.lgbm.monotone.{home_win,over_5_5}` and mapped **by feature name** onto the positional order of `feature_manifest`:

| Task | +1 | −1 |
|------|----|----|
| `home_win` | `diff_goal_diff_roll_mean_*`, `diff_gf_roll_mean_*` | `diff_ga_roll_mean_*` |
| `over_5_5` | `sum_gf_roll_mean_*`, `sum_ga_roll_mean_*` | — |

Patterns use `fnmatch` (exact names or `*` wildcards). Unmatched patterns raise `ConfigError` (fail-fast). Empty monotone spec → train without constraints (logged at INFO).

Raw probabilities are returned without clipping; calibration is stage 9.

## Probability calibration (stage 9)

Post-hoc calibration lives in [`modeling/calibrate.py`](../modeling/calibrate.py). It is **model-family agnostic**: logreg and LGBM both produce raw probabilities on `calibration_k`, and the same calibrator API fits on those scores.

### Two-step protocol (no leakage)

1. **Raw classifier** (stages 7–8) is trained **only on `train_k`**. Stage 9 never retrains it.
2. **Calibrator** is fit **only on `calibration_k`**: pairs `(raw_p_cal, y_cal)` — frozen raw-model probabilities on the calibration block and the corresponding labels.
3. Evaluation on `test_k` and `holdout` uses the chain **raw → calibrator**.

`sklearn.calibration.CalibratedClassifierCV` is **forbidden**: its internal cross-validation does not respect temporal order and cannot accept a `TimeSeriesSplit` for `method='isotonic'`. The project uses an explicit two-step pipeline instead.

### Methods (`calibration.method`)

| Method | Estimator | Notes |
|--------|-----------|-------|
| `isotonic` | `IsotonicRegression(out_of_bounds='clip')` | Monotonic; clips out-of-range inputs |
| `platt` | `LogisticRegression` on **raw probability** `p.reshape(-1, 1)` | Not logit(p) — avoids instability near 0/1 |

Unknown methods raise `CalibrationError` (no silent fallback).

### Small calibration block (`calibration.min_samples`)

When `|calibration_k| < calibration.min_samples` (default **500**):

- `calibration_skipped=true` in metadata;
- calibrator is an identity marker (`apply_calibrator` returns `raw_p` unchanged);
- stage 5/10 report uses raw probabilities with an explicit skip flag.

### Artifacts

Per fold (stage 10 layout):

```text
artifacts/models/<task>/<model>/<run_id>/fold_<k>/
    model_raw.joblib
    calibrator.joblib
    metadata.json
```

`metadata.json` includes `method`, `calibration_skipped`, `n_calibration`, slice date ranges, `seed`, `features_hash`, library versions, and `git_commit`.

### Result format vs logreg (stage 10)

Both trainers return a per-task dataclass with shared fields: `task`, `chosen_inner_val_log_loss`, `n_rows_train`, `n_rows_inner_val`. Grid diagnostics differ because logreg searches a 1-D `C` grid (`FitResult.inner_val_log_loss_by_C: dict[float, float]`) while LGBM searches a multi-dimensional grid (`LgbmFitResult.inner_val_log_loss_by_config: list[tuple[dict, float]]`). Stage 10 CLI must normalise these into a common logging/report shape; no shared dataclass is required at stage 8.

## Dependencies

Offline training uses [`requirements-modeling.txt`](../requirements-modeling.txt):

- **Stage 3 (seven ML packages):** `numpy`, `pandas`, `scikit-learn`, `lightgbm`, `matplotlib`, `pyyaml`, `joblib` — all pinned with `==`.
- **Stage 2 extension:** `pydantic` v2 — not in the stage-3 seven-pack list, but required by [`modeling/config.py`](../modeling/config.py) for YAML config validation; included here so `make modeling-dev` installs the full modeling stack (config + future train code), not bot runtime.

Install after the base venv:

```bash
make modeling-dev
```

These packages are **not** in `requirements.txt`, are **not** installed by `make setup` / `make run-bot`, and are **not** included in the bot Docker image. **CatBoost** is out of scope for v1 (optional stage 15 in the UPDATE plan).

## Required YAML sections

The default YAML and validator require at least:

| Section | Purpose |
|---------|---------|
| `random_seed` | Single source for all subsystem seeds (derived in later stages) |
| `compute.num_threads`, `compute.log_level` | Thread limit and logging level |
| `tasks.home_win.enabled`, `tasks.over_5_5.enabled` | At least one task must be enabled |
| `split.*` | Walk-forward method, window counts, holdout (`fraction` **or** `date_range`, mutually exclusive) |
| `models.logreg.grids.C` | Logistic regression penalty grid |
| `models.lgbm.grids.*` | LightGBM hyperparameter grid |
| `models.lgbm.monotone.*` | Reference monotone signs by feature name (validated here; manifest check in stage 8) |
| `calibration.method`, `calibration.min_samples` | Post-hoc calibration protocol |
| `evaluation.*` | ECE bins, bootstrap settings, probability clip epsilon |

Unknown keys at any modeled level raise `ConfigError` (`extra = forbid`).

## Priority: YAML vs `metadata_train.json`

**Source of truth** for dataset identity fields:

- `feature_set_version`
- `rolling_windows`
- `features_hash`
- `feature_manifest`
- `cold_start_policy_predict`

Rules:

1. Resolved training config **always** takes these values from `metadata_train.json`.
2. YAML may contain matching reference copies for human readability.
3. If a reference field in YAML **differs** from metadata → `ConfigError` with a field-by-field diff (`value_yaml` vs `value_metadata`). No silent overrides.
4. If a reference field is absent in YAML → value from metadata is merged into the resolved config.

Resolution happens in `modeling.config.resolve_config()` / `load_config()`.

## CLI (`train` subcommand)

Stage 10 implements full training orchestration in `modeling/train_runner.py`; `modeling/cli.py` stays a thin argparse wrapper.

```bash
python -m modeling.cli train --config configs/modeling_default.yaml
make modeling-train
```

### Flags

| Flag | Description |
|------|-------------|
| `--config <path.yaml>` | Training YAML (default: `configs/modeling_default.yaml`). |
| `--set key=value` | Repeatable dotted-path override; value parsed as a YAML literal. |
| `--task {home_win,over_5_5,both}` | Tasks to train (default: `both`; respects `tasks.*.enabled`). |
| `--model {logreg,lgbm,both}` | Model families (default: `both`). |
| `--run-id RUN_ID` | Override autogenerated `<run_id>` (tests / single `(task, model)` pair only). **Rejected** when `--task both` and/or `--model both` would produce more than one pair — use autogenerated ids or one pair at a time. |
| `--dry-run` | Load data + build splits; print resolved config and **all block sizes** (`train/inner_val/calibration/test/holdout` per fold and final retrain); no training or artifacts. |
| `--print-resolved-config` | Print merged YAML and exit 0 (no training). |
| `--metadata <path>` | `metadata_train.json` for truth merge (default: `artifacts/datasets/metadata_train.json`). |
| `--dataset <path>` | `dataset_train.csv` (default: `dataset_train.csv` next to `--metadata`). |

`--task home_win` when `tasks.home_win.enabled=false` → `ConfigError` (non-zero exit), not a silent no-op.

Dry-run example:

```bash
python -m modeling.cli train \
  --config configs/modeling_default.yaml \
  --dry-run
```

### Run id format

Canonical run id (`modeling.config.build_run_id()` / `modeling.train_runner.resolve_run_id()`):

```text
<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>
```

Example: `home_win_lgbm_b334df68_20260530T143022Z`.

All `(task, model)` pairs in one CLI invocation share the same UTC timestamp at start; only the `<task>_<model>_` prefix differs so artifacts never overwrite each other.

### Artifact layout

**Models** (walk-forward folds + production bundle):

```text
artifacts/models/<task>/<model>/<run_id>/
    fold_<k>/
        model_raw.joblib
        calibrator.joblib
        metadata.json
    final/
        model.joblib          # model_final (raw classifier)
        calibrator.joblib     # calibrator_final
        metadata.json         # includes train/inner_val/calibration/holdout ranges;
                              # test_days=null, n_rows_test=0 (no walk-forward test block);
                              # library_versions.scikit-learn; bootstrap.{N,block_by_day,seed}; status
```

**Reports**:

```text
artifacts/reports/<run_id>/
    metrics.json
    summary.md
    reliability_<task>.png
    run.log
```

After a successful run (`status: ok`), an atomic symlink update points:

```text
artifacts/models/<task>/<model>/latest  →  <run_id>/final/
```

(relative target inside `artifacts/models/<task>/<model>/`). If symlinks are unsupported, `latest.txt` holds the relative path instead (logged at WARNING).

When `status: failed_baseline_check`, **`latest` is not updated** — production must not reference a model that did not beat the trivial baseline.

### Final retrain before holdout

Holdout is never used for training, calibration, or hyperparameter selection.

1. **`calibration_final`** — last `split.calibration_games` rows in the walk-forward region (immediately before holdout).
2. **`train_full`** — all walk-forward rows strictly before `calibration_final`.
3. **`model_final`** — raw classifier fit on **`train_full`**; hyperparameters chosen on `train_hp` + `inner_val_final` inside `train_full` (same protocol as stages 7–8).
4. **`calibrator_final`** — post-hoc calibrator on `calibration_final` using frozen `model_final` probabilities.
5. Holdout metrics use the chain **`model_final → calibrator_final`** only.

Trivial baseline base rate on holdout is computed from **`train_full`**, not from holdout.

### Baseline gate

On holdout, if **no** trained model family for a task strictly improves calibrated log loss vs `trivial_base_rate`, the task run is marked `status: failed_baseline_check` in `summary.md` and `final/metadata.json`, and the CLI exits non-zero (for CI). When `status: ok`, `latest` is updated as above.

### Full training example

```bash
make modeling-dev
python -m modeling.cli build-dataset --mode train   # separate step; requires DB
make modeling-train
# Inspect:
#   artifacts/reports/<run_id>/summary.md
#   artifacts/reports/<run_id>/reliability_<task>.png
#   artifacts/models/<task>/<model>/latest/
```

Train path isolation: `modeling/cli.py` lazy-imports `dataset_builder` only for `build-dataset`. `train` / `train_runner` load data only via `load_training_table_split()` — no `psycopg2`, no `modeling.dataset_builder.*`.

Each full run writes `artifacts/reports/<run_id>/run.log` at `compute.log_level` via `configure_run_logger()` in `modeling/report.py` (UTC timestamps).

## Subsystem seeds

All randomness in the train CLI path flows from YAML ``random_seed`` passed directly to splits consumers, logreg/LGBM training, calibration (Platt), and bootstrap — no independent or derived seeds on the CLI side.

:meth:`modeling.config.ResolvedConfig.derive_seed` / :func:`modeling.config.derive_seed` remain available for future subsystems or tests; stages 6–10 train orchestration use ``random_seed`` as-is per UPDATE plan §3.

## Isolation from PostgreSQL

Training configuration code must not import `psycopg2` or `modeling.dataset_builder.*`. The `train` CLI subcommand lazy-imports `dataset_builder` only in `build-dataset`, so `--print-resolved-config` and future training do not require PostgreSQL drivers. Training consumes only `dataset_train.csv` + `metadata_train.json` via `modeling/train_input.py`.
