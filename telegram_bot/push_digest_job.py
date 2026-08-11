#!/usr/bin/env python3
"""Рассылка утреннего дайджеста и кратких итогов по команде подписчикам.

Отдельный cron-скрипт вне процесса бота: поднимает собственный ``Application``
(без polling и без ``JobQueue``), рассылает и завершается. Запуск из cron после
загрузки данных в БД, например раз в день:

    cd telegram_bot && ENABLE_PUSH_DIGEST=1 ../.venv/bin/python push_digest_job.py

Требует TELEGRAM_BOT_TOKEN в окружении, таблицу bot_subscriptions и загруженную NHL-БД.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackContext

# Запуск из каталога telegram_bot (как make bot).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
from bot_messages import day_digest, game_message  # noqa: E402
from stats_handlers import dispatch_day_digest_messages  # noqa: E402
from subscription_repo import (  # noqa: E402
    list_active_morning_digest_chat_ids,
    list_active_team_scores_rows,
    mark_subscription_inactive_by_chat_kind_team,
)
from database import fetch_all  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("push_digest_job")


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


async def run_morning_digest_broadcast(context: CallbackContext) -> None:
    """Шлёт дайджест текущего дня всем активным подписчикам ``morning_digest``.

    Зачем: ровно те же карточки, что и меню ``/stats``, но без навигации диалога
    (``attach_conv_nav_on_last=False``) — в рассылке кнопки диалога некуда вести.
    Заблокировавший бота чат (``Forbidden``) деактивируется, чтобы не долбиться
    в него каждый день.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``.
    """
    day_label, games = day_digest()
    for chat_id in list_active_morning_digest_chat_ids():
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
        # Telegram лимитирует бота ~30 сообщениями в секунду.
        await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))


async def run_team_scores_broadcast(context: CallbackContext) -> None:
    """Календарный «вчера»: краткая строка по каждому матчу команды.

    Зачем: подписка ``team_scores`` — одно короткое сообщение на чат со счётом
    матчей его команды за вчерашний календарный день; чат без матчей пропускается.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``.
    """
    yday = (date.today() - timedelta(days=1)).isoformat()
    for chat_id, team_id in list_active_team_scores_rows():
        gids = _game_ids_for_team_on_calendar_day(team_id, yday)
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
                text=f"<b>Ваши матчи ({yday})</b>\n\n{html_body}",
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


async def main() -> None:
    """Точка входа cron-скрипта: проверяет флаги и прогоняет обе рассылки.

    Зачем ``Application``, а не голый ``Bot``: ``dispatch_day_digest_messages``
    принимает ``CallbackContext``, а построить его можно только от ``Application``.
    Ни polling, ни ``JobQueue`` скрипту не нужны — отключены явно; ``async with``
    инициализирует и корректно гасит HTTP-клиент бота.
    """
    if not config.TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN пуст — выход.")
        sys.exit(1)
    if not config.ENABLE_PUSH_DIGEST:
        logger.info("ENABLE_PUSH_DIGEST выключен — рассылка пропущена.")
        return
    application = (
        Application.builder().token(config.TOKEN).updater(None).job_queue(None).build()
    )
    async with application:
        context = CallbackContext(application)
        await run_morning_digest_broadcast(context)
        await run_team_scores_broadcast(context)
    logger.info("Рассылка завершена.")


if __name__ == "__main__":
    asyncio.run(main())
