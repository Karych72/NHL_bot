"""Load train dataset artifacts for modeling (CSV + metadata).

Training input contract (v1)
==============================

Required artifacts (same directory after ``python -m modeling.cli build-dataset``):

- ``dataset_train.csv`` — tabular training rows (primary format for v1).
- ``metadata_train.json`` — JSON written by the dataset builder.

Parquet is **not** supported here until symmetric read paths are added and this
contract is extended.

JSON fields required by :func:`load_training_table`
--------------------------------------------------
The loader treats these keys as authoritative:

- ``feature_manifest`` — ordered list of ``{\"name\", \"dtype\", \"position\"}``
  entries; defines **exactly** which columns form the feature matrix ``X``
  (names and order). Must match the builder output.
- ``features_hash`` — SHA-256 fingerprint from ``schema.features_hash``; must
  match the tuple ``(manifest, rolling_windows, cold_start_policy string,
  feature_set_version)`` stored alongside it (fail-fast if recomputation disagrees).
- ``feature_set_version`` — semantic feature-set tag included in ``features_hash``.
- ``rolling_windows`` and ``cold_start_policy_predict`` — required to recompute
  ``features_hash`` the same way as ``dataset_builder/base.py``.

Semantics of splits
-------------------
- **X** — built **only** from ``feature_manifest`` column names, in manifest order.
  No prefix-based guessing and no \"all other columns\" fallback when the manifest
  lists the full feature set.
- **Keys** — columns intersecting ``schema.KEY_COLUMNS`` that exist in the table,
  ordered as in ``KEY_COLUMNS``.
- **Labels** — columns intersecting ``schema.LABEL_COLUMNS`` that exist (typically
  both ``y_home_win`` and ``y_over_5_5`` for train).
- **Service** — columns present in the frame that belong to ``schema.SERVICE_COLUMNS``.

Any column not in ``KEY_COLUMNS ∪ LABEL_COLUMNS ∪ SERVICE_COLUMNS ∪ manifest names``
raises ``TrainSchemaError`` (fail-fast).

After those checks we call ``schema.assert_feature_parity``. It was introduced in the
predict build path (the parameter there is ``predict_df``), but its rule applies to **any**
wide frame: ordered non-(key / label / service) columns must match the manifest names and dtypes.
Training CSV loads use the same check; wording of some exceptions inside ``schema.py`` still says
``Predict`` only for historical reasons.
"""


from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Sequence

import pandas as pd

from modeling.dataset_builder.schema import (
    KEY_COLUMNS,
    LABEL_COLUMNS,
    SERVICE_COLUMNS,
    assert_feature_parity,
    features_hash,
)


class TrainMetadataError(ValueError):
    """Raised when ``metadata_train.json`` is missing contract fields or fails checks."""


class TrainSchemaError(ValueError):
    """Raised when the CSV disagrees with the manifest or contains forbidden columns."""


