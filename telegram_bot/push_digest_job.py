#!/usr/bin/env python3
"""Рассылка итогов матчей ночи, утреннего дайджеста и превью матчей подписчикам.

Отдельный скрипт вне процесса бота: поднимает собственный aiogram ``Bot``
(без ``Dispatcher`` и polling), рассылает и завершается. Запускается сервисом
`sync` (`pipeline/scheduled_sync.py`) после каждого успешного прогона загрузчика —
раз в 30 минут с 12:00 до 08:00 UTC — и сам решает, пора ли. Итог матча приходит чату
ровно один раз (Задача 85), сколько бы подписок на него ни вело; отметка — пара (чат, матч).
Матч подписанной команды или с игроками подписанной страны — одним сообщением под подписки
чата, на первом прогоне после конца матча, когда NHL выложила клипы всех его голов, а к
11:00 МСК — с тем, что есть: карточка с видео голов внутри и блоками стран; при подписке
только на страны карточка под табло свёрнута.
Дайджест — остальные матчи ночи, как только загружен последний её матч, а к 11:00 МСК — в
любом случае (Задача 63); отметка ``last_sent_night`` не даёт отправить ночь дважды.
Предстоящие матчи (Задача 78) — на каждом прогоне с 15:30 МСК одно превью: ближайший ещё
не начавшийся матч дня, которого подписчик не получал; каждый матч — один раз. Дневные
слоты sync (с 12:30 UTC) зовут скрипт с ``--upcoming-only``: тогда идёт только превью.
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
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from typing import Awaitable, Callable, Dict, List, NamedTuple, Set, Tuple, TypeVar

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

# Запуск из каталога telegram_bot (как make bot).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
from bot_messages import (  # noqa: E402
    MODEL_PREDICTION_TEASER,
    MSK,
    country_name,
    country_night_blocks,
    day_digest,
    matchup_season_preview,
    night_goals,
    rich_footer,
    rich_message,
    with_model_prediction,
)
import premium  # noqa: E402
from help_text import SUBSCRIPTIONS_FOOTER  # noqa: E402
from nhl_scoreboard import (  # noqa: E402
    fetch_score,
    league_today,
    parse_start_utc,
    slate_games_sorted,
)
from stats_handlers import (  # noqa: E402
    dispatch_day_digest_messages,
    send_game_card,
)
from subscription_repo import (  # noqa: E402
    chat_games_sent,
    deactivate_chat,
    list_active_game_rows,
    list_active_morning_digest_rows,
    list_active_upcoming_rows,
    mark_chat_games_sent,
    mark_game_sent,
    mark_digest_night_sent,
    sent_games,
)
from database import fetch_all  # noqa: E402
from video_replay import goal_clip_published  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("push_digest_job")

# Крайний срок ночи — последний ночной прогон sync (08:00 UTC). Прогонов позже нет, поэтому
# недозагруженная ночь (перенос матча) уходит в этот момент с тем, что есть в БД. Если сам
# этот прогон упал на загрузчике, рассылка не запускается и ночь не уходит вовсе.
NIGHT_DEADLINE_MSK = time(11, 0)

# Превью матчей дня лиги — не раньше первого дневного слота sync, через полчаса после
# публикации прогноза в 12:00 UTC (решение человека, Задача 78; scheduled_sync.PREVIEW_START_UTC
# — тот же момент по UTC).
UPCOMING_START_MSK = time(15, 30)

# Сдвиг, переводящий момент ночи в игровую дату лиги: матчи даты D (ET) идут
# с 16:00 UTC D до ~08:00 UTC D+1, и любой момент ночного окна sync минус 12 часов
# попадает в D.
_NIGHT_SHIFT = timedelta(hours=12)

# Матчи, которые загрузчик пишет в ``games``: регулярка и плей-офф (``gameType`` 2 и 3,
# Задача 104) по расписанию (``gameScheduleState`` OK) — перенесённый или отменённый матч
# ночь не держит.
_LOADED_GAME_TYPES = (2, 3)
_SCHEDULED_AS_PLANNED = "OK"

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
    """Все ли матчи регулярки и плей-офф ночи *night* из расписания NHL уже в ``games``.

    Зачем: дайджест не должен уходить неполным. Слейт берётся из ``score/{night}``
    NHL API, а не из ``scheduled_games``: та держит только не начавшиеся игры,
    и идущий или только что закончившийся матч из неё
    выпадает раньше, чем попадает в ``games``.

    Аргументы:
        night: игровая дата ночи.
    """
    slate = [
        int(g["id"])
        for g in fetch_score(night)["games"]
        if g["gameDate"] == night.isoformat()
        and g["gameType"] in _LOADED_GAME_TYPES
        and g["gameScheduleState"] == _SCHEDULED_AS_PLANNED
    ]
    loaded = fetch_all(
        "SELECT game_id FROM games WHERE game_id = ANY(%s)", (slate,), columns=["game_id"]
    )
    missing = set(slate) - set(loaded["game_id"])
    if missing:
        logger.info("Night %s not loaded yet: games %s missing", night, sorted(missing))
    return not missing


class _GameSubscriptions(NamedTuple):
    """Подписки чата, которые ведут на один матч ночи (``subscribed_games``)."""

    teams: List[str]
    """Полные названия подписанных команд матча, хозяева первыми."""
    countries: List[Tuple[str, str]]
    """(код, блок игроков страны) подписанных стран, чьи игроки играли, — по алфавиту."""


# chat_id → {game_id: подписки чата на матч}.
_SubscribedGames = Dict[int, Dict[int, _GameSubscriptions]]


def subscribed_games(night: date) -> _SubscribedGames:
    """Матчи ночи *night*, на которые у чата есть подписка на команду или страну.

    Зачем: такой матч приходит чату одним сообщением под его подписки
    (``run_game_broadcast``), а в дайджест не попадает (``run_morning_digest_broadcast``).
    Чата без таких матчей в ответе нет.

    Аргументы:
        night: игровая дата ночи (``night_of``).
    """
    rows = list_active_game_rows()
    if not rows:
        return {}
    games = fetch_all(
        "SELECT g.game_id, g.home_team_id, h.name, g.away_team_id, a.name "
        "FROM games g "
        "JOIN teams h ON h.team_id = g.home_team_id AND h.season_id = g.season_id "
        "JOIN teams a ON a.team_id = g.away_team_id AND a.season_id = g.season_id "
        "WHERE g.season_id = %s AND g.day = %s ORDER BY g.game_id",
        (config.SEASON_ID, night),
        columns=["game_id", "home_id", "home", "away_id", "away"],
    )
    blocks = {
        country: dict(country_night_blocks(country, night))
        for country in sorted({country for _, _, country in rows if country is not None})
    }
    chats: Dict[int, Tuple[Set[int], List[str]]] = defaultdict(lambda: (set(), []))
    for chat_id, team_id, country in rows:
        if team_id is not None:
            chats[chat_id][0].add(team_id)
        if country is not None:
            chats[chat_id][1].append(country)
    result: _SubscribedGames = {}
    for chat_id, (team_ids, countries) in chats.items():
        for i in range(games["count_rows"]):
            game_id = int(games["game_id"][i])
            subs = _GameSubscriptions(
                [
                    games[side][i]
                    for side in ("home", "away")
                    if int(games[f"{side}_id"][i]) in team_ids
                ],
                [(country, blocks[country][game_id]) for country in countries if game_id in blocks[country]],
            )
            if subs.teams or subs.countries:
                result.setdefault(chat_id, {})[game_id] = subs
    return result


async def throttled(call: Callable[[], Awaitable[_T]], chat_id: int) -> _T:
    """Выполняет одну отправку и выдерживает паузу до следующей.

    Зачем: ``TelegramRetryAfter`` означает, что отправка не доставлена, — повторяем
    её один раз после указанной API задержки. Второй ``TelegramRetryAfter``, как и
    любая другая ошибка Telegram, улетает вызывающему.

    Аргументы: ``call`` — отправка без аргументов (её можно вызвать повторно);
    ``chat_id`` — получатель, для лога.
    """
    try:
        result = await call()
    except TelegramRetryAfter as exc:
        wait = float(exc.retry_after)
        logger.warning("429 RetryAfter %ss for chat_id=%s", wait, chat_id)
        await asyncio.sleep(wait)
        result = await call()
    # Telegram лимитирует бота ~30 сообщениями в секунду.
    await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))
    return result


async def run_morning_digest_broadcast(
    bot: Bot, night: date, final: bool, subscribed: _SubscribedGames
) -> None:
    """Шлёт дайджест ночи *night* подписчикам ``morning_digest``, которым пора.

    Зачем: та же сводка, что у ``/today`` (``dispatch_day_digest_messages``), но и при одном
    матче — сводка: полная карточка открывается кнопкой; матч подписки чата на крайнем сроке
    остаётся карточкой с видео голов. В дайджесте чата — только матчи, итог которых ему ещё не ушёл (Задача 85); до крайнего
    срока не берутся и матчи его подписок (``subscribed``): ждущий клипов уйдёт своим
    сообщением. На крайнем сроке рассылка по матчам уже прошла, и матч подписки без отметки
    (её сбой) попадает сюда. Отфильтрованная сводка — «Остальные матчи ночи»; не осталось
    ничего — дайджеста нет. Чату, которому ночь уже ушла (``last_sent_night``), не шлётся
    ничего; после попытки ночь и её матчи отмечаются отправленными. Ночь без матчей в БД не
    рассылается. Заблокировавший бота чат (``TelegramForbiddenError``) теряет все подписки.

    Аргументы: ``bot`` — бот рассылки; ``night`` — игровая дата ночи (``night_of``);
    ``final`` — наступил ли крайний срок; ``subscribed`` — ``subscribed_games(night)``.
    """
    due = [
        chat_id
        for chat_id, last_sent in list_active_morning_digest_rows()
        if last_sent != night
    ]
    if not due:
        return
    day_label, games = day_digest(night)
    if not games:
        logger.info("No games in DB for night %s: digest not sent", night)
        return
    sent = chat_games_sent(games)
    for chat_id in due:
        own = set() if final else set(subscribed.get(chat_id, {}))
        rest = [game for game in games if (chat_id, game) not in sent and game not in own]
        if rest:
            send = partial(
                dispatch_day_digest_messages,
                bot,
                chat_id,
                day_label,
                rest,
                inter_message_sleep_sec=config.PUSH_SEND_INTERVAL_SEC,
                heading="Матчи ночи" if len(rest) == len(games) else "Остальные матчи ночи",
                summary_only=not any(game in subscribed.get(chat_id, {}) for game in rest),
            )
            try:
                try:
                    await send()
                except TelegramRetryAfter as exc:
                    logger.warning("digest RetryAfter %ss chat_id=%s", exc.retry_after, chat_id)
                    await asyncio.sleep(float(exc.retry_after))
                    await send()
            except TelegramForbiddenError:
                logger.info("chat_id=%s blocked bot; deactivate chat", chat_id)
                deactivate_chat(chat_id)
            except TelegramAPIError as exc:
                logger.warning("digest send failed chat_id=%s: %s", chat_id, exc)
            mark_chat_games_sent(chat_id, rest)
        mark_digest_night_sent(chat_id, night)
        # Telegram лимитирует бота ~30 сообщениями в секунду.
        await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))


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


async def run_game_broadcast(
    bot: Bot, night: date, final: bool, subscribed: _SubscribedGames
) -> None:
    """Итог каждого матча подписок чата (``subscribed``) — одним сообщением, ровно один раз.

    Зачем: правило «каждый матч ночи — чату ровно один раз» (Задача 85): сколько бы
    подписок ни вело на матч (команда, страны), сообщение одно и собрано под них. Первая
    строка — табло матча (видно в уведомлении), подписки «По подписке: …» и ссылка на
    их настройку — подвал. Карточка с видео голов в строках голов (``send_game_card``) и
    блоки стран под ней; подписка только на страны — карточка под табло свёрнута.
    Матч уходит на первом прогоне sync, когда он уже в ``games`` и NHL выложила клипы всех
    его голов; ``final`` (крайний срок ночи) — с тем, что есть. Отметка — пара (чат, матч)
    в ``bot_chat_game_sent``, ставится после любой попытки: сбой Telegram на одном матче
    логируется и не повторяется, остальные уходят; иной сбой (БД, ffmpeg) тоже отмечает матч
    и обрывает прогон. Заблокировавший бота чат теряет все подписки.

    Аргументы: ``bot`` — бот рассылки; ``night`` — игровая дата ночи (``night_of``);
    ``final`` — наступил ли крайний срок; ``subscribed`` — ``subscribed_games(night)``.
    """
    if not subscribed:
        return
    sent = chat_games_sent(sorted({game_id for games in subscribed.values() for game_id in games}))
    goals: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for game_id, event_id in night_goals(night):
        goals[game_id].append((game_id, event_id))
    published: Dict[Tuple[int, int], bool] = {}
    for chat_id, games in subscribed.items():
        for game_id, subs in games.items():
            if (chat_id, game_id) in sent:
                continue
            if not final and not await _clips_published(goals[game_id], published):
                logger.info("game_id=%s: клипы ещё не выложены, ждём", game_id)
                continue
            labels = [html.escape(team) for team in subs.teams] + [
                html.escape(country_name(country)) for country, _ in subs.countries
            ]
            # Причина и ссылка на подписки — подвал: первой строкой, видной в
            # уведомлении, идёт счёт матча.
            footer = rich_footer(f"По подписке: {' · '.join(labels)}\n{SUBSCRIPTIONS_FOOTER}")
            blocks = "".join(f"\n\n{block}" for _, block in subs.countries)
            try:
                await throttled(
                    partial(
                        send_game_card, bot, chat_id, game_id, f"{blocks}\n{footer}", collapsed=not subs.teams
                    ),
                    chat_id,
                )
            except TelegramForbiddenError:
                logger.info("chat_id=%s blocked bot; deactivate chat", chat_id)
                deactivate_chat(chat_id)
                break
            except TelegramAPIError as exc:
                logger.warning("game send failed chat_id=%s game_id=%s: %s", chat_id, game_id, exc)
            finally:
                # И при сбое не из Telegram (БД, ffmpeg) после ушедшего текста: матч не
                # повторится и не попадёт ещё и в дайджест; сама ошибка всплывает.
                mark_chat_games_sent(chat_id, [game_id])


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


async def run_upcoming_broadcast(bot: Bot, now: datetime) -> None:
    """Превью предстоящего матча — подписчикам ``upcoming_games`` (Задача 78).

    Зачем: бот на виду у подписчика весь день — с 15:30 МСК на каждом прогоне (раз в
    30 минут) каждому приходит одно превью из ``/tonight`` (сравнение команд, премиум-чату —
    и строка оценки модели, если прогноз опубликован, Задача 96): ближайший по началу ещё не
    начавшийся матч дня, которого он не получал (решение человека 2026-10-25). Превью
    собирается раз на матч, а не на подписчика, и без прогноза — его строка добавляется при
    отправке каждому чату. Отметка — по паре (подписка, матч) в ``bot_game_sent``,
    ставится после попытки, как у страны: сбой логируется, следующий прогон шлёт
    следующий матч. Прогон без матчей ничего не шлёт и подписчиков не читает.

    Аргументы: ``bot`` — бот рассылки; ``now`` — текущий момент (aware).
    """
    games = upcoming_games(now)
    if not games:
        return
    sent = sent_games([game[0] for game in games])
    previews: Dict[int, Tuple[str, str]] = {}
    for subscription_id, chat_id in list_active_upcoming_rows():
        nxt = next((g for g in games if (subscription_id, g[0]) not in sent), None)
        if nxt is None:
            continue
        game_id, start, away, home = nxt
        if game_id not in previews:
            previews[game_id] = matchup_season_preview(game_id, away, home, collapsed=True)
        body, footer = previews[game_id]
        if premium.is_premium(chat_id):
            footer = with_model_prediction(footer, game_id, home)
        else:
            footer = f"{footer}\n{MODEL_PREDICTION_TEASER}"
        # Первая строка — время начала: её видно в уведомлении.
        text = (
            f"⏰ Начало в {start.astimezone(MSK):%H:%M} МСК\n{body}\n"
            + rich_footer(f"{footer}\n{SUBSCRIPTIONS_FOOTER}")
        )
        try:
            await throttled(
                partial(
                    bot.send_rich_message,
                    chat_id=chat_id,
                    rich_message=rich_message(text),
                ),
                chat_id,
            )
        except TelegramForbiddenError:
            logger.info("chat_id=%s blocked bot; deactivate chat", chat_id)
            deactivate_chat(chat_id)
        except TelegramAPIError as exc:
            logger.warning("upcoming send failed chat_id=%s game_id=%s: %s", chat_id, game_id, exc)
        mark_game_sent(subscription_id, game_id)


async def broadcast_if_due(bot: Bot, now: datetime) -> None:
    """Прогоняет рассылки за ночь момента *now*.

    Сначала итоги по матчам подписок (``run_game_broadcast``) — на каждом прогоне, по
    готовым матчам; затем дайджест — когда ночь загружена целиком (``night_is_loaded``) или
    наступил крайний срок ``NIGHT_DEADLINE_MSK``: на крайнем сроке ждущие матчи уже ушли, и
    дайджест берёт остаток. После крайнего срока NHL API о готовности ночи не спрашивается.
    Превью предстоящих матчей — последним, с моментом на свой старт (матчи с видео идут
    минутами). Рассылки независимы: сбой одной (NHL API, ffmpeg) логируется и не срывает
    следующие — дайджест на крайнем сроке, у которого следующей попытки нет, уйдёт и после
    сбоя матчей, — а после всех пробрасывается: прогон красный в статусе sync.

    Аргументы: ``bot`` — бот рассылки; ``now`` — текущий момент (aware).
    """
    night = night_of(now)
    final = now >= _msk_moment(night, NIGHT_DEADLINE_MSK)
    subscribed = subscribed_games(night)

    async def digest() -> None:
        if final or night_is_loaded(night):
            await run_morning_digest_broadcast(bot, night, final, subscribed)
        else:
            logger.info("Ночь %s загружена не целиком: дайджест отложен.", night)

    errors: List[Exception] = []
    for name, broadcast in (
        ("games", lambda: run_game_broadcast(bot, night, final, subscribed)),
        ("digest", digest),
        ("upcoming", lambda: run_upcoming_broadcast(bot, datetime.now(timezone.utc))),
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

    ``async with`` закрывает HTTP-сессию бота после рассылки. Таймаут запроса — 300 с:
    карточка с клипами голов (до 45 МБ загрузки) не уйдёт за 60 с по умолчанию.
    """
    config.validate_env()
    if not config.ENABLE_PUSH_DIGEST:
        logger.info("ENABLE_PUSH_DIGEST выключен — рассылка пропущена.")
        return
    async with Bot(config.TOKEN, session=AiohttpSession(timeout=300)) as bot:
        if upcoming_only:
            await run_upcoming_broadcast(bot, datetime.now(timezone.utc))
        else:
            await broadcast_if_due(bot, datetime.now(timezone.utc))
    logger.info("Рассылка завершена.")


if __name__ == "__main__":
    assert sys.argv[1:] in ([], ["--upcoming-only"]), f"неизвестные аргументы: {sys.argv[1:]}"
    asyncio.run(main(sys.argv[1:] == ["--upcoming-only"]))
