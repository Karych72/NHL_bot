"""Планировщик ежедневной публикации прогноза (Задача 73).

Сервис ``predict`` раз в сутки (``scheduled_retrain.RETRAIN_HOUR_UTC``:00 UTC, после ночного
sync) собирает датасет ближайших игр и публикует в ``game_predictions`` вероятность победы
хозяев и тотала больше 5.5 — бот показывает их в превью ``/tonight``. Модели не обучаются:
обе зафиксированы в репозитории (``deploy/models/``) и приезжают в образе. Расписание,
статус и предусловие sync — из ``scheduled_sync`` и ``scheduled_retrain``.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import scheduled_retrain
import scheduled_sync
from scheduled_sync import SyncCommand

logger = logging.getLogger(__name__)

# Порог "публикация протухла" для healthcheck: сутки расписания + запас на прогон.
STALE_AFTER = timedelta(hours=26)

# Догоняющий прогон при старте, если последнего нет или он старше суток.
MIN_INTERVAL = timedelta(days=1)

_REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = _REPO_ROOT / "all_data" / "publish_status.json"

# Модели в образе (корень артефактов) и рабочий каталог прогона (в образе пишется только
# /app/artifacts, каталог deploy/ принадлежит root).
MODELS_ROOT = "deploy"
WORK_DIR = "artifacts/predict"


def needs_catch_up(status_file: Path, now: datetime) -> bool:
    """Нужен ли прогон при старте: реального прогона нет или он старше ``MIN_INTERVAL``.

    Отказ по предусловию sync прогоном не считается (``scheduled_retrain._last_attempt_age``):
    перезапуск контейнера после такого отказа публикует сразу, а не на следующий день.

    Аргументы:
        status_file: путь JSON-файла статуса публикации.
        now: текущий момент (UTC).
    """
    age = scheduled_retrain._last_attempt_age(status_file, now)
    return age is None or age > MIN_INTERVAL


def build_commands(now: datetime) -> List[SyncCommand]:
    """Цепочка публикации: датасет ближайших игр → прогноз победы → публикация победы и тотала.

    Датасет собирается с ``--min-prior-games 5`` и политикой ``drop`` (значение по умолчанию):
    в него попадают только игры, где у обеих команд не меньше 5 игр в сезоне, — других
    прогнозов бот не показывает. Признаки сверяются с ``metadata.json`` модели победы.
    Цели — игры не позже завтрашнего дня UTC (``--target-day-to``): ``scheduled_games`` держит
    две недели расписания (для ``/BOS_FULL``, Задача 100), а прогноз и журнал ``prediction_log``
    — только про то, что бот покажет в превью сегодня и завтра.

    Аргументы:
        now: момент прогона (UTC) — от него считается «завтра».
    """
    dataset = f"{WORK_DIR}/dataset_predict.csv"
    home_win_csv = f"{WORK_DIR}/home_win_logreg_predictions.csv"
    last_day = (now.astimezone(timezone.utc).date() + timedelta(days=1)).isoformat()
    steps = [
        [
            "build-dataset", "--mode", "predict", "--output-dir", WORK_DIR, "--min-prior-games", "5",
            "--train-metadata-path", f"{MODELS_ROOT}/models/home_win/logreg/latest/metadata.json",
            "--target-day-to", last_day,
        ],
        [
            "predict", "--task", "home_win", "--model", "logreg", "--predict-dataset", dataset,
            "--predict-metadata", f"{WORK_DIR}/metadata_predict.json",
            "--artifacts-root", MODELS_ROOT, "--output", home_win_csv,
        ],
        [
            "publish-predictions", "--task", "home_win", "--model", "logreg",
            "--artifacts-root", MODELS_ROOT, "--predictions", home_win_csv,
        ],
        [
            "publish-goals", "--games", f"{WORK_DIR}/games_train.csv", "--predict-dataset", dataset,
            "--artifacts-root", MODELS_ROOT,
        ],
    ]  # fmt: skip
    return [SyncCommand([sys.executable, "-m", "modeling.cli", *step], _REPO_ROOT) for step in steps]


def _run_now() -> bool:
    """Прогон публикации с боевыми путями статусов и текущим временем."""
    now = datetime.now(timezone.utc)
    return scheduled_retrain.run_after_sync(
        build_commands(now),
        STATUS_FILE,
        scheduled_sync.STATUS_FILE,
        now,
        name="Publish",
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI (``scheduled_retrain.serve``): прогон каждое пробуждение, догон — ``needs_catch_up``.

    Аргументы:
        argv: аргументы командной строки без имени программы; ``None`` — ``sys.argv[1:]``.
    """
    return scheduled_retrain.serve(
        argv,
        name="Publish",
        status_file=STATUS_FILE,
        stale_after=STALE_AFTER,
        run_now=_run_now,
        catch_up=needs_catch_up,
        should_run=lambda _status, _now: True,
    )


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    sys.exit(main())
