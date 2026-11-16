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
``modeling/predict_runner.py`` loading the ``latest`` trained artifact (``latest`` symlink) and refusing to score with a stale one.
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
    """Return the current HEAD commit hash, or ``None`` when git cannot answer.

    A missing ``git`` binary, a timeout or a non-zero exit code (no ``.git`` in the
    Docker image) return ``None``; the metadata contract allows ``git_commit: null``.

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
    except (OSError, subprocess.TimeoutExpired):  # no git binary / hung git → null (contract)
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
        versions[pkg] = importlib.import_module(import_name).__version__
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

    Reads the symlink written by ``train_runner.update_latest_symlink``.

    Args:
        artifacts_root: Root directory containing ``models/<task>/<model>/latest``.
        task: ``"home_win"`` or ``"over_5_5"``.
        model: ``"logreg"`` or ``"lgbm"``.

    Returns:
        Absolute path to the resolved ``final/`` directory.

    Raises:
        FileNotFoundError: No ``latest`` symlink exists
            (no run with ``status: ok`` has been produced yet for this pair).
    """
    base = Path(artifacts_root) / "models" / task / model
    link_path = base / "latest"
    if link_path.is_symlink():
        return (base / os.readlink(link_path)).resolve()
    raise FileNotFoundError(
        f"no 'latest' pointer for {task}/{model} under {base} "
        "(expected a symlink; train a model with status=ok first)"
    )


def load_latest_calibrator(path_dir: Path | str) -> Any:
    """Load the raw calibrator estimator saved alongside a ``final/`` model bundle.

    ``train_runner.run_training`` writes this file directly with
    ``joblib.dump(final_calibrator.calibrator, final_dir / "calibrator.joblib")``
    — a plain estimator.

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
        KeyError: If ``model_metadata`` has no ``features_hash`` key — a malformed
            model artifact is a louder failure than a coincidental ``None == None``
            match against a similarly-missing key on the predict side.
    """
    model_hash = model_metadata["features_hash"]
    predict_hash = predict_metadata.get("features_hash")
    if model_hash != predict_hash:
        raise ValueError(
            "features_hash mismatch between loaded model and predict dataset: "
            f"model={model_hash!r}, predict={predict_hash!r}"
        )


__all__ = [
    "check_features_hash_match",
    "load_latest_calibrator",
    "load_model_artifact",
    "resolve_latest_model_dir",
    "save_model_artifact",
]
