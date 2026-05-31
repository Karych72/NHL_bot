"""Model artifact persistence for prematch classifiers (UPDATE plan stage 7).

Saves and loads a trained sklearn pipeline (``model.joblib``) together with a
rich JSON sidecar (``metadata.json``) that captures everything needed for
audit, feature-parity checks at inference time, and reproducibility.

Designed to be re-used by stage 8 (LightGBM) without modification — the
``model_family`` field in ``metadata.json`` disambiguates families.

Calibrators written by stage 9 are stored in a **separate** sibling file
``calibrator.joblib``; that file is not touched here.

Directory layout written by this module::

    <path_dir>/
        model.joblib      # serialised sklearn pipeline (joblib)
        metadata.json     # JSON sidecar (UTF-8, sorted keys, indent=2)

No database access.  No ``run_id`` generation — the caller (CLI stage 10) owns
that responsibility and passes it via *metadata*.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

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
    "load_model_artifact",
    "save_model_artifact",
]
