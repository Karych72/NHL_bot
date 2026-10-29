"""Тесты cron-скрипта рассылки ``telegram_bot/push_digest_job.py``.

Модуль годами был сломан незамеченным (сначала ``ImportError`` на
``telegram.error.Forbidden``, затем несделанные ``await`` после перехода на
PTB 21.x) ровно потому, что ни один тест его не импортировал. Здесь он
именно **вызывается**: рассылка прогоняется через настоящий
``dispatch_day_digest_messages`` на настоящем aiogram ``Bot`` из conftest
(``fake_bot`` на ``FakeSession``), поэтому забытый ``await`` виден как «сообщение
не отправлено», а не как тихо брошенная корутина. Границы — БД
(``subscription_repo``, ``bot_messages``) и HTTP-сессия Telegram — подменяются,
сеть не используется.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMediaGroup, SendMessage, SendRichMessage, SendVideo
from aiogram.types import BufferedInputFile

from tests._telegram_fakes import FAKE_TOKEN, body, rich_html

# Один «настоящий» матч дня: dispatch_day_digest_messages шлёт карточку матча,
# с подсказкой «Ещё: …» в подвале.
ONE_GAME_DAY = ("2026-01-15", [(2026020001, "<b>BOS 3 : 2 TOR</b>", [])])

# Ночь игровой даты 2026-10-06: прогоны sync — с 18:00 UTC 06.10 до 08:00 UTC 07.10.
NIGHT = date(2026, 10, 6)


def _method() -> SendMessage:
    """Запрос, на котором «споткнулась» отправка: aiogram-ошибки без него не строятся."""
    return SendMessage(chat_id=1, text="x")


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=_method(), message="Forbidden: bot was blocked by the user")


def _retry_after() -> TelegramRetryAfter:
    return TelegramRetryAfter(method=_method(), message="Too Many Requests", retry_after=0)


@pytest.fixture
def push_job(bot_module):
    """Импортированный ``push_digest_job`` (импорт модуля — уже часть проверки)."""
    return bot_module("push_digest_job")


def _rows(*chat_ids, last_sent=None):
    """Строки ``list_active_morning_digest_rows`` для чатов с одинаковой подпиской."""
    return [(chat_id, last_sent) for chat_id in chat_ids]


@pytest.fixture
def chat_marks(push_job, monkeypatch):
    """Отметки «итог матча ушёл в чат» (Задача 85) в памяти вместо ``bot_chat_game_sent``:
    множество (chat_id, game_id), как таблица; SQL отметок — в tests/test_db_nhl.py."""
    marks = set()
    monkeypatch.setattr(
        push_job, "chat_games_sent", lambda game_ids: {m for m in marks if m[1] in game_ids}
    )
    monkeypatch.setattr(
        push_job, "mark_chat_games_sent",
        lambda chat_id, game_ids: marks.update((chat_id, game_id) for game_id in game_ids),
    )
    return marks


# ---------------------------------------------------------------------------
# Утренний дайджест
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_digest_reaches_every_active_subscriber(push_job, fake_bot, api, chat_marks):
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, 222)
    ), patch.object(push_job, "mark_digest_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    assert [(type(r), r.chat_id) for r in api.outgoing] == [
        (SendRichMessage, 111), (SendRichMessage, 222),
    ]
    for card in api.sent_rich:
        html_ = card.rich_message.html
        assert html_.startswith(rich_html("<b>BOS 3 : 2 TOR</b>")), "первая строка — счёт матча"
        # Подсказка «Ещё: …» — подвал самого сообщения, а не отдельное сообщение.
        assert html_.endswith("Настроить подписки: /subscriptions</footer>")
        assert "<footer>Ещё: /tonight" in html_
    assert chat_marks == {(111, 2026020001), (222, 2026020001)}


@pytest.mark.asyncio
async def test_morning_digest_deactivates_chat_that_blocked_the_bot(push_job, fake_bot, chat_marks):
    """``TelegramForbiddenError`` ловится, только если корутина рассылки действительно ждётся;
    заблокировавший бота чат теряет все подписки (Задача 85)."""

    async def blocked(*args, **kwargs):
        raise _forbidden()

    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(
        push_job, "dispatch_day_digest_messages", side_effect=blocked
    ), patch.object(push_job, "deactivate_chat") as deactivate, patch.object(push_job, "mark_digest_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    deactivate.assert_called_once_with(111)


@pytest.mark.asyncio
async def test_morning_digest_retries_once_after_retry_after(push_job, fake_bot, api, chat_marks):
    """429 означает «не доставлено» — рассылка обязана повторить попытку."""
    attempts = []

    async def flaky(bot, chat_id, *args, **kwargs):
        attempts.append(chat_id)
        if len(attempts) == 1:
            raise _retry_after()
        await bot.send_message(chat_id=chat_id, text="ok")

    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "dispatch_day_digest_messages", side_effect=flaky), patch.object(
        push_job, "mark_digest_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    assert attempts == [111, 111]
    assert [m.chat_id for m in api.sent_messages] == [111]


# Ночь из четырёх матчей для дайджеста без уже присланных: сводка вместо карточки.
G1, G2, G3, G4 = 2026020011, 2026020012, 2026020013, 2026020014
FOUR_GAME_DAY = ("2026-10-06", [(g, f"CARD {g}", []) for g in (G1, G2, G3, G4)])


@pytest.mark.asyncio
async def test_morning_digest_leaves_out_games_the_chat_already_got_or_will_get(
    push_job, bot_module, fake_bot, api, chat_marks, monkeypatch
):
    """Задача 85: в дайджесте — только матчи, итог которых чату ещё не ушёл; матч его
    подписки, ждущий клипов, до крайнего срока тоже не берётся — уйдёт своим сообщением.
    Сводка тогда — «Остальные матчи ночи»; не осталось ничего — дайджеста нет, но ночь
    отмечена. Чат только с «Матчами» получает всю ночь."""
    stats_handlers = bot_module("stats_handlers")
    monkeypatch.setattr(stats_handlers, "day_digest_summary_body", lambda ids: [f"{g}" for g in ids])
    monkeypatch.setattr(stats_handlers, "digest_game_button_labels", lambda ids: [f"{g}" for g in ids])
    chat_marks.update({(111, G1), (333, G1), (333, G2)})
    subscribed = {111: {G2: "subs"}, 333: {G3: "subs", G4: "subs"}}
    nights = {}
    with patch.object(push_job, "day_digest", return_value=FOUR_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, 222, 333)
    ), patch.object(push_job, "mark_digest_night_sent", side_effect=lambda chat, *a: nights.setdefault(chat, a)):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, subscribed)

    summaries = {m.chat_id: m for m in api.sent_rich}
    assert sorted(summaries) == [111, 222], "чату 333 всё уже ушло или уйдёт — дайджеста нет"
    assert body(summaries[111]).startswith("<p><b>Остальные матчи ночи</b></p>")
    assert "<footer>Игровой день 2026-10-06 · матчей: 2<br>" in body(summaries[111])
    assert [b.callback_data for row in summaries[111].reply_markup.inline_keyboard for b in row] == [
        f"dg:{G3}", f"dg:{G4}",
    ]
    assert body(summaries[222]).startswith("<p><b>Матчи ночи</b></p>")
    assert "<footer>Игровой день 2026-10-06 · матчей: 4<br>" in body(summaries[222])
    assert sorted(nights) == [111, 222, 333]
    assert (111, G2) not in chat_marks, "матч подписки уйдёт своим сообщением"
    assert {(111, G3), (111, G4), (222, G1), (222, G4)} <= chat_marks


@pytest.mark.asyncio
async def test_morning_digest_at_the_deadline_takes_unsent_games_of_subscriptions(
    push_job, fake_bot, api, chat_marks
):
    """На крайнем сроке рассылка по матчам уже прошла: матч подписки без отметки (её сбой)
    уходит в дайджесте, а не пропадает."""
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "mark_digest_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, True, {111: {2026020001: "subs"}})

    [card] = api.sent_rich
    assert card.rich_message.html.startswith(rich_html("<b>BOS 3 : 2 TOR</b>"))
    assert card.rich_message.html.endswith("</footer>")


# ---------------------------------------------------------------------------
# Точка входа cron-скрипта
# ---------------------------------------------------------------------------

class _ForbiddenBot:
    """Заглушка ``Bot``: создание бота означает лишний выход в сеть."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("Bot must not be created when the job is a no-op")


