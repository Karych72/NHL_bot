"""Post-hoc probability calibration without leakage (UPDATE plan stage 9).

Two-step protocol:

1. **Raw classifier** (logreg / lgbm from stages 7–8) is trained only on ``train_k``.
   This module never retrains it.
2. **Calibrator** is fit only on ``calibration_k`` pairs
   ``(raw_p_cal, y_cal)`` — probabilities from the frozen raw model on the
   calibration block and the corresponding labels.

``sklearn.calibration.CalibratedClassifierCV`` is **forbidden**: its internal
cross-validation does not respect temporal ordering and cannot accept a
``TimeSeriesSplit`` for ``method='isotonic'``.

No database access.  No ``run_id`` generation — the CLI (stage 10) owns that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from modeling.artifacts import _collect_library_versions, _get_git_commit

logger = logging.getLogger(__name__)

_MODEL_RAW_FILENAME = "model_raw.joblib"
_CALIBRATOR_FILENAME = "calibrator.joblib"
_METADATA_FILENAME = "metadata.json"

_SUPPORTED_METHODS: frozenset[str] = frozenset({"isotonic", "platt"})


class CalibrationError(ValueError):
    """Invalid calibration inputs, configuration, or method."""


class _IdentityCalibrator:
    """Sentinel stored in ``calibrator.joblib`` when calibration is skipped."""

    __slots__ = ()


def _validate_min_samples(min_samples: int) -> int:
    if not isinstance(min_samples, (int, np.integer)):
        raise CalibrationError(
            f"min_samples must be an int, got {type(min_samples).__name__}"
        )
    n = int(min_samples)
    if n <= 0:
        raise CalibrationError(f"min_samples must be positive, got {n}")
    return n


def _validate_num_threads(num_threads: int) -> int:
    if not isinstance(num_threads, (int, np.integer)):
        raise CalibrationError(
            f"num_threads must be an int, got {type(num_threads).__name__}"
        )
    n = int(num_threads)
    if n <= 0:
        raise CalibrationError(f"num_threads must be >= 1, got {n}")
    return n


def _validate_method(method: str) -> str:
    if method not in _SUPPORTED_METHODS:
        raise CalibrationError(
            f"Unknown calibration method {method!r}. Supported: {sorted(_SUPPORTED_METHODS)}"
        )
    return method


def _validate_calibration_inputs(
    raw_p_cal: np.ndarray,
    y_cal: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(raw_p_cal, dtype=float).ravel()
    y = np.asarray(y_cal).ravel()

    if p.shape != y.shape:
        raise CalibrationError(
            f"raw_p_cal and y_cal length mismatch: {p.size} vs {y.size}"
        )
    if p.size == 0:
        raise CalibrationError("calibration inputs must be non-empty")

    if np.any(np.isnan(p)):
        raise CalibrationError("raw_p_cal contains NaN")
    if np.any((p < 0.0) | (p > 1.0)):
        raise CalibrationError("raw_p_cal values must lie in [0, 1]")

    unique_y = set(np.unique(y).tolist())
    if not unique_y.issubset({0, 1}):
        raise CalibrationError(f"y_cal must contain only {{0, 1}}, got {sorted(unique_y)}")

    return p, y.astype(int)


def _build_platt_calibrator(
    raw_p_cal: np.ndarray,
    y_cal: np.ndarray,
    *,
    seed: int,
    num_threads: int,
) -> LogisticRegression:
    """Fit Platt scaling via logistic regression on raw probabilities.

    The single input feature is the **raw probability** ``p`` (not its logit).
    Probabilities are already in ``[0, 1]`` and map linearly to a well-conditioned
    feature space for ``LogisticRegression``; using ``logit(p)`` would amplify
    numerical instability near 0 and 1 without improving fit quality on typical
    sports-classifier score distributions.
    """
    lr = LogisticRegression(
        penalty="l2",
        solver="lbfgs",
        max_iter=5000,
        random_state=seed,
        n_jobs=num_threads,
    )
    lr.fit(raw_p_cal.reshape(-1, 1), y_cal)
    return lr


def _apply_fitted_calibrator(calibrator: Any, method: str, raw_p: np.ndarray) -> np.ndarray:
    p = np.asarray(raw_p, dtype=float).ravel()
    if isinstance(calibrator, _IdentityCalibrator):
        return p.copy()
    if method == "isotonic":
        return np.clip(calibrator.predict(p), 0.0, 1.0)
    if method == "platt":
        return calibrator.predict_proba(p.reshape(-1, 1))[:, 1]
    raise CalibrationError(f"Unknown calibration method {method!r}")


@dataclass(frozen=True)
class CalibratorFit:
    """Result of fitting a post-hoc calibrator on ``calibration_k``."""

    method: str
    calibration_skipped: bool
    n_calibration: int
    seed: int
    calibrator: Any

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable metadata fields (no runtime estimator state)."""
        return {
            "method": self.method,
            "calibration_skipped": self.calibration_skipped,
            "n_calibration": self.n_calibration,
            "seed": self.seed,
        }


