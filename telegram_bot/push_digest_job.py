#!/usr/bin/env python3
"""Рассылка утреннего дайджеста, итогов по команде и игроков страны подписчикам.

Отдельный скрипт вне процесса бота: поднимает собственный ``Application``
(без polling и без ``JobQueue``), рассылает и завершается. Запускается сервисом
`sync` (`pipeline/scheduled_sync.py`) после каждого успешного прогона загрузчика —
раз в 30 минут ночью — и сам решает, кому пора: дайджест уходит в более позднее из
двух — выбранное подписчиком время или загрузка последнего матча ночи (Задача 60);
отметка ``last_sent_night`` не даёт отправить ночь дважды. Подписка на страну
(Задача 61) — сообщение об игроках страны за ночь и альбомы видео их голов, в выбранное
подписчиком время по тому же правилу, что и дайджест. Сам выходит с кодом 0 и
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
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar, Union

from telegram import Bot, InputMediaVideo
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackContext

# Запуск из каталога telegram_bot (как make bot).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
from bot_messages import (  # noqa: E402
    MSK,
    country_night_goals,
    country_night_message,
    day_digest,
    game_message,
)
from nhl_scoreboard import fetch_score  # noqa: E402
from stats_handlers import dispatch_day_digest_messages  # noqa: E402
from subscription_repo import (  # noqa: E402
    LATEST_DIGEST_TIME,
    list_active_country_rows,
    list_active_morning_digest_rows,
    list_active_team_scores_rows,
    mark_night_sent,
    mark_subscription_inactive_by_chat_kind_team,
)
from database import fetch_all  # noqa: E402
from video_replay import download_goal_video  # noqa: E402

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

# Telegram принимает в одном альбоме (sendMediaGroup) от 2 до 10 медиа.
_ALBUM_SIZE = 10

_T = TypeVar("_T")


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


async def _throttled(call: Callable[[], Awaitable[_T]], chat_id: int) -> _T:
    """Выполняет одну отправку и выдерживает паузу до следующей.

    Зачем: ``RetryAfter`` означает, что отправка не доставлена, — повторяем
    её один раз после указанной API задержки. Второй ``RetryAfter``, как и
    любая другая ошибка Telegram, улетает вызывающему.

    Аргументы: ``call`` — отправка без аргументов (её можно вызвать повторно);
    ``chat_id`` — получатель, для лога.
    """
    try:
        result = await call()
    except RetryAfter as exc:
        wait = float(exc.retry_after)
        logger.warning("429 RetryAfter %ss for chat_id=%s", wait, chat_id)
        await asyncio.sleep(wait)
        result = await call()
    # Telegram лимитирует бота ~30 сообщениями в секунду.
    await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))
    return result


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
            await _throttled(
                lambda: context.bot.send_message(
                    chat_id=chat_id,
                    text=f"<b>Ваши матчи ({night})</b>\n\n{html_body}",
                    parse_mode="HTML",
                ),
                chat_id,
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


async def _prepare_goal_clips(
    goals: List[Tuple[int, int, str]], cache: Dict[Tuple[int, int], Optional[str]]
) -> List[Tuple[Tuple[int, int], Union[str, bytes], Dict[str, Any]]]:
    """Клипы голов к отправке: (ключ кэша, ``file_id`` из кэша или байты mp4, параметры,
    общие для ``send_video`` и ``InputMediaVideo``).

    Байты, а не готовый ``InputFile``: так PTB сам ставит ``attach://`` в альбоме —
    без него Telegram альбом не примет.

    Аргументы: ``goals`` — (game_id, event_id, подпись); ``cache`` — (game_id,
    event_id) → ``file_id`` Telegram, ``None`` — клипа нет (второй раз не качаем).
    Гол без клипа пропускается с записью в лог.
    """
    clips: List[Tuple[Tuple[int, int], Union[str, bytes], Dict[str, Any]]] = []
    for game_id, event_id, caption in goals:
        key = (game_id, event_id)
        if key in cache:
            file_id = cache[key]
            if file_id is not None:
                clips.append((key, file_id, {"caption": caption}))
            continue
        # В отдельном потоке: скачивание и ffmpeg блокируют event loop (см. video_replay).
        d = await asyncio.to_thread(download_goal_video, game_id, event_id)
        if d is None:
            logger.warning("No video for goal game_id=%s event_id=%s", game_id, event_id)
            cache[key] = None
            continue
        params: Dict[str, Any] = {
            "caption": caption, "filename": f"{event_id}.mp4",
            "width": d.width, "height": d.height, "duration": d.duration,
        }
        try:
            if d.thumb_path:
                params["thumbnail"] = Path(d.thumb_path).read_bytes()
            video = Path(d.path).read_bytes()
        finally:
            os.unlink(d.path)
            if d.thumb_path:
                os.unlink(d.thumb_path)
        clips.append((key, video, params))
    return clips


async def _send_goal_videos(
    bot: Bot,
    chat_id: int,
    goals: List[Tuple[int, int, str]],
    cache: Dict[Tuple[int, int], Optional[str]],
) -> None:
    """Шлёт видео голов чату альбомами по ``_ALBUM_SIZE`` (хвост из одного видео —
    ``send_video``: альбом требует минимум два медиа).

    Зачем: ``file_id`` отправленных видео кладётся в ``cache`` — следующим
    подписчикам клип уходит без повторной загрузки.
    """
    clips = await _prepare_goal_clips(goals, cache)
    for start in range(0, len(clips), _ALBUM_SIZE):
        chunk = clips[start:start + _ALBUM_SIZE]
        if len(chunk) == 1:
            _, video, params = chunk[0]
            sent = [
                await _throttled(
                    lambda: bot.send_video(
                        chat_id=chat_id, video=video, supports_streaming=True, **params
                    ),
                    chat_id,
                )
            ]
        else:
            media = [
                InputMediaVideo(video, supports_streaming=True, **params)
                for _, video, params in chunk
            ]
            sent = list(
                await _throttled(lambda: bot.send_media_group(chat_id=chat_id, media=media), chat_id)
            )
        for (key, _, _), message in zip(chunk, sent):
            assert message.video is not None  # ответ на отправку видео всегда содержит video
            cache[key] = message.video.file_id


async def run_country_broadcast(
    context: CallbackContext, now: datetime, night: date, night_loaded: bool
) -> None:
    """Игроки страны за ночь *night* и видео их голов — подписчикам ``country_players``.

    Зачем: подписка на страну (Задача 61) — сообщение об игроках страны и альбомы
    видео голов; время и ``last_sent_night`` — по каждой паре (чат, страна). Ночь без
    игроков страны ничего не шлёт, но отмечается отправленной.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``now``, ``night``, ``night_loaded`` — см. ``is_due``.
    """
    due = [
        (chat_id, country)
        for chat_id, country, send_time, last_sent in list_active_country_rows()
        if last_sent != night and is_due(now, night, send_time, night_loaded)
    ]
    texts: Dict[str, Optional[str]] = {}
    goals: Dict[str, List[Tuple[int, int, str]]] = {}
    file_ids: Dict[Tuple[int, int], Optional[str]] = {}
    for chat_id, country in due:
        if country not in texts:
            texts[country] = country_night_message(country, night)
            goals[country] = country_night_goals(country, night) if texts[country] else []
        text = texts[country]
        if text is None:
            logger.info("No %s players in night %s: nothing sent", country, night)
        else:
            try:
                await _throttled(
                    lambda: context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML"),
                    chat_id,
                )
                await _send_goal_videos(context.bot, chat_id, goals[country], file_ids)
            except Forbidden:
                logger.info("chat_id=%s blocked bot; deactivate country %s", chat_id, country)
                mark_subscription_inactive_by_chat_kind_team(
                    chat_id, "country_players", None, country
                )
            except TelegramError as exc:
                logger.warning("country send failed chat_id=%s %s: %s", chat_id, country, exc)
        mark_night_sent(chat_id, "country_players", None, night, country=country)


async def main() -> None:
    """Точка входа скрипта (запускается сервисом `sync`): проверяет флаги и прогоняет три рассылки.

    Ночь и её готовность (запрос слейта к NHL API, ``night_is_loaded``) считаются
    один раз на запуск и общие для всех рассылок; после крайнего срока API не
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
        Application.builder()
        .token(config.TOKEN)
        .updater(None)
        .job_queue(None)
        # Альбом из 10 клипов (~9 МБ каждый) не загрузится за 20 с по умолчанию.
        .media_write_timeout(300)
        .build()
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
        await run_country_broadcast(context, now, night, night_loaded)
    logger.info("Рассылка завершена.")


if __name__ == "__main__":
    asyncio.run(main())
