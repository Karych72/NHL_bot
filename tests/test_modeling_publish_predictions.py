"""Tests for modeling.publish_predictions (Задача 22B).

Артефакты — настоящие файлы в tmp (``latest`` symlink + ``metadata.json``), CSV — настоящий;
соединение с PG подменено рекордером, а утверждения — о фактически выполненных SQL и параметрах.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from modeling.dataset_builder.base import _load_train_metadata
from modeling.goal_model import fit_goal_model, forecast_upcoming, prepare_games
from modeling.publish_predictions import (
    _SETTLE_SQL,
    GOAL_MODEL,
    publish_goal_predictions,
    publish_predictions,
)
from tests.test_modeling_goal_model import _synthetic_games
from tests.test_modeling_predict_runner import (
    FEATURE_SET_VERSION,
    MANIFEST,
    _features_hash,
    _write_latest_model as _write_real_latest_model,
)

RUN_ID = "home_win_logreg_deadbeef_20260101T000000Z"
DELETE = "DELETE FROM game_predictions WHERE task = %s"


class _Cursor:
    def __init__(self, log: list[tuple[str, str, object]]) -> None:
        self._log = log

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self._log.append(("execute", sql, params))

    def executemany(self, sql: str, rows: object) -> None:
        self._log.append(("executemany", sql, list(rows)))  # type: ignore[call-overload]


class _Conn:
    """Записывает SQL и события транзакции (commit при чистом выходе, rollback при ошибке)."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, object]] = []
        self.outcome: str | None = None

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        self.outcome = "rollback" if exc_type else "commit"

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)


def _write_latest(root: Path, status: str) -> None:
    base = root / "models" / "home_win" / "logreg"
    final = base / RUN_ID / "final"
    final.mkdir(parents=True)
    (final / "metadata.json").write_text(json.dumps({"run_id": RUN_ID, "status": status}), encoding="utf-8")
    os.symlink(Path(RUN_ID) / "final", base / "latest")


def _write_csv(tmp: Path) -> Path:
    path = tmp / "preds.csv"
    pd.DataFrame(
        {
            "game_id": [101, 102],
            "day": ["2026-09-30", "2026-09-30"],
            "season_id": [20262027, 20262027],
            "home_team_id": [10, 20],
            "away_team_id": [11, 21],
            "probability": [0.61, 0.42],
        }
    ).to_csv(path, index=False)
    return path


class TestPublishPredictions(unittest.TestCase):
    def _publish(self, tmp: Path, conn: _Conn) -> int:
        return publish_predictions(
            conn,
            task="home_win",
            model="logreg",
            predictions_csv=_write_csv(tmp),
            artifacts_root=tmp / "artifacts",
        )

    def test_no_latest_deletes_task_rows_and_inserts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            conn = _Conn()
            inserted = self._publish(Path(tmp_str), conn)
        self.assertEqual(inserted, 0)
        self.assertEqual(conn.log, [("execute", _SETTLE_SQL, None), ("execute", DELETE, ("home_win",))])
        self.assertEqual(conn.outcome, "commit")

    def test_status_not_ok_deletes_task_rows_and_inserts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_latest(tmp / "artifacts", status="failed_baseline_check")
            conn = _Conn()
            inserted = self._publish(tmp, conn)
        self.assertEqual(inserted, 0)
        self.assertEqual(conn.log, [("execute", _SETTLE_SQL, None), ("execute", DELETE, ("home_win",))])
        self.assertEqual(conn.outcome, "commit")

    def test_ok_replaces_task_rows_in_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_latest(tmp / "artifacts", status="ok")
            conn = _Conn()
            inserted = self._publish(tmp, conn)
        self.assertEqual(inserted, 2)
        self.assertEqual(len(conn.log), 4)
        self.assertEqual(conn.log[0], ("execute", _SETTLE_SQL, None))
        self.assertEqual(conn.log[1], ("execute", DELETE, ("home_win",)))
        kind, sql, rows = conn.log[2]
        self.assertEqual(kind, "executemany")
        self.assertIn("INSERT INTO game_predictions (game_id, task, model, run_id, probability)", sql)
        self.assertEqual(
            rows,
            [
                (101, "home_win", "logreg", RUN_ID, 0.61),
                (102, "home_win", "logreg", RUN_ID, 0.42),
            ],
        )
        log_kind, log_sql, log_rows = conn.log[3]
        self.assertEqual(log_kind, "executemany")
        self.assertIn("INSERT INTO prediction_log (game_id, task, model, run_id, probability)", log_sql)
        self.assertEqual(log_rows, rows)
        self.assertEqual(conn.outcome, "commit")

    def test_ok_without_csv_fails_loudly_before_touching_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_latest(tmp / "artifacts", status="ok")
            conn = _Conn()
            with self.assertRaises(FileNotFoundError):
                publish_predictions(
                    conn,
                    task="home_win",
                    model="logreg",
                    predictions_csv=tmp / "missing.csv",
                    artifacts_root=tmp / "artifacts",
                )
        self.assertEqual(conn.log, [])


