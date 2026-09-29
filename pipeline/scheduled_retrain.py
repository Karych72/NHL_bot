"""Планировщик еженедельного retrain (Задача 26).

Сервис ``retrain`` раз в неделю (понедельник ``RETRAIN_HOUR_UTC``:00 UTC) пересобирает
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
from typing import List, Optional, Sequence

import scheduled_sync
from scheduled_sync import SyncCommand

logger = logging.getLogger(__name__)

# Понедельник (datetime.weekday()), 12:00 UTC: разнесено с sync (08:00 UTC ежедневно),
# к этому моменту данные выходных уже загружены.
RETRAIN_WEEKDAY = 0
RETRAIN_HOUR_UTC = 12

# Порог "retrain протух" для healthcheck: неделя расписания + сутки на длительность прогона.
STALE_AFTER = timedelta(days=8)

# Не чаще раза в неделю, в том числе при перезапусках контейнера (догоняющий прогон).
MIN_INTERVAL = timedelta(days=7)

STATUS_FILE = Path(__file__).resolve().parents[1] / "all_data" / "retrain_status.json"

_REPO_ROOT = Path(__file__).resolve().parents[1]


def seconds_until_next_run(now: datetime) -> float:
    """Секунд до ближайшего понедельника ``RETRAIN_HOUR_UTC``:00 строго после *now*.

    Если *now* — понедельник, ровно ``RETRAIN_HOUR_UTC``:00 или позже, возвращает
    время до следующей недели, а не 0/отрицательное число.

    Аргументы:
        now: текущий момент (UTC; tzinfo сохраняется в сравнении).
    """
    slot = datetime.combine(
        now.date(), datetime.min.time().replace(hour=RETRAIN_HOUR_UTC), tzinfo=now.tzinfo
    )
    slot += timedelta(days=(RETRAIN_WEEKDAY - now.weekday()) % 7)
    if slot <= now:
        slot += timedelta(days=7)
    return (slot - now).total_seconds()


def needs_catch_up(status_file: Path, now: datetime) -> bool:
    """Нужен ли догоняющий прогон при старте цикла.

    ``True``, если статуса нет или его ``finished_at`` старше ``MIN_INTERVAL``.
    Неуспешный статус тоже считается прогоном: перезапуск контейнера не должен
    превращаться в повтор обучения чаще раза в неделю.

    Аргументы:
        status_file: путь JSON-файла статуса retrain.
        now: текущий момент (UTC).
    """
    if not status_file.exists():
        return True
    finished_at = datetime.fromisoformat(
        json.loads(status_file.read_text(encoding="utf-8"))["finished_at"]
    )
    return now - finished_at > MIN_INTERVAL


def build_commands() -> List[SyncCommand]:
    """Цепочка retrain: пересборка датасета обучения, затем ``train`` без продвижения.

    Аргументы ``build-dataset`` совпадают с ручным путём (значения по умолчанию CLI и
    ``Makefile``), иначе ``features_hash`` датасета разойдётся с ручным. ``over_5_5``
    не обучается (не проходит гейт, ``docs/modeling_training.md`` §8), ``--no-promote``
    оставляет ``latest`` человеку.
    """
    return [
        SyncCommand(
            argv=[
                sys.executable,
                "-m",
                "modeling.cli",
                "build-dataset",
                "--mode",
                "train",
                "--output-dir",
                "artifacts/datasets",
                "--feature-set-version",
                "v2",
                "--rolling-windows",
                "5,10,20",
                "--min-prior-games",
                "5",
            ],
            cwd=_REPO_ROOT,
        ),
        SyncCommand(
            argv=[
                sys.executable,
                "-m",
                "modeling.cli",
                "train",
                "--config",
                "configs/modeling_default.yaml",
                "--task",
                "home_win",
                "--no-promote",
            ],
            cwd=_REPO_ROOT,
        ),
    ]


def run_retrain(
    commands: Sequence[SyncCommand], status_file: Path, sync_status_file: Path, now: datetime
) -> bool:
    """Один прогон retrain: предусловие sync, затем цепочка *commands*.

    Если последний sync не успешен или протух (``scheduled_sync.check`` != 0), цепочка
    не стартует: в *status_file* пишется неуспех с пояснением, в лог — ошибка.

    Аргументы:
        commands: цепочка команд (``build_commands()``).
        status_file: путь статуса retrain.
        sync_status_file: путь статуса sync (предусловие).
        now: текущий момент (UTC) для проверки свежести sync.

    Возвращает: ``True``, если цепочка завершилась кодом 0.
    """
    if scheduled_sync.check(sync_status_file, now) != 0:
        logger.error("Retrain not started: last sync run is missing, failed or stale")
        scheduled_sync._write_status_atomic(
            status_file,
            {
                "finished_at": now.isoformat(),
                "ok": False,
                "failed_command": "precondition: last sync run is not ok/fresh",
                "returncode": None,
                "window": None,
            },
        )
        return False

    ok = scheduled_sync.run_once(commands, status_file, name="Retrain")
    if ok:
        logger.info(
            "Retrain ok, latest NOT moved. Run ids are in the train lines above "
            "(run_id=... task=... model=... status=...). Promote manually: "
            "make modeling-promote TASK=home_win MODEL=<lgbm|logreg> RUN_ID=<run_id>; "
            "report: artifacts/reports/<run_id>/summary.md"
        )
    return ok


def _run_now() -> bool:
    """Прогон retrain с боевыми путями статусов и текущим временем."""
    return run_retrain(
        build_commands(), STATUS_FILE, scheduled_sync.STATUS_FILE, datetime.now(timezone.utc)
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI: подкоманды ``loop`` / ``once`` / ``check``.

    - ``loop``: при старте прогон, только если ``needs_catch_up``; затем бесконечно спит
      до ближайшего понедельника ``RETRAIN_HOUR_UTC``:00 UTC и прогоняет. Неуспешный
      прогон цикл не останавливает (отказ виден через статус-файл и ``check``).
    - ``once``: один прогон (предусловие + цепочка), код выхода 0/1.
    - ``check``: 0, если статус есть, ``ok`` и моложе ``STALE_AFTER``; иначе 1.

    Аргументы:
        argv: аргументы командной строки без имени программы; ``None`` — ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(
        description="Scheduled weekly retrain (pipeline/scheduled_retrain.py).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    subparsers.add_parser("loop", help="Run forever: catch-up if due, then weekly.")
    subparsers.add_parser("once", help="Run once (sync precondition + chain), exit 0/1.")
    subparsers.add_parser("check", help="Healthcheck exit code from the last run's status.")
    args = parser.parse_args(argv)

    if args.subcommand == "check":
        return scheduled_sync.check(STATUS_FILE, datetime.now(timezone.utc), STALE_AFTER)

    if args.subcommand == "once":
        ok = _run_now()
        return 0 if ok else 1

    # loop
    if needs_catch_up(STATUS_FILE, datetime.now(timezone.utc)):
        _run_now()
    target = datetime.now(timezone.utc)
    while True:
        target, sleep_seconds = scheduled_sync._next_target(
            datetime.now(timezone.utc), target, seconds_until_next_run
        )
        logger.info("Sleeping %.0f seconds until next retrain run", sleep_seconds)
        time.sleep(sleep_seconds)
        _run_now()


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    sys.exit(main())
