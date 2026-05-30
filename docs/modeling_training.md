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

Stage 2 adds config parsing only; full training is implemented in stage 10.

```bash
python -m modeling.cli train \
  --config configs/modeling_default.yaml \
  --metadata artifacts/datasets/metadata_train.json \
  --print-resolved-config
```

Flags:

| Flag | Description |
|------|-------------|
| `--config <path.yaml>` | **Required.** Training YAML path. |
| `--set key=value` | Repeatable dotted-path override; value parsed as a YAML literal (e.g. `--set compute.num_threads=8`, `--set models.lgbm.grids.learning_rate=[0.05,0.1]`). |
| `--metadata <path>` | `metadata_train.json` for truth merge (default: `artifacts/datasets/metadata_train.json`). |
| `--print-resolved-config` | Print merged YAML to stdout and exit with code 0 (no training). |

Without `--print-resolved-config`, the command currently stops with `NotImplementedError("stage 10")`.

## Run id and logs

Canonical run id format (built by `modeling.config.build_run_id()`):

```text
<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>
```

Example: `home_win_lgbm_b334df68_20260530T143022Z`.

Each run writes logs to:

```text
artifacts/reports/<run_id>/run.log
```

Log level comes from `compute.log_level` in the resolved config. Report runs use `configure_run_logger()` in `modeling/report.py` (UTC timestamps); stage 2 also provides a root-logger helper in `modeling/config.py` for early CLI smoke tests — stage 10 wires the report logger for full training runs.

## Subsystem seeds

All randomness flows from YAML `random_seed`. Per-subsystem seeds use :meth:`modeling.config.ResolvedConfig.derive_seed` (TZ one-arg form). The module helper :func:`modeling.config.derive_seed` takes ``(name, random_seed)`` explicitly for pure call sites and tests.

## Isolation from PostgreSQL

Training configuration code must not import `psycopg2` or `modeling.dataset_builder.*`. The `train` CLI subcommand lazy-imports `dataset_builder` only in `build-dataset`, so `--print-resolved-config` and future training do not require PostgreSQL drivers. Training consumes only `dataset_train.csv` + `metadata_train.json` via `modeling/train_input.py`.
