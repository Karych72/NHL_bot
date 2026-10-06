#!/usr/bin/env python3
"""Рассылка утреннего дайджеста и кратких итогов по команде подписчикам.

Отдельный скрипт вне процесса бота: поднимает собственный ``Application``
(без polling и без ``JobQueue``), рассылает и завершается. Запускается сервисом
`sync` (`pipeline/scheduled_sync.py`) после каждого успешного прогона загрузчика —
раз в 30 минут ночью — и сам решает, кому пора: дайджест уходит в более позднее из
двух — выбранное подписчиком время или загрузка последнего матча ночи (Задача 60);
отметка ``last_sent_night`` не даёт отправить ночь дважды. Сам выходит с кодом 0 и
логирует, если ``ENABLE_PUSH_DIGEST`` выключен. Ручной запуск — та же команда:

    cd telegram_bot && ENABLE_PUSH_DIGEST=1 ../.venv/bin/python push_digest_job.py

Требует TELEGRAM_BOT_TOKEN в окружении, таблицу bot_subscriptions и загруженную NHL-БД.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackContext

# Запуск из каталога telegram_bot (как make bot).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
from bot_messages import MSK, day_digest, game_message  # noqa: E402
from nhl_scoreboard import fetch_score  # noqa: E402
from stats_handlers import dispatch_day_digest_messages  # noqa: E402
from subscription_repo import (  # noqa: E402
    LATEST_DIGEST_TIME,
    list_active_morning_digest_rows,
    list_active_team_scores_rows,
    mark_night_sent,
    mark_subscription_inactive_by_chat_kind_team,
)
from database import fetch_all  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("push_digest_job")

# Крайний срок ночи — последний ночной прогон sync (08:00 UTC). Прогонов позже нет, поэтому
# недозагруженная ночь (перенос матча) уходит в этот момент с тем, что есть в БД. Если сам
# этот прогон упал на загрузчике, рассылка не запускается и ночь не уходит вовсе.
NIGHT_DEADLINE_MSK = LATEST_DIGEST_TIME

# Сдвиг, переводящий момент ночи в игровую дату лиги: матчи даты D (ET) идут
# с 16:00 UTC D до ~08:00 UTC D+1, и любой момент ночного окна sync минус 12 часов
# попадает в D.
_NIGHT_SHIFT = timedelta(hours=12)

# Матчи, которые загрузчик пишет в ``games``: регулярка (``gameType`` 2) по расписанию
# (``gameScheduleState`` OK) — перенесённый или отменённый матч ночь не держит.
_REGULAR_SEASON_GAME_TYPE = 2
_SCHEDULED_AS_PLANNED = "OK"


def night_of(now: datetime) -> date:
    """Игровая дата ночи, которой принадлежит момент *now*.

    Зачем: рассылка отмечается по ночи, а не по календарю — прогоны идут через полночь.

    Аргументы:
        now: aware-момент.
    """
    return (now.astimezone(timezone.utc) - _NIGHT_SHIFT).date()


def _msk_moment(night: date, at: time) -> datetime:
    """Момент *at* по МСК утром после ночи *night*."""
    return datetime.combine(night + timedelta(days=1), at, tzinfo=MSK)


def is_due(now: datetime, night: date, send_time: Optional[time], night_loaded: bool) -> bool:
    """Пора ли слать рассылку за ночь *night*.

    Пора в более позднее из двух: время подписчика или загрузка всей ночи; после
    крайнего срока ``NIGHT_DEADLINE_MSK`` — в любом случае.

    Аргументы:
        now: текущий момент (aware).
        night: игровая дата ночи (``night_of``).
        send_time: время подписчика по МСК; ``None`` — своего времени нет
            (``team_scores``), ждать только загрузки ночи.
        night_loaded: все ли матчи ночи уже в ``games`` (``night_is_loaded``).
    """
    if send_time is not None and now < _msk_moment(night, send_time):
        return False
    return night_loaded or now >= _msk_moment(night, NIGHT_DEADLINE_MSK)


def night_is_loaded(night: date) -> bool:
    """Все ли матчи регулярки ночи *night* из расписания NHL уже в ``games``.

    Зачем: дайджест не должен уходить неполным. Слейт берётся из ``score/{night}``
    NHL API, а не из ``scheduled_games``: та держит только не начавшиеся игры
    окна «сегодня+завтра UTC», и идущий или только что закончившийся матч из неё
    выпадает раньше, чем попадает в ``games``.

    Аргументы:
        night: игровая дата ночи.
    """
    slate = [
        int(g["id"])
        for g in fetch_score(night)["games"]
        if g["gameDate"] == night.isoformat()
        and g["gameType"] == _REGULAR_SEASON_GAME_TYPE
        and g["gameScheduleState"] == _SCHEDULED_AS_PLANNED
    ]
    loaded = fetch_all(
        "SELECT game_id FROM games WHERE game_id = ANY(%s)", (slate,), columns=["game_id"]
    )
    missing = set(slate) - set(loaded["game_id"])
    if missing:
        logger.info("Night %s not loaded yet: games %s missing", night, sorted(missing))
    return not missing


def _game_ids_for_team_on_calendar_day(team_id: int, day_iso: str) -> list:
    row = fetch_all(
        "SELECT game_id FROM games WHERE season_id = %s AND day = %s::date "
        "AND (home_team_id = %s OR away_team_id = %s) ORDER BY game_id",
        (config.SEASON_ID, day_iso, team_id, team_id),
        columns=["game_id"],
    )
    return [int(row["game_id"][i]) for i in range(row["count_rows"])]


async def _send_throttled(bot: Bot, chat_id: int, **kwargs: Any) -> None:
    """Отправляет одно сообщение и выдерживает паузу до следующего.

    Зачем: ``RetryAfter`` означает, что сообщение не доставлено, — повторяем
    его один раз после указанной API задержки. Второй ``RetryAfter``, как и
    любая другая ошибка Telegram, улетает вызывающему.

    Аргументы: ``bot`` — клиент Bot API; ``chat_id`` — получатель;
    ``kwargs`` — параметры ``Bot.send_message`` (``text``, ``parse_mode``, …).
    """
    try:
        await bot.send_message(chat_id=chat_id, **kwargs)
    except RetryAfter as exc:
        wait = float(exc.retry_after)
        logger.warning("429 RetryAfter %ss for chat_id=%s", wait, chat_id)
        await asyncio.sleep(wait)
        await bot.send_message(chat_id=chat_id, **kwargs)
    # Telegram лимитирует бота ~30 сообщениями в секунду.
    await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))


async def run_morning_digest_broadcast(
    context: CallbackContext, now: datetime, night: date, night_loaded: bool
) -> None:
    """Шлёт дайджест ночи *night* подписчикам ``morning_digest``, которым пора.

    Зачем: ровно те же карточки, что и меню ``/stats``, но без навигации диалога
    (``attach_conv_nav_on_last=False``) — в рассылке кнопки диалога некуда вести.
    Чату, которому ночь уже ушла (``last_sent_night``), или чьё время не наступило
    (``is_due``), не шлётся ничего. После попытки ночь отмечается отправленной —
    один заход на чат, как и прежде. Ночь без матчей в БД не рассылается.
    Заблокировавший бота чат (``Forbidden``) деактивируется, чтобы не долбиться
    в него каждый день.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``now``, ``night``, ``night_loaded`` — см. ``is_due``.
    """
    due = [
        chat_id
        for chat_id, send_time, last_sent in list_active_morning_digest_rows()
        if last_sent != night and is_due(now, night, send_time, night_loaded)
    ]
    if not due:
        return
    day_label, games = day_digest(night)
    if games[0][0] == 0:
        logger.info("No games in DB for night %s: digest not sent", night)
        return
    for chat_id in due:
        try:
            await dispatch_day_digest_messages(
                context,
                chat_id,
                day_label,
                games,
                attach_conv_nav_on_last=False,
                inter_message_sleep_sec=config.PUSH_SEND_INTERVAL_SEC,
            )
        except Forbidden:
            logger.info("chat_id=%s blocked bot; deactivate morning_digest", chat_id)
            mark_subscription_inactive_by_chat_kind_team(
                chat_id, "morning_digest", None
            )
        except RetryAfter as exc:
            wait = float(exc.retry_after)
            logger.warning("digest RetryAfter %ss chat_id=%s", wait, chat_id)
            await asyncio.sleep(wait)
            try:
                await dispatch_day_digest_messages(
                    context,
                    chat_id,
                    day_label,
                    games,
                    attach_conv_nav_on_last=False,
                    inter_message_sleep_sec=config.PUSH_SEND_INTERVAL_SEC,
                )
            except Forbidden:
                mark_subscription_inactive_by_chat_kind_team(
                    chat_id, "morning_digest", None
                )
        except TelegramError as exc:
            logger.warning("digest send failed chat_id=%s: %s", chat_id, exc)
        mark_night_sent(chat_id, "morning_digest", None, night)
        # Telegram лимитирует бота ~30 сообщениями в секунду.
        await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))


async def run_team_scores_broadcast(
    context: CallbackContext, now: datetime, night: date, night_loaded: bool
) -> None:
    """Краткая строка по каждому матчу команды за ночь *night*.

    Зачем: подписка ``team_scores`` — одно короткое сообщение на чат со счётом
    матчей его команды за ночь; чат без матчей пропускается. Своего времени у
    подписки нет: уходит, как только ночь загружена (или к крайнему сроку), —
    короткому счёту ждать утра незачем.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``now``, ``night``, ``night_loaded`` — см. ``is_due``.
    """
    if not is_due(now, night, None, night_loaded):
        return
    for chat_id, team_id, last_sent in list_active_team_scores_rows():
        if last_sent == night:
            continue
        gids = _game_ids_for_team_on_calendar_day(team_id, night.isoformat())
        if not gids:
            continue
        parts = []
        for gid in gids:
            try:
                text, _meta = game_message(gid)
                line = text.strip().split("\n", 1)[0].strip()
                if line:
                    parts.append(line)
            except Exception:
                logger.exception("game_message failed game_id=%s", gid)
        if not parts:
            continue
        html_body = "\n".join(parts[:5])
        try:
            await _send_throttled(
                context.bot,
                chat_id,
                text=f"<b>Ваши матчи ({night})</b>\n\n{html_body}",
                parse_mode="HTML",
            )
        except Forbidden:
            logger.info(
                "chat_id=%s blocked bot; deactivate team_scores team_id=%s",
                chat_id,
                team_id,
            )
            mark_subscription_inactive_by_chat_kind_team(
                chat_id, "team_scores", team_id
            )
        except TelegramError as exc:
            logger.warning("team notify failed chat_id=%s: %s", chat_id, exc)
        mark_night_sent(chat_id, "team_scores", team_id, night)


async def main() -> None:
    """Точка входа скрипта (запускается сервисом `sync`): проверяет флаги и прогоняет обе рассылки.

    Ночь и её готовность (запрос слейта к NHL API, ``night_is_loaded``) считаются
    один раз на запуск и общие для обеих рассылок; после крайнего срока API не
    запрашивается.

    Зачем ``Application``, а не голый ``Bot``: ``dispatch_day_digest_messages``
    принимает ``CallbackContext``, а построить его можно только от ``Application``.
    Ни polling, ни ``JobQueue`` скрипту не нужны — отключены явно; ``async with``
    инициализирует и корректно гасит HTTP-клиент бота.
    """
    config.validate_env()
    if not config.ENABLE_PUSH_DIGEST:
        logger.info("ENABLE_PUSH_DIGEST выключен — рассылка пропущена.")
        return
    application = (
        Application.builder().token(config.TOKEN).updater(None).job_queue(None).build()
    )
    now = datetime.now(timezone.utc)
    night = night_of(now)
    # После крайнего срока готовность не нужна (``is_due``) — NHL API не спрашиваем,
    # чтобы его сбой не сорвал отправку по сроку.
    night_loaded = now >= _msk_moment(night, NIGHT_DEADLINE_MSK) or night_is_loaded(night)
    async with application:
        context = CallbackContext(application)
        await run_morning_digest_broadcast(context, now, night, night_loaded)
        await run_team_scores_broadcast(context, now, night, night_loaded)
    logger.info("Рассылка завершена.")


if __name__ == "__main__":
    asyncio.run(main())