def _read_json_metadata(path: Path) -> MutableMapping[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrainMetadataError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TrainMetadataError(f"metadata root must be an object, got {type(raw).__name__}")
    return raw


def _require_str(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise TrainMetadataError(f"missing or invalid metadata[{key!r}] (non-empty string required)")
    return value


def _require_manifest(metadata: Mapping[str, Any]) -> list:
    manifest = metadata.get("feature_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise TrainMetadataError("missing or invalid metadata['feature_manifest'] (non-empty list required)")
    for i, item in enumerate(manifest):
        if not isinstance(item, dict):
            raise TrainMetadataError(f"feature_manifest[{i}] must be an object, got {type(item).__name__}")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise TrainMetadataError(f"feature_manifest[{i}] requires non-empty string field 'name'")
        dtype = item.get("dtype")
        if not isinstance(dtype, str) or not dtype:
            raise TrainMetadataError(f"feature_manifest[{i}] requires non-empty string field 'dtype'")
    return manifest


def _require_rolling_windows(metadata: Mapping[str, Any]) -> Sequence[int]:
    windows = metadata.get("rolling_windows")
    if not isinstance(windows, list) or not windows:
        raise TrainMetadataError("missing or invalid metadata['rolling_windows'] (non-empty list required)")
    out: list[int] = []
    for item in windows:
        if isinstance(item, bool) or not isinstance(item, int):
            raise TrainMetadataError(f"rolling_windows must contain ints, got {item!r}")
        out.append(item)
    return out


def _verify_metadata_features_hash(metadata: Mapping[str, Any]) -> None:
    manifest = _require_manifest(metadata)
    rolling = _require_rolling_windows(metadata)
    cold_predict = _require_str(metadata, "cold_start_policy_predict")
    fsv = _require_str(metadata, "feature_set_version")
    stored_hash = _require_str(metadata, "features_hash")

    cold_policy = f"train:drop|predict:{cold_predict}"
    recomputed = features_hash(
        feature_manifest=manifest,
        rolling_windows=rolling,
        cold_start_policy=cold_policy,
        feature_set_version=fsv,
    )
    if recomputed != stored_hash:
        raise TrainMetadataError(
            "features_hash mismatch: metadata JSON is inconsistent "
            f"(stored={stored_hash!r}, recomputed={recomputed!r})"
        )


def _validate_training_metadata(metadata: Mapping[str, Any]) -> None:
    _require_manifest(metadata)
    _require_str(metadata, "features_hash")
    _require_str(metadata, "feature_set_version")
    _verify_metadata_features_hash(metadata)


def _allowed_column_union(feature_names: Sequence[str]) -> set[str]:
    return set(KEY_COLUMNS) | set(LABEL_COLUMNS) | set(SERVICE_COLUMNS) | set(feature_names)


def _load_training_csv_validated(csv_path: Path, metadata: Mapping[str, Any]) -> pd.DataFrame:
    manifest = _require_manifest(metadata)
    feature_names = [item["name"] for item in manifest]

    df = pd.read_csv(csv_path)
    unknown = sorted(set(df.columns) - _allowed_column_union(feature_names))
    if unknown:
        raise TrainSchemaError(
            "unexpected columns not covered by KEY_COLUMNS, LABEL_COLUMNS, "
            f"SERVICE_COLUMNS, or feature_manifest: {unknown}"
        )

    missing_features = [name for name in feature_names if name not in df.columns]
    if missing_features:
        raise TrainSchemaError(f"missing manifest feature columns in CSV: {missing_features}")

    # See module docstring: parity helper is named for predict-vs-train in the builder, but its
    # rule is manifest order + dtypes for feature columns vs any wide frame — valid for train CSV.
    try:
        assert_feature_parity(df, manifest)
    except ValueError as exc:
        raise TrainSchemaError(str(exc)) from exc

    return df


def _prepare_training_frame(csv_path: Path, meta_path: Path) -> tuple[pd.DataFrame, Dict[str, Any]]:
    metadata = dict(_read_json_metadata(meta_path))
    _validate_training_metadata(metadata)
    df = _load_training_csv_validated(csv_path, metadata)
    return df, metadata


def load_training_table(
    dataset_csv_path: Path | str,
    metadata_json_path: Path | str,
) -> pd.DataFrame:
    """Load a train CSV and validate against ``metadata_train.json``.

    Args:
        dataset_csv_path: Path to ``dataset_train.csv`` (v1: CSV only).
        metadata_json_path: Path to ``metadata_train.json``.

    Returns:
        Full DataFrame including keys, features, labels (if present), and service columns.

    Raises:
        TrainMetadataError: Missing/invalid metadata, inconsistent ``features_hash``, invalid JSON.
        TrainSchemaError: CSV columns disagree with manifest (parity, extras, missing features).

    Use :func:`split_training_frame` or :func:`load_training_table_split` for matrix ``X``
    strictly aligned with ``feature_manifest``.
    """
    csv_path = Path(dataset_csv_path)
    meta_path = Path(metadata_json_path)
    df, _ = _prepare_training_frame(csv_path, meta_path)
    return df


def split_training_frame(
    df: pd.DataFrame,
    metadata: Mapping[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split a validated train frame into ``X``, keys, labels, and service columns.

    Args:
        df: DataFrame already validated against ``metadata`` (e.g. from :func:`load_training_table`).
        metadata: Training metadata dict containing ``feature_manifest``.

    Returns:
        Tuple ``(X, keys, labels, service)`` — ``X`` columns match manifest names **in manifest order**.
        Keys follow ``KEY_COLUMNS``, labels ``LABEL_COLUMNS``, service ``SERVICE_COLUMNS`` (presence only).

    Raises:
        TrainMetadataError: Invalid ``feature_manifest`` structure.
        TrainSchemaError: Columns in ``df`` are not covered by the contract union.
    """
    manifest = _require_manifest(metadata)

    feature_names = [item["name"] for item in manifest]
    allowed = _allowed_column_union(feature_names)
    unknown = sorted(set(df.columns) - allowed)
    if unknown:
        raise TrainSchemaError(f"unexpected columns before split: {unknown}")

    X = df[feature_names].copy()

    key_cols = [c for c in KEY_COLUMNS if c in df.columns]
    label_cols = [c for c in LABEL_COLUMNS if c in df.columns]
    service_cols = [c for c in SERVICE_COLUMNS if c in df.columns]

    keys_df = df[key_cols].copy() if key_cols else pd.DataFrame(index=df.index)
    labels_df = df[label_cols].copy() if label_cols else pd.DataFrame(index=df.index)
    service_df = df[service_cols].copy() if service_cols else pd.DataFrame(index=df.index)

    return X, keys_df, labels_df, service_df


def load_training_table_split(
    dataset_csv_path: Path | str,
    metadata_json_path: Path | str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """Load train CSV + metadata and return split frames plus metadata dict.

    Args:
        dataset_csv_path: Path to ``dataset_train.csv``.
        metadata_json_path: Path to ``metadata_train.json``.

    Returns:
        ``(X, keys, labels, service, metadata)`` where ``metadata`` is the parsed JSON object.

    Raises:
        TrainMetadataError: Metadata contract / hash / JSON errors.
        TrainSchemaError: Table vs manifest mismatch.

    Single-call facade for training pipelines (stage 2+).
    """
    csv_path = Path(dataset_csv_path)
    meta_path = Path(metadata_json_path)
    df, metadata = _prepare_training_frame(csv_path, meta_path)
    X, keys_df, labels_df, service_df = split_training_frame(df, metadata)
    return X, keys_df, labels_df, service_df, metadata


__all__ = [
    "TrainMetadataError",
    "TrainSchemaError",
    "load_training_table",
    "split_training_frame",
    "load_training_table_split",
]
