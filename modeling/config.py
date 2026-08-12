"""Typed training configuration loader and resolver.

Stage 2 contract: all training stages read hyperparameters only through
:func:`load_config` / :func:`resolve_config` — never parse YAML ad hoc.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

METADATA_TRUTH_KEYS: tuple[str, ...] = (
    "feature_set_version",
    "rolling_windows",
    "features_hash",
    "feature_manifest",
    "cold_start_policy_predict",
)

_LOG_LEVELS = frozenset(logging._nameToLevel.keys())  # noqa: SLF001 — stdlib mapping of level names


class ConfigError(ValueError):
    """Invalid training config or conflict with ``metadata_train.json``."""


class _ForbidExtraModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SplitMethod(str, Enum):
    month = "month"
    fixed_games = "fixed_games"


class CalibrationMethod(str, Enum):
    isotonic = "isotonic"
    platt = "platt"


class ComputeConfig(_ForbidExtraModel):
    num_threads: int = Field(..., ge=1)
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _LOG_LEVELS:
            allowed = ", ".join(sorted(_LOG_LEVELS))
            raise ValueError(f"log_level must be one of: {allowed}")
        return upper


class TaskToggle(_ForbidExtraModel):
    enabled: bool


class TasksConfig(_ForbidExtraModel):
    home_win: TaskToggle
    over_5_5: TaskToggle

    @model_validator(mode="after")
    def _at_least_one_enabled(self) -> TasksConfig:
        if not self.home_win.enabled and not self.over_5_5.enabled:
            raise ValueError("at least one task must have enabled=true")
        return self


class HoldoutDateRange(_ForbidExtraModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None

    @model_validator(mode="after")
    def _validate_iso_dates(self) -> HoldoutDateRange:
        for key, raw in (("from", self.from_), ("to", self.to)):
            if raw is None:
                continue
            _parse_iso_date(raw)
        return self


class HoldoutConfig(_ForbidExtraModel):
    fraction: Optional[float] = None
    date_range: HoldoutDateRange = Field(default_factory=HoldoutDateRange)

    @model_validator(mode="after")
    def _validate_holdout_mode(self) -> HoldoutConfig:
        has_date = self.date_range.from_ is not None or self.date_range.to is not None
        if self.fraction is not None and has_date:
            raise ValueError(
                "holdout must use exactly one of fraction or date_range; "
                "do not set both fraction and date_range.from/to"
            )
        if self.fraction is not None:
            if not (0.0 < self.fraction < 0.5):
                raise ValueError("holdout.fraction must satisfy 0 < fraction < 0.5")
            return self
        if not has_date:
            raise ValueError("holdout requires fraction or date_range.from/to")
        return self


class SplitConfig(_ForbidExtraModel):
    """Walk-forward split geometry (YAML ``split.*``).

    Drives :func:`modeling.splits.build_walk_forward_splits`: ordered outer
    windows ``k=1..n_test_windows`` with expanding ``train_k``, fixed-size
    ``inner_val_k`` / ``calibration_k``, and ``test_k``; plus a single holdout
    cut from the timeline tail. Positional index arrays are returned in
    chronological order; monotonicity and holdout isolation are validated
    inside the splitter.
    """

    method: SplitMethod
    n_test_windows: int = Field(..., ge=5)
    inner_val_games: int = Field(..., ge=300)
    calibration_games: int = Field(..., ge=300)
    holdout: HoldoutConfig
    outer_block_games: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_outer_block_games(self) -> SplitConfig:
        if self.method == SplitMethod.fixed_games:
            if self.outer_block_games is None:
                raise ValueError("split.outer_block_games is required when method=fixed_games")
            min_block = self.inner_val_games + self.calibration_games + 1
            if self.outer_block_games < min_block:
                raise ValueError(
                    "split.outer_block_games must be >= inner_val_games + calibration_games + 1 "
                    f"(need at least {min_block}, got {self.outer_block_games})"
                )
        elif self.outer_block_games is not None:
            raise ValueError("split.outer_block_games is allowed only when method=fixed_games")
        return self


class LogregGrids(_ForbidExtraModel):
    C: List[float]

    @field_validator("C")
    @classmethod
    def _validate_c_grid(cls, value: List[float]) -> List[float]:
        if not value:
            raise ValueError("models.logreg.grids.C must be non-empty")
        if any(item <= 0 for item in value):
            raise ValueError("models.logreg.grids.C values must be > 0")
        return value


class LogregConfig(_ForbidExtraModel):
    grids: LogregGrids


class LgbmGrids(_ForbidExtraModel):
    num_leaves: List[int]
    min_data_in_leaf: List[int]
    feature_fraction: List[float]
    bagging_fraction: List[float]
    lambda_l1: List[float]
    lambda_l2: List[float]
    learning_rate: List[float]

    @model_validator(mode="after")
    def _non_empty_lists(self) -> LgbmGrids:
        for key in (
            "num_leaves",
            "min_data_in_leaf",
            "feature_fraction",
            "bagging_fraction",
            "lambda_l1",
            "lambda_l2",
            "learning_rate",
        ):
            if not getattr(self, key):
                raise ValueError(f"models.lgbm.grids.{key} must be non-empty")
        return self


class LgbmMonotone(_ForbidExtraModel):
    home_win: Dict[str, int] = Field(default_factory=dict)
    over_5_5: Dict[str, int] = Field(default_factory=dict)

    @staticmethod
    def _validate_signs(mapping: Dict[str, int], task: str) -> Dict[str, int]:
        allowed = {-1, 0, 1}
        for name, sign in mapping.items():
            if not isinstance(name, str):
                raise ValueError(f"models.lgbm.monotone.{task} keys must be strings")
            if sign not in allowed:
                raise ValueError(
                    f"models.lgbm.monotone.{task}[{name!r}] must be one of {{-1, 0, 1}}, got {sign}"
                )
        return mapping

    @field_validator("home_win")
    @classmethod
    def _home_win_signs(cls, value: Dict[str, int]) -> Dict[str, int]:
        return cls._validate_signs(value, "home_win")

    @field_validator("over_5_5")
    @classmethod
    def _over_signs(cls, value: Dict[str, int]) -> Dict[str, int]:
        return cls._validate_signs(value, "over_5_5")


class LgbmConfig(_ForbidExtraModel):
    grids: LgbmGrids
    monotone: LgbmMonotone


class ModelsConfig(_ForbidExtraModel):
    logreg: LogregConfig
    lgbm: LgbmConfig


class CalibrationConfig(_ForbidExtraModel):
    method: CalibrationMethod
    min_samples: int = Field(..., ge=1)


class EvaluationConfig(_ForbidExtraModel):
    ece_bins: int = Field(..., ge=2)
    bootstrap_samples: int = Field(..., ge=1)
    bootstrap_block_by_day: bool
    epsilon_clip: float

    @field_validator("epsilon_clip")
    @classmethod
    def _validate_epsilon(cls, value: float) -> float:
        if not (0.0 < value < 1e-3):
            raise ValueError("evaluation.epsilon_clip must satisfy 0 < epsilon_clip < 1e-3")
        return value


class ModelingConfig(_ForbidExtraModel):
    """YAML training config before metadata merge."""

    random_seed: int
    compute: ComputeConfig
    tasks: TasksConfig
    split: SplitConfig
    models: ModelsConfig
    calibration: CalibrationConfig
    evaluation: EvaluationConfig
    feature_set_version: Optional[str] = None
    rolling_windows: Optional[List[int]] = None
    features_hash: Optional[str] = None
    feature_manifest: Optional[List[Dict[str, Any]]] = None
    cold_start_policy_predict: Optional[str] = None


class ResolvedConfig(ModelingConfig):
    """Fully resolved config after metadata merge."""

    feature_set_version: str
    rolling_windows: List[int]
    features_hash: str
    feature_manifest: List[Dict[str, Any]]
    cold_start_policy_predict: str
    run_id: Optional[str] = None

    def derive_seed(self, name: str) -> int:
        """Derive a subsystem seed from this config's ``random_seed`` (TZ one-arg form)."""
        return derive_seed(name, self.random_seed)


