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

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

logger = logging.getLogger(__name__)


_SUPPORTED_METHODS: frozenset[str] = frozenset({"isotonic", "platt"})

# Clip bound for the logit transform Platt scaling fits on (Задача 40a).
# Mirrors the default of the YAML `evaluation.epsilon_clip` field, but that
# field is not threaded into this module's API (both `fit_calibrator` and
# `apply_calibrator` are also called from modeling/predict_runner.py without
# it) -- a local constant avoids widening those signatures for a value that
# is only ever this default in practice.
_PLATT_LOGIT_EPS = 1e-15


def _logit(p: np.ndarray, *, eps: float = _PLATT_LOGIT_EPS) -> np.ndarray:
    """Log-odds of *p*, clipped to ``[eps, 1 - eps]`` to stay finite at 0/1."""
    clipped = np.clip(p, eps, 1.0 - eps)
    return np.log(clipped / (1.0 - clipped))


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
    """Fit Platt scaling: unregularized logistic regression on ``logit(p)``.

    This is classical Platt scaling -- a sigmoid over the raw model's logit,
    ``sigmoid(a * logit(p) + b)`` -- not a logistic regression over ``p``
    itself. ``penalty=None`` (no L2 shrinkage): with L2 the previous ``p``-input
    fit regularized the slope toward a near-constant prediction on some models
    (spike 40a: Elo's fitted Platt slope was -0.014). See
    ``docs/modeling_training.md`` §7 (Задача 40) for the check-season numbers this
    is based on.
    """
    x_cal = _logit(raw_p_cal).reshape(-1, 1)
    lr = LogisticRegression(
        penalty=None,
        solver="lbfgs",
        max_iter=5000,
        random_state=seed,
        n_jobs=num_threads,
    )
    lr.fit(x_cal, y_cal)
    return lr


def _apply_fitted_calibrator(calibrator: Any, method: str, raw_p: np.ndarray) -> np.ndarray:
    p = np.asarray(raw_p, dtype=float).ravel()
    if isinstance(calibrator, _IdentityCalibrator):
        return p.copy()
    if method == "isotonic":
        return np.clip(calibrator.predict(p), 0.0, 1.0)
    if method == "platt":
        return calibrator.predict_proba(_logit(p).reshape(-1, 1))[:, 1]
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


def identity_calibrator(method: str, seed: int) -> CalibratorFit:
    """Calibrator for a model that is used as fitted: ``calibration_skipped`` and no calibration rows."""
    return CalibratorFit(
        method=_validate_method(method),
        calibration_skipped=True,
        n_calibration=0,
        seed=seed,
        calibrator=_IdentityCalibrator(),
    )


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


def calibrator_fit_from_metadata(metadata: Mapping[str, Any], calibrator: Any) -> CalibratorFit:
    """Rebuild a :class:`CalibratorFit` from a saved ``metadata.json`` sidecar.

    Used by ``modeling.predict_runner`` for the flat ``final/`` bundle that
    ``train_runner.run_training`` writes. Defines what ``calibration_skipped``
    means for the estimator that ends up in the returned :class:`CalibratorFit`.

    Args:
        metadata: Parsed ``metadata.json`` with at least ``method`` (must be one
            of :data:`_SUPPORTED_METHODS`), and optionally ``calibration_skipped``,
            ``n_calibration``, ``seed``/``random_seed``.
        calibrator: The unpickled calibrator estimator (or ``_IdentityCalibrator``
            sentinel) loaded alongside *metadata*.

    Returns:
        :class:`CalibratorFit`. When ``calibration_skipped`` is true, the
        estimator is replaced with a fresh identity marker regardless of what
        was actually unpickled — only the metadata flag drives
        :func:`apply_calibrator`'s behaviour.

    Raises:
        CalibrationError: ``metadata["method"]`` is missing or not one of
            :data:`_SUPPORTED_METHODS`.
    """
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

    return CalibratorFit(
        method=method,
        calibration_skipped=calibration_skipped,
        n_calibration=int(metadata.get("n_calibration", 0)),
        seed=int(metadata.get("seed", metadata.get("random_seed", 0))),
        calibrator=calibrator if not calibration_skipped else _IdentityCalibrator(),
    )


__all__ = [
    "CalibrationError",
    "CalibratorFit",
    "apply_calibrator",
    "calibrator_fit_from_metadata",
    "fit_calibrator",
]
