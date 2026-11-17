"""Тесты cron-скрипта рассылки ``telegram_bot/push_digest_job.py``.

Модуль годами был сломан незамеченным (сначала ``ImportError`` на
``telegram.error.Forbidden``, затем несделанные ``await`` после перехода на
PTB 21.x) ровно потому, что ни один тест его не импортировал. Здесь он
именно **вызывается**: рассылка прогоняется через настоящий
``dispatch_day_digest_messages`` на настоящем aiogram ``Bot`` из conftest
(``fake_bot`` на ``FakeSession``), поэтому забытый ``await`` виден как «сообщение
не отправлено», а не как тихо брошенная корутина. Границы — БД
(``subscription_repo``, экранные модули) и HTTP-сессия Telegram — подменяются,
сеть не используется.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import EditMessageText, SendMediaGroup, SendMessage, SendRichMessage, SendVideo

from tests._telegram_fakes import BOT_USERNAME, FAKE_TOKEN, body, rich_html

# Один «настоящий» матч дня: рассылка шлёт сводку с кнопкой карточки (``summary_db``),
# с подсказкой «Ещё: …» в подвале; сам текст карточки в сообщение не попадает.
ONE_GAME_DAY = ("2026-01-15", [2026020001])

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


@pytest.fixture
def summary_db(fake_db_router):
    """БД сводки дайджеста: любой матч — «BOS 1:0 TOR», кнопка «BOS – TOR»."""
    return fake_db_router([
        ("JOIN teams th", lambda params: [(gid, "BOS", "TOR") for gid in params[0]]),
        ("get_game_stats", [
            (1, 0, 0, 0, 0, False, False, "home", "BOS", "BOS"),
            (0, 0, 0, 0, 0, False, False, "away", "TOR", "TOR"),
        ]),
        ("get_goals_game", [
            ("X", "C", None, None, None, None, None, 1, "01:00", 1, 0,
             False, False, False, True, 1, 1),
        ]),
    ])


# ---------------------------------------------------------------------------
# Утренний дайджест
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_digest_reaches_every_active_subscriber(
    push_job, fake_bot, api, chat_marks, summary_db
):
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, 222)
    ), patch.object(push_job, "mark_digest_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    assert [(type(r), r.chat_id) for r in api.outgoing] == [
        (SendRichMessage, 111), (SendRichMessage, 222),
    ]
    for digest in api.sent_rich:
        html_ = digest.rich_message.html
        assert html_.startswith(rich_html("<b>Матчи ночи</b>"))
        assert "Звёзды матча" not in html_, "один матч — сводка, карточка открывается кнопкой"
        assert "1. BOS 1:0 TOR" in html_
        # Подсказка «Ещё: …» — подвал самого сообщения, а не отдельное сообщение.
        assert html_.endswith("Настроить подписки: /subscriptions</footer>")
        assert "Ещё: /tonight" in html_.rsplit("<footer>", 1)[1]
        [[button]] = digest.reply_markup.inline_keyboard
        assert button.callback_data == "dg:2026020001"
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
FOUR_GAME_DAY = ("2026-10-06", [G1, G2, G3, G4])


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
    push_job, bot_module, fake_bot, api, chat_marks, summary_db, monkeypatch
):
    """На крайнем сроке рассылка по матчам уже прошла: матч подписки без отметки (её сбой)
    уходит в дайджесте, а не пропадает."""
    stats_handlers = bot_module("stats_handlers")
    monkeypatch.setattr(stats_handlers, "game_message", lambda game_id, clips, collapsed: "<b>BOS 3 : 2 TOR</b>")
    monkeypatch.setattr(stats_handlers, "game_goal_events", lambda game_id: [])
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "mark_digest_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, True, {111: {2026020001: "subs"}})

    [card] = api.sent_rich
    assert card.rich_message.html.startswith(rich_html("<b>BOS 3 : 2 TOR</b>")), "подписка — полная карточка"
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
    no_games = ("2026-10-06", [])
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "day_digest", return_value=no_games) as digest, patch.object(
        push_job, "mark_digest_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT, False, {})

    digest.assert_called_once_with(NIGHT)
    assert api.outgoing == []


def _games_in_db(loaded_ids, queried):
    """Подмена ``fetch_all``: из запрошенных id «в ``games``» только *loaded_ids*; запросы — в *queried*."""

    def fetch(query, params, columns):
        queried.append(sorted(params[0]))
        return {"game_id": [g for g in params[0] if g in loaded_ids], "count_rows": 0}

    return fetch


def test_night_is_loaded_counts_games_played_as_scheduled(push_job):
    """Ночь готова, когда в ``games`` все матчи регулярки и плей-офф её даты; перенесённый,
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
    queried: list = []

    with patch.object(push_job, "fetch_score", return_value=payload) as score, patch.object(
        push_job, "fetch_all", side_effect=_games_in_db({2026020050}, queried)
    ):
        assert push_job.night_is_loaded(NIGHT) is False
    score.assert_called_once_with(NIGHT)

    with patch.object(push_job, "fetch_score", return_value=payload), patch.object(
        push_job, "fetch_all", side_effect=_games_in_db({2026020050, 2026020051}, queried)
    ):
        assert push_job.night_is_loaded(NIGHT) is True
    assert queried == [[2026020050, 2026020051]] * 2


def test_night_of_only_playoff_games_waits_for_them(push_job):
    """Ночь только с плей-офф (Задача 104): слейт не пуст, и дайджест ждёт, пока
    загрузчик запишет её матчи, а не уходит пустым сразу."""
    payload = {
        "games": [
            {"id": 2026030111, "gameDate": "2026-10-06", "gameType": 3, "gameScheduleState": "OK"},
        ]
    }
    queried: list = []

    with patch.object(push_job, "fetch_score", return_value=payload), patch.object(
        push_job, "fetch_all", side_effect=_games_in_db(set(), queried)
    ):
        assert push_job.night_is_loaded(NIGHT) is False
    with patch.object(push_job, "fetch_score", return_value=payload), patch.object(
        push_job, "fetch_all", side_effect=_games_in_db({2026030111}, queried)
    ):
        assert push_job.night_is_loaded(NIGHT) is True
    assert queried == [[2026030111]] * 2