@pytest.mark.asyncio
async def test_main_raises_and_never_creates_bot_without_token(
    push_job, monkeypatch, set_required_bot_env
):
    """``main()`` падает на ``validate_env()`` до создания ``Bot``, если токена нет —
    раньше это было мягкое ``sys.exit(1)`` внутри самого скрипта."""
    set_required_bot_env(TELEGRAM_BOT_TOKEN="")
    monkeypatch.setattr(push_job, "Bot", _ForbiddenBot)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        await push_job.main(False)


@pytest.mark.asyncio
async def test_main_does_nothing_while_enable_push_digest_is_off(
    push_job, bot_module, monkeypatch, set_required_bot_env
):
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", False)
    monkeypatch.setattr(push_job, "Bot", _ForbiddenBot)

    await push_job.main(False)


@pytest.mark.asyncio
async def test_main_runs_all_broadcasts_on_one_bot_with_long_timeout_and_closes_it(
    push_job, bot_module, monkeypatch, set_required_bot_env
):
    """Обвязка ``main()``: настоящий ``Bot`` на ``AiohttpSession`` с таймаутом 300 с,
    все рассылки получают один и тот же бот, сессия закрывается после них.

    Бот и сессия настоящие, подменён только ``AiohttpSession.close`` (он нужен, чтобы
    увидеть порядок): запросов к Telegram тест не делает — рассылки подменены.
    """
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", True)

    steps = []

    async def fake_close(self):
        steps.append("close")

    monkeypatch.setattr(AiohttpSession, "close", fake_close)

    bots = []
    nights = []
    before = push_job.night_of(datetime.now(timezone.utc))

    async def record(bot, night, *args):
        steps.append("broadcast")
        bots.append(bot)
        nights.append(night)

    monkeypatch.setattr(push_job, "night_is_loaded", lambda night: True)
    monkeypatch.setattr(push_job, "subscribed_games", lambda night: {})
    monkeypatch.setattr(push_job, "run_game_broadcast", record)
    monkeypatch.setattr(push_job, "run_morning_digest_broadcast", record)

    async def record_upcoming(bot, now):
        steps.append("upcoming")
        bots.append(bot)

    monkeypatch.setattr(push_job, "run_upcoming_broadcast", record_upcoming)

    await push_job.main(False)

    # Рассылки идут строго до закрытия сессии — иначе HTTP-клиент бота уже погашен,
    # а после них сессия обязана закрыться.
    assert steps == ["broadcast", "broadcast", "upcoming", "close"]

    after = push_job.night_of(datetime.now(timezone.utc))
    assert len(nights) == 2 and len(set(nights)) == 1 and nights[0] in (before, after)
    bot = bots[0]
    assert isinstance(bot, Bot) and all(b is bot for b in bots)
    assert bot.token == FAKE_TOKEN
    # Альбом из 10 клипов не загрузится за 60 с по умолчанию.
    assert isinstance(bot.session, AiohttpSession) and bot.session.timeout == 300


# ---------------------------------------------------------------------------
# Время рассылки (Задачи 60, 63): ночь загружена или наступил крайний срок 11:00 МСК
# ---------------------------------------------------------------------------

def _msk(hour: int, minute: int = 0) -> datetime:
    """Момент утра после ночи ``NIGHT`` по МСК."""
    return datetime(2026, 10, 7, hour, minute, tzinfo=timezone(timedelta(hours=3)))


def _night_slots():
    """Прогоны sync ночи ``NIGHT``: каждые 30 минут с 21:00 до 11:00 МСК."""
    slot = datetime(2026, 10, 6, 21, 0, tzinfo=timezone(timedelta(hours=3)))
    while slot <= _msk(11):
        yield slot
        slot += timedelta(minutes=30)


async def _run_night(push_env, game_at, loaded_at):
    """Прогоняет ``broadcast_if_due`` после каждого слота ночи из одного матча (``GAME_A``).

    Чат 111 — только дайджест, 222 — команда матча, 333 — команда и дайджест. Матч
    появляется в ``games`` со слота *game_at*, ночь загружена целиком со слота *loaded_at*
    (``None`` — не загружается до утра). Отметки — как в БД (``push_env``).

    Возвращает ``{chat_id: [(момент МСК, "card" | "digest")]}``.
    """
    push_env.digest_chats = [111, 333]
    team = push_env.subs(["TOR"], [])
    sent_at = {111: [], 222: [], 333: []}
    for slot in _night_slots():
        before = len(push_env.api.outgoing)
        push_env.loaded = loaded_at is not None and slot >= loaded_at
        push_env.subscribed = {222: {GAME_A: team}, 333: {GAME_A: team}} if slot >= game_at else {}
        await push_env.job.broadcast_if_due(push_env.bot, slot)
        for message in push_env.api.outgoing[before:]:
            kind = "card" if "По подписке" in body(message) else "digest"
            sent_at[message.chat_id].append((slot, kind))
    return sent_at


def test_night_of_covers_the_whole_sync_window(push_job):
    """Все прогоны ночи — с 21:00 МСК до 11:00 МСК следующего дня — одна ночь."""
    assert {push_job.night_of(slot) for slot in _night_slots()} == {NIGHT}


@pytest.mark.asyncio
async def test_unloaded_night_holds_digest_but_not_games(push_job, fake_bot):
    """Ночь не загружена, срок не наступил — дайджест ждёт; итоги по матчам идут по
    готовым матчам на каждом прогоне и раньше дайджеста (Задача 85)."""
    calls = []

    async def record(name, *args):
        calls.append((name, args))

    with patch.object(push_job, "night_is_loaded", return_value=False), patch.object(
        push_job, "subscribed_games", return_value={111: {}}
    ), patch.object(
        push_job, "run_morning_digest_broadcast", partial(record, "digest")
    ), patch.object(push_job, "run_game_broadcast", partial(record, "games")), patch.object(
        push_job, "run_upcoming_broadcast", partial(record, "upcoming")
    ):
        await push_job.broadcast_if_due(fake_bot, _msk(10, 30))

    assert [name for name, _ in calls] == ["games", "upcoming"]
    assert calls[0][1] == (fake_bot, NIGHT, False, {111: {}})
    assert calls[1][1][0] is fake_bot