class TestPublishGoalPredictions(unittest.TestCase):
    """Задача 73: тотал — прогноз голевой модели по играм predict-датасета, без гейта."""

    def test_replaces_over_rows_with_the_goal_forecast_of_predict_games(self) -> None:
        games = _synthetic_games()
        model = fit_goal_model(prepare_games(games), alpha=0.01)
        upcoming = pd.DataFrame(
            {"game_id": [901, 902], "day": ["2024-12-01", "2024-12-01"], "home_team_id": [1, 2], "away_team_id": [3, 4]}
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            base = tmp / "artifacts" / "models" / "over_5_5" / GOAL_MODEL
            final = base / "goals_run" / "final"
            final.mkdir(parents=True)
            joblib.dump(model, final / "model.joblib")
            (final / "metadata.json").write_text(
                json.dumps({"run_id": "goals_run", "tie_halflife_days": 100}), encoding="utf-8"
            )
            os.symlink(Path("goals_run") / "final", base / "latest")
            games.to_csv(tmp / "games_train.csv", index=False)
            upcoming.to_csv(tmp / "dataset_predict.csv", index=False)
            conn = _Conn()
            inserted = publish_goal_predictions(
                conn,
                games_csv=tmp / "games_train.csv",
                predict_dataset=tmp / "dataset_predict.csv",
                artifacts_root=tmp / "artifacts",
            )
        expected = forecast_upcoming(model, games, upcoming, 100)
        self.assertEqual(inserted, 2)
        self.assertEqual(conn.log[0], ("execute", _SETTLE_SQL, None))
        self.assertEqual(conn.log[1], ("execute", DELETE, ("over_5_5",)))
        _, _, rows = conn.log[2]
        self.assertEqual(conn.log[3][2], rows)
        self.assertEqual([row[:4] for row in rows], [(901, "over_5_5", "goals", "goals_run"), (902, "over_5_5", "goals", "goals_run")])
        np.testing.assert_allclose([row[4] for row in rows], expected["p_over"])
        self.assertEqual(conn.outcome, "commit")


class TestLatestMetadataIsValidTrainMetadata(unittest.TestCase):
    def test_latest_metadata_loads_as_build_dataset_train_metadata(self) -> None:
        """Makefile передаёт latest/metadata.json как --train-metadata-path build-dataset."""
        with tempfile.TemporaryDirectory() as tmp_str:
            artifacts_root = Path(tmp_str) / "artifacts"
            _write_real_latest_model(artifacts_root, model_family="logreg")
            loaded = _load_train_metadata(artifacts_root / "models" / "home_win" / "logreg" / "latest" / "metadata.json")
        self.assertEqual(loaded["feature_manifest"], MANIFEST)
        self.assertEqual(loaded["features_hash"], _features_hash("drop"))
        self.assertEqual(loaded["feature_set_version"], FEATURE_SET_VERSION)


if __name__ == "__main__":
    unittest.main()