# ---------------------------------------------------------------------------
# Итоги по матчу (Задачи 61, 76, 85, 119): одно сообщение под подписки чата, клипы голов
# внутри него, кэш file_id
# ---------------------------------------------------------------------------

_SEND_KIND = {SendRichMessage: "message", SendMediaGroup: "album", SendVideo: "video", EditMessageText: "edit"}


def _order(api):
    """Порядок запросов рассылки: ("message" | "edit" | "album" | "video", chat_id)."""
    return [(_SEND_KIND[type(r)], r.chat_id) for r in api.requests if type(r) in _SEND_KIND]


def _media(message):
    """Клипы rich-сообщения: (id, ``file_id`` или имя загружаемого файла)."""
    return [
        (m.id, m.media.media if isinstance(m.media.media, str) else m.media.media.filename)
        for m in message.rich_message.media or []
    ]


GAME_A, GAME_B = 2026020001, 2026020002
# Подвал рассылок — `<footer>` после тела сообщения (Задача 88).
FOOTER = "<footer>Настроить подписки: /subscriptions</footer>"
# Причина рассылки — в подвале («По подписке: …»), а не первой строкой: первой идёт табло.
RUS_FOOTER = "<footer>По подписке: Россия<br>Настроить подписки: /subscriptions</footer>"
# Подвал превью предстоящего матча: «Сезон …» из `matchup_season_preview`, затем подписки;
# строки прогноза и тизера премиума нет (Задача 123).
PREVIEW_FOOTER = "<footer>Сезон 2026/27<br>Настроить подписки: /subscriptions</footer>"


def _goals(game_id, n, first_event=100):
    return [(game_id, first_event + i) for i in range(n)]


def _card(game_id, clips, collapsed):
    """Заглушка ``game_message``: какие голы получили клип и свёрнута ли карточка."""
    return f"CARD {game_id} clips={sorted(clips)}" + (" collapsed" if collapsed else "")


@pytest.fixture
def push_env(push_job, bot_module, monkeypatch, tmp_path, fake_bot, api, goal_clip_store, chat_marks, summary_db):
    """Рассылки ночи с подменой БД, NHL и скачивания: бот (``fake_bot``, запросы — в
    ``api``), журнал скачиваний и проверок клипов, кэш клипов ``clips``
    (``goal_clip_store``), отметки (чат, матч) — ``sent`` (как в БД) и ``marked`` (по
    порядку), погашенные чаты ``deactivated``. Состояние ночи: ``goals`` — (game_id,
    event_id, страна автора) голов ночи (карточка — ``_card``), ``subscribed`` — ответ ``subscribed_games``,
    ``digest_chats`` и ``last_sent`` — подписчики дайджеста и их ``last_sent_night``,
    ``loaded`` — загружена ли ночь; ``unpublished`` — голы, клипа которых NHL ещё не
    выложила, ``missing`` — голы, клип которых не скачался. ``subs(teams, countries)`` —
    подписки чата на матч; ``run(chats, games, goals, final)`` — ``run_game_broadcast`` по
    странам: ``chats`` — (chat_id, страна), ``games`` — (game_id, блок страны), ``goals`` —
    (game_id, event_id) голов россиян или тройки с другой страной."""
    stats_handlers = bot_module("stats_handlers")
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
        # Автор гола (четвёртое поле) нужен только альбому подписки на игрока: по умолчанию None.
        ("night_goals", lambda night: [goal if len(goal) == 4 else (*goal, None) for goal in env.goals]),
        ("goal_clip_published", published),
        ("deactivate_chat", env.deactivated.append),
        ("subscribed_games", lambda night: env.subscribed),
        ("night_is_loaded", lambda night: env.loaded),
        ("list_active_morning_digest_rows", lambda: [(c, env.last_sent.get(c)) for c in env.digest_chats]),
        ("mark_digest_night_sent", lambda chat_id, night: env.last_sent.update({chat_id: night})),
        ("day_digest", lambda night: ONE_GAME_DAY),
        ("upcoming_games", lambda now: []),
    ):
        monkeypatch.setattr(push_job, name, fake)
    for name, fake in (
        ("game_message", _card),
        ("game_goal_events", lambda game_id: [e for g, e, *_ in env.goals if g == game_id]),
        ("download_goal_video", download),
    ):
        monkeypatch.setattr(stats_handlers, name, fake)

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


def _attached_parts(fake_bot, request):
    """Multipart-форма, как её соберёт aiogram: ({имя части → имя файла}, ссылки
    ``attach://…`` из JSON-полей по порядку). Telegram принимает файл, только если он есть
    отдельной частью формы и JSON на него указывает ``attach://<имя части>``."""
    form = AiohttpSession().build_form_data(fake_bot, request)
    parts, refs = {}, []
    for options, _headers, value in form._fields:
        if "filename" in options:
            parts[options["name"]] = options["filename"]
        elif isinstance(value, str):
            refs += re.findall(r'"attach://([^"]+)"', value)
    return parts, refs