@pytest.mark.asyncio
async def test_game_goes_out_after_it_ends_and_digest_after_the_night_is_loaded(push_env):
    """Матч в БД с 01:00, ночь загрузилась к 08:30: карточка подписчикам команды — в 01:00,
    дайджест — в 08:30, и только тому, кому матч ещё не ушёл; до утра ничего не повторяется."""
    sent_at = await _run_night(push_env, game_at=_msk(1, 0), loaded_at=_msk(8, 30))
    assert sent_at == {
        111: [(_msk(8, 30), "digest")], 222: [(_msk(1, 0), "card")], 333: [(_msk(1, 0), "card")],
    }


@pytest.mark.asyncio
async def test_unloaded_night_goes_out_at_the_deadline(push_env):
    """Ночь так и не загрузилась целиком (перенос, сбой) — дайджест уходит в 11:00 с тем,
    что есть; матч подписки к тому времени уже ушёл и в дайджест не попадает."""
    sent_at = await _run_night(push_env, game_at=_msk(1, 0), loaded_at=None)
    assert sent_at == {
        111: [(_msk(11, 0), "digest")], 222: [(_msk(1, 0), "card")], 333: [(_msk(1, 0), "card")],
    }


@pytest.mark.asyncio
async def test_deadline_does_not_ask_nhl_api(push_job, fake_bot):
    """После крайнего срока готовность не нужна: сбой NHL API не срывает отправку."""
    with patch.object(push_job, "night_is_loaded", side_effect=AssertionError("API")), patch.object(
        push_job, "subscribed_games", return_value={}
    ), patch.object(push_job, "run_morning_digest_broadcast") as digest, patch.object(
        push_job, "run_game_broadcast"
    ) as games, patch.object(push_job, "run_upcoming_broadcast") as upcoming:
        await push_job.broadcast_if_due(fake_bot, _msk(11, 0))

    games.assert_awaited_once_with(fake_bot, NIGHT, True, {})
    digest.assert_awaited_once_with(fake_bot, NIGHT, True, {})
    upcoming.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_tick_after_sending_sends_nothing(push_job, fake_bot, api):
    """Ночь уже отмечена отправленной — повторный тик (рестарт, ручной once) молчит."""
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, last_sent=NIGHT)
    ), patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "mark_digest_night_sent"
    ) as mark:
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    assert api.outgoing == []
    mark.assert_not_called()


