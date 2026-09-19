"""Model artifact persistence for prematch classifiers (UPDATE plan stage 7).

Saves and loads a trained sklearn pipeline (``model.joblib``) together with a
rich JSON sidecar (``metadata.json``) that captures everything needed for
audit, feature-parity checks at inference time, and reproducibility.

Designed to be re-used by stage 8 (LightGBM) without modification — the
``model_family`` field in ``metadata.json`` disambiguates families.

Calibrators written by stage 9 are stored in a **separate** sibling file
``calibrator.joblib``; that file is not touched by ``save_model_artifact`` /
``load_model_artifact`` above, but *is* read by :func:`load_latest_calibrator`
below.

Directory layout written by this module::

    <path_dir>/
        model.joblib      # serialised sklearn pipeline (joblib)
        metadata.json     # JSON sidecar (UTF-8, sorted keys, indent=2)

No database access.  No ``run_id`` generation — the caller (CLI stage 10) owns
that responsibility and passes it via *metadata*.

**Inference-side additions (Задача 15):** :func:`resolve_latest_model_dir`,
:func:`load_latest_calibrator`, and :func:`check_features_hash_match` support
``modeling/predict_runner.py`` loading the ``latest`` trained artifact (symlink
or ``latest.txt`` fallback) and refusing to score with a stale one.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

import joblib

logger = logging.getLogger(__name__)

_MODEL_FILENAME = "model.joblib"
_METADATA_FILENAME = "metadata.json"


# ---------------------------------------------------------------------------
# Git commit helper
# ---------------------------------------------------------------------------


def _get_git_commit(repo_root: Path | None = None) -> str | None:
    """Return the current HEAD commit hash, or ``None`` on any failure.

    Wrapped in a broad try/except so missing ``.git`` directories, absent
    ``git`` binary, or non-zero exit codes all silently return ``None``.

    Args:
        repo_root: Directory to run ``git rev-parse HEAD`` in.  Defaults to the
            directory three levels up from this file (project root).
    """
    try:
        cwd = repo_root or Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:  # noqa: BLE001 — intentionally broad; any failure → null
        pass
    return None


# ---------------------------------------------------------------------------
# Library-version helper
# ---------------------------------------------------------------------------


def _collect_library_versions() -> dict[str, str]:
    """Return actual ``__version__`` strings for key modeling libraries."""
    import importlib

    versions: dict[str, str] = {}
    for pkg, import_name in [
        ("scikit-learn", "sklearn"),
        ("numpy", "numpy"),
        ("pandas", "pandas"),
        ("joblib", "joblib"),
        ("lightgbm", "lightgbm"),
    ]:
        try:
            mod = importlib.import_module(import_name)
            versions[pkg] = getattr(mod, "__version__", "unknown")
        except Exception:  # noqa: BLE001
            versions[pkg] = "unknown"
    return versions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_model_artifact(
    path_dir: Path | str,
    *,
    model: Any,
    metadata: dict[str, Any],
) -> None:
    """Serialise *model* and *metadata* into *path_dir*.

    Creates *path_dir* (and any missing parents) if it does not already exist.
    The caller is responsible for providing all required metadata fields; this
    function augments *metadata* with library versions and ``git_commit`` if
    they are not already present, then writes ``model.joblib`` and
    ``metadata.json``.

    **Augmented fields** (added only when absent from the caller-supplied dict):
    - ``library_versions``: ``{scikit-learn, numpy, pandas, joblib}`` versions.
    - ``git_commit``: HEAD SHA or ``null``.

    Args:
        path_dir: Target directory.  Created automatically.
        model: Any sklearn-compatible estimator (Pipeline, LGBMClassifier, …).
        metadata: Caller-supplied dict with fields listed in UPDATE plan §12.4.
            ``run_id`` is optional; ``null`` is acceptable if CLI has not yet
            assigned one.

    Raises:
        OSError: If the directory cannot be created or files cannot be written.
    """
    out_dir = Path(path_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model_path = out_dir / _MODEL_FILENAME
    joblib.dump(model, model_path)
    logger.info("Saved model → %s", model_path)

    full_metadata = dict(metadata)
    if "library_versions" not in full_metadata:
        full_metadata["library_versions"] = _collect_library_versions()
    if "git_commit" not in full_metadata:
        full_metadata["git_commit"] = _get_git_commit()

    meta_path = out_dir / _METADATA_FILENAME
    meta_path.write_text(
        json.dumps(full_metadata, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    logger.info("Saved metadata → %s", meta_path)


def load_model_artifact(path_dir: Path | str) -> tuple[Any, dict[str, Any]]:
    """Load model and metadata from *path_dir*.

    Args:
        path_dir: Directory previously written by :func:`save_model_artifact`.

    Returns:
        ``(model, metadata)`` where *metadata* is the parsed JSON object.

    Raises:
        FileNotFoundError: If ``model.joblib`` or ``metadata.json`` is missing.
        json.JSONDecodeError: If ``metadata.json`` is not valid JSON.
    """
    in_dir = Path(path_dir)
    model_path = in_dir / _MODEL_FILENAME
    meta_path = in_dir / _METADATA_FILENAME

    if not model_path.exists():
        raise FileNotFoundError(f"model.joblib not found in {in_dir}")
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {in_dir}")

    model = joblib.load(model_path)
    metadata: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    logger.info("Loaded model artifact from %s", in_dir)
    return model, metadata


# ---------------------------------------------------------------------------
# Inference-side loading (Задача 15): resolve ``latest``, load calibrator,
# compare features_hash against a predict dataset.
# ---------------------------------------------------------------------------


def resolve_latest_model_dir(artifacts_root: Path | str, task: str, model: str) -> Path:
    """Resolve ``artifacts/models/<task>/<model>/latest`` to its ``<run_id>/final/`` directory.

    Handles both forms written by ``train_runner.update_latest_symlink``
    (``modeling/train_runner.py:579-621``): a real symlink, or the
    ``latest.txt`` fallback used on filesystems without symlink support.

    Args:
        artifacts_root: Root directory containing ``models/<task>/<model>/latest``.
        task: ``"home_win"`` or ``"over_5_5"``.
        model: ``"logreg"`` or ``"lgbm"``.

    Returns:
        Absolute path to the resolved ``final/`` directory.

    Raises:
        FileNotFoundError: Neither a ``latest`` symlink nor ``latest.txt`` exists
            (no run with ``status: ok`` has been produced yet for this pair).
    """
    base = Path(artifacts_root) / "models" / task / model
    link_path = base / "latest"
    if link_path.is_symlink():
        return (base / os.readlink(link_path)).resolve()
    txt_path = base / "latest.txt"
    if txt_path.is_file():
        rel_target = txt_path.read_text(encoding="utf-8").strip()
        return (base / rel_target).resolve()
    raise FileNotFoundError(
        f"no 'latest' pointer for {task}/{model} under {base} "
        "(expected a symlink or latest.txt; train a model with status=ok first)"
    )


def load_latest_calibrator(path_dir: Path | str) -> Any:
    """Load the raw calibrator estimator saved alongside a ``final/`` model bundle.

    ``train_runner.run_training`` writes this file directly with
    ``joblib.dump(final_calibrator.calibrator, final_dir / "calibrator.joblib")``
    (``modeling/train_runner.py:975``) — a plain estimator, not the
    ``model_raw.joblib`` triple that :func:`modeling.calibrate.load_calibration_artifact`
    expects for fold artifacts, so that loader does not apply here.

    Args:
        path_dir: A ``final/`` directory, e.g. from :func:`resolve_latest_model_dir`.

    Raises:
        FileNotFoundError: If ``calibrator.joblib`` is missing.
    """
    calibrator_path = Path(path_dir) / "calibrator.joblib"
    if not calibrator_path.exists():
        raise FileNotFoundError(f"calibrator.joblib not found in {path_dir}")
    return joblib.load(calibrator_path)


def check_features_hash_match(
    model_metadata: Mapping[str, Any],
    predict_metadata: Mapping[str, Any],
) -> None:
    """Fail loudly when the loaded model and predict dataset disagree on ``features_hash``.

    This is an inference-time guard, distinct from the predict-vs-train-manifest
    check already performed at dataset-build time
    (``modeling/dataset_builder/base.py:323-329``): that one checks the predict
    build against the *train* metadata it was built with; this one checks the
    *currently loaded model* against the *current* predict dataset, catching a
    ``latest`` artifact that has gone stale relative to it.

    Args:
        model_metadata: Parsed ``metadata.json`` from the loaded model artifact.
        predict_metadata: Parsed ``metadata_predict.json`` from the predict dataset.

    Raises:
        ValueError: If the two ``features_hash`` values differ, with both values
            in the message.
    """
    model_hash = model_metadata.get("features_hash")
    predict_hash = predict_metadata.get("features_hash")
    if model_hash != predict_hash:
        raise ValueError(
            "features_hash mismatch between loaded model and predict dataset: "
            f"model={model_hash!r}, predict={predict_hash!r}"
        )


def build_logreg_metadata(
    *,
    task: str,
    chosen_C: float,
    class_weight: object,
    random_seed: int,
    features_hash: str,
    feature_set_version: str,
    feature_manifest: list[dict[str, Any]],
    n_rows_train: int,
    n_rows_inner_val: int,
        train_days: tuple[str, str] | None = None,
        inner_val_days: tuple[str, str] | None = None,
        calibration_days: tuple[str, str] | None = None,
        run_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the required ``metadata.json`` fields for a logreg artifact.

    This is a convenience builder used by CLI stage 10.  All fields match the
    schema in UPDATE plan §12.4.  Library versions and ``git_commit`` are
    added automatically by :func:`save_model_artifact`.

    Args:
        task: ``"home_win"`` or ``"over_5_5"``.
        chosen_C: The C value selected by inner-val log-loss minimisation.
        class_weight: Value passed to LogisticRegression (``None`` or ``"balanced"``).
        random_seed: Integer seed used during training.
        features_hash: SHA-256 fingerprint from ``metadata_train.json``.
        feature_set_version: Semantic feature-set tag.
        feature_manifest: Ordered list of ``{"name", "dtype", "position"}`` dicts.
        n_rows_train: Number of training rows.
        n_rows_inner_val: Number of inner-validation rows.
        train_days: Optional ``(min_day, max_day)`` ISO-date strings for the
            training block.  If ``None``, the key ``train_days`` will be
            **absent** from the returned dict and therefore from
            ``metadata.json``.  UPDATE plan §7 lists date ranges as a minimum
            requirement; callers (CLI stage 10) must supply this argument to
            satisfy the spec.
        inner_val_days: Optional ``(min_day, max_day)`` for the inner-validation
            block.  Same absence semantics as *train_days*.
        calibration_days: Optional ``(min_day, max_day)`` for the calibration
            block.  Written by CLI stage 10 after calibration (stage 9); absent
            from logreg-only artifacts.
        run_id: Identifier assigned by the CLI; ``None`` if not yet available.

    Returns:
        Dict ready to pass as *metadata* to :func:`save_model_artifact`.
    """
    meta: dict[str, Any] = {
        "model_family": "logreg",
        "task": task,
        "chosen_C": chosen_C,
        "class_weight": class_weight,
        "random_seed": random_seed,
        "features_hash": features_hash,
        "feature_set_version": feature_set_version,
        "feature_manifest": feature_manifest,
        "n_rows_train": n_rows_train,
        "n_rows_inner_val": n_rows_inner_val,
        "run_id": run_id,
    }
    if train_days is not None:
        meta["train_days"] = {"min": train_days[0], "max": train_days[1]}
    if inner_val_days is not None:
        meta["inner_val_days"] = {"min": inner_val_days[0], "max": inner_val_days[1]}
    if calibration_days is not None:
        meta["calibration_days"] = {"min": calibration_days[0], "max": calibration_days[1]}
    return meta