@pytest.mark.asyncio
async def test_country_game_is_collapsed_card_with_clips_then_goal_album(push_env, fake_bot):
    """Задачи 76, 119: подписка на страну — табло, свёрнутая карточка с клипами в строках
    голов и блок страны, без кнопки «Карточка матча»; за ним голы страны альбомом с общей
    подписью — по ``file_id``, которые загрузила карточка. Гол без клипа — строкой без видео
    и не в альбоме. Второму чату клипы — по ``file_id``, без повторной загрузки."""
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
    first_a, first_b, second_a, _ = api.sent_rich
    assert first_a.rich_message.html == rich_html(
        f"CARD {GAME_A} clips=[100, 102] collapsed\n\nBLOCK A"
    ) + RUS_FOOTER
    assert first_b.rich_message.html == rich_html(f"CARD {GAME_B} clips=[] collapsed\n\nBLOCK B") + RUS_FOOTER
    assert all(m.reply_markup is None for m in api.sent_rich)
    assert _media(first_a) == [("goal100", "100.mp4"), ("goal102", "102.mp4")]
    assert first_a.rich_message.media[0].media.thumbnail.filename == "100.jpg"
    assert _media(first_b) == []
    assert _media(second_a) == [("goal100", "fid-1"), ("goal102", "fid-2")]
    # Форма запроса: каждый файл — отдельная часть, а JSON сообщения ссылается на них через
    # attach:// — и на глубине ``rich_message.media[].media``.
    parts, refs = _attached_parts(fake_bot, first_a)
    assert sorted(refs) == sorted(parts) and len(refs) == 4
    assert sorted(parts.values()) == ["100.jpg", "100.mp4", "102.jpg", "102.mp4"]
    # Клипы качаются один раз; гол без клипа — у каждого чата заново (в кэш он не попадает).
    assert push_env.downloads == _goals(GAME_A, 3) + [(GAME_A, 101)]
    assert push_env.clips == {
        (GAME_A, 100): ("fid-1", "<b>GOAL</b> 100"), (GAME_A, 102): ("fid-2", "<b>GOAL</b> 102"),
    }
    # Альбом: Telegram показывает одну подпись — описания обоих голов у первого видео.
    first_album, second_album = (r.media for r in api.sent(SendMediaGroup))
    assert [(m.media, m.caption) for m in first_album] == [
        ("fid-1", "<b>GOAL</b> 100\n\n<b>GOAL</b> 102"), ("fid-2", None),
    ]
    assert first_album[0].parse_mode == "HTML"
    assert [(m.media, m.caption) for m in second_album] == [(m.media, m.caption) for m in first_album]
    assert list(push_env.tmp_path.iterdir()) == [], "временные файлы удалены"
    assert push_env.marked == [(111, GAME_A), (111, GAME_B), (222, GAME_A), (222, GAME_B)]


@pytest.mark.asyncio
async def test_country_game_waits_for_clips_until_the_deadline(push_env):
    """Задачи 76, 85: матч уже в БД, но NHL ещё не выложила клип одного из его голов — матч
    ждёт следующего прогона (матч без голов уходит сразу); к крайнему сроку уходит с тем,
    что есть. NHL спрашивается один раз на гол за прогон, сколько бы ни было подписчиков."""
    games = [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")]
    goals = _goals(GAME_A, 2)
    push_env.unpublished = {(GAME_A, 101)}
    await push_env.run([(111, "RUS"), (222, "RUS")], games, goals)

    assert push_env.marked == [(111, GAME_B), (222, GAME_B)]
    assert push_env.clip_checks == _goals(GAME_A, 2)
    assert _order(push_env.api) == [("message", 111), ("message", 222)]

    # Следующий прогон: клип выложен — матч уходит с клипами всех голов; ушедший GAME_B
    # не повторяется.
    push_env.unpublished = set()
    await push_env.run([(111, "RUS"), (222, "RUS")], games, goals)
    assert push_env.marked[2:] == [(111, GAME_A), (222, GAME_A)]
    assert [len(_media(m)) for m in push_env.api.sent_rich[2:]] == [2, 2]

    # Крайний срок: клипа так и нет — матч уходит без проверки NHL.
    push_env.unpublished = {(GAME_B, 200)}
    push_env.clip_checks.clear()
    await push_env.run([(333, "RUS")], [(GAME_B, "BLOCK B")], [(GAME_B, 200)], final=True)
    assert push_env.marked[4:] == [(333, GAME_B)]
    assert push_env.clip_checks == []


@pytest.mark.asyncio
async def test_clips_over_upload_budget_are_topped_up_for_the_first_subscriber(
    push_env, bot_module, monkeypatch
):
    """Задачи 75, 119: Bot API принимает до 50 МБ файлов на запрос. Первому чату карточка
    уходит с клипами, влезшими в бюджет, и доливается правками до всех голов; альбом и
    следующие чаты — уже по ``file_id``, без загрузки."""
    monkeypatch.setattr(bot_module("stats_handlers"), "_UPLOAD_BUDGET_BYTES", 25)  # клип с превью — 10 байт
    await push_env.run([(111, "RUS"), (222, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 5), final=True)
    api = push_env.api

    assert _order(api) == [
        ("message", 111), ("edit", 111), ("edit", 111), ("album", 111),
        ("message", 222), ("album", 222),
    ]
    assert [len(_media(m)) for m in api.sent_rich + api.edited_texts] == [2, 5, 4, 5]
    assert _media(api.edited_texts[-1]) == [(f"goal{e}", f"fid-{e - 99}") for e in range(100, 104)] + [
        ("goal104", "104.mp4")
    ]
    assert _media(api.sent_rich[1]) == [(f"goal{e}", f"fid-{e - 99}") for e in range(100, 105)]


@pytest.mark.asyncio
async def test_country_broadcast_failed_game_does_not_stop_the_next(push_env):
    """Ночь 2026-10-13: отказ Telegram погасил всю рассылку чату. Теперь сбой одного матча
    логируется и отмечается (повтора не будет), остальные матчи уходят."""
    push_env.api.fail_next(
        SendRichMessage,
        TelegramBadRequest(method=_method(), message="Bad Request: Request Entity Too Large"),
    )
    await push_env.run(
        [(111, "RUS")],
        [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")],
        _goals(GAME_A, 2) + _goals(GAME_B, 1, first_event=200),
    )

    [sent] = push_env.api.sent_rich
    assert sent.rich_message.html.startswith(f"<p>CARD {GAME_B} ")
    assert push_env.deactivated == []
    assert push_env.marked == [(111, GAME_A), (111, GAME_B)]


@pytest.mark.asyncio
async def test_clip_uploaded_once_serves_later_runs_and_the_menu_card(push_env, bot_module, monkeypatch):
    """Задача 87: клип, загруженный рассылкой, следующий прогон шлёт по ``file_id`` без
    скачивания, и карточка из меню берёт тот же ``file_id``."""
    await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 1))
    assert push_env.downloads == _goals(GAME_A, 1)

    push_env.downloads.clear()
    await push_env.run([(222, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 1))
    stats_handlers = bot_module("stats_handlers")
    monkeypatch.setattr(stats_handlers, "game_exists", lambda game_id: True)
    monkeypatch.setattr(
        stats_handlers, "get_chat_subscriptions",
        lambda chat_id: bot_module("subscription_repo").ChatSubscriptions(False, False, set(), [], set()),
    )
    await stats_handlers.send_game_card_message(push_env.bot, 333, GAME_A)

    assert push_env.downloads == []
    by_upload, by_run, by_menu = push_env.api.sent_rich
    assert _media(by_upload) == [("goal100", "100.mp4")]
    assert _media(by_run) == _media(by_menu) == [("goal100", "fid-1")]
    assert by_menu.rich_message.html == rich_html(f"CARD {GAME_A} clips=[100]")


@pytest.mark.asyncio
async def test_rejected_cached_file_id_is_logged_and_clips_reuploaded_once(push_env, caplog):
    """Задача 87: Telegram отверг ``file_id`` из кэша (протух) — громкая ошибка в логе и одна
    повторная отправка: клипы скачиваются заново, кэш получает новые ``file_id``."""
    push_env.clips[(GAME_A, 100)] = ("stale", "<b>GOAL</b> 100")
    push_env.api.fail_next(
        SendRichMessage,
        TelegramBadRequest(method=_method(), message="Bad Request: wrong file identifier/HTTP URL specified"),
    )
    await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 2))

    assert push_env.downloads == [(GAME_A, 101), (GAME_A, 100), (GAME_A, 101)]
    [message] = push_env.api.sent_rich
    assert _media(message) == [("goal100", "100.mp4"), ("goal101", "101.mp4")], "порядок голов сохранён"
    assert push_env.clips == {
        (GAME_A, 100): ("fid-1", "<b>GOAL</b> 100"), (GAME_A, 101): ("fid-2", "<b>GOAL</b> 101"),
    }
    assert [r.levelname for r in caplog.records if "rejected cached clips" in r.getMessage()] == ["ERROR"]
    assert push_env.marked == [(111, GAME_A)]


