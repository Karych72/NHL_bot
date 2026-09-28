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

import pandas as pd

from modeling.publish_predictions import publish_predictions

RUN_ID = "home_win_lgbm_deadbeef_20260101T000000Z"
DELETE = "DELETE FROM game_predictions WHERE task = %s"


class _Cursor:
    def __init__(self, log: list[tuple[str, str, object]]) -> None:
        self._log = log

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object) -> None:
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
    base = root / "models" / "home_win" / "lgbm"
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
            model="lgbm",
            predictions_csv=_write_csv(tmp),
            artifacts_root=tmp / "artifacts",
        )

    def test_no_latest_deletes_task_rows_and_inserts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            conn = _Conn()
            inserted = self._publish(Path(tmp_str), conn)
        self.assertEqual(inserted, 0)
        self.assertEqual(conn.log, [("execute", DELETE, ("home_win",))])
        self.assertEqual(conn.outcome, "commit")

    def test_status_not_ok_deletes_task_rows_and_inserts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_latest(tmp / "artifacts", status="failed_baseline_check")
            conn = _Conn()
            inserted = self._publish(tmp, conn)
        self.assertEqual(inserted, 0)
        self.assertEqual(conn.log, [("execute", DELETE, ("home_win",))])

    def test_ok_replaces_task_rows_in_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _write_latest(tmp / "artifacts", status="ok")
            conn = _Conn()
            inserted = self._publish(tmp, conn)
        self.assertEqual(inserted, 2)
        self.assertEqual(len(conn.log), 2)
        self.assertEqual(conn.log[0], ("execute", DELETE, ("home_win",)))
        kind, sql, rows = conn.log[1]
        self.assertEqual(kind, "executemany")
        self.assertIn("INSERT INTO game_predictions (game_id, task, model, run_id, probability)", sql)
        self.assertEqual(
            rows,
            [
                (101, "home_win", "lgbm", RUN_ID, 0.61),
                (102, "home_win", "lgbm", RUN_ID, 0.42),
            ],
        )
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
                    model="lgbm",
                    predictions_csv=tmp / "missing.csv",
                    artifacts_root=tmp / "artifacts",
                )
        self.assertEqual(conn.log, [])


if __name__ == "__main__":
    unittest.main()
