"""Планировщик автообновления данных (Задача 34).

Единственный сервис, который вызывает ``load_season_modern.py`` без участия
человека: ночью, каждые ``SYNC_STEP`` с ``NIGHT_START_HOUR_UTC`` до ``NIGHT_END_HOUR_UTC``,
прогоняет загрузчик за скользящее окно дат — завершённые матчи появляются в БД по ходу
ночи; после каждого прогона зовёт ``push_digest_job.py``, который сам решает, кому из
подписчиков пора (Задача 60). Пишет статус
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
from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Ночное окно прогонов: каждые SYNC_STEP с 18:00 UTC (21:00 МСК) до 08:00 UTC (11:00 МСК)
# включительно — сутки игр NHL от дневных матчей выходных до поздних западных. МСК без
# перехода на летнее время, смещение UTC+3 постоянно. Слот NIGHT_END_HOUR_UTC — последний
# в ночи и крайний срок дайджеста (push_digest_job.NIGHT_DEADLINE_MSK — тот же момент по МСК).
NIGHT_START_HOUR_UTC = 18
NIGHT_END_HOUR_UTC = 8
SYNC_STEP = timedelta(minutes=30)

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


def _in_night(slot: datetime) -> bool:
    """Лежит ли точка сетки ``SYNC_STEP`` в ночном окне ``NIGHT_START_HOUR_UTC``–``NIGHT_END_HOUR_UTC``:00."""
    return slot.hour >= NIGHT_START_HOUR_UTC or (slot.hour, slot.minute) <= (NIGHT_END_HOUR_UTC, 0)


def seconds_until_next_run(now: datetime) -> float:
    """Секунд до ближайшего слота прогона строго после *now*.

    Слоты — точки сетки ``SYNC_STEP`` (:00 и :30) внутри ночного окна; днём, после
    ``NIGHT_END_HOUR_UTC``:00, следующий слот — ``NIGHT_START_HOUR_UTC``:00. Если *now*
    ровно на слоте — возвращает время до следующего, а не 0.

    Аргументы:
        now: текущий момент (ожидается UTC; tzinfo сохраняется в сравнении).
    """
    step_minutes = int(SYNC_STEP.total_seconds() // 60)
    slot = now.replace(minute=now.minute - now.minute % step_minutes, second=0, microsecond=0)
    slot += SYNC_STEP
    while not _in_night(slot):
        slot += SYNC_STEP
    return (slot - now).total_seconds()


def _next_target(
    now: datetime,
    target: datetime,
    seconds_until: Callable[[datetime], float] = seconds_until_next_run,
) -> Tuple[datetime, float]:
    """Следующий целевой момент прогона в цикле и секунды сна до него.

    *target* — момент последнего запланированного прогона (или момента старта
    цикла, до самого первого прогона). Следующий слот считается не от *now*
    само по себе, а от ``max(now, target)``: если бы считали только от *now*,
    неточность ``time.sleep`` (или скачок часов) могла бы разбудить цикл на
    волосок раньше ``target`` — тогда *now* всё ещё "видит" себя ДО уже
    состоявшегося прогона, `seconds_until_next_run(now)` вернула бы то же
    самое (уже отработанное) время почти без задержки, и загрузчик прогнался бы
    второй раз подряд за тот же слот. Взяв больший из *now*/*target*, для уже
    прошедшего слота следующий шаг всегда считается на сутки вперёд от него
    самого. Долгий простой (например, контейнер был приостановлен на
    несколько дней) не ломается симметрично: тогда *now* заведомо позже
    *target*, и следующий слот считается от актуального текущего момента, а
    не откуда-то из прошлого.

    Аргументы:
        now: текущий момент (UTC).
        target: момент последнего прогона/старта цикла (UTC).
        seconds_until: расчёт секунд до ближайшего слота после переданного
            момента; по умолчанию ночная сетка sync (``scheduled_retrain``
            подставляет свой суточный слот).

    Возвращает: ``(next_target, sleep_seconds)`` — момент следующего прогона
    (передать как *target* следующему вызову) и секунды сна до него.
    """
    effective = max(now, target)
    sleep_seconds = seconds_until(effective)
    next_target = effective + timedelta(seconds=sleep_seconds)
    return next_target, sleep_seconds


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


def run_once(
    commands: Sequence[SyncCommand],
    status_file: Path,
    window: Optional[Tuple[str, str]] = None,
    name: str = "Sync",
) -> bool:
    """Выполняет *commands* по очереди, останавливаясь на первой ошибке.

    Каждая команда запускается через ``subprocess.run(..., check=False)``;
    ненулевой код останавливает цепочку (дайджест не шлётся после неудачной
    загрузки). Итог пишется атомарно в *status_file* и логируется (INFO при
    успехе, ERROR с командой и кодом — при неуспехе).

    Аргументы:
        commands: команды в порядке выполнения (см. ``build_commands``).
        status_file: путь JSON-файла статуса для healthcheck (``check``).
        window: ``(date_from, date_to)`` — сохраняется в статусе как контекст
            прогона, на выполнение команд не влияет; ``None`` — у прогонов без окна дат.
        name: название прогона в строках лога (``scheduled_retrain`` — "Retrain").

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
        "window": list(window) if window else None,
    }
    _write_status_atomic(status_file, status)

    if ok:
        logger.info("%s run succeeded (window=%s)", name, window)
    else:
        logger.error(
            "%s run failed: command=%r returncode=%s (window=%s)",
            name,
            failed_command,
            returncode,
            window,
        )
    return ok


def build_commands(window: Tuple[str, str]) -> List[SyncCommand]:
    """Строит команды одного прогона: загрузчик, затем рассылка.

    Рассылка идёт после каждого прогона: ``push_digest_job.py`` сам решает, кому
    пора, и не шлёт ночь дважды; сам завершается кодом 0 и логирует, если
    ``ENABLE_PUSH_DIGEST`` выключен — здесь эти проверки не дублируются.

    Аргументы:
        window: ``(date_from, date_to)`` для ``--date-from``/``--date-to`` загрузчика.
    """
    date_from, date_to = window
    return [
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
        ),
        SyncCommand(argv=[sys.executable, "-u", "push_digest_job.py"], cwd=_TELEGRAM_BOT_DIR),
    ]


def check(
    status_file: Path,
    now: datetime,
    stale_after: timedelta = STALE_AFTER,
    name: str = "Sync",
) -> int:
    """Код выхода healthcheck: 0 — данные свежие, иначе 1.

    Нездоровые случаи (файла нет / последний прогон неуспешен / устарел)
    логируются с понятной причиной перед возвратом 1.

    Аргументы:
        status_file: путь JSON-файла статуса, пишет ``run_once``.
        now: текущий момент (UTC), с которым сравнивается ``finished_at``.
        stale_after: порог устаревания статуса (у sync — сутки, у retrain — 8 дней).
        name: название прогона в строках лога (``scheduled_retrain`` — "Retrain").
    """
    if not status_file.exists():
        logger.error("%s status file not found: %s", name, status_file)
        return 1

    data = json.loads(status_file.read_text(encoding="utf-8"))

    if not data.get("ok"):
        logger.error("Last %s run failed: %s", name.lower(), status_file)
        return 1

    finished_at = datetime.fromisoformat(data["finished_at"])
    if now - finished_at > stale_after:
        logger.error(
            "%s status is stale: finished_at=%s older than %s", name, finished_at, stale_after
        )
        return 1

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI: подкоманды ``loop`` / ``once`` / ``check``.

    - ``loop``: сразу при старте — прогон (догнать данные после перезапуска;
      повторной рассылки нет — ночь отмечается отправленной в БД), затем
      бесконечно спит до следующего ночного слота и прогоняет. Неуспешный прогон не
      роняет цикл (отказ виден через статус-файл и ``check``), но исключения
      самого цикла (не команд) не глушатся.
    - ``once``: один прогон, код выхода 0/1.
    - ``check``: см. ``check()``.

    Аргументы:
        argv: аргументы командной строки без имени программы; ``None`` — взять
            ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(
        description="Scheduled nightly NHL data sync (pipeline/scheduled_sync.py).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    subparsers.add_parser("loop", help="Run forever: catch-up run, then every SYNC_STEP at night.")
    subparsers.add_parser("once", help="Run once (loader + digest job), exit 0/1.")
    subparsers.add_parser("check", help="Healthcheck exit code from the last run's status.")
    args = parser.parse_args(argv)

    if args.subcommand == "once":
        window = sync_window(datetime.now(timezone.utc).date())
        ok = run_once(build_commands(window), STATUS_FILE, window)
        return 0 if ok else 1

    if args.subcommand == "check":
        return check(STATUS_FILE, datetime.now(timezone.utc))

    # loop
    catch_up_window = sync_window(datetime.now(timezone.utc).date())
    run_once(build_commands(catch_up_window), STATUS_FILE, catch_up_window)
    target = datetime.now(timezone.utc)
    while True:
        target, sleep_seconds = _next_target(datetime.now(timezone.utc), target)
        logger.info("Sleeping %.0f seconds until next sync run", sleep_seconds)
        time.sleep(sleep_seconds)
        window = sync_window(datetime.now(timezone.utc).date())
        run_once(build_commands(window), STATUS_FILE, window)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    sys.exit(main())