@pytest.mark.asyncio
async def test_rejected_cached_clip_that_fails_to_download_drops_out_of_the_retry(push_env):
    """Задача 87: клип с протухшим ``file_id`` при повторе не скачался — его гол уходит
    строкой без видео, старый ``file_id`` второй раз не шлётся и в кэше не меняется."""
    push_env.clips[(GAME_A, 100)] = ("stale", "<b>GOAL</b> 100")
    push_env.missing = {(GAME_A, 100)}
    push_env.api.fail_next(
        SendRichMessage,
        TelegramBadRequest(method=_method(), message="Bad Request: wrong file identifier/HTTP URL specified"),
    )
    await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 2))

    [message] = push_env.api.sent_rich
    assert _media(message) == [("goal101", "101.mp4")]
    assert message.rich_message.html.startswith(f"<p>CARD {GAME_A} clips=[101]")
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
    «Матчей» получает о матче одно сообщение — открытую карточку с клипами всех голов,
    блоками обеих стран и строкой подписок, за ней альбомом голы игроков подписанных стран
    (гол чужой страны — только в карточке); дайджест этого матча не содержит. Повторный
    прогон ничего не дублирует."""
    push_env.goals = [(GAME_A, 100, "RUS"), (GAME_A, 101, "BLR"), (GAME_A, 102, "CAN")]
    push_env.subscribed = {
        111: {GAME_A: push_env.subs(["TOR", "BOS"], [("BLR", "BLR BLOCK"), ("RUS", "RUS BLOCK")])}
    }
    push_env.digest_chats, push_env.loaded = [111], True
    for _ in range(2):
        await push_env.job.broadcast_if_due(push_env.bot, _msk(8, 0))

    [card] = push_env.api.outgoing
    assert card.rich_message.html == rich_html(
        f"CARD {GAME_A} clips=[100, 101, 102]\n\nBLR BLOCK\n\nRUS BLOCK"
    ) + "<footer>По подписке: TOR · BOS · Беларусь · Россия<br>Настроить подписки: /subscriptions</footer>"
    assert card.reply_markup is None, "видео голов — в карточке, не кнопками"
    assert _order(push_env.api) == [("message", 111), ("album", 111)]
    [album] = push_env.api.sent(SendMediaGroup)
    assert [m.media for m in album.media] == ["fid-1", "fid-2"]
    assert push_env.downloads == _goals(GAME_A, 3)
    assert push_env.marked == [(111, GAME_A)]
    assert push_env.last_sent == {111: NIGHT}, "дайджест отмечен, но пуст — не ушёл"


@pytest.mark.asyncio
async def test_non_telegram_failure_marks_the_game_and_fails_loudly(push_env, monkeypatch, bot_module):
    """Ревью Задачи 85: сбой не из Telegram (скачивание клипа, ffmpeg) — матч всё равно
    отмечен (иначе попытка повторялась бы каждые полчаса и ещё раз в дайджесте), ошибка
    всплывает."""
    def broken(game_id, event_id):
        raise RuntimeError("ffmpeg")

    monkeypatch.setattr(bot_module("stats_handlers"), "download_goal_video", broken)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await push_env.run([(111, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 1))

    assert _order(push_env.api) == []
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
async def test_subscriber_of_two_countries_gets_one_message_with_both_blocks(push_env):
    """Задача 85: только страны — одно сообщение со свёрнутой карточкой, блоками обеих
    стран и строкой подписок (первая строка — табло, здесь — заглушка карточки), за ним
    одним альбомом голы игроков этих стран; гол чужой страны в альбом не берётся."""
    push_env.goals = [(GAME_A, 100, "RUS"), (GAME_A, 101, "CAN"), (GAME_A, 102, "BLR")]
    subs = push_env.subs([], [("BLR", "BLR BLOCK"), ("RUS", "RUS BLOCK")])
    await push_env.job.run_game_broadcast(push_env.bot, NIGHT, False, {111: {GAME_A: subs}})

    [message] = push_env.api.outgoing
    assert message.rich_message.html == rich_html(
        f"CARD {GAME_A} clips=[100, 101, 102] collapsed\n\nBLR BLOCK\n\nRUS BLOCK"
    ) + "<footer>По подписке: Беларусь · Россия<br>Настроить подписки: /subscriptions</footer>"
    [album] = push_env.api.sent(SendMediaGroup)
    assert [m.media for m in album.media] == ["fid-1", "fid-3"]


@pytest.mark.asyncio
async def test_player_subscriber_gets_collapsed_card_with_players_block_and_own_goals(push_env):
    """Задача 122: подписка на игрока и его страну — одно сообщение: свёрнутая карточка,
    «Ваши игроки» перед блоком страны, в подвале игрок после стран; в альбом — голы
    подписанного игрока и игроков страны, чужие — нет. Страна, чьи игроки в матче все подписаны
    (пустой блок), — только в подвале. С подписанной командой карточка открыта."""
    push_env.goals = [(GAME_A, 100, "USA", 7), (GAME_A, 101, "CAN", 8), (GAME_A, 102, "RUS", 9)]
    subs = push_env.subs(
        [], [("BLR", ""), ("RUS", "RUS BLOCK")], ("Michkov (PHI)",), "PLAYERS BLOCK", frozenset({7})
    )
    await push_env.job.run_game_broadcast(push_env.bot, NIGHT, False, {111: {GAME_A: subs}})

    [message] = push_env.api.outgoing
    assert message.rich_message.html == rich_html(
        f"CARD {GAME_A} clips=[100, 101, 102] collapsed\n\nPLAYERS BLOCK\n\nRUS BLOCK"
    ) + "<footer>По подписке: Беларусь · Россия · Michkov (PHI)<br>Настроить подписки: /subscriptions</footer>"
    [album] = push_env.api.sent(SendMediaGroup)
    assert [m.media for m in album.media] == ["fid-1", "fid-3"]

    team = push_env.subs(["Flyers"], [], ("Michkov (PHI)",), "PLAYERS BLOCK", frozenset({7}))
    await push_env.job.run_game_broadcast(push_env.bot, NIGHT, False, {222: {GAME_A: team}})
    assert push_env.api.sent_rich[-1].chat_id == 222
    assert " collapsed" not in push_env.api.sent_rich[-1].rich_message.html


def test_owner_gets_every_game_of_the_night_first(push_job, monkeypatch):
    """Задача 119: владелец — первым и со всеми матчами ночи (без подписки — с пустыми
    подписками): его карточка грузит клипы в кэш, остальным они уходят по ``file_id``."""
    owner = 168669418
    monkeypatch.setattr(push_job.config, "OWNER_CHAT_ID", owner)
    monkeypatch.setattr(push_job, "list_active_game_rows", lambda: [(222, 10, None, None), (owner, 20, None, None)])
    monkeypatch.setattr(push_job, "fetch_all", lambda query, params, columns: {
        "game_id": [GAME_A, GAME_B], "home_id": [10, 30], "home": ["Boston", "New York"],
        "away_id": [40, 20], "away": ["Toronto", "Montréal"], "count_rows": 2,
    })

    result = push_job.subscribed_games(NIGHT)

    assert list(result) == [owner, 222]
    assert result[owner] == {GAME_A: push_job._GameSubscriptions([], []), GAME_B: push_job._GameSubscriptions(["Montréal"], [])}
    assert result[222] == {GAME_A: push_job._GameSubscriptions(["Boston"], [])}


def test_without_owner_and_subscriptions_nothing_is_read(push_job, monkeypatch):
    monkeypatch.setattr(push_job.config, "OWNER_CHAT_ID", None)
    monkeypatch.setattr(push_job, "list_active_game_rows", lambda: [])
    monkeypatch.setattr(push_job, "fetch_all", lambda *a: pytest.fail("без подписок матчи ночи не нужны"))
    assert push_job.subscribed_games(NIGHT) == {}


@pytest.mark.asyncio
async def test_owner_game_without_subscription_is_open_card_without_reason(push_env):
    """Матч владельца без подписки — открытая карточка, в подвале только ссылка на подписки."""
    push_env.goals = [(GAME_A, 100, "CAN")]
    await push_env.job.run_game_broadcast(push_env.bot, NIGHT, False, {1: {GAME_A: push_env.subs([], [])}})

    [card] = push_env.api.outgoing
    assert card.rich_message.html == rich_html(f"CARD {GAME_A} clips=[100]") + FOOTER
    assert push_env.api.sent(SendMediaGroup) == [] and push_env.api.sent(SendVideo) == []


@pytest.mark.asyncio
async def test_game_card_resends_once_after_retry_after(push_env):
    """``throttled`` повторяет отправку карточки; без ``await`` 429 вообще не всплыла бы."""
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
    ``run(bot, now)`` — один прогон."""
    env = SimpleNamespace(days=[], previews=[], sent=set(), deactivated=[], chats=[], read_subs=0)

    def fetch(day):
        env.days.append(day)
        return SLATE

    def preview(game_id, away, home, collapsed):
        assert collapsed, "в рассылке таблица превью свёрнута"
        env.previews.append((away, home))
        return f"PREVIEW {away} @ {home}", "Сезон 2026/27"

    def rows():
        env.read_subs += 1
        return env.chats

    monkeypatch.setattr(push_job, "upcoming_preview_text", AsyncMock(return_value=""))
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
    """Решение человека (2026-10-25): с 15:30 МСК на каждом прогоне с превью (раз в полчаса)
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

    async def channel_results(bot, night, final):
        calls.append(("channel results", night, final))
        raise RuntimeError("bot is not a channel admin")

    async def channel_previews(bot, now):
        calls.append(("channel previews", now))

    async def channel_weekly(bot, now):
        calls.append(("channel weekly", now))

    started = _msk(11, 0)
    with patch.object(push_job, "subscribed_games", return_value={}), patch.object(
        push_job, "run_game_broadcast", games
    ), patch.object(push_job, "run_morning_digest_broadcast", digest), patch.object(
        push_job, "run_upcoming_broadcast", upcoming
    ), patch.object(push_job, "run_channel_results", channel_results), patch.object(
        push_job, "run_channel_previews", channel_previews
    ), patch.object(push_job, "run_channel_weekly", channel_weekly):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            await push_job.broadcast_if_due(fake_bot, started)

    assert calls[:2] == ["games", "digest"]
    assert calls[2][0] == "upcoming" and calls[2][1] > started
    # Канал (Задача 118) — после чатов, итоги ночи на крайнем сроке, затем превью; его сбой
    # не срывает превью канала.
    assert calls[3] == ("channel results", NIGHT, True)
    assert calls[4][0] == "channel previews" and calls[4][1] > started
    assert calls[5][0] == "channel weekly" and calls[5][1] > started


@pytest.mark.asyncio
async def test_night_run_without_previews_skips_only_subscriber_previews(push_job, fake_bot):
    """Ночной слот вне сетки получаса (``--no-upcoming``, Задача 120): итоги, дайджест и канал
    идут, превью подписчикам — нет: не чаще одного в полчаса."""
    calls = []

    def step(name):
        async def run(*args):
            calls.append(name)
        return run

    with patch.object(push_job, "subscribed_games", return_value={}), patch.object(
        push_job, "night_is_loaded", return_value=True
    ), patch.object(push_job, "run_game_broadcast", step("games")), patch.object(
        push_job, "run_morning_digest_broadcast", step("digest")
    ), patch.object(push_job, "run_upcoming_broadcast", step("upcoming")), patch.object(
        push_job, "run_channel_results", step("channel results")
    ), patch.object(push_job, "run_channel_previews", step("channel previews")), patch.object(
        push_job, "run_channel_weekly", step("channel weekly")
    ):
        await push_job.broadcast_if_due(fake_bot, _msk(2, 10), previews=False)

    assert calls == ["games", "digest", "channel results", "channel previews", "channel weekly"]


@pytest.mark.asyncio
async def test_main_upcoming_only_skips_the_night(push_job, bot_module, monkeypatch, set_required_bot_env):
    """Дневной слот (``--upcoming-only``): только превью — подписчикам и в канал (Задача 118),
    ночь и NHL API о ней не трогаются; сбой превью подписчикам не срывает канал."""
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", True)

    async def noop(self):
        return None

    monkeypatch.setattr(AiohttpSession, "close", noop)
    calls = []

    async def upcoming(bot, now):
        calls.append(("upcoming", bot.token))
        raise RuntimeError("NHL API")

    async def channel_previews(bot, now):
        calls.append(("channel previews", bot.token))

    async def channel_weekly(bot, now):
        calls.append(("channel weekly", bot.token))

    async def night(*args):
        raise AssertionError("ночь днём не рассылается")

    monkeypatch.setattr(push_job, "run_upcoming_broadcast", upcoming)
    monkeypatch.setattr(push_job, "run_channel_previews", channel_previews)
    monkeypatch.setattr(push_job, "run_channel_weekly", channel_weekly)
    monkeypatch.setattr(push_job, "broadcast_if_due", night)

    with pytest.raises(RuntimeError, match="NHL API"):
        await push_job.main(True)

    assert calls == [("upcoming", FAKE_TOKEN), ("channel previews", FAKE_TOKEN), ("channel weekly", FAKE_TOKEN)]


# ---------------------------------------------------------------------------
# Канал (Задачи 118, 120): превью, итоги всех матчей ночи, посты выходных
# ---------------------------------------------------------------------------

# Id канала строкой, как его читает `config` из `.env` (@username Telegram принимает так же).
CHANNEL = "-1001234567890"
CHANNEL_FOOTER_LINE = f"Вся статистика NHL — в боте @{BOT_USERNAME}"


def _team_stats(abbr, name, wins):
    """Строка ``teams_stats ts`` превью: сезон команды из 10 игр."""
    return (abbr, name, 10, wins, 10 - wins, 0, 2 * wins, 10.0 * wins, 3.0, 2.5, 20.0, 80.0, 30.0, 50.0)


@pytest.fixture
def channel_env(push_job, bot_module, monkeypatch, fake_db_router):
    """Канал ``CHANNEL`` без паузы между постами: отметки (game_id, вид) — в ``posted``;
    NHL ``score`` — ``SLATE``; загруженные матчи ночи — ``games``; БД превью и блоков стран — ``fake_db_router``: у любой пары
    команд есть сезон, ``players`` — {страна: строки ``game_player_stats``}. Превью и блоки
    стран собираются настоящими сборщиками."""
    config = bot_module("config")
    monkeypatch.setattr(config, "CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(push_job, "_CHANNEL_POST_INTERVAL_SEC", 0)
    env = SimpleNamespace(posted=set(), players={}, scores=0, games=[])

    def fetch(day):
        env.scores += 1
        return SLATE

    monkeypatch.setattr(push_job, "fetch_score", fetch)
    monkeypatch.setattr(
        push_job, "channel_posted", lambda game_ids, kind: {g for g, k in env.posted if k == kind and g in game_ids}
    )
    monkeypatch.setattr(push_job, "mark_channel_posted", lambda game_id, kind: env.posted.add((game_id, kind)))
    fake_db_router([
        ("SELECT game_id FROM games WHERE season_id", lambda params: [(game_id,) for game_id in env.games]),
        ("teams_stats ts", lambda params: [_team_stats(abbr, f"{abbr} Club", 6) for abbr in params[1]]),
        ("team_id FROM teams", []),
        ("game_player_stats", lambda params: env.players.get(params[1], [])),
        ("game_goalie_stats", []),
    ])
    return env


@pytest.mark.asyncio
async def test_channel_previews_every_upcoming_game_once(push_job, channel_env, fake_bot, api):
    """С 15:30 МСК все предстоящие матчи дня — одним постом по времени начала: у каждого
    время, команды, сравнение свёрнуто; ни подписок, ни кнопок — подвал один, в нём ссылка
    на бота. Повторный прогон ничего не дублирует."""
    await push_job.run_channel_previews(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    await push_job.run_channel_previews(fake_bot, datetime(2026, 10, 24, 13, 0, tzinfo=UTC))

    [post] = api.sent_rich
    assert post.chat_id == CHANNEL and post.reply_markup is None
    html_ = post.rich_message.html
    assert html_.startswith(
        "<p>⏰ Ночь с 24 на 25 октября: 3 матча NHL</p><p>&nbsp;</p><p>Игры начнутся с 02:00 до 05:00 МСК.<br>"
    )
    assert re.findall(r"⏰ Начало в (\d\d:\d\d) МСК<br><b>(\w+) Club @ (\w+) Club", html_) == [
        ("02:00", "BOS", "TOR"), ("02:30", "NYR", "MTL"), ("05:00", "VAN", "SEA"),
    ]
    assert html_.count("<summary>Сравнение команд</summary>") == 3
    assert html_.count("<footer>") == 1
    assert "<footer>Сезон " in html_ and html_.endswith(f"<br>{CHANNEL_FOOTER_LINE}</footer>")
    assert not re.search("модел|премиум|🔒|/subscriptions", html_)
    assert channel_env.posted == {(GAME_A, "preview"), (GAME_B, "preview"), (2026020004, "preview")}


def test_channel_preview_intro_night_window_and_daily_variant(push_job):
    """Вступление: ночь через границу месяца, один слот начала — «в», склонение числа
    матчей; вариант меняется день ото дня и повторяется через полный круг."""
    def at(hour: int, minute: int) -> datetime:
        return datetime(2026, 11, 1, hour, minute, tzinfo=push_job.MSK)

    intro = push_job.channel_preview_intro
    assert intro(date(2026, 10, 31), [at(3, 30)]) == (
        "🌙 Ночь с 31 октября на 1 ноября — 1 матч NHL\n\nНачало в 03:30 МСК.\n"
        "К каждой игре — статистика команд в этом сезоне."
    )
    assert intro(date(2026, 11, 1), [at(3, 0)] * 2 + [at(5, 0)] * 9).startswith(
        "🏒 NHL в ночь с 1 на 2 ноября: 11 матчей\n\nШайбу вбросят с 03:00 до 05:00 МСК."
    )
    assert "22 матча" in intro(date(2026, 11, 2), [at(3, 0)] * 22)
    assert "с 31 декабря на 1 января: 12 матчей" in intro(date(2026, 12, 31), [at(3, 0)] * 12)
    heads = [intro(date(2026, 11, 1) + timedelta(days=i), [at(3, 0)]).split("\n")[0] for i in range(6)]
    assert len({head[:2] for head in heads[:5]}) == 5 and heads[5][:2] == heads[0][:2]


@pytest.mark.asyncio
async def test_channel_preview_failure_is_retried_next_run(push_job, channel_env, fake_bot, api):
    """Отказ Telegram на пост превью (бот не админ канала) — прогон красный, матчи не
    отмечены; следующий прогон публикует пост целиком. Уже опубликованный матч в него не
    попадает."""
    channel_env.posted.add((GAME_A, "preview"))
    api.fail_next(SendRichMessage, TelegramBadRequest(method=_method(), message="need administrator rights"))
    with pytest.raises(TelegramBadRequest):
        await push_job.run_channel_previews(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    assert channel_env.posted == {(GAME_A, "preview")} and api.sent_rich == []

    await push_job.run_channel_previews(fake_bot, datetime(2026, 10, 24, 13, 0, tzinfo=UTC))
    [post] = api.sent_rich
    assert "BOS Club" not in post.rich_message.html
    assert post.rich_message.html.startswith("<p>⏰ Начало в 02:30 МСК<br>")  # вступление — у первого поста дня
    assert "NYR Club @ MTL Club" in post.rich_message.html and "VAN Club @ SEA Club" in post.rich_message.html
    assert channel_env.posted == {(GAME_A, "preview"), (GAME_B, "preview"), (2026020004, "preview")}


@pytest.mark.asyncio
async def test_channel_without_channel_id_is_not_touched(push_job, channel_env, bot_module, monkeypatch, fake_bot, api):
    """Пустой ``CHANNEL_ID`` — канал выключен на стенде: ни постов, ни NHL API, ни БД."""
    monkeypatch.setattr(bot_module("config"), "CHANNEL_ID", None)
    await push_job.run_channel_previews(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    await push_job.run_channel_results(fake_bot, NIGHT, True)
    await push_job.run_channel_weekly(fake_bot, datetime(2026, 11, 21, 15, 0, tzinfo=UTC))
    assert api.requests == [] and channel_env.scores == 0


def _skater(game_id, lastname, team, player_id=8470000):
    """Строка ``game_player_stats`` блока страны: нападающий с голом."""
    return (game_id, player_id, lastname, team, "L", 1, 0, 3, 1, 2, 0, 0, 0, "17:05")


@pytest.mark.asyncio
async def test_channel_results_are_every_game_of_the_night_once(push_job, push_env, channel_env, fake_bot):
    """Итог — каждый загруженный матч ночи (Задача 120): свёрнутая карточка с клипами и
    ссылкой на бота, кнопок нет. Где играли россияне или белорусы — на виду блоки обеих
    стран, за карточкой альбом их голов, гол чужой страны в альбом не берётся; матч без
    них — карточка без блоков и без альбома. Матч ждёт клипов всех голов, на крайнем сроке
    уходит с тем, что есть; повторный прогон ничего не дублирует."""
    channel_env.games = [GAME_A, GAME_B]
    channel_env.players = {
        "RUS": [_skater(GAME_A, "Овечкин", "WSH")],
        "BLR": [_skater(GAME_A, "Протас", "WSH")],
    }
    push_env.goals = [(GAME_A, 100, "RUS"), (GAME_A, 101, "CAN"), (GAME_A, 102, "BLR"), (GAME_B, 200, "CAN")]
    push_env.unpublished = {(GAME_A, 101)}
    await push_job.run_channel_results(fake_bot, NIGHT, False)
    [other] = push_env.api.sent_rich
    assert other.chat_id == CHANNEL and other.reply_markup is None
    assert other.rich_message.html == rich_html(f"CARD {GAME_B} clips=[200] collapsed") + (
        f"<footer>{CHANNEL_FOOTER_LINE}</footer>"
    )
    assert push_env.api.sent(SendMediaGroup) == [] and push_env.api.sent(SendVideo) == []

    await push_job.run_channel_results(fake_bot, NIGHT, True)
    await push_job.run_channel_results(fake_bot, NIGHT, True)

    [_other, post] = push_env.api.sent_rich
    assert post.chat_id == CHANNEL and post.reply_markup is None
    assert post.rich_message.html == rich_html(
        f"CARD {GAME_A} clips=[100, 101, 102] collapsed\n\n"
        "<b>🇷🇺 Россия</b>\n\nОвечкин (WSH)\n1+0 · +1 · 3 бр. · 2 хита · 17:05\n\n"
        "<b>🇧🇾 Беларусь</b>\n\nПротас (WSH)\n1+0 · +1 · 3 бр. · 2 хита · 17:05"
    ) + f"<footer>{CHANNEL_FOOTER_LINE}</footer>"
    [album] = push_env.api.sent(SendMediaGroup)
    assert album.chat_id == CHANNEL
    assert [(m.media, m.caption) for m in album.media] == [
        # fid-1 — клип матча B: его карточка ушла первой.
        ("fid-2", "<b>GOAL</b> 100\n\n<b>GOAL</b> 102"), ("fid-4", None),
    ]
    assert channel_env.posted == {(GAME_A, "result"), (GAME_B, "result")}


@pytest.mark.asyncio
async def test_channel_result_failure_is_marked_and_does_not_block_other_games(
    push_job, push_env, channel_env, fake_bot
):
    """Карточка — отправка и правки: сбой может случиться, когда пост уже в канале, поэтому
    итог отмечается после любой попытки и не повторяется (дубль хуже пропуска). Следующий
    матч уходит, прогон красный."""
    channel_env.games = [GAME_A, GAME_B]
    channel_env.players = {"RUS": [_skater(GAME_A, "Овечкин", "WSH"), _skater(GAME_B, "Кучеров", "TBL")]}
    push_env.api.fail_next(SendRichMessage, TelegramBadRequest(method=_method(), message="message is too long"))
    with pytest.raises(TelegramBadRequest):
        await push_job.run_channel_results(fake_bot, NIGHT, True)
    await push_job.run_channel_results(fake_bot, NIGHT, True)

    [post] = push_env.api.sent_rich
    assert post.rich_message.html.startswith(rich_html(f"CARD {GAME_B} clips=[] collapsed"))
    assert channel_env.posted == {(GAME_A, "result"), (GAME_B, "result")}


# Посты выходных (Задача 120): суббота 21.11.2026 и воскресенье 22.11.2026, 18:00 МСК = 15:00 UTC.
SATURDAY, SUNDAY = date(2026, 11, 21), date(2026, 11, 22)


def _utc_on(day, hour, minute=0):
    """Момент дня ``day`` в UTC."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


