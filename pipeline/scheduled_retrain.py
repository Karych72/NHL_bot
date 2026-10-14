"""Планировщик еженедельного retrain (Задача 26).

Сервис ``retrain`` еженедельно (понедельник ``RETRAIN_HOUR_UTC``:00 UTC; ежедневное пробуждение
догоняет отказанный по предусловию прогон) пересобирает
датасет обучения и обучает ``home_win`` с ``--no-promote`` — ``latest`` не двигается,
продвижение делает человек (``make modeling-promote``). Стартует только после
успешного последнего sync. Расписание, статус-файл и healthcheck — из
``scheduled_sync`` (Задача 34); здесь только цепочка команд, предусловие и недельный слот.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import scheduled_sync
from scheduled_sync import SyncCommand

logger = logging.getLogger(__name__)

# Понедельник (datetime.weekday()), 12:00 UTC: вне ночного окна sync (18:00–08:00 UTC),
# к этому моменту данные выходных уже загружены.
RETRAIN_WEEKDAY = 0
RETRAIN_HOUR_UTC = 12

# Порог "retrain протух" для healthcheck: неделя расписания + сутки на длительность прогона.
STALE_AFTER = timedelta(days=8)

# Не чаще раза в неделю, в том числе при перезапусках контейнера (догоняющий прогон).
MIN_INTERVAL = timedelta(days=7)

# Понедельничный слот требует 6 дней с прошлого прогона, а не 7: строгие 7 дней сдвигали бы
# понедельничный ритм на длительность прогона.
MONDAY_MIN_INTERVAL = timedelta(days=6)

_REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = _REPO_ROOT / "all_data" / "retrain_status.json"

# Значение failed_command при отказе по предусловию sync (см. needs_catch_up).
PRECONDITION_FAILED = "precondition: last sync run is not ok/fresh"


def seconds_until_next_run(now: datetime) -> float:
    """Секунд до ближайших ``RETRAIN_HOUR_UTC``:00 строго после *now*.

    Цикл просыпается ежедневно (после утреннего sync), а нужен ли прогон в этот день,
    решает ``should_run``. Если *now* ровно ``RETRAIN_HOUR_UTC``:00 или позже —
    возвращает время до завтрашнего слота.

    Аргументы:
        now: текущий момент (UTC; tzinfo сохраняется в сравнении).
    """
    slot = datetime.combine(
        now.date(), datetime.min.time().replace(hour=RETRAIN_HOUR_UTC), tzinfo=now.tzinfo
    )
    if slot <= now:
        slot += timedelta(days=1)
    return (slot - now).total_seconds()


def _last_attempt_age(status_file: Path, now: datetime) -> Optional[timedelta]:
    """Возраст последнего реального прогона; ``None``, если его нет.

    Реальный прогон — статус, не являющийся отказом по предусловию
    (``PRECONDITION_FAILED``): отказ ничего не обучал и прогоном не считается, иначе
    старт retrain одновременно с догоняющей загрузкой sync (статус sync ещё не свеж)
    отложил бы обучение на неделю. Неуспех самой цепочки прогоном считается —
    перезапуски контейнера не должны учащать обучение.
    """
    if not status_file.exists():
        return None
    data = json.loads(status_file.read_text(encoding="utf-8"))
    if data["failed_command"] == PRECONDITION_FAILED:
        return None
    return now - datetime.fromisoformat(data["finished_at"])


def needs_catch_up(status_file: Path, now: datetime) -> bool:
    """Нужен ли догоняющий прогон: реального прогона нет или он старше ``MIN_INTERVAL``.

    Аргументы:
        status_file: путь JSON-файла статуса retrain.
        now: текущий момент (UTC).
    """
    age = _last_attempt_age(status_file, now)
    return age is None or age > MIN_INTERVAL


def should_run(status_file: Path, now: datetime) -> bool:
    """Нужен ли прогон в дневное пробуждение цикла.

    Да, если нужен догоняющий прогон, либо сегодня понедельник и реального прогона не было
    за ``MONDAY_MIN_INTERVAL``: утренний догон в понедельник не даёт второго прогона в слот
    12:00, а догон в воскресенье пропускает ближайший понедельник.

    Аргументы:
        status_file: путь JSON-файла статуса retrain.
        now: текущий момент (UTC).
    """
    if needs_catch_up(status_file, now):
        return True
    age = _last_attempt_age(status_file, now)
    return now.weekday() == RETRAIN_WEEKDAY and age is not None and age > MONDAY_MIN_INTERVAL


def build_commands() -> List[SyncCommand]:
    """Цепочка retrain: пересборка датасета обучения, затем ``train`` без продвижения.

    Аргументы ``build-dataset`` совпадают с ручным путём (значения по умолчанию CLI и
    ``Makefile``), иначе ``features_hash`` датасета разойдётся с ручным. ``over_5_5``
    не обучается (не проходит гейт, ``docs/modeling_training.md`` §8), ``--no-promote``
    оставляет ``latest`` человеку.
    """
    dataset = [
        "build-dataset", "--mode", "train", "--output-dir", "artifacts/datasets",
        "--feature-set-version", "v2", "--rolling-windows", "5,10,20", "--min-prior-games", "5",
    ]  # fmt: skip
    train = [
        "train", "--config", "configs/modeling_default.yaml", "--task", "home_win", "--no-promote",
    ]  # fmt: skip
    return [
        SyncCommand([sys.executable, "-m", "modeling.cli", *dataset], _REPO_ROOT),
        SyncCommand([sys.executable, "-m", "modeling.cli", *train], _REPO_ROOT),
    ]


def run_after_sync(
    commands: Sequence[SyncCommand],
    status_file: Path,
    sync_status_file: Path,
    now: datetime,
    name: str = "Retrain",
) -> bool:
    """Один прогон: предусловие sync, затем цепочка *commands* (retrain и публикация, Задача 73).

    Если последний sync не успешен или протух (``scheduled_sync.check`` != 0), цепочка
    не стартует: в *status_file* пишется неуспех с пояснением, в лог — ошибка.

    Аргументы:
        commands: цепочка команд (``build_commands()``).
        status_file: путь статуса прогона.
        sync_status_file: путь статуса sync (предусловие).
        now: текущий момент (UTC) для проверки свежести sync.
        name: название прогона в строках лога (``scheduled_publish`` — "Publish").

    Возвращает: ``True``, если цепочка завершилась кодом 0.
    """
    if scheduled_sync.check(sync_status_file, now) != 0:
        logger.error("%s not started: last sync run is missing, failed or stale", name)
        scheduled_sync._write_status_atomic(
            status_file,
            {
                "finished_at": now.isoformat(),
                "ok": False,
                "failed_command": PRECONDITION_FAILED,
                "returncode": None,
                "window": None,
            },
        )
        return False

    return scheduled_sync.run_once(commands, status_file, name=name)


def _run_now() -> bool:
    """Прогон retrain с боевыми путями статусов и текущим временем."""
    ok = run_after_sync(
        build_commands(), STATUS_FILE, scheduled_sync.STATUS_FILE, datetime.now(timezone.utc)
    )
    if ok:
        logger.info(
            "Retrain ok, latest NOT moved. Run ids are in the train lines above "
            "(run_id=... task=... model=... status=...). Promote manually: "
            "make modeling-promote TASK=home_win MODEL=logreg RUN_ID=<run_id>; "
            "report: artifacts/reports/<run_id>/summary.md"
        )
    return ok


def serve(
    argv: Optional[List[str]],
    *,
    name: str,
    status_file: Path,
    stale_after: timedelta,
    run_now: Callable[[], bool],
    catch_up: Callable[[Path, datetime], bool],
    should_run: Callable[[Path, datetime], bool],
) -> int:
    """Общий CLI суточных сервисов после sync (retrain, публикация): ``loop`` / ``once`` / ``check``.

    - ``loop``: при старте прогон, только если *catch_up*; затем ежедневно в
      ``RETRAIN_HOUR_UTC``:00 UTC (после sync) прогоняет, если *should_run*. Неуспешный
      прогон цикл не останавливает (отказ виден через статус-файл и ``check``).
    - ``once``: один прогон (предусловие + цепочка), код выхода 0/1.
    - ``check``: 0, если статус есть, ``ok`` и моложе *stale_after*; иначе 1.

    Аргументы:
        argv: аргументы командной строки без имени программы; ``None`` — ``sys.argv[1:]``.
        name: название прогона в логах и справке ("Retrain", "Publish").
        status_file: путь статуса прогона.
        stale_after: порог устаревания статуса для ``check``.
        run_now: один прогон с боевыми путями; ``True`` — успех.
        catch_up: нужен ли прогон при старте ``loop``.
        should_run: нужен ли прогон в дневное пробуждение.
    """
    parser = argparse.ArgumentParser(description=f"{name} after the nightly sync.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    subparsers.add_parser("loop", help="Run forever: catch-up if due, then on schedule.")
    subparsers.add_parser("once", help="Run once (sync precondition + chain), exit 0/1.")
    subparsers.add_parser("check", help="Healthcheck exit code from the last run's status.")
    args = parser.parse_args(argv)

    if args.subcommand == "check":
        return scheduled_sync.check(status_file, datetime.now(timezone.utc), stale_after, name=name)

    if args.subcommand == "once":
        return 0 if run_now() else 1

    # loop
    if catch_up(status_file, datetime.now(timezone.utc)):
        run_now()
    target = datetime.now(timezone.utc)
    while True:
        target, sleep_seconds = scheduled_sync._next_target(
            datetime.now(timezone.utc), target, seconds_until_next_run
        )
        logger.info("Sleeping %.0f seconds until next %s wake-up", sleep_seconds, name.lower())
        time.sleep(sleep_seconds)
        if should_run(status_file, datetime.now(timezone.utc)):
            run_now()


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI retrain (:func:`serve`): догон — ``needs_catch_up``, пробуждение — ``should_run``.

    Аргументы:
        argv: аргументы командной строки без имени программы; ``None`` — ``sys.argv[1:]``.
    """
    return serve(
        argv,
        name="Retrain",
        status_file=STATUS_FILE,
        stale_after=STALE_AFTER,
        run_now=_run_now,
        catch_up=needs_catch_up,
        should_run=should_run,
    )


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    sys.exit(main())