def fit_calibrator(
    raw_p_cal: np.ndarray,
    y_cal: np.ndarray,
    *,
    method: str,
    min_samples: int = 500,
    seed: int,
    num_threads: int = 1,
) -> CalibratorFit:
    """Fit a post-hoc calibrator on raw-model probabilities from ``calibration_k``.

    When ``len(raw_p_cal) < min_samples`` (default 500), calibration is skipped:
    ``calibration_skipped=True`` and the stored calibrator is an identity marker.

    Args:
        raw_p_cal: Raw positive-class probabilities on ``calibration_k``, shape ``(n,)``.
        y_cal: Binary labels on ``calibration_k``, values in ``{0, 1}``.
        method: ``"isotonic"`` or ``"platt"`` (from YAML ``calibration.method``).
        min_samples: Skip threshold from YAML ``calibration.min_samples``.
        seed: YAML ``random_seed`` (used for Platt ``LogisticRegression``).
        num_threads: YAML ``compute.num_threads`` (passed as ``n_jobs`` to Platt LR).

    Returns:
        :class:`CalibratorFit` with a fitted estimator or identity marker.
    """
    method = _validate_method(method)
    min_samples = _validate_min_samples(min_samples)
    _validate_num_threads(num_threads)
    p_cal, y = _validate_calibration_inputs(raw_p_cal, y_cal)
    n_cal = int(p_cal.size)

    if n_cal < min_samples:
        logger.info(
            "Skipping calibration: n_calibration=%d < min_samples=%d",
            n_cal,
            min_samples,
        )
        return CalibratorFit(
            method=method,
            calibration_skipped=True,
            n_calibration=n_cal,
            seed=seed,
            calibrator=_IdentityCalibrator(),
        )

    if method == "isotonic":
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(p_cal, y)
        calibrator: Any = iso
    else:
        calibrator = _build_platt_calibrator(
            p_cal, y, seed=seed, num_threads=num_threads
        )

    return CalibratorFit(
        method=method,
        calibration_skipped=False,
        n_calibration=n_cal,
        seed=seed,
        calibrator=calibrator,
    )


def apply_calibrator(calibrator_fit: CalibratorFit, raw_p: np.ndarray) -> np.ndarray:
    """Apply raw → calibrator chain; identity when ``calibration_skipped``."""
    p = np.asarray(raw_p, dtype=float).ravel()
    if np.any(np.isnan(p)):
        raise CalibrationError("raw_p contains NaN")
    if np.any((p < 0.0) | (p > 1.0)):
        raise CalibrationError("raw_p values must lie in [0, 1]")

    if calibrator_fit.calibration_skipped:
        return p.copy()

    return _apply_fitted_calibrator(
        calibrator_fit.calibrator,
        calibrator_fit.method,
        p,
    )