@pytest.mark.asyncio
async def test_night_without_games_is_not_sent(push_job, fake_bot, api):
    """Ночь без матчей в БД не рассылается — раньше уходил дайджест прошлого игрового дня."""
    no_games = ("2026-10-06", [(0, "За 2026-10-06 завершенных матчей не найдено.", [])])
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "day_digest", return_value=no_games) as digest, patch.object(
        push_job, "mark_digest_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    digest.assert_called_once_with(NIGHT)
    assert api.outgoing == []


def test_night_is_loaded_counts_only_regular_games_played_as_scheduled(push_job):
    """Ночь готова, когда в ``games`` все матчи регулярки её даты; перенесённый,
    предсезонный и матч соседней даты (в ответе ``score``) ночь не держат."""
    payload = {
        "games": [
            {"id": 2026020050, "gameDate": "2026-10-06", "gameType": 2, "gameScheduleState": "OK"},
            {"id": 2026020051, "gameDate": "2026-10-06", "gameType": 2, "gameScheduleState": "OK"},
            {"id": 2026020052, "gameDate": "2026-10-06", "gameType": 2, "gameScheduleState": "PPD"},
            {"id": 2026010099, "gameDate": "2026-10-06", "gameType": 1, "gameScheduleState": "OK"},
            {"id": 2026020060, "gameDate": "2026-10-07", "gameType": 2, "gameScheduleState": "OK"},
        ]
    }
    queried = []

    def games_in_db(loaded_ids):
        def fetch(query, params, columns):
            queried.append(sorted(params[0]))
            return {"game_id": [g for g in params[0] if g in loaded_ids], "count_rows": 0}

        return fetch

    with patch.object(push_job, "fetch_score", return_value=payload) as score, patch.object(
        push_job, "fetch_all", side_effect=games_in_db({2026020050})
    ):
        assert push_job.night_is_loaded(NIGHT) is False
    score.assert_called_once_with(NIGHT)

    with patch.object(push_job, "fetch_score", return_value=payload), patch.object(
        push_job, "fetch_all", side_effect=games_in_db({2026020050, 2026020051})
    ):
        assert push_job.night_is_loaded(NIGHT) is True
    assert queried == [[2026020050, 2026020051]] * 2


# ---------------------------------------------------------------------------
# Итоги по матчу (Задачи 61, 76, 85): одно сообщение под подписки чата, альбомы видео,
# кэш file_id
# ---------------------------------------------------------------------------

_SEND_KIND = {SendRichMessage: "message", SendMessage: "message", SendMediaGroup: "album", SendVideo: "video"}


def _order(api):
    """Порядок отправок рассылки: ("message" | "album" | "video", chat_id)."""
    return [(_SEND_KIND[type(r)], r.chat_id) for r in api.requests if type(r) in _SEND_KIND]


GAME_A, GAME_B = 2026020001, 2026020002
# Подвал рассылок — `<footer>` после тела сообщения (Задача 88).
FOOTER = "<footer>Настроить подписки: /subscriptions</footer>"
# Причина рассылки — в подвале («По подписке: …»), а не первой строкой: первой идёт счёт.
RUS_FOOTER = "<footer>По подписке: Россия<br>Настроить подписки: /subscriptions</footer>"
# Подвал превью предстоящего матча: «Сезон …» из `matchup_season_preview`, затем подписки.
PREVIEW_FOOTER = "<footer>Сезон 2026/27<br>Настроить подписки: /subscriptions</footer>"


def _goals(game_id, n, first_event=100):
    return [(game_id, first_event + i) for i in range(n)]


@pytest.fixture
def push_env(push_job, monkeypatch, tmp_path, fake_bot, api, goal_clip_store, chat_marks):
    """Рассылки ночи с подменой БД, NHL и скачивания: бот (``fake_bot``, запросы — в
    ``api``), журнал скачиваний и проверок клипов, кэш клипов ``clips``
    (``goal_clip_store``), отметки (чат, матч) — ``sent`` (как в БД) и ``marked`` (по
    порядку), погашенные чаты ``deactivated``. Состояние ночи: ``goals`` — (game_id,
    event_id, страна автора) голов ночи (карточка матча ``CARD <id>`` — с кнопкой на каждый
    гол), ``subscribed`` — ответ ``subscribed_games``, ``digest_chats`` и ``last_sent`` —
    подписчики дайджеста и их ``last_sent_night``, ``loaded`` — загружена ли ночь;
    ``unpublished`` — голы, клипа которых NHL ещё не выложила, ``missing`` — голы, клип
    которых не скачался. ``subs(teams, countries)`` — подписки чата на матч;
    ``run(chats, games, goals, final)`` — ``run_game_broadcast`` по странам: ``chats`` —
    (chat_id, страна), ``games`` — (game_id, блок страны), ``goals`` — (game_id, event_id)
    голов россиян или тройки с другой страной."""
    env = SimpleNamespace(
        job=push_job, bot=fake_bot, api=api, downloads=[], clip_checks=[], marked=[],
        sent=chat_marks, deactivated=[], missing=set(), unpublished=set(), clips=goal_clip_store,
        goals=[], subscribed={}, digest_chats=[], last_sent={}, loaded=False,
        subs=push_job._GameSubscriptions,
    )

    def published(game_id, event_id):
        env.clip_checks.append((game_id, event_id))
        return (game_id, event_id) not in env.unpublished

    def mark(chat_id, game_ids):
        env.marked += [(chat_id, game_id) for game_id in game_ids]
        env.sent.update((chat_id, game_id) for game_id in game_ids)

    def card(game_id):
        return f"CARD {game_id}", [
            {"label": f"▶ {e}", "game_id": g, "event_id": e} for g, e, _ in env.goals if g == game_id
        ]

    def download(game_id, event_id):
        env.downloads.append((game_id, event_id))
        if (game_id, event_id) in env.missing:
            return None
        video, thumb = tmp_path / f"{event_id}.mp4", tmp_path / f"{event_id}.jpg"
        video.write_bytes(b"video")
        thumb.write_bytes(b"thumb")
        return SimpleNamespace(
            path=str(video), caption=f"<b>GOAL</b> {event_id}",
            width=1280, height=720, duration=9, thumb_path=str(thumb),
        )

    for name, fake in (
        ("mark_chat_games_sent", mark),
        ("night_goals", lambda night: env.goals),
        ("game_message", card),
        ("game_score_line", lambda game_id: f"SCORE {game_id}"),
        ("goal_clip_published", published),
        ("download_goal_video", download),
        ("deactivate_chat", env.deactivated.append),
        ("subscribed_games", lambda night: env.subscribed),
        ("night_is_loaded", lambda night: env.loaded),
        ("list_active_morning_digest_rows", lambda: [(c, env.last_sent.get(c)) for c in env.digest_chats]),
        ("mark_digest_night_sent", lambda chat_id, night: env.last_sent.update({chat_id: night})),
        ("day_digest", lambda night: ONE_GAME_DAY),
        ("upcoming_games", lambda now: []),
    ):
        monkeypatch.setattr(push_job, name, fake)

    def run(chats, games, goals, final=False):
        env.goals = [goal if len(goal) == 3 else (*goal, "RUS") for goal in goals]
        subscribed = {}
        for chat_id, country in chats:
            for game_id, block in games:
                subscribed.setdefault(chat_id, {})[game_id] = env.subs([], [(country, block)])
        return push_job.run_game_broadcast(env.bot, NIGHT, final, subscribed)

    env.run = run
    env.tmp_path = tmp_path
    return env


def _attached_parts(fake_bot, album_request):
    """Multipart-форма, как её соберёт aiogram для альбома: ({имя части → имя файла},
    поле ``media`` как JSON). Telegram принимает файл альбома, только если он есть
    отдельной частью формы и в ``media`` на него указывает ``attach://<имя части>``."""
    form = AiohttpSession().build_form_data(fake_bot, album_request)
    parts, media_json = {}, None
    for options, _headers, value in form._fields:
        if options["name"] == "media":
            media_json = json.loads(value)
        elif "filename" in options:
            parts[options["name"]] = options["filename"]
    return parts, media_json


@pytest.mark.asyncio
async def test_country_game_is_short_text_with_card_button_then_goal_album(push_env, fake_bot):
    """Задача 76: матч — короткий текст (счёт и блок страны, без полной карточки) с кнопкой
    «Карточка матча», за ним голы страны альбомом с общей подписью — описаниями всех его
    голов; без голов — только текст. Второму чату клипы — по ``file_id``, без повторной
    загрузки, с той же подписью."""
    push_env.missing = {(GAME_A, 101)}
    await push_env.run(
        [(111, "RUS"), (222, "RUS")],
        [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")],
        _goals(GAME_A, 3),
    )
    api = push_env.api

    assert _order(api) == [
        ("message", 111), ("album", 111), ("message", 111),
        ("message", 222), ("album", 222), ("message", 222),
    ]
    assert api.sent_messages == [], "тексты матчей уходят rich-сообщениями"
    messages = api.sent_rich
    assert [m.rich_message.html for m in messages[:2]] == [
        rich_html(f"SCORE {GAME_A}\n\nBLOCK A") + RUS_FOOTER,
        rich_html(f"SCORE {GAME_B}\n\nBLOCK B") + RUS_FOOTER,
    ]
    assert all(m.rich_message.html.startswith("<p>SCORE ") for m in messages[:2]), "первая строка — счёт"
    buttons = [m.reply_markup.inline_keyboard for m in messages[:2]]
    assert [[(b.text, b.callback_data) for row in kb for b in row] for kb in buttons] == [
        [("Карточка матча", f"dg:{GAME_A}")], [("Карточка матча", f"dg:{GAME_B}")],
    ]
    # Гол без клипа пропущен: в альбоме два видео; Telegram показывает одну подпись
    # альбома, поэтому описания обоих голов — у первого видео.
    first_request, second_request = api.sent(SendMediaGroup)
    first, second = first_request.media, second_request.media
    assert [m.caption for m in first] == ["<b>GOAL</b> 100\n\n<b>GOAL</b> 102", None]
    assert first[0].parse_mode == "HTML"
    assert [m.caption for m in second] == [first[0].caption, None]
    files = [f for m in first for f in (m.media, m.thumbnail)]
    assert all(isinstance(f, BufferedInputFile) for f in files), "первому чату — загрузка"
    assert len({id(f) for f in files}) == 4, "у каждого видео и превью свой файл"
    assert [m.media for m in second] == ["fid-1", "fid-2"]
    # Форма запроса: каждый файл — отдельная часть, а ``media`` ссылается на них через
    # attach://; иначе поля выпадают из запроса и Telegram альбом не принимает.
    parts, media_json = _attached_parts(fake_bot, first_request)
    attached = [item[field] for item in media_json for field in ("media", "thumbnail")]
    assert all(ref.startswith("attach://") for ref in attached)
    assert [ref.removeprefix("attach://") for ref in attached] == list(parts)
    assert len(set(attached)) == 4, "у каждого видео и превью свой attach://"
    assert sorted(parts.values()) == ["100.jpg", "100.mp4", "102.jpg", "102.mp4"]
    # Клипы качаются один раз; гол без клипа — у каждого чата заново (в кэш он не попадает).
    assert sorted(push_env.downloads) == sorted(_goals(GAME_A, 3) + [(GAME_A, 101)])
    assert push_env.clips == {
        (GAME_A, 100): ("fid-1", "<b>GOAL</b> 100"), (GAME_A, 102): ("fid-2", "<b>GOAL</b> 102"),
    }
    assert list(push_env.tmp_path.iterdir()) == [], "временные файлы удалены"
    assert push_env.marked == [(111, GAME_A), (111, GAME_B), (222, GAME_A), (222, GAME_B)]


@pytest.mark.asyncio
async def test_country_game_waits_for_clips_until_the_deadline(push_env):
    """Задачи 76, 85: матч уже в БД, но NHL ещё не выложила клип одного из его голов (даже
    не гола страны) — матч ждёт следующего прогона (матч без голов уходит сразу); к
    крайнему сроку уходит с тем, что есть. NHL спрашивается один раз на гол за прогон,
    сколько бы ни было подписчиков."""
    games = [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")]
    goals = _goals(GAME_A, 1) + [(GAME_A, 101, "CAN")]
    push_env.unpublished = {(GAME_A, 101)}
    await push_env.run([(111, "RUS"), (222, "RUS")], games, goals)

    assert push_env.marked == [(111, GAME_B), (222, GAME_B)]
    assert push_env.clip_checks == _goals(GAME_A, 2)
    assert _order(push_env.api) == [("message", 111), ("message", 222)]

    # Следующий прогон: клип выложен — матч уходит; ушедший GAME_B не повторяется. В
    # видео — только гол россиянина.
    push_env.unpublished = set()
    await push_env.run([(111, "RUS"), (222, "RUS")], games, goals)
    assert push_env.marked[2:] == [(111, GAME_A), (222, GAME_A)]
    assert [r.caption for r in push_env.api.sent(SendVideo)] == ["<b>GOAL</b> 100"] * 2

    # Крайний срок: клипа так и нет — матч уходит без проверки NHL.
    push_env.unpublished = {(GAME_B, 200)}
    push_env.clip_checks.clear()
    await push_env.run([(333, "RUS")], [(GAME_B, "BLOCK B")], [(GAME_B, 200)], final=True)
    assert push_env.marked[4:] == [(333, GAME_B)]
    assert push_env.clip_checks == []


def test_albums_respect_album_size_and_upload_budget(push_job, monkeypatch):
    """Альбом — не больше 10 клипов и не больше бюджета загружаемых байт;
    клип по ``file_id`` бюджета не тратит, клип тяжелее бюджета уходит один."""
    monkeypatch.setattr(push_job, "_UPLOAD_BUDGET_BYTES", 100)

    def clip(n, size):
        video = BufferedInputFile(b"v" * size, filename=f"{n}.mp4")
        return ((GAME_A, n), video, {"thumbnail": BufferedInputFile(b"", filename=f"{n}.jpg")}, "")

    cached = [((GAME_A, n), f"fid-{n}", {}, "") for n in range(12)]
    assert [len(a) for a in push_job._albums(cached)] == [10, 2]
    heavy = [clip(0, 60), clip(1, 40), clip(2, 1), clip(3, 150), clip(4, 10)]
    assert [[c[0][1] for c in a] for a in push_job._albums(heavy)] == [[0, 1], [2], [3], [4]]


def test_upload_bytes_counts_video_and_thumbnail_but_not_cached_file_id(push_job):
    """Загрузка клипа — байты mp4 плюс превью; клип по ``file_id`` не грузит ничего."""
    video = BufferedInputFile(b"v" * 7, filename="1.mp4")
    thumbnail = BufferedInputFile(b"t" * 3, filename="1.jpg")
    assert push_job._upload_bytes(((GAME_A, 1), video, {"thumbnail": thumbnail}, "")) == 10
    assert push_job._upload_bytes(((GAME_A, 1), video, {}, "")) == 7
    assert push_job._upload_bytes(((GAME_A, 1), "fid-1", {}, "")) == 0


def test_albums_split_when_common_caption_exceeds_telegram_limit(push_job):
    """Общая подпись альбома — до 1024 видимых символов: HTML-теги не считаются,
    описания разделяет пустая строка (2 символа); лишний гол — в следующий альбом."""
    def clip(n, visible):
        return ((GAME_A, n), f"fid-{n}", {}, "<b>" + "x" * visible + "</b>")

    fits = [clip(0, 500), clip(1, 522)]  # 500 + 2 + 522 = 1024
    assert [len(a) for a in push_job._albums(fits)] == [2]
    over = [clip(0, 500), clip(1, 523), clip(2, 10)]
    assert [[c[0][1] for c in a] for a in push_job._albums(over)] == [[0], [1, 2]]


@pytest.mark.asyncio
async def test_country_game_over_upload_budget_goes_out_in_several_albums(
    push_env, push_job, monkeypatch
):
    """Задача 75: альбом из 10 клипов по 8–19 МБ Telegram отверг (лимит 50 МБ на запрос).
    Клипы матча делятся по бюджету загрузки, одиночный остаток — ``send_video``. Второму
    чату клипы идут по ``file_id`` — бюджет не тратят."""
    monkeypatch.setattr(push_job, "_UPLOAD_BUDGET_BYTES", 25)  # клип с превью — 10 байт
    await push_env.run(
        [(111, "RUS"), (222, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 5), final=True
    )
    api = push_env.api

    assert _order(api) == [
        ("message", 111), ("album", 111), ("album", 111), ("video", 111),
        ("message", 222), ("album", 222),
    ]
    assert [len(r.media) for r in api.sent(SendMediaGroup)] == [2, 2, 5]
    [video] = api.sent(SendVideo)
    assert video.caption == "<b>GOAL</b> 104", "одиночный клип — со своей подписью"
    assert video.parse_mode == "HTML"


@pytest.mark.asyncio
async def test_country_broadcast_failed_game_does_not_stop_the_next(push_env):
    """Ночь 2026-10-13: отказ Telegram на альбоме погасил всю рассылку чату. Теперь сбой
    одного матча логируется и отмечается (повтора не будет), остальные матчи уходят."""
    push_env.api.fail_next(
        SendMediaGroup,
        TelegramBadRequest(method=_method(), message="Bad Request: Request Entity Too Large"),
    )
    await push_env.run(
        [(111, "RUS")],
        [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")],
        _goals(GAME_A, 2) + _goals(GAME_B, 1, first_event=200),
    )

    assert _order(push_env.api) == [("message", 111), ("message", 111), ("video", 111)]
    assert push_env.deactivated == []
    assert push_env.marked == [(111, GAME_A), (111, GAME_B)]


@pytest.mark.asyncio
async def test_clip_uploaded_once_serves_later_runs_and_the_card_button(
    push_env, bot_module, make_callback, monkeypatch
):
    """Задача 87: клип, загруженный рассылкой, следующий прогон шлёт по ``file_id`` без
    скачивания, и кнопка видео в карточке берёт тот же ``file_id`` — сразу, без загрузки."""
    await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 1))
    assert push_env.downloads == _goals(GAME_A, 1)
    [uploaded] = push_env.api.sent(SendVideo)
    assert isinstance(uploaded.video, BufferedInputFile)

    push_env.downloads.clear()
    await push_env.run([(222, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 1))
    stats_handlers = bot_module("stats_handlers")
    monkeypatch.setattr(stats_handlers, "download_goal_video", push_env.downloads.append)
    data = f"gv:{GAME_A}:100"
    match = re.match(stats_handlers.GOAL_VIDEO_CALLBACK_PATTERN, data)
    await stats_handlers.handle_goal_video(make_callback(data, chat_id=333), push_env.bot, match)

    assert push_env.downloads == []
    _, by_run, by_button = push_env.api.sent(SendVideo)
    assert (by_run.chat_id, by_run.video, by_run.caption) == (222, "fid-1", "<b>GOAL</b> 100")
    assert (by_button.chat_id, by_button.video, by_button.caption) == (333, "fid-1", "<b>GOAL</b> 100")
    assert push_env.api.answers == [None], "из кэша — без «Загружаю видео гола...»"


@pytest.mark.asyncio
async def test_rejected_cached_file_id_is_logged_and_clips_reuploaded_once(push_env, caplog):
    """Задача 87: Telegram отверг ``file_id`` из кэша (протух) — громкая ошибка в логе и одна
    повторная отправка альбома: клип из кэша скачивается заново, загруженный в этот раз —
    не качается второй раз; кэш получает новый ``file_id``."""
    push_env.clips[(GAME_A, 100)] = ("stale", "<b>GOAL</b> 100")
    push_env.api.fail_next(
        SendMediaGroup,
        TelegramBadRequest(method=_method(), message="Bad Request: wrong file identifier/HTTP URL specified"),
    )
    await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 2))

    assert push_env.downloads == [(GAME_A, 101), (GAME_A, 100)]
    [album] = push_env.api.sent(SendMediaGroup)
    assert all(isinstance(m.media, BufferedInputFile) for m in album.media)
    assert [m.media.filename for m in album.media] == ["100.mp4", "101.mp4"], "порядок голов сохранён"
    assert push_env.clips == {
        (GAME_A, 100): ("fid-1", "<b>GOAL</b> 100"), (GAME_A, 101): ("fid-2", "<b>GOAL</b> 101"),
    }
    assert [r.levelname for r in caplog.records if "rejected cached clips" in r.getMessage()] == ["ERROR"]
    assert push_env.marked == [(111, GAME_A)]


@pytest.mark.asyncio
async def test_rejected_cached_clip_that_fails_to_download_drops_out_of_the_retry(push_env):
    """Задача 87: клип с протухшим ``file_id`` при повторе не скачался — он выпадает, а
    старый ``file_id`` второй раз не шлётся; оставшийся клип уходит один, ``send_video``."""
    push_env.clips[(GAME_A, 100)] = ("stale", "<b>GOAL</b> 100")
    push_env.missing = {(GAME_A, 100)}
    push_env.api.fail_next(
        SendMediaGroup,
        TelegramBadRequest(method=_method(), message="Bad Request: wrong file identifier/HTTP URL specified"),
    )
    await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 2))

    assert push_env.downloads == [(GAME_A, 101), (GAME_A, 100)]
    assert push_env.api.sent(SendMediaGroup) == []
    [video] = push_env.api.sent(SendVideo)
    assert video.video.filename == "101.mp4"
    assert push_env.clips[(GAME_A, 100)] == ("stale", "<b>GOAL</b> 100")


