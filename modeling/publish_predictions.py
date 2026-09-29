"""Публикация вероятностей predict в таблицу ``game_predictions`` (Задача 22B).

Читает CSV, который записал ``modeling.cli predict``, и заменяет в PostgreSQL строки
своей задачи — бот в контейнере не имеет стека моделирования и читает только PG.
Гейт качества не пересчитывается: источник истины — ``metadata.json`` по ``latest``
(``latest`` ставит ``train`` только при ``status: ok``). Отдельный от ``predict_runner``
модуль, чтобы предсказание оставалось без доступа к БД.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from modeling.artifacts import resolve_latest_model_dir

logger = logging.getLogger(__name__)

_DELETE_SQL = "DELETE FROM game_predictions WHERE task = %s"
_INSERT_SQL = (
    "INSERT INTO game_predictions (game_id, task, model, run_id, probability) "
    "VALUES (%s, %s, %s, %s, %s)"
)


def _gate_failure_reason(artifacts_root: Path, task: str, model: str) -> tuple[str | None, str | None]:
    """Проверить гейт по артефакту.

    Returns:
        ``(reason, run_id)``: ``reason`` — почему публиковать нельзя (иначе ``None``),
        ``run_id`` — из ``metadata.json`` ``latest`` при пройденном гейте.
    """
    try:
        model_dir = resolve_latest_model_dir(artifacts_root, task, model)
    except FileNotFoundError:
        return f"нет latest для {task}/{model}", None
    metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("status") != "ok":
        return f"status={metadata.get('status')!r} у {model_dir}", None
    return None, metadata["run_id"]


def publish_predictions(
    conn: Any,
    *,
    task: str,
    model: str,
    predictions_csv: Path,
    artifacts_root: Path,
) -> int:
    """Заменить строки *task* в ``game_predictions`` вероятностями из *predictions_csv*.

    Гейт не пройден (нет ``latest`` или ``status != "ok"``) — строки задачи удаляются,
    ничего не вставляется, возвращается 0 (штатный исход, не ошибка). Иначе delete всех
    строк задачи и insert из CSV идут в одной транзакции; ``run_id`` берётся из
    ``metadata.json`` модели (в CSV его нет).

    Args:
        conn: соединение psycopg2 (транзакция фиксируется здесь).
        task: ``"home_win"`` или ``"over_5_5"``.
        model: ``"logreg"`` или ``"lgbm"``.
        predictions_csv: CSV от ``modeling.cli predict``.
        artifacts_root: корень с ``models/<task>/<model>/latest``.

    Returns:
        Число вставленных строк.

    Raises:
        FileNotFoundError: гейт пройден, но CSV отсутствует.
    """
    reason, run_id = _gate_failure_reason(artifacts_root, task, model)
    rows: list[tuple[int, str, str, str, float]] = []
    if reason is None:
        frame = pd.read_csv(predictions_csv)
        rows = [
            (int(game_id), task, model, str(run_id), float(p))
            for game_id, p in zip(frame["game_id"], frame["probability"])
        ]
    else:
        logger.warning("Гейт не пройден (%s): строки задачи %s удаляются, публикации нет", reason, task)

    with conn:
        with conn.cursor() as cur:
            cur.execute(_DELETE_SQL, (task,))
            if rows:
                cur.executemany(_INSERT_SQL, rows)
    logger.info("game_predictions: task=%s вставлено %d строк", task, len(rows))
    return len(rows)


__all__ = ["publish_predictions"]