@pytest.fixture
def weekly_env(push_job, bot_module, monkeypatch, fake_db_router):
    """Канал ``CHANNEL``: отметки (день, вид) — в ``posted``; БД турнирной таблицы и рейтинга
    России — ``fake_db_router``, тексты собирают настоящие ``team_table`` и ``country_profile``."""
    monkeypatch.setattr(bot_module("config"), "CHANNEL_ID", CHANNEL)
    env = SimpleNamespace(posted=set())
    monkeypatch.setattr(push_job, "channel_weekly_posted", lambda day, kind: (day, kind) in env.posted)
    monkeypatch.setattr(push_job, "mark_channel_weekly_posted", lambda day, kind: env.posted.add((day, kind)))
    fake_db_router([
        ("FROM teams_stats ts", [("Washington Capitals", 20, 30, 75.0, 15, 5, 0, "Metropolitan", "Eastern")]),
        ("SELECT max(day)::text AS d", [("2026-11-20",)]),
        ("GROUP BY r.nationality", [("RUS", 4, 100, 40)]),
        ("FROM goalies_season_stats", []),
        ("r.position = 'D'", []),
        ("OVER ()", [("Ovechkin", "WSH", 10, 20, 18, "19:00", 30, 60, 5, 25)]),
    ])
    return env


