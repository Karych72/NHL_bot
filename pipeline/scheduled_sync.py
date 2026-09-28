"""Планировщик автообновления данных (Задача 34).

Единственный сервис, который вызывает ``load_season_modern.py`` без участия
человека: ежедневно в ``SYNC_HOUR_UTC`` прогоняет загрузчик за скользящее окно
дат и (если включена рассылка) ``push_digest_job.py`` следом. Пишет статус
последнего прогона на диск для docker-healthcheck (``check``). Только stdlib —
новых зависимостей нет.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Ежедневный запуск в 08:00 UTC (11:00 МСК) — к этому времени матчи в Северной
# Америке уже закончены.
SYNC_HOUR_UTC = 8

# Окно загрузки [сегодня − WINDOW_DAYS_BACK, сегодня] (UTC-дата): захватывает
# поздние финалы вчерашнего дня, которые NHL API мог доотдать после полуночи.
WINDOW_DAYS_BACK = 2

# Порог "данные протухли" для healthcheck (check()).
STALE_AFTER = timedelta(hours=26)

# Путь от __file__, как RAW_CACHE_DIR в load_season_modern.py.
STATUS_FILE = Path(__file__).resolve().parents[1] / "all_data" / "sync_status.json"

_PIPELINE_DIR = Path(__file__).resolve().parent
_TELEGRAM_BOT_DIR = _PIPELINE_DIR.parent / "telegram_bot"


class SyncCommand(NamedTuple):
    """Одна команда прогона: аргументы процесса и рабочий каталог.

    Отдельный тип вместо голого списка/кортежа — ``run_once`` логирует и
    ``argv``, и ``cwd`` при падении, а падение на позиционном ``tuple[list, Path]``
    легко перепутать местами (как ``SeasonReferenceRows`` в load_season_modern.py).
    """

    argv: List[str]
    cwd: Path


def sync_window(today: date) -> Tuple[str, str]:
    """Возвращает окно загрузки ``(today - WINDOW_DAYS_BACK, today)`` как ISO-строки.

    Аргументы:
        today: UTC-дата "сегодня", от которой строится окно.
    """
    start = today - timedelta(days=WINDOW_DAYS_BACK)
    return start.isoformat(), today.isoformat()


def seconds_until_next_run(now: datetime) -> float:
    """Секунд до ближайших ``SYNC_HOUR_UTC:00`` строго после *now*.

    Если *now* ровно ``SYNC_HOUR_UTC:00`` или позже — возвращает время до
    завтрашнего запуска, а не 0/отрицательное число.

    Аргументы:
        now: текущий момент (ожидается UTC; tzinfo сохраняется в сравнении).
    """
    next_run = datetime.combine(
        now.date(), datetime.min.time().replace(hour=SYNC_HOUR_UTC), tzinfo=now.tzinfo
    )
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


def _write_status_atomic(status_file: Path, status: dict) -> None:
    """Пишет JSON статуса атомарно: tmp-файл рядом + ``os.replace``.

    Тот же приём, что кэш ``fetch_game_json`` в load_season_modern.py — процесс,
    прерванный между записью и переименованием, не может оставить
    ``status_file``, который существует, но содержит битый JSON.
    """
    status_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = status_file.with_name(status_file.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(status, fh)
    os.replace(tmp_path, status_file)


def run_once(commands: Sequence[SyncCommand], status_file: Path, window: Tuple[str, str]) -> bool:
    """Выполняет *commands* по очереди, останавливаясь на первой ошибке.

    Каждая команда запускается через ``subprocess.run(..., check=False)``;
    ненулевой код останавливает цепочку (дайджест не шлётся после неудачной
    загрузки). Итог пишется атомарно в *status_file* и логируется (INFO при
    успехе, ERROR с командой и кодом — при неуспехе).

    Аргументы:
        commands: команды в порядке выполнения (см. ``build_commands``).
        status_file: путь JSON-файла статуса для healthcheck (``check``).
        window: ``(date_from, date_to)`` — сохраняется в статусе как контекст
            прогона, на выполнение команд не влияет.

    Возвращает: ``True``, если все команды завершились кодом 0.
    """
    failed_command: Optional[str] = None
    returncode: Optional[int] = None
    ok = True
    for cmd in commands:
        logger.info("Running: %s (cwd=%s)", " ".join(cmd.argv), cmd.cwd)
        result = subprocess.run(cmd.argv, cwd=cmd.cwd, check=False)
        if result.returncode != 0:
            ok = False
            failed_command = " ".join(cmd.argv)
            returncode = result.returncode
            break

    status = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "failed_command": failed_command,
        "returncode": returncode,
        "window": list(window),
    }
    _write_status_atomic(status_file, status)

    if ok:
        logger.info("Sync run succeeded (window=%s)", window)
    else:
        logger.error(
            "Sync run failed: command=%r returncode=%s (window=%s)",
            failed_command,
            returncode,
            window,
        )
    return ok


def build_commands(window: Tuple[str, str], with_digest: bool) -> List[SyncCommand]:
    """Строит команды одного прогона: загрузчик, затем опционально дайджест.

    ``push_digest_job.py`` сам завершается кодом 0 и логирует, если
    ``ENABLE_PUSH_DIGEST`` выключен — здесь эта проверка не дублируется.

    Аргументы:
        window: ``(date_from, date_to)`` для ``--date-from``/``--date-to`` загрузчика.
        with_digest: добавлять ли команду рассылки дайджеста после загрузки.
    """
    date_from, date_to = window
    commands = [
        SyncCommand(
            argv=[
                sys.executable,
                "-u",
                "load_season_modern.py",
                "--date-from",
                date_from,
                "--date-to",
                date_to,
            ],
            cwd=_PIPELINE_DIR,
        )
    ]
    if with_digest:
        commands.append(
            SyncCommand(argv=[sys.executable, "-u", "push_digest_job.py"], cwd=_TELEGRAM_BOT_DIR)
        )
    return commands


def check(status_file: Path, now: datetime) -> int:
    """Код выхода healthcheck: 0 — данные свежие, иначе 1.

    Нездоровые случаи (файла нет / последний прогон неуспешен / устарел)
    логируются с понятной причиной перед возвратом 1.

    Аргументы:
        status_file: путь JSON-файла статуса, пишет ``run_once``.
        now: текущий момент (UTC), с которым сравнивается ``finished_at``.
    """
    if not status_file.exists():
        logger.error("Sync status file not found: %s", status_file)
        return 1

    data = json.loads(status_file.read_text(encoding="utf-8"))

    if not data.get("ok"):
        logger.error("Last sync run failed: %s", status_file)
        return 1

    finished_at = datetime.fromisoformat(data["finished_at"])
    if now - finished_at > STALE_AFTER:
        logger.error(
            "Sync status is stale: finished_at=%s older than %s", finished_at, STALE_AFTER
        )
        return 1

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI: подкоманды ``loop`` / ``once`` / ``check``.

    - ``loop``: сразу при старте — прогон БЕЗ дайджеста (догнать данные после
      перезапуска, не спамя подписчиков повторной рассылкой), затем бесконечно
      спит до ``SYNC_HOUR_UTC`` и прогоняет С дайджестом. Неуспешный прогон не
      роняет цикл (отказ виден через статус-файл и ``check``), но исключения
      самого цикла (не команд) не глушатся.
    - ``once``: один прогон с дайджестом, код выхода 0/1.
    - ``check``: см. ``check()``.

    Аргументы:
        argv: аргументы командной строки без имени программы; ``None`` — взять
            ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(
        description="Scheduled daily NHL data sync (pipeline/scheduled_sync.py).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    subparsers.add_parser("loop", help="Run forever: catch-up run, then daily at SYNC_HOUR_UTC.")
    subparsers.add_parser("once", help="Run once (loader + digest), exit 0/1.")
    subparsers.add_parser("check", help="Healthcheck exit code from the last run's status.")
    args = parser.parse_args(argv)

    if args.subcommand == "once":
        window = sync_window(datetime.now(timezone.utc).date())
        ok = run_once(build_commands(window, with_digest=True), STATUS_FILE, window)
        return 0 if ok else 1

    if args.subcommand == "check":
        return check(STATUS_FILE, datetime.now(timezone.utc))

    # loop
    catch_up_window = sync_window(datetime.now(timezone.utc).date())
    run_once(build_commands(catch_up_window, with_digest=False), STATUS_FILE, catch_up_window)
    while True:
        sleep_seconds = seconds_until_next_run(datetime.now(timezone.utc))
        logger.info("Sleeping %.0f seconds until next sync run", sleep_seconds)
        time.sleep(sleep_seconds)
        window = sync_window(datetime.now(timezone.utc).date())
        run_once(build_commands(window, with_digest=True), STATUS_FILE, window)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    sys.exit(main())
