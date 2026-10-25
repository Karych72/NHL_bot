#!/usr/bin/env python3
"""Рассылка утреннего дайджеста, итогов по команде, игроков страны и превью матчей подписчикам.

Отдельный скрипт вне процесса бота: поднимает собственный ``Application``
(без polling и без ``JobQueue``), рассылает и завершается. Запускается сервисом
`sync` (`pipeline/scheduled_sync.py`) после каждого успешного прогона загрузчика —
раз в 30 минут с 12:00 до 08:00 UTC — и сам решает, пора ли: дайджест и команды уходят одной рассылкой,
как только загружен последний матч ночи, а к 11:00 МСК — в любом случае (Задача 63;
времени у подписчика нет); отметка ``last_sent_night`` не даёт отправить ночь дважды.
Подписка на страну (Задачи 61, 75, 76) — по матчу, на первом прогоне после его конца,
когда выложены клипы голов: счёт и игроки страны с кнопкой карточки, за ними видео голов.
Предстоящие матчи (Задача 78) — на каждом прогоне с 15:00 МСК одно превью: ближайший ещё
не начавшийся матч дня, которого подписчик не получал; каждый матч — один раз. Дневные
слоты sync (с 12:00 UTC) зовут скрипт с ``--upcoming-only``: тогда идёт только превью.
Подписками управляет команда ``/subscriptions`` (``bot.py``); каждое сообщение рассылки
заканчивается строкой «Настроить подписки: /subscriptions». Сам выходит с кодом 0 и
логирует, если ``ENABLE_PUSH_DIGEST`` выключен. Ручной запуск — та же команда:

    cd telegram_bot && ENABLE_PUSH_DIGEST=1 ../.venv/bin/python push_digest_job.py

Требует TELEGRAM_BOT_TOKEN в окружении, таблицу bot_subscriptions и загруженную NHL-БД.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar, Union

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackContext

# Запуск из каталога telegram_bot (как make bot).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
from bot_messages import (  # noqa: E402
    MSK,
    country_night_blocks,
    country_night_goals,
    day_digest,
    game_message,
    game_score_line,
    matchup_season_preview,
)
from help_text import SUBSCRIPTIONS_FOOTER  # noqa: E402
from nhl_scoreboard import (  # noqa: E402
    fetch_score,
    league_today,
    parse_start_utc,
    slate_games_sorted,
)
from stats_handlers import (  # noqa: E402
    DIGEST_EXPAND_PREFIX,
    dispatch_day_digest_messages,
    goal_video_markup,
)
from subscription_repo import (  # noqa: E402
    list_active_country_rows,
    list_active_morning_digest_rows,
    list_active_team_scores_rows,
    list_active_upcoming_rows,
    mark_game_sent,
    mark_night_sent,
    mark_subscription_inactive_by_chat_kind_team,
    sent_games,
)
from database import fetch_all  # noqa: E402
from video_replay import download_goal_video, goal_clip_published  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("push_digest_job")

# Крайний срок ночи — последний ночной прогон sync (08:00 UTC). Прогонов позже нет, поэтому
# недозагруженная ночь (перенос матча) уходит в этот момент с тем, что есть в БД. Если сам
# этот прогон упал на загрузчике, рассылка не запускается и ночь не уходит вовсе.
NIGHT_DEADLINE_MSK = time(11, 0)

# Превью матчей дня лиги — не раньше первого дневного слота sync (решение человека, Задача 78;
# scheduled_sync.PREVIEW_START_HOUR_UTC — тот же момент по UTC).
UPCOMING_START_MSK = time(15, 0)

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

# Bot API принимает не больше 50 МБ файлов в одном запросе: альбом из 10 клипов по 8–19 МБ
# Telegram отверг, и голы ночи 2026-10-13 не ушли (Задача 75). Бюджет — с запасом на
# multipart-обвязку; клип из кэша (``file_id``) не загружается и бюджет не тратит.
_UPLOAD_BUDGET_BYTES = 45_000_000

# Подпись к видео и альбому — до 1024 символов видимого текста (UTF-16, без HTML-разметки).
_CAPTION_LIMIT = 1024

_HTML_TAG = re.compile(r"<[^>]+>")

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


async def run_morning_digest_broadcast(context: CallbackContext, night: date) -> None:
    """Шлёт дайджест ночи *night* подписчикам ``morning_digest``, которым пора.

    Зачем: ровно те же карточки, что и меню ``/stats``, но без навигации диалога
    (``attach_conv_nav_on_last=False``) — в рассылке кнопки диалога некуда вести.
    Чату, которому ночь уже ушла (``last_sent_night``), не шлётся ничего. После
    попытки ночь отмечается отправленной — один заход на чат, как и прежде. Ночь без
    матчей в БД не рассылается.
    Заблокировавший бота чат (``Forbidden``) деактивируется, чтобы не долбиться
    в него каждый день.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``night`` — игровая дата ночи (``night_of``).
    """
    due = [
        chat_id
        for chat_id, last_sent in list_active_morning_digest_rows()
        if last_sent != night
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


async def run_team_scores_broadcast(context: CallbackContext, night: date) -> None:
    """Полная карточка каждого матча команды за ночь *night*.

    Зачем: подписка ``team_scores`` — карточка матча (счёт, голы, вратари) с кнопками
    видео голов, как в меню, а не одна строка счёта; чат без матчей пропускается.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``night`` — игровая дата ночи (``night_of``).
    """
    for chat_id, team_id, last_sent in list_active_team_scores_rows():
        if last_sent == night:
            continue
        gids = _game_ids_for_team_on_calendar_day(team_id, night.isoformat())
        if not gids:
            continue
        cards = [game_message(gid) for gid in gids]
        try:
            for text, goals_meta in cards:
                await _throttled(
                    partial(
                        context.bot.send_message,
                        chat_id=chat_id,
                        text=f"<b>Матч вашей команды ({night})</b>\n\n{text}\n\n"
                        f"{SUBSCRIPTIONS_FOOTER}",
                        parse_mode="HTML",
                        reply_markup=goal_video_markup(goals_meta),
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


# Клип к отправке: (ключ кэша, ``file_id`` из кэша или байты mp4, параметры видео,
# HTML-описание гола ``goal_caption``).
_Clip = Tuple[Tuple[int, int], Union[str, bytes], Dict[str, Any], str]

# Кэш клипов на прогон: (game_id, event_id) → (``file_id`` Telegram, описание гола);
# ``None`` — клипа нет.
_ClipCache = Dict[Tuple[int, int], Optional[Tuple[str, str]]]


async def _prepare_goal_clips(goals: List[Tuple[int, int]], cache: _ClipCache) -> List[_Clip]:
    """Клипы голов к отправке: (ключ кэша, ``file_id`` из кэша или байты mp4, параметры,
    общие для ``send_video`` и ``InputMediaVideo``, описание гола).

    Байты, а не готовый ``InputFile``: так PTB сам ставит ``attach://`` в альбоме —
    без него Telegram альбом не примет.

    Аргументы: ``goals`` — (game_id, event_id); ``cache`` — см. ``_ClipCache``
    (гол без клипа второй раз не качаем).
    Гол без клипа пропускается с записью в лог.
    """
    clips: List[_Clip] = []
    for key in goals:
        game_id, event_id = key
        if key in cache:
            cached = cache[key]
            if cached is not None:
                clips.append((key, cached[0], {}, cached[1]))
            continue
        # В отдельном потоке: скачивание и ffmpeg блокируют event loop (см. video_replay).
        d = await asyncio.to_thread(download_goal_video, game_id, event_id)
        if d is None:
            logger.warning("No video for goal game_id=%s event_id=%s", game_id, event_id)
            cache[key] = None
            continue
        params: Dict[str, Any] = {
            "filename": f"{event_id}.mp4",
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
        clips.append((key, video, params, d.caption))
    return clips


def _upload_bytes(clip: _Clip) -> int:
    """Сколько байт клип добавит к запросу: mp4 и превью; ``file_id`` из кэша — ноль."""
    _, video, params, _ = clip
    if isinstance(video, str):
        return 0
    return len(video) + len(params.get("thumbnail", b""))


def _caption_length(text: str) -> int:
    """Длина подписи из HTML-текста — видимые символы в UTF-16, как считает Telegram."""
    return len(html.unescape(_HTML_TAG.sub("", text)).encode("utf-16-le")) // 2


def _album_caption(album: List[_Clip]) -> str:
    """Общая подпись альбома — описания его голов через пустую строку: в альбоме
    Telegram показывает только одну подпись."""
    return "\n\n".join(caption for _, _, _, caption in album)


def _albums(clips: List[_Clip]) -> List[List[_Clip]]:
    """Клипы по альбомам в исходном порядке: в каждом не больше ``_ALBUM_SIZE`` клипов,
    ``_UPLOAD_BUDGET_BYTES`` загружаемых байт и ``_CAPTION_LIMIT`` символов общей подписи.
    Клип тяжелее бюджета уходит один — Telegram его отвергнет, и ошибка всплывёт."""
    albums: List[List[_Clip]] = []
    size = 0
    for clip in clips:
        clip_size = _upload_bytes(clip)
        if (
            not albums
            or len(albums[-1]) == _ALBUM_SIZE
            or size + clip_size > _UPLOAD_BUDGET_BYTES
            or _caption_length(_album_caption(albums[-1] + [clip])) > _CAPTION_LIMIT
        ):
            albums.append([])
            size = 0
        albums[-1].append(clip)
        size += clip_size
    return albums


async def _send_country_game(
    bot: Bot,
    chat_id: int,
    text: str,
    game_id: int,
    goals: List[Tuple[int, int]],
    cache: _ClipCache,
) -> None:
    """Один матч рассылки по стране (Задача 76): короткое сообщение ``text`` (счёт и
    игроки страны) с кнопкой полной карточки матча, за ним видео голов страны альбомом.

    Кнопка — на тексте: к альбому Telegram клавиатуру не прикрепляет. Подпись альбома —
    описания всех его голов (``_album_caption``): кто забил, с чьих передач, когда. Клипы, не
    влезающие в один запрос (``_albums``), идут несколькими альбомами подряд;
    одиночный клип — ``send_video``: альбом требует минимум два медиа. ``file_id``
    отправленных видео кладётся в ``cache`` — следующим подписчикам клип уходит без
    повторной загрузки.

    Аргументы: ``bot``; ``chat_id`` — получатель; ``text`` — HTML сообщения;
    ``game_id`` — матч (для кнопки); ``goals`` — (game_id, event_id) голов страны в
    матче; ``cache`` — см. ``_prepare_goal_clips``.
    """
    card_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Карточка матча", callback_data=f"{DIGEST_EXPAND_PREFIX}{game_id}")]]
    )
    await _throttled(
        lambda: bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=card_button
        ),
        chat_id,
    )
    for album in _albums(await _prepare_goal_clips(goals, cache)):
        caption = _album_caption(album)
        if len(album) == 1:
            _, video, params, _ = album[0]
            sent = [
                await _throttled(
                    lambda: bot.send_video(
                        chat_id=chat_id, video=video, caption=caption, parse_mode="HTML",
                        supports_streaming=True, **params,
                    ),
                    chat_id,
                )
            ]
        else:
            media = [
                InputMediaVideo(
                    video, caption=caption if n == 0 else None, parse_mode="HTML",
                    supports_streaming=True, **params,
                )
                for n, (_, video, params, _) in enumerate(album)
            ]
            sent = list(
                await _throttled(lambda: bot.send_media_group(chat_id=chat_id, media=media), chat_id)
            )
        for (key, _, _, goal_text), message in zip(album, sent):
            assert message.video is not None  # ответ на отправку видео всегда содержит video
            cache[key] = (message.video.file_id, goal_text)


async def _clips_published(
    goals: List[Tuple[int, int]], published: Dict[Tuple[int, int], bool]
) -> bool:
    """Выложены ли клипы всех голов ``goals``; ответы NHL кэшируются в ``published``
    на прогон — матч проверяется один раз, сколько бы у него ни было подписчиков."""
    for goal in goals:
        if goal not in published:
            published[goal] = await asyncio.to_thread(goal_clip_published, *goal)
        if not published[goal]:
            return False
    return True


async def run_country_broadcast(context: CallbackContext, night: date, final: bool) -> None:
    """Матчи ночи *night* с игроками страны — подписчикам ``country_players``, по матчу.

    Зачем: подписка на страну (Задача 76) — сообщение о матче через полчаса после его
    конца, а не утром. Матч уходит на первом прогоне sync, когда он уже в ``games`` и у
    всех голов страны в нём NHL выложила клипы; ``final`` (крайний срок ночи) — с тем,
    что есть. Отметка — по паре (подписка, матч) в ``bot_game_sent``, ставится
    после попытки: сбой одного матча логируется и не повторяется, остальные уходят.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``night`` — игровая дата ночи (``night_of``); ``final`` — наступил ли крайний срок.
    """
    rows = list_active_country_rows()
    games: Dict[str, List[Tuple[int, str, List[Tuple[int, int]]]]] = {}
    for country in sorted({country for _, _, country in rows}):
        goals_by_game: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
        for goal in country_night_goals(country, night):
            goals_by_game[goal[0]].append(goal)
        games[country] = [
            (game_id, block, goals_by_game[game_id])
            for game_id, block in country_night_blocks(country, night)
        ]
    game_ids = sorted({game_id for country_games in games.values() for game_id, _, _ in country_games})
    if not game_ids:
        return
    sent = sent_games(game_ids)
    published: Dict[Tuple[int, int], bool] = {}
    score_lines: Dict[int, str] = {}
    file_ids: _ClipCache = {}
    for subscription_id, chat_id, country in rows:
        for game_id, block, goals in games[country]:
            if (subscription_id, game_id) in sent:
                continue
            if not final and not await _clips_published(goals, published):
                logger.info("game_id=%s: клипы %s ещё не выложены, ждём", game_id, country)
                continue
            if game_id not in score_lines:
                score_lines[game_id] = game_score_line(game_id)
            text = f"{score_lines[game_id]}\n\n{block}\n\n{SUBSCRIPTIONS_FOOTER}"
            try:
                await _send_country_game(context.bot, chat_id, text, game_id, goals, file_ids)
            except Forbidden:
                logger.info("chat_id=%s blocked bot; deactivate country %s", chat_id, country)
                mark_subscription_inactive_by_chat_kind_team(
                    chat_id, "country_players", None, country
                )
                break
            except TelegramError as exc:
                logger.warning(
                    "country send failed chat_id=%s %s game_id=%s: %s",
                    chat_id, country, game_id, exc,
                )
            mark_game_sent(subscription_id, game_id)


def upcoming_games(now: datetime) -> List[Tuple[int, datetime, str, str]]:
    """Ещё не начавшиеся к *now* матчи игрового дня лиги: (game_id, начало UTC, гости,
    хозяева) по времени начала.

    Зачем: превью подписки «Предстоящие матчи» (Задача 78). Слейт и время начала — из
    ``score/{день лиги}`` NHL API, тем же разбором, что ``/tonight``: в БД времени начала
    нет. Перенесённые и отменённые матчи (``gameScheduleState`` не OK) не берутся. До
    ``UPCOMING_START_MSK`` дня лиги — пусто: игровой день меняется в 03:00 ET, и утренние
    ночные слоты sync (до 11:00 МСК) иначе слали бы превью нового дня раньше времени.

    Аргументы:
        now: aware-момент прогона.
    """
    day = league_today(now)
    if now < datetime.combine(day, UPCOMING_START_MSK, tzinfo=MSK):
        return []
    return [
        (int(g["id"]), start, g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"])
        for g in slate_games_sorted(fetch_score(day))
        if g["gameScheduleState"] == _SCHEDULED_AS_PLANNED
        and now < (start := parse_start_utc(g.get("startTimeUTC"))[0])
    ]


async def run_upcoming_broadcast(context: CallbackContext, now: datetime) -> None:
    """Превью предстоящего матча — подписчикам ``upcoming_games`` (Задача 78).

    Зачем: бот на виду у подписчика весь день — с 15:00 МСК на каждом прогоне (раз в
    30 минут) каждому приходит одно превью из ``/tonight`` (сравнение команд и строка
    оценки модели, если прогноз опубликован): ближайший по началу ещё не начавшийся матч
    дня, которого он не получал (решение человека 2026-10-25). Превью собирается раз на
    матч, а не на подписчика. Отметка — по паре (подписка, матч) в ``bot_game_sent``,
    ставится после попытки, как у страны: сбой логируется, следующий прогон шлёт
    следующий матч. Прогон без матчей ничего не шлёт и подписчиков не читает.

    Аргументы: ``context`` — контекст PTB, нужен ради ``context.bot``;
    ``now`` — текущий момент (aware).
    """
    games = upcoming_games(now)
    if not games:
        return
    sent = sent_games([game[0] for game in games])
    previews: Dict[int, str] = {}
    for subscription_id, chat_id in list_active_upcoming_rows():
        nxt = next((g for g in games if (subscription_id, g[0]) not in sent), None)
        if nxt is None:
            continue
        game_id, start, away, home = nxt
        if game_id not in previews:
            previews[game_id] = (
                f"⏰ <b>Начало в {start.astimezone(MSK):%H:%M} МСК</b>\n"
                + matchup_season_preview(game_id, away, home)
            )
        text = f"{previews[game_id]}\n\n{SUBSCRIPTIONS_FOOTER}"
        try:
            await _throttled(
                partial(context.bot.send_message, chat_id=chat_id, text=text, parse_mode="HTML"),
                chat_id,
            )
        except Forbidden:
            logger.info("chat_id=%s blocked bot; deactivate upcoming_games", chat_id)
            mark_subscription_inactive_by_chat_kind_team(chat_id, "upcoming_games")
        except TelegramError as exc:
            logger.warning("upcoming send failed chat_id=%s game_id=%s: %s", chat_id, game_id, exc)
        mark_game_sent(subscription_id, game_id)


async def broadcast_if_due(context: CallbackContext, now: datetime) -> None:
    """Прогоняет рассылки за ночь момента *now*.

    Дайджест и команды — когда ночь загружена целиком (``night_is_loaded``) или
    наступил крайний срок ``NIGHT_DEADLINE_MSK``. Страна — на каждом прогоне, по
    готовым матчам (``run_country_broadcast``): её сбой (NHL, ffmpeg) не срывает дайджест
    на крайнем сроке, у которого следующей попытки нет. После крайнего срока NHL API о
    готовности ночи не спрашивается. Превью предстоящих матчей — на каждом прогоне, с
    моментом на свой старт (страна с видео идёт минутами). Страна и превью независимы:
    сбой одной (NHL API, ffmpeg) логируется и не срывает другую, а после обеих
    пробрасывается — прогон красный в статусе sync, ошибка не глушится.

    Аргументы: ``context`` — контекст PTB; ``now`` — текущий момент (aware).
    """
    night = night_of(now)
    final = now >= _msk_moment(night, NIGHT_DEADLINE_MSK)
    if final or night_is_loaded(night):
        await run_morning_digest_broadcast(context, night)
        await run_team_scores_broadcast(context, night)
    else:
        logger.info("Ночь %s загружена не целиком: дайджест и команды отложены.", night)
    errors: List[Exception] = []
    for name, broadcast in (
        ("country", lambda: run_country_broadcast(context, night, final)),
        ("upcoming", lambda: run_upcoming_broadcast(context, datetime.now(timezone.utc))),
    ):
        try:
            await broadcast()
        except Exception as exc:  # noqa: BLE001 — изоляция рассылок, ошибка пробрасывается ниже
            logger.exception("%s broadcast failed", name)
            errors.append(exc)
    if errors:
        raise errors[0]


async def main(upcoming_only: bool) -> None:
    """Точка входа скрипта (запускается сервисом `sync`): проверяет флаги и вызывает
    ``broadcast_if_due`` или, в дневном слоте (``upcoming_only``, флаг ``--upcoming-only``),
    только ``run_upcoming_broadcast`` — днём ночи нет, и NHL API о ней не спрашивается.

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
    async with application:
        context = CallbackContext(application)
        if upcoming_only:
            await run_upcoming_broadcast(context, datetime.now(timezone.utc))
        else:
            await broadcast_if_due(context, datetime.now(timezone.utc))
    logger.info("Рассылка завершена.")


if __name__ == "__main__":
    assert sys.argv[1:] in ([], ["--upcoming-only"]), f"неизвестные аргументы: {sys.argv[1:]}"
    asyncio.run(main(sys.argv[1:] == ["--upcoming-only"]))