def build_lgbm_metadata(
    *,
    task: str,
    chosen_params: dict[str, Any],
    best_iteration: int,
    random_seed: int,
    features_hash: str,
    feature_set_version: str,
    feature_manifest: list[dict[str, Any]],
    n_rows_train: int,
    n_rows_inner_val: int,
    train_days: tuple[str, str] | None = None,
    inner_val_days: tuple[str, str] | None = None,
    calibration_days: tuple[str, str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the required ``metadata.json`` fields for an LGBM artifact."""
    meta: dict[str, Any] = {
        "model_family": "lgbm",
        "task": task,
        "chosen_params": chosen_params,
        "best_iteration": best_iteration,
        "random_seed": random_seed,
        "features_hash": features_hash,
        "feature_set_version": feature_set_version,
        "feature_manifest": feature_manifest,
        "n_rows_train": n_rows_train,
        "n_rows_inner_val": n_rows_inner_val,
        "run_id": run_id,
    }
    if train_days is not None:
        meta["train_days"] = {"min": train_days[0], "max": train_days[1]}
    if inner_val_days is not None:
        meta["inner_val_days"] = {"min": inner_val_days[0], "max": inner_val_days[1]}
    if calibration_days is not None:
        meta["calibration_days"] = {"min": calibration_days[0], "max": calibration_days[1]}
    return meta


__all__ = [
    "build_lgbm_metadata",
    "build_logreg_metadata",
    "check_features_hash_match",
    "load_latest_calibrator",
    "load_model_artifact",
    "resolve_latest_model_dir",
    "save_model_artifact",
]