@pytest.mark.asyncio
async def test_channel_weekly_standings_on_saturday_and_russia_on_sunday_once(push_job, weekly_env, fake_bot, api):
    """В субботу с 18:00 МСК — турнирная таблица, в воскресенье — лучшие игроки России, тем же
    текстом, что ``/standings`` и ``/RUS``, со ссылкой на бота в конце; по одному разу за день,
    до 18:00 и в будни — ничего."""
    for moment in (
        _utc_on(SATURDAY, 14, 59), _utc_on(SATURDAY, 15, 0), _utc_on(SATURDAY, 15, 30),
        _utc_on(SUNDAY, 15, 0), _utc_on(SUNDAY, 20, 0), _utc_on(SUNDAY + timedelta(days=1), 15, 0),
    ):
        await push_job.run_channel_weekly(fake_bot, moment)

    standings, russia = api.sent_rich
    assert {standings.chat_id, russia.chat_id} == {CHANNEL}
    assert standings.reply_markup is None and russia.reply_markup is None
    assert standings.rich_message.html.startswith("<p><b>Турнирная таблица NHL</b></p>")
    assert "Washington Capitals" in standings.rich_message.html
    assert "РОССИЯ В NHL" in russia.rich_message.html and "Ovechkin" in russia.rich_message.html
    # Подвал один: сезон и ссылка на бота, без команд бота — в канале они не работают.
    season = push_job.config.CURRENT_SEASON
    assert standings.rich_message.html.endswith(
        f"<footer>Сезон {season} · данные на 2026-11-20<br>{CHANNEL_FOOTER_LINE}</footer>"
    )
    assert russia.rich_message.html.endswith(f"<footer>Сезон {season}<br>{CHANNEL_FOOTER_LINE}</footer>")
    for post in (standings, russia):
        assert post.rich_message.html.count("<footer>") == 1 and "/countries" not in post.rich_message.html
    assert weekly_env.posted == {(SATURDAY, "standings"), (SUNDAY, "country")}