@pytest.mark.asyncio
async def test_country_broadcast_night_without_players_sends_nothing(push_env):
    await push_env.run([(111, "RUS")], [], [])

    assert (_order(push_env.api), push_env.downloads, push_env.marked) == ([], [], [])


@pytest.mark.asyncio
async def test_game_broadcast_deactivates_only_the_blocked_chat(push_env):
    """Заблокировавший бота чат теряет все подписки один раз и больше матчей не получает;
    остальным рассылка идёт."""
    # Первый запрос рассылки — сообщение чату 111: он «заблокировал» бота.
    push_env.api.fail_next(SendRichMessage, _forbidden())
    await push_env.run(
        [(111, "RUS"), (222, "RUS")], [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")], []
    )

    assert push_env.deactivated == [111]
    assert _order(push_env.api) == [("message", 222), ("message", 222)]
    assert push_env.marked == [(111, GAME_A), (222, GAME_A), (222, GAME_B)]


@pytest.mark.asyncio
async def test_subscriber_of_both_countries_and_both_teams_gets_the_game_once(push_env):
    """Задача 85, правило человека 2026-10-25: подписчик RUS, BLR, обеих команд встречи и
    «Матчей» получает о матче одно сообщение — полную карточку с блоками обеих стран и
    строкой подписок и кнопкой видео каждого гола, за ней альбомом голы игроков
    подписанных стран (гол чужой страны — только кнопкой); дайджест этого матча не
    содержит. Повторный прогон ничего не дублирует."""
    push_env.goals = [(GAME_A, 100, "RUS"), (GAME_A, 101, "BLR"), (GAME_A, 102, "CAN")]
    push_env.subscribed = {
        111: {GAME_A: push_env.subs(["TOR", "BOS"], [("BLR", "BLR BLOCK"), ("RUS", "RUS BLOCK")])}
    }
    push_env.digest_chats, push_env.loaded = [111], True
    for _ in range(2):
        await push_env.job.broadcast_if_due(push_env.bot, _msk(8, 0))

    assert _order(push_env.api) == [("message", 111), ("album", 111)]
    [card] = push_env.api.sent_rich
    assert card.rich_message.html == rich_html(
        f"CARD {GAME_A}\n\nBLR BLOCK\n\nRUS BLOCK"
    ) + "<footer>По подписке: TOR · BOS · Беларусь · Россия<br>Настроить подписки: /subscriptions</footer>"
    assert card.rich_message.html.startswith(f"<p>CARD {GAME_A}</p>"), "первая строка — карточка, не подписка"
    assert [b.callback_data for row in card.reply_markup.inline_keyboard for b in row] == [
        f"gv:{GAME_A}:100", f"gv:{GAME_A}:101", f"gv:{GAME_A}:102",
    ]
    [album] = push_env.api.sent(SendMediaGroup)
    assert [m.media.filename for m in album.media] == ["100.mp4", "101.mp4"]
    assert push_env.downloads == [(GAME_A, 100), (GAME_A, 101)]
    assert push_env.marked == [(111, GAME_A)]
    assert push_env.last_sent == {111: NIGHT}, "дайджест отмечен, но пуст — не ушёл"


@pytest.mark.asyncio
async def test_team_subscriber_without_countries_gets_the_card_without_clips(push_env):
    """Подписка только на команду: карточка с кнопками видео, без альбома и без скачивания
    клипов — даже если голы забили игроки страны, на которую у чата подписки нет."""
    push_env.goals = [(GAME_A, 100, "RUS"), (GAME_A, 101, "CAN")]
    subs = push_env.subs(["MIN"], [])
    await push_env.job.run_game_broadcast(push_env.bot, NIGHT, False, {111: {GAME_A: subs}})

    assert _order(push_env.api) == [("message", 111)]
    assert push_env.downloads == []
    assert push_env.marked == [(111, GAME_A)]


@pytest.mark.asyncio
async def test_non_telegram_failure_after_the_text_marks_the_game_and_fails_loudly(push_env, monkeypatch):
    """Ревью Задачи 85: текст ушёл, а скачивание клипа упало не ошибкой Telegram — матч всё
    равно отмечен (иначе текст приходил бы каждые полчаса и ещё раз в дайджесте), ошибка
    всплывает."""
    def broken(game_id, event_id):
        raise RuntimeError("ffmpeg")

    monkeypatch.setattr(push_env.job, "download_goal_video", broken)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 1))

    assert _order(push_env.api) == [("message", 111)]
    assert push_env.marked == [(111, GAME_A)]


