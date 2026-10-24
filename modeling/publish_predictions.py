"""Публикация вероятностей в таблицу ``game_predictions`` (Задачи 22B, 73).

Победа: читает CSV, который записал ``modeling.cli predict``; тотал: прогноз голевой модели
(``modeling.goal_model``) по играм того же ``dataset_predict.csv``. Каждая публикация заменяет
в PostgreSQL строки своей задачи — бот в контейнере не имеет стека моделирования и читает
только PG. Гейт победы не пересчитывается: источник истины — ``metadata.json`` по ``latest``
(``latest`` ставит ``train`` только при ``status: ok``). Отдельный от ``predict_runner``
модуль, чтобы предсказание оставалось без доступа к БД.

Задача 77: та же транзакция ведёт журнал ``prediction_log`` — дописывает исход (голы) сыгранных
игр, затем кладёт туда опубликованные строки. Журнал только растёт, ничего не удаляется.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from modeling.artifacts import resolve_latest_model_dir
from modeling.goal_model import forecast_upcoming

logger = logging.getLogger(__name__)

# Имя модели тотала в ``game_predictions.model`` и в ``models/over_5_5/<имя>/``.
GOAL_MODEL = "goals"

_DELETE_SQL = "DELETE FROM game_predictions WHERE task = %s"
_INSERT_SQL = (
    "INSERT INTO game_predictions (game_id, task, model, run_id, probability) "
    "VALUES (%s, %s, %s, %s, %s)"
)
# Журнал (Задача 77): повтор в тот же день обновляет вероятность — в журнале то, что бот показал последним.
_LOG_INSERT_SQL = (
    "INSERT INTO prediction_log (game_id, task, model, run_id, probability) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (game_id, task, run_id, published_on) "
    "DO UPDATE SET probability = EXCLUDED.probability, published_at = now()"
)
# Исход: ``goals`` — итоговый счёт с +1 победителю овертайма/буллитов (правило букмекеров).
_SETTLE_SQL = """
UPDATE prediction_log p
SET home_goals = hs.goals, away_goals = aws.goals, settled_at = now()
FROM games g
JOIN game_team_stats hs ON hs.game_id = g.game_id AND hs.team_id::bigint = g.home_team_id
JOIN game_team_stats aws ON aws.game_id = g.game_id AND aws.team_id::bigint = g.away_team_id
WHERE p.game_id = g.game_id
  AND p.home_goals IS NULL
  AND g.winner_id IS NOT NULL
"""


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
    return _replace_task_rows(conn, task, rows)


def publish_goal_predictions(
    conn: Any,
    *,
    games_csv: Path,
    predict_dataset: Path,
    artifacts_root: Path,
) -> int:
    """Заменить строки ``over_5_5`` в ``game_predictions`` прогнозом голевой модели (Задача 73).

    Игры — строки *predict_dataset*: в нём уже только матчи, где у обеих команд не меньше
    ``min_prior_games`` игр в сезоне, — то же правило, что у победы. Гейта нет: приёмку 67
    модель не прошла, тотал выходит в бот по решению человека 2026-10-14. Предстоящие игры для
    флагов b2b — тоже только из датасета: в первые недели сезона игра вчерашнего дня, отброшенная
    правилом 5 игр, флаг b2b не ставит (неточность принята, к середине октября исчезает).

    Args:
        conn: соединение psycopg2 (транзакция фиксируется здесь).
        games_csv: ``games_train.csv`` той же сборки — сыгранные игры для b2b и доли ничьих.
        predict_dataset: ``dataset_predict.csv``.
        artifacts_root: корень с ``models/over_5_5/goals/latest`` (``modeling.cli fit-goals``).

    Returns:
        Число вставленных строк.
    """
    model_dir = resolve_latest_model_dir(artifacts_root, "over_5_5", GOAL_MODEL)
    metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    forecast = forecast_upcoming(
        joblib.load(model_dir / "model.joblib"),
        pd.read_csv(games_csv),
        pd.read_csv(predict_dataset),
        metadata["tie_halflife_days"],
    )
    rows = [
        (int(game_id), "over_5_5", GOAL_MODEL, metadata["run_id"], float(p))
        for game_id, p in zip(forecast["game_id"], forecast["p_over"])
    ]
    return _replace_task_rows(conn, "over_5_5", rows)


def _replace_task_rows(conn: Any, task: str, rows: list[tuple[int, str, str, str, float]]) -> int:
    """Одной транзакцией: дописать исходы в журнал, заменить строки *task*, добавить *rows* в журнал.

    Исходы дописываются при каждой публикации независимо от гейта (идемпотентно).
    Возвращает число вставленных в ``game_predictions`` строк.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(_SETTLE_SQL)
            cur.execute(_DELETE_SQL, (task,))
            if rows:
                cur.executemany(_INSERT_SQL, rows)
                cur.executemany(_LOG_INSERT_SQL, rows)
    logger.info("game_predictions: task=%s вставлено %d строк", task, len(rows))
    return len(rows)


__all__ = ["GOAL_MODEL", "publish_goal_predictions", "publish_predictions"]
