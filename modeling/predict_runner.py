"""Inference orchestration for prematch classifiers (Задача 15).

Loads the ``latest`` trained artifact for a ``(task, model)`` pair, scores a
predict dataset built by ``modeling.dataset_builder`` (``build-dataset --mode
predict``), applies the frozen post-hoc calibrator, and writes per-game
probabilities to CSV. Mirrors ``train_runner.py``'s isolation: no PostgreSQL,
no ``modeling.dataset_builder`` import, no walk-forward/training/calibration
fitting — everything here is already frozen by a prior ``train`` run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from modeling.artifacts import (
    check_features_hash_match,
    load_latest_calibrator,
    load_model_artifact,
    resolve_latest_model_dir,
)
from modeling.calibrate import apply_calibrator, calibrator_fit_from_metadata
from modeling.train_common import predict_raw_proba
from modeling.train_input import load_training_table_split

logger = logging.getLogger(__name__)

PROBABILITY_COLUMN = "probability"


@dataclass(frozen=True)
class PredictRunResult:
    """Outcome of a single :func:`run_predict` call.

    Attributes:
        task: ``"home_win"`` or ``"over_5_5"``.
        model: ``"logreg"`` or ``"lgbm"``.
        run_id: ``run_id`` of the loaded model, from its ``metadata.json``.
        model_dir: Resolved ``final/`` directory the model was loaded from.
        predictions: Per-game predictions (key columns + ``probability``),
            same content as written to ``output_path``.
        output_path: Where *predictions* was written.
    """

    task: str
    model: str
    run_id: str
    model_dir: Path
    predictions: pd.DataFrame
    output_path: Path


def run_predict(
    *,
    task: str,
    model: str,
    predict_dataset: Path,
    predict_metadata: Path,
    artifacts_root: Path,
    output_path: Path,
) -> PredictRunResult:
    """Score *predict_dataset* with the ``latest`` trained ``(task, model)`` artifact.

    Args:
        task: ``"home_win"`` or ``"over_5_5"``.
        model: ``"logreg"`` or ``"lgbm"``.
        predict_dataset: Path to ``dataset_predict.csv``
            (``build-dataset --mode predict``).
        predict_metadata: Path to the matching ``metadata_predict.json``.
        artifacts_root: Root containing ``models/<task>/<model>/latest``.
        output_path: Where to write the predictions CSV.

    Returns:
        :class:`PredictRunResult` with the resolved run id, model directory, and
        the predictions frame (also written to *output_path*).

    Raises:
        FileNotFoundError: No ``latest`` pointer for ``(task, model)``, or a
            required artifact/dataset file is missing.
        ValueError: ``features_hash`` mismatch between the loaded model and the
            predict dataset.
        CalibrationError: Invalid or missing calibration method in the model's
            ``metadata.json`` (a ``ValueError`` subclass — see
            ``modeling.calibrate.calibrator_fit_from_metadata``).
    """
    model_dir = resolve_latest_model_dir(artifacts_root, task, model)
    raw_model, model_metadata = load_model_artifact(model_dir)
    calibrator = load_latest_calibrator(model_dir)

    X, keys, _labels, _service, dataset_metadata = load_training_table_split(
        predict_dataset, predict_metadata
    )
    check_features_hash_match(model_metadata, dataset_metadata)

    model_family = model  # `train_common.predict_raw_proba`'s first arg is named model_family
    raw_p = predict_raw_proba(model_family, raw_model, X)
    calibrator_fit = calibrator_fit_from_metadata(model_metadata, calibrator)
    calibrated_p = apply_calibrator(calibrator_fit, raw_p)

    predictions = keys.copy()
    predictions[PROBABILITY_COLUMN] = calibrated_p

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(output_path, index=False)
    logger.info("Wrote %d predictions to %s", len(predictions), output_path)

    return PredictRunResult(
        task=task,
        model=model,
        run_id=model_metadata["run_id"],
        model_dir=model_dir,
        predictions=predictions,
        output_path=output_path,
    )


__all__ = ["PredictRunResult", "run_predict"]