@pytest.mark.asyncio
async def test_country_subscribed_after_the_digest_does_not_resend_its_games(push_env):
    """Задача 85: матч ушёл чату в дайджесте, затем чат подписался на страну, чьи игроки в
    нём играли, — следующий прогон матч не шлёт: каждый матч — один раз."""
    push_env.digest_chats, push_env.loaded = [111], True
    await push_env.job.broadcast_if_due(push_env.bot, _msk(8, 0))
    before = len(push_env.api.outgoing)

    push_env.subscribed = {111: {GAME_A: push_env.subs([], [("RUS", "BLOCK")])}}
    await push_env.job.broadcast_if_due(push_env.bot, _msk(8, 30))

    assert before > 0 and len(push_env.api.outgoing) == before
    assert push_env.marked == [(111, GAME_A)]


@pytest.mark.asyncio
async def test_subscriber_of_two_countries_gets_one_short_message_and_each_clip_once(push_env):
    """Задача 85: только страны — одно короткое сообщение со строкой подписок и блоками
    обеих стран, за ним одним альбомом голы игроков этих стран, каждый клип один раз;
    гол чужой страны в альбом не берётся."""
    push_env.goals = [(GAME_A, 100, "RUS"), (GAME_A, 101, "CAN"), (GAME_A, 102, "BLR")]
    subs = push_env.subs([], [("BLR", "BLR BLOCK"), ("RUS", "RUS BLOCK")])
    await push_env.job.run_game_broadcast(push_env.bot, NIGHT, False, {111: {GAME_A: subs}})

    assert _order(push_env.api) == [("message", 111), ("album", 111)]
    [message] = push_env.api.sent_rich
    assert message.rich_message.html == rich_html(
        f"SCORE {GAME_A}\n\nBLR BLOCK\n\nRUS BLOCK"
    ) + "<footer>По подписке: Беларусь · Россия<br>Настроить подписки: /subscriptions</footer>"
    assert message.rich_message.html.startswith(f"<p>SCORE {GAME_A}</p>"), "первая строка — счёт"
    [album] = push_env.api.sent(SendMediaGroup)
    assert [m.media.filename for m in album.media] == ["100.mp4", "102.mp4"]
    assert push_env.downloads == [(GAME_A, 100), (GAME_A, 102)]