def _parse_iso_date(value: str) -> None:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date {value!r}") from exc


def _load_yaml_mapping(path: Path) -> MutableMapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    return raw


def _deep_copy_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data))


def _set_nested(data: MutableMapping[str, Any], keys: Sequence[str], value: Any) -> None:
    if not keys:
        raise ConfigError("override path must not be empty")
    current: MutableMapping[str, Any] = data
    for key in keys[:-1]:
        nested = current.get(key)
        if nested is None:
            nested = {}
            current[key] = nested
        if not isinstance(nested, MutableMapping):
            raise ConfigError(f"override path {'.'.join(keys)}: {key!r} is not a mapping")
        current = nested  # type: ignore[assignment]
    current[keys[-1]] = value


def apply_overrides(
    config_data: Mapping[str, Any],
    overrides: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
) -> dict[str, Any]:
    """Apply dotted-path overrides to a config mapping."""
    merged = _deep_copy_mapping(config_data)
    if not overrides:
        return merged
    items: Sequence[tuple[str, Any]]
    if isinstance(overrides, Mapping):
        items = list(overrides.items())
    else:
        items = overrides
    for path, value in items:
        _set_nested(merged, path.split("."), value)
    return merged


def parse_override(raw: str) -> tuple[str, Any]:
    """Parse ``key=value`` CLI override; value is a YAML literal."""
    if "=" not in raw:
        raise ConfigError(f"invalid --set {raw!r}, expected key=value")
    key, _, value_str = raw.partition("=")
    key = key.strip()
    if not key:
        raise ConfigError(f"invalid --set {raw!r}, empty key")
    try:
        value = yaml.safe_load(value_str)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML literal in --set {raw!r}: {exc}") from exc
    return key, value


def _validate_modeling_config(data: Mapping[str, Any]) -> ModelingConfig:
    try:
        return ModelingConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc


def _require_metadata_field(metadata: Mapping[str, Any], key: str) -> Any:
    if key not in metadata:
        raise ConfigError(f"metadata missing required field {key!r}")
    return metadata[key]


def _metadata_conflicts(yaml_cfg: ModelingConfig, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    payload = yaml_cfg.model_dump(by_alias=True)
    for key in METADATA_TRUTH_KEYS:
        yaml_value = payload.get(key)
        if yaml_value is None:
            continue
        meta_value = metadata.get(key)
        if meta_value is None:
            continue
        if yaml_value != meta_value:
            diffs.append(
                {
                    "field": key,
                    "value_yaml": yaml_value,
                    "value_metadata": meta_value,
                }
            )
    return diffs


def _format_metadata_diff(diffs: Sequence[Mapping[str, Any]]) -> str:
    lines = ["Config metadata conflict (YAML vs metadata_train.json):"]
    for item in diffs:
        lines.append(
            f"  {item['field']}: value_yaml={item['value_yaml']!r} value_metadata={item['value_metadata']!r}"
        )
    return "\n".join(lines)


def _resolved_metadata_values(metadata: Mapping[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key in METADATA_TRUTH_KEYS:
        value = _require_metadata_field(metadata, key)
        if key == "rolling_windows":
            if not isinstance(value, list) or not value:
                raise ConfigError("metadata['rolling_windows'] must be a non-empty list")
            for item in value:
                if isinstance(item, bool) or not isinstance(item, int):
                    raise ConfigError(f"metadata rolling_windows must contain ints, got {item!r}")
        if key == "feature_manifest":
            if not isinstance(value, list) or not value:
                raise ConfigError("metadata['feature_manifest'] must be a non-empty list")
        if key in {"feature_set_version", "features_hash", "cold_start_policy_predict"}:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"metadata[{key!r}] must be a non-empty string")
        resolved[key] = value
    return resolved


def resolve_config(
    yaml_path: Union[Path, str],
    metadata: Mapping[str, Any],
    overrides: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
) -> ResolvedConfig:
    """Load YAML, apply overrides, validate, merge metadata truth fields."""
    path = Path(yaml_path)
    raw = _load_yaml_mapping(path)
    merged = apply_overrides(raw, overrides)
    yaml_cfg = _validate_modeling_config(merged)

    diffs = _metadata_conflicts(yaml_cfg, metadata)
    if diffs:
        raise ConfigError(_format_metadata_diff(diffs))

    resolved_data = yaml_cfg.model_dump(by_alias=True)
    resolved_data.update(_resolved_metadata_values(metadata))
    try:
        return ResolvedConfig.model_validate(resolved_data)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc


def load_config(
    yaml_path: Union[Path, str],
    overrides: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ResolvedConfig:
    """Public loader used by CLI and tests."""
    if metadata is None:
        raise ConfigError("metadata is required to build a resolved training config")
    return resolve_config(yaml_path, metadata, overrides)


def load_metadata_json(path: Union[Path, str]) -> dict[str, Any]:
    """Load ``metadata_train.json`` for config resolution."""
    meta_path = Path(path)
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {meta_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"metadata root must be an object, got {type(raw).__name__}")
    return raw


def resolved_config_to_yaml(config: ResolvedConfig) -> str:
    """Serialize resolved config as YAML for ``--print-resolved-config``."""
    return yaml.safe_dump(config.model_dump(by_alias=True, mode="json"), sort_keys=False, default_flow_style=False)


def derive_seed(name: str, random_seed: int) -> int:
    """Deterministic subsystem seed derived from ``random_seed`` and ``name``.

    The stage-2 TZ names ``derive_seed(name) -> int``; that one-arg form is
    :meth:`ResolvedConfig.derive_seed`. This module-level helper keeps
    ``random_seed`` explicit for tests and pure call sites (stages 6–8).
    """
    digest = hashlib.sha256(f"{random_seed}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def build_run_id(task: str, model: str, features_hash: str, now_utc: datetime) -> str:
    """Build canonical run id: ``<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>``."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    return f"{task}_{model}_{features_hash[:8]}_{stamp}"


def configure_run_logger(
    run_id: str,
    log_level: str,
    reports_root: Union[Path, str] = "artifacts/reports",
) -> Path:
    """Create ``artifacts/reports/<run_id>/run.log`` and configure the root logger."""
    log_dir = Path(reports_root) / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run.log"

    level_name = log_level.upper()
    level = logging._nameToLevel.get(level_name, logging.INFO)  # noqa: SLF001

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)

    return log_path


__all__ = [
    "CalibrationMethod",
    "ConfigError",
    "METADATA_TRUTH_KEYS",
    "ModelingConfig",
    "ResolvedConfig",
    "SplitMethod",
    "apply_overrides",
    "build_run_id",
    "configure_run_logger",
    "derive_seed",
    "load_config",
    "load_metadata_json",
    "parse_override",
    "resolve_config",
    "resolved_config_to_yaml",
]