def save_calibration_artifact(
    path_dir: Path | str,
    *,
    model_raw: Any,
    calibrator_fit: CalibratorFit,
    metadata: dict[str, Any],
) -> None:
    """Persist raw model, calibrator, and JSON sidecar into *path_dir*.

    Writes ``model_raw.joblib``, ``calibrator.joblib``, and ``metadata.json``.
    Does **not** validate *metadata* — the stage-10 CLI is responsible for
    supplying a complete sidecar.  This function only augments missing audit
    fields (``library_versions``, ``git_commit``) and merges calibration fields
    from *calibrator_fit* when absent (``method``, ``calibration_skipped``,
    ``n_calibration``, ``seed``).

    **Caller must set** in *metadata* (UPDATE plan §12.4 / stage-9 §3.5):

    - ``features_hash`` — SHA-256 from ``metadata_train.json`` (passed through
      unchanged).
    - ``train_days`` — ``{"min": "<ISO-date>", "max": "<ISO-date>"}`` for the
      training block.
    - ``calibration_days`` — same shape for the calibration block.
    - ``n_rows_train`` — row count of ``train_k`` (or final train for production).
    - ``n_rows_calibration`` — row count of ``calibration_k`` (should match
      ``calibrator_fit.n_calibration``).
    - ``seed`` — YAML ``random_seed`` used for this fold/run.

    Optional but expected from stage-10 orchestration: ``task``, ``model_family``,
    ``run_id``, ``feature_set_version``, inner-val slice fields when applicable.

    **Filled automatically** when absent: ``library_versions`` (includes
    scikit-learn), ``git_commit`` (``null`` outside a git repo).
    """
    out_dir = Path(path_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model_path = out_dir / _MODEL_RAW_FILENAME
    joblib.dump(model_raw, model_path)
    logger.info("Saved raw model → %s", model_path)

    cal_path = out_dir / _CALIBRATOR_FILENAME
    joblib.dump(calibrator_fit.calibrator, cal_path)
    logger.info("Saved calibrator → %s", cal_path)

    full_metadata = dict(metadata)
    fit_fields = calibrator_fit.to_dict()
    for key, value in fit_fields.items():
        full_metadata.setdefault(key, value)

    if "library_versions" not in full_metadata:
        full_metadata["library_versions"] = _collect_library_versions()
    if "git_commit" not in full_metadata:
        full_metadata["git_commit"] = _get_git_commit()

    meta_path = out_dir / _METADATA_FILENAME
    meta_path.write_text(
        json.dumps(full_metadata, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    logger.info("Saved calibration metadata → %s", meta_path)


def load_calibration_artifact(
    path_dir: Path | str,
) -> tuple[Any, CalibratorFit, dict[str, Any]]:
    """Load raw model, calibrator fit, and metadata from *path_dir*."""
    in_dir = Path(path_dir)
    model_path = in_dir / _MODEL_RAW_FILENAME
    cal_path = in_dir / _CALIBRATOR_FILENAME
    meta_path = in_dir / _METADATA_FILENAME

    for path, name in (
        (model_path, _MODEL_RAW_FILENAME),
        (cal_path, _CALIBRATOR_FILENAME),
        (meta_path, _METADATA_FILENAME),
    ):
        if not path.exists():
            raise FileNotFoundError(f"{name} not found in {in_dir}")

    model_raw = joblib.load(model_path)
    calibrator = joblib.load(cal_path)
    metadata: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))

    method = metadata.get("method")
    if method not in _SUPPORTED_METHODS:
        raise CalibrationError(
            f"metadata.json missing or invalid calibration method: {method!r}"
        )

    calibration_skipped = bool(metadata.get("calibration_skipped", False))
    if calibration_skipped and not isinstance(calibrator, _IdentityCalibrator):
        logger.warning(
            "calibration_skipped=true but calibrator is not identity marker; "
            "trusting metadata flag"
        )

    calibrator_fit = CalibratorFit(
        method=method,
        calibration_skipped=calibration_skipped,
        n_calibration=int(metadata.get("n_calibration", 0)),
        seed=int(metadata.get("seed", metadata.get("random_seed", 0))),
        calibrator=calibrator if not calibration_skipped else _IdentityCalibrator(),
    )
    logger.info("Loaded calibration artifact from %s", in_dir)
    return model_raw, calibrator_fit, metadata


__all__ = [
    "CalibrationError",
    "CalibratorFit",
    "apply_calibrator",
    "fit_calibrator",
    "load_calibration_artifact",
    "save_calibration_artifact",
]