@pytest.mark.asyncio
async def test_game_card_resends_once_after_retry_after(push_env):
    """``_throttled`` повторяет отправку карточки; без ``await`` 429 вообще не всплыла бы."""
    push_env.api.fail_next(SendRichMessage, _retry_after())
    await push_env.job.run_game_broadcast(
        push_env.bot, NIGHT, False, {111: {GAME_A: push_env.subs(["TOR"], [])}}
    )

    # Первая попытка упала на 429 и не записана; без повтора сообщения не было бы вовсе.
    assert [m.chat_id for m in push_env.api.sent_rich] == [111]
    assert push_env.marked == [(111, GAME_A)]


# ---------------------------------------------------------------------------
# Предстоящие матчи (Задача 78): с 15:30 МСК по превью на прогон, каждый матч один раз
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _slate_game(game_id, start, away="BOS", home="TOR", state="OK"):
    """Матч из ``score/{день}`` NHL API — только поля, которые читает рассылка."""
    return {
        "id": game_id, "gameDate": "2026-10-24", "startTimeUTC": start,
        "gameScheduleState": state,
        "awayTeam": {"abbrev": away}, "homeTeam": {"abbrev": home},
    }


SLATE = {
    "currentDate": "2026-10-24",
    "games": [
        _slate_game(GAME_B, "2026-10-24T23:30:00Z", "NYR", "MTL"),
        _slate_game(GAME_A, "2026-10-24T23:00:00Z"),
        _slate_game(2026020003, "2026-10-24T23:00:00Z", "PIT", "WSH", state="PPD"),
        _slate_game(2026020004, "2026-10-25T02:00:00Z", "VAN", "SEA"),
    ],
}


@pytest.fixture
def upcoming_env(push_job, monkeypatch):
    """Рассылка превью с подменой NHL API, превью и БД: ``days`` — дни лиги, за которыми
    ходили в NHL, ``previews`` — пары (гости, хозяева), для которых собиралось превью,
    ``sent`` — отметки (подписка, матч), ``deactivated`` — погашенные подписки, ``chats`` —
    (id подписки, chat_id) активных подписчиков, ``read_subs`` — сколько раз их читали из БД;
    ``premium`` — чаты с премиумом (Задача 96), ``predicted`` — матчи, чей прогноз читался из
    ``game_predictions`` (у каждого — победа хозяев 61%); ``run(bot, now)`` — один прогон."""
    env = SimpleNamespace(days=[], previews=[], sent=set(), deactivated=[], chats=[], read_subs=0,
                          premium=set(), predicted=[])

    def fetch(day):
        env.days.append(day)
        return SLATE

    def preview(away, home):
        env.previews.append((away, home))
        return f"PREVIEW {away} @ {home}", "Сезон 2026/27"

    def rows():
        env.read_subs += 1
        return env.chats

    def predictions(query, params, columns):
        env.predicted.append(params[0])
        return {"count_rows": 1, "task": ["home_win"], "probability": [0.61]}

    # Строка прогноза — настоящая `with_model_prediction`, подменена только её выборка.
    monkeypatch.setattr(sys.modules["bot_messages"], "fetch_all", predictions)
    monkeypatch.setattr(push_job.premium, "is_premium", lambda chat_id: chat_id in env.premium)
    monkeypatch.setattr(push_job, "fetch_score", fetch)
    monkeypatch.setattr(push_job, "matchup_season_preview", preview)
    monkeypatch.setattr(push_job, "list_active_upcoming_rows", rows)
    monkeypatch.setattr(
        push_job, "sent_games", lambda game_ids: {s for s in env.sent if s[1] in game_ids}
    )
    monkeypatch.setattr(push_job, "mark_game_sent", lambda sub, game: env.sent.add((sub, game)))
    monkeypatch.setattr(push_job, "deactivate_chat", env.deactivated.append)
    env.run = push_job.run_upcoming_broadcast
    return env


def test_upcoming_games_are_not_started_games_of_the_day(push_job, upcoming_env):
    """Предстоящие — ещё не начавшиеся матчи игрового дня лиги (ET), по времени начала;
    перенесённый (``PPD``) — нет; матч, начинающийся ровно сейчас, уже не предстоящий."""
    games = push_job.upcoming_games(datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    assert [(g[0], g[1].strftime("%H:%M"), g[2], g[3]) for g in games] == [
        (GAME_A, "23:00", "BOS", "TOR"), (GAME_B, "23:30", "NYR", "MTL"),
        (2026020004, "02:00", "VAN", "SEA"),
    ]
    assert upcoming_env.days == [date(2026, 10, 24)]
    assert [g[0] for g in push_job.upcoming_games(datetime(2026, 10, 24, 23, 0, tzinfo=UTC))] == [
        GAME_B, 2026020004
    ]
    assert push_job.upcoming_games(datetime(2026, 10, 25, 2, 0, tzinfo=UTC)) == []


def test_upcoming_games_wait_for_preview_start_of_the_league_day(push_job, upcoming_env):
    """Игровой день меняется в 03:00 ET (07:00 UTC летом), а ночные слоты sync идут до
    08:00 UTC: утром весь вечерний слейт «ещё не начался», но превью — только с 15:30 МСК
    дня лиги; до этого NHL API не спрашивается."""
    for hour, minute in ((7, 30), (8, 0), (12, 0), (12, 29)):
        assert push_job.upcoming_games(datetime(2026, 10, 24, hour, minute, tzinfo=UTC)) == []
    assert upcoming_env.days == []
    assert push_job.upcoming_games(datetime(2026, 10, 24, 12, 30, tzinfo=UTC)) != []


@pytest.mark.asyncio
async def test_upcoming_one_preview_per_run_in_start_order(push_job, upcoming_env, fake_bot, api):
    """Решение человека (2026-10-25): с 15:30 МСК на каждом прогоне (раз в полчаса)
    подписчику одно превью — следующий по началу матч дня, каждый матч один раз; когда
    матчи кончились, прогон ничего не шлёт. Первая строка — время начала по МСК; превью
    собирается раз на матч, а не на подписчика."""
    upcoming_env.chats = [(10, 111), (20, 222)]
    by_slot = []
    for hour, minute in ((12, 30), (13, 0), (13, 30), (14, 0)):
        before = len(api.sent_rich)
        await upcoming_env.run(fake_bot, datetime(2026, 10, 24, hour, minute, tzinfo=UTC))
        by_slot.append([(m.chat_id, m.rich_message.html) for m in api.sent_rich[before:]])

    a = rich_html("⏰ Начало в 02:00 МСК\nPREVIEW BOS @ TOR") + PREVIEW_FOOTER
    b = rich_html("⏰ Начало в 02:30 МСК\nPREVIEW NYR @ MTL") + PREVIEW_FOOTER
    c = rich_html("⏰ Начало в 05:00 МСК\nPREVIEW VAN @ SEA") + PREVIEW_FOOTER
    assert by_slot == [[(111, a), (222, a)], [(111, b), (222, b)], [(111, c), (222, c)], []]
    assert api.sent_messages == [], "превью уходят rich-сообщениями"
    assert all(m.rich_message.html.startswith("<p>⏰ Начало в ") for m in api.sent_rich)
    assert upcoming_env.previews == [("BOS", "TOR"), ("NYR", "MTL"), ("VAN", "SEA")]
    assert upcoming_env.predicted == [], "бесплатным прогноз не читается"


@pytest.mark.asyncio
async def test_upcoming_prediction_only_for_premium_chat(push_job, upcoming_env, fake_bot, api):
    """Задача 96: один матч, два чата — премиум получает строку прогноза, бесплатный нет;
    превью общее и собирается один раз, прогноз дописывается при отправке, кто бы ни шёл
    в рассылке первым."""
    upcoming_env.chats = [(10, 111), (20, 222), (30, 333)]
    upcoming_env.premium = {222}
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))

    plain = rich_html("⏰ Начало в 02:00 МСК\nPREVIEW BOS @ TOR") + PREVIEW_FOOTER
    with_prediction = rich_html("⏰ Начало в 02:00 МСК\nPREVIEW BOS @ TOR") + (
        "<footer>Сезон 2026/27<br>Оценка модели (не совет): победа TOR — 61%"
        "<br>Настроить подписки: /subscriptions</footer>"
    )
    assert [(m.chat_id, m.rich_message.html) for m in api.sent_rich] == [
        (111, plain), (222, with_prediction), (333, plain),
    ]
    assert upcoming_env.previews == [("BOS", "TOR")]
    assert upcoming_env.predicted == [GAME_A]