@pytest.mark.asyncio
async def test_channel_weekly_failure_is_retried_the_same_day(push_job, weekly_env, fake_bot, api):
    """Отказ Telegram в слот 18:00 — прогон красный, пост не отмечен; следующий прогон того же
    дня публикует его."""
    api.fail_next(SendRichMessage, TelegramBadRequest(method=_method(), message="need administrator rights"))
    with pytest.raises(TelegramBadRequest):
        await push_job.run_channel_weekly(fake_bot, _utc_on(SATURDAY, 15, 0))
    assert weekly_env.posted == set() and api.sent_rich == []

    await push_job.run_channel_weekly(fake_bot, _utc_on(SATURDAY, 15, 30))
    [post] = api.sent_rich
    assert "Турнирная таблица NHL" in post.rich_message.html
    assert weekly_env.posted == {(SATURDAY, "standings")}


@pytest.mark.asyncio
async def test_upcoming_ai_shared_and_escaped(push_job, upcoming_env, fake_bot, api, monkeypatch):
    upcoming_env.chats = [(10, 111), (20, 222)]
    generate = AsyncMock(return_value="Смотрите на <атаку> & оборону.")
    monkeypatch.setattr(push_job, "upcoming_preview_text", generate)
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    generate.assert_awaited_once()
    assert generate.call_args.args[1] == "PREVIEW BOS @ TOR\nСезон 2026/27"
    assert len(api.sent_rich) == 2
    assert all("&lt;атаку&gt; &amp;" in m.rich_message.html for m in api.sent_rich)