@pytest.mark.asyncio
async def test_upcoming_next_game_is_per_subscriber(push_job, upcoming_env, fake_bot, api):
    """Следующий матч — свой у каждого подписчика: кому первый уже ушёл, получает второй;
    уже начавшиеся пропускаются."""
    upcoming_env.chats = [(10, 111), (20, 222)]
    upcoming_env.sent = {(20, GAME_A)}
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    assert [(m.chat_id, m.rich_message.html) for m in api.sent_rich] == [
        (111, rich_html("⏰ Начало в 02:00 МСК\nPREVIEW BOS @ TOR") + PREVIEW_FOOTER),
        (222, rich_html("⏰ Начало в 02:30 МСК\nPREVIEW NYR @ MTL") + PREVIEW_FOOTER),
    ]

    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 23, 40, tzinfo=UTC))
    assert [(m.chat_id, m.rich_message.html) for m in api.sent_rich[2:]] == [
        (111, rich_html("⏰ Начало в 05:00 МСК\nPREVIEW VAN @ SEA") + PREVIEW_FOOTER),
        (222, rich_html("⏰ Начало в 05:00 МСК\nPREVIEW VAN @ SEA") + PREVIEW_FOOTER),
    ]


@pytest.mark.asyncio
async def test_run_without_upcoming_games_sends_nothing(push_job, upcoming_env, fake_bot, api):
    """Прогон без предстоящих матчей ничего не шлёт и подписчиков из БД не читает."""
    upcoming_env.chats = [(10, 111)]
    await upcoming_env.run(fake_bot, datetime(2026, 10, 25, 2, 30, tzinfo=UTC))
    assert api.outgoing == []
    assert upcoming_env.read_subs == 0
    assert upcoming_env.sent == set()


@pytest.mark.asyncio
async def test_upcoming_blocked_chat_is_deactivated_and_others_still_get_it(
    push_job, upcoming_env, fake_bot, api
):
    """Заблокировавший бота чат теряет все подписки, матч ему отмечается; следующий
    подписчик превью получает."""
    upcoming_env.chats = [(10, 111), (20, 222)]
    api.fail_next(SendRichMessage, _forbidden())
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))

    assert [m.chat_id for m in api.sent_rich] == [222]
    assert upcoming_env.deactivated == [111]
    assert upcoming_env.sent == {(10, GAME_A), (20, GAME_A)}


@pytest.mark.asyncio
async def test_upcoming_send_error_moves_on_to_next_game(push_job, upcoming_env, fake_bot, api):
    """Сбой отправки (не блокировка) логируется и не повторяется: матч отмечен, следующий
    прогон шлёт следующий матч, подписка жива."""
    upcoming_env.chats = [(10, 111)]
    api.fail_next(SendRichMessage, TelegramAPIError(method=_method(), message="flood"))
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    assert api.outgoing == []
    assert upcoming_env.sent == {(10, GAME_A)}

    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 13, 0, tzinfo=UTC))
    assert [m.rich_message.html for m in api.sent_rich] == [
        rich_html("⏰ Начало в 02:30 МСК\nPREVIEW NYR @ MTL") + PREVIEW_FOOTER
    ]
    assert upcoming_env.deactivated == []


@pytest.mark.asyncio
async def test_game_failure_does_not_stop_digest_and_upcoming_and_still_fails_the_run(
    push_job, fake_bot
):
    """Рассылки независимы (ревью Задачи 78): постоянный сбой итогов по матчам (ffmpeg,
    Replay API) не срывает ни дайджест на крайнем сроке, ни превью, но прогон всё равно
    падает — ошибка пробрасывается после всех рассылок. Превью — с моментом своего старта,
    а не прогона."""
    calls = []

    async def games(*args):
        calls.append("games")
        raise RuntimeError("ffmpeg")

    async def digest(*args):
        calls.append("digest")

    async def upcoming(bot, now):
        calls.append(("upcoming", now))

    started = _msk(11, 0)
    with patch.object(push_job, "subscribed_games", return_value={}), patch.object(
        push_job, "run_game_broadcast", games
    ), patch.object(push_job, "run_morning_digest_broadcast", digest), patch.object(
        push_job, "run_upcoming_broadcast", upcoming
    ):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            await push_job.broadcast_if_due(fake_bot, started)

    assert calls[:2] == ["games", "digest"]
    assert calls[2][0] == "upcoming" and calls[2][1] > started


@pytest.mark.asyncio
async def test_main_upcoming_only_skips_the_night(push_job, bot_module, monkeypatch, set_required_bot_env):
    """Дневной слот (``--upcoming-only``): только превью, ночь и NHL API о ней не трогаются."""
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", True)

    async def noop(self):
        return None

    monkeypatch.setattr(AiohttpSession, "close", noop)
    calls = []

    async def upcoming(bot, now):
        calls.append(bot.token)

    async def night(*args):
        raise AssertionError("ночь днём не рассылается")

    monkeypatch.setattr(push_job, "run_upcoming_broadcast", upcoming)
    monkeypatch.setattr(push_job, "broadcast_if_due", night)

    await push_job.main(True)

    assert calls == [FAKE_TOKEN]
