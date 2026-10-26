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
from datetime import date, datetime, timedelta, timezone
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
# а следом, без ``dialog``, подсказку «Ещё: …».
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


# ---------------------------------------------------------------------------
# Утренний дайджест
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_digest_reaches_every_active_subscriber(push_job, fake_bot, api):
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, 222)
    ), patch.object(push_job, "mark_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT)

    assert [(type(r), r.chat_id) for r in api.outgoing] == [
        (SendRichMessage, 111), (SendMessage, 111), (SendRichMessage, 222), (SendMessage, 222),
    ]
    cards, hints = api.sent_rich, api.sent_messages
    assert cards[0].rich_message.html == rich_html("<b>BOS 3 : 2 TOR</b>")
    assert hints[0].text.startswith("Ещё:")
    assert hints[0].text.endswith("Настроить подписки: /subscriptions")


@pytest.mark.asyncio
async def test_morning_digest_deactivates_chat_that_blocked_the_bot(push_job, fake_bot):
    """``TelegramForbiddenError`` ловится, только если корутина рассылки действительно ждётся."""

    async def blocked(*args, **kwargs):
        raise _forbidden()

    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(
        push_job, "dispatch_day_digest_messages", side_effect=blocked
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive, patch.object(push_job, "mark_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT)

    mark_inactive.assert_called_once_with(111, "morning_digest", None)


@pytest.mark.asyncio
async def test_morning_digest_retries_once_after_retry_after(push_job, fake_bot, api):
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
        push_job, "mark_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT)

    assert attempts == [111, 111]
    assert [m.chat_id for m in api.sent_messages] == [111]


# ---------------------------------------------------------------------------
# Итоги по командам
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_scores_sends_full_card_of_every_game_of_the_night(push_job, fake_bot, api):
    """Отзыв 2026-10-09: подписчику — вся карточка матча с кнопками видео голов,
    а не одна строка счёта."""
    goal = {"label": "▶ 1:0 Pastrnak 6:33", "game_id": 1, "event_id": 55}
    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1, 2]
    ), patch.object(
        push_job,
        "game_message",
        side_effect=[("BOS 3 : 2 TOR\nподробности", [goal]), ("BOS 1 : 4 MTL\nещё", [])],
    ):
        await push_job.run_team_scores_broadcast(fake_bot, NIGHT)

    assert api.sent_messages == [], "карточки уходят rich-сообщениями"
    first, second = api.sent_rich
    assert [m.chat_id for m in (first, second)] == [111, 111]
    assert first.rich_message.html == rich_html(
        "<b>Матч вашей команды (2026-10-06)</b>\n\nBOS 3 : 2 TOR\nподробности"
        "\n\nНастроить подписки: /subscriptions"
    )
    [[button]] = first.reply_markup.inline_keyboard
    assert (button.text, button.callback_data) == ("▶ 1:0 Pastrnak 6:33", "gv:1:55")
    assert second.rich_message.html == rich_html(
        "<b>Матч вашей команды (2026-10-06)</b>\n\nBOS 1 : 4 MTL\nещё"
        "\n\nНастроить подписки: /subscriptions"
    )
    assert second.reply_markup is None


@pytest.mark.asyncio
async def test_team_scores_skips_chat_without_games_that_night(push_job, fake_bot, api):
    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[]):
        await push_job.run_team_scores_broadcast(fake_bot, NIGHT)

    assert api.outgoing == []


@pytest.mark.asyncio
async def test_team_scores_resends_once_after_retry_after(push_job, fake_bot, api):
    """``_throttled`` повторяет отправку; без ``await`` 429 вообще не всплывёт."""
    api.fail_next(SendRichMessage, _retry_after())

    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
    ), patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])):
        await push_job.run_team_scores_broadcast(fake_bot, NIGHT)

    # Первая попытка упала на 429 и не записана; без повтора сообщения не было бы вовсе.
    assert [m.chat_id for m in api.sent_rich] == [111]


@pytest.mark.asyncio
async def test_team_scores_deactivates_chat_that_blocked_the_bot(push_job, fake_bot, api):
    api.fail_next(SendRichMessage, _forbidden())

    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
    ), patch.object(
        push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive:
        await push_job.run_team_scores_broadcast(fake_bot, NIGHT)

    mark_inactive.assert_called_once_with(111, "team_scores", 6)


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

    async def record(bot, night, *final):
        steps.append("broadcast")
        bots.append(bot)
        nights.append(night)

    monkeypatch.setattr(push_job, "night_is_loaded", lambda night: True)
    monkeypatch.setattr(push_job, "run_morning_digest_broadcast", record)
    monkeypatch.setattr(push_job, "run_team_scores_broadcast", record)
    monkeypatch.setattr(push_job, "run_country_broadcast", record)

    async def record_upcoming(bot, now):
        steps.append("upcoming")
        bots.append(bot)

    monkeypatch.setattr(push_job, "run_upcoming_broadcast", record_upcoming)

    await push_job.main(False)

    # Рассылки идут строго до закрытия сессии — иначе HTTP-клиент бота уже погашен,
    # а после них сессия обязана закрыться.
    assert steps == ["broadcast", "broadcast", "broadcast", "upcoming", "close"]

    after = push_job.night_of(datetime.now(timezone.utc))
    assert len(nights) == 3 and len(set(nights)) == 1 and nights[0] in (before, after)
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


async def _run_night(push_job, fake_bot, api, loaded_at):
    """Прогоняет ``broadcast_if_due`` после каждого слота ночи для дайджеста и команды
    (страна — по матчу, своим прогоном: ``_run_country_night``).

    Чат 111 — дайджест, 222 — команда; отметка ``last_sent_night`` живёт в словаре
    так же, как в ``bot_subscriptions``. *loaded_at* — с какого слота ночь загружена
    целиком (``None`` — не загружается до утра).

    Возвращает ``{chat_id: [моменты МСК, когда чату ушла рассылка]}``.
    """
    last_sent = {111: None, 222: None}
    sent_at = {chat_id: [] for chat_id in last_sent}

    def mark(chat_id, kind, team_id, night):
        last_sent[chat_id] = night

    for slot in _night_slots():
        before = len(api.outgoing)
        loaded = loaded_at is not None and slot >= loaded_at
        with patch.object(
            push_job, "list_active_morning_digest_rows", return_value=[(111, last_sent[111])]
        ), patch.object(
            push_job, "list_active_team_scores_rows", return_value=[(222, 6, last_sent[222])]
        ), patch.object(
            push_job, "list_active_country_rows", return_value=[]
        ), patch.object(push_job, "mark_night_sent", side_effect=mark), patch.object(
            push_job, "day_digest", return_value=ONE_GAME_DAY
        ), patch.object(
            push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
        ), patch.object(
            push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])
        ), patch.object(push_job, "night_is_loaded", return_value=loaded), patch.object(
            push_job, "upcoming_games", return_value=[]
        ):
            await push_job.broadcast_if_due(fake_bot, slot)
        for message in api.outgoing[before:]:
            if not body(message).startswith("Ещё:"):
                sent_at[message.chat_id].append(slot)
    return sent_at


def test_night_of_covers_the_whole_sync_window(push_job):
    """Все прогоны ночи — с 21:00 МСК до 11:00 МСК следующего дня — одна ночь."""
    assert {push_job.night_of(slot) for slot in _night_slots()} == {NIGHT}


@pytest.mark.asyncio
async def test_unloaded_night_holds_digest_and_teams_but_not_country(push_job, fake_bot):
    """Ночь не загружена, срок не наступил — дайджест и команды ждут; страна идёт по
    готовым матчам на каждом прогоне (Задача 76), крайний срок ещё не наступил."""
    with patch.object(push_job, "night_is_loaded", return_value=False), patch.object(
        push_job, "run_morning_digest_broadcast"
    ) as digest, patch.object(push_job, "run_team_scores_broadcast") as team, patch.object(
        push_job, "run_country_broadcast"
    ) as country, patch.object(push_job, "run_upcoming_broadcast") as upcoming:
        await push_job.broadcast_if_due(fake_bot, _msk(10, 30))

    digest.assert_not_called()
    team.assert_not_called()
    country.assert_awaited_once_with(fake_bot, NIGHT, False)
    upcoming.assert_awaited_once()
    assert upcoming.await_args.args[0] is fake_bot


@pytest.mark.asyncio
async def test_digest_and_teams_go_out_once_at_the_first_run_after_the_night_is_loaded(
    push_job, fake_bot, api
):
    """Ночь загрузилась к 08:30 — дайджест и команды уходят в 08:30 и не повторяются до утра."""
    sent_at = await _run_night(push_job, fake_bot, api, loaded_at=_msk(8, 30))
    assert sent_at == {111: [_msk(8, 30)], 222: [_msk(8, 30)]}


@pytest.mark.asyncio
async def test_unloaded_night_goes_out_at_the_deadline(push_job, fake_bot, api):
    """Ночь так и не загрузилась целиком (перенос, сбой) — всё уходит в 11:00 с тем, что есть."""
    sent_at = await _run_night(push_job, fake_bot, api, loaded_at=None)
    assert sent_at == {111: [_msk(11, 0)], 222: [_msk(11, 0)]}


@pytest.mark.asyncio
async def test_deadline_does_not_ask_nhl_api(push_job, fake_bot):
    """После крайнего срока готовность не нужна: сбой NHL API не срывает отправку."""
    with patch.object(push_job, "night_is_loaded", side_effect=AssertionError("API")), patch.object(
        push_job, "run_morning_digest_broadcast"
    ) as digest, patch.object(push_job, "run_team_scores_broadcast") as team, patch.object(
        push_job, "run_country_broadcast"
    ) as country, patch.object(push_job, "run_upcoming_broadcast") as upcoming:
        await push_job.broadcast_if_due(fake_bot, _msk(11, 0))

    digest.assert_awaited_once_with(fake_bot, NIGHT)
    team.assert_awaited_once_with(fake_bot, NIGHT)
    country.assert_awaited_once_with(fake_bot, NIGHT, True)
    upcoming.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_tick_after_sending_sends_nothing(push_job, fake_bot, api):
    """Ночь уже отмечена отправленной — повторный тик (рестарт, ручной once) молчит."""
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, last_sent=NIGHT)
    ), patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "mark_night_sent"
    ) as mark:
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT)

    assert api.outgoing == []
    mark.assert_not_called()


@pytest.mark.asyncio
async def test_night_without_games_is_not_sent(push_job, fake_bot, api):
    """Ночь без матчей в БД не рассылается — раньше уходил дайджест прошлого игрового дня."""
    no_games = ("2026-10-06", [(0, "За 2026-10-06 завершенных матчей не найдено.", [])])
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "day_digest", return_value=no_games) as digest, patch.object(
        push_job, "mark_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_bot, NIGHT)

    digest.assert_called_once_with(NIGHT)
    assert api.outgoing == []


@pytest.mark.asyncio
async def test_team_scores_are_not_repeated(push_job, fake_bot, api):
    """``team_scores`` не повторяется: ночь с отметкой ``last_sent_night`` молчит."""
    patches = (
        patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]),
        patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])),
    )
    with patches[0], patches[1], patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(push_job, "mark_night_sent") as mark:
        await push_job.run_team_scores_broadcast(fake_bot, NIGHT)
    mark.assert_called_once_with(111, "team_scores", 6, NIGHT)
    assert [m.rich_message.html for m in api.sent_rich] == [
        rich_html("<b>Матч вашей команды (2026-10-06)</b>\n\nBOS 3 : 2 TOR\n\nНастроить подписки: /subscriptions")
    ]

    with patches[0], patches[1], patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, NIGHT)]
    ), patch.object(push_job, "mark_night_sent"):
        await push_job.run_team_scores_broadcast(fake_bot, NIGHT)
    assert len(api.sent_rich) == 1


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
# Подписка на страну (Задача 61): сообщение, альбомы видео, кэш file_id
# ---------------------------------------------------------------------------

_SEND_KIND = {SendRichMessage: "message", SendMessage: "message", SendMediaGroup: "album", SendVideo: "video"}


def _order(api):
    """Порядок отправок рассылки: ("message" | "album" | "video", chat_id)."""
    return [(_SEND_KIND[type(r)], r.chat_id) for r in api.requests if type(r) in _SEND_KIND]


GAME_A, GAME_B = 2026020001, 2026020002
FOOTER = "\n\nНастроить подписки: /subscriptions"


def _goals(game_id, n, first_event=100):
    return [(game_id, first_event + i) for i in range(n)]


def _sub(chat_id):
    """id строки подписки чата в фикстуре — чтобы отметки читались по чату."""
    return chat_id * 10


@pytest.fixture
def country_env(push_job, monkeypatch, tmp_path, fake_bot, api):
    """Рассылка по стране с подменой БД, NHL и скачивания: бот (``fake_bot``, запросы —
    в ``api``), журнал скачиваний и проверок клипов, отметки «матч ушёл» (``sent`` — в БД,
    ``marked`` — по порядку), погашенные подписки и ``run(chats, games, goals, final)``;
    ``chats`` — (chat_id, страна), ``games`` — (game_id, блок страны) матчей ночи,
    ``goals`` — (game_id, event_id) голов страны; ``unpublished`` — голы, клипа которых
    NHL ещё не выложила, ``missing`` — голы, клип которых не скачался."""
    env = SimpleNamespace(
        bot=fake_bot, api=api, downloads=[], clip_checks=[], marked=[], sent=set(),
        deactivated=[], missing=set(), unpublished=set(),
    )

    def published(game_id, event_id):
        env.clip_checks.append((game_id, event_id))
        return (game_id, event_id) not in env.unpublished

    def mark(subscription_id, game_id):
        env.marked.append((subscription_id, game_id))
        env.sent.add((subscription_id, game_id))

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

    def run(chats, games, goals, final=False):
        monkeypatch.setattr(push_job, "list_active_country_rows", lambda: [
            (_sub(chat), chat, country) for chat, country in chats
        ])
        monkeypatch.setattr(push_job, "country_night_blocks", lambda *a: games)
        monkeypatch.setattr(push_job, "country_night_goals", lambda *a: goals)
        monkeypatch.setattr(push_job, "game_score_line", lambda game_id: f"SCORE {game_id}")
        monkeypatch.setattr(push_job, "goal_clip_published", published)
        monkeypatch.setattr(push_job, "download_goal_video", download)
        monkeypatch.setattr(
            push_job, "sent_games",
            lambda game_ids: {s for s in env.sent if s[1] in game_ids},
        )
        monkeypatch.setattr(push_job, "mark_game_sent", mark)
        monkeypatch.setattr(
            push_job, "mark_subscription_inactive_by_chat_kind_team",
            lambda *a: env.deactivated.append(a),
        )
        return push_job.run_country_broadcast(env.bot, NIGHT, final)

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
async def test_country_game_is_short_text_with_card_button_then_goal_album(country_env, fake_bot):
    """Задача 76: матч — короткий текст (счёт и блок страны, без полной карточки) с кнопкой
    «Карточка матча», за ним голы страны альбомом с общей подписью — описаниями всех его
    голов; без голов — только текст. Второму чату клипы — по ``file_id``, без повторной
    загрузки, с той же подписью."""
    country_env.missing = {(GAME_A, 101)}
    await country_env.run(
        [(111, "RUS"), (222, "RUS")],
        [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")],
        _goals(GAME_A, 3),
    )
    api = country_env.api

    assert _order(api) == [
        ("message", 111), ("album", 111), ("message", 111),
        ("message", 222), ("album", 222), ("message", 222),
    ]
    assert api.sent_messages == [], "тексты матчей уходят rich-сообщениями"
    messages = api.sent_rich
    assert [m.rich_message.html for m in messages[:2]] == [
        rich_html(f"SCORE {GAME_A}\n\nBLOCK A" + FOOTER),
        rich_html(f"SCORE {GAME_B}\n\nBLOCK B" + FOOTER),
    ]
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
    assert sorted(country_env.downloads) == _goals(GAME_A, 3)
    assert list(country_env.tmp_path.iterdir()) == [], "временные файлы удалены"
    assert country_env.marked == [
        (_sub(111), GAME_A), (_sub(111), GAME_B), (_sub(222), GAME_A), (_sub(222), GAME_B),
    ]


@pytest.mark.asyncio
async def test_country_game_waits_for_clips_until_the_deadline(country_env):
    """Задача 76: матч уже в БД, но NHL ещё не выложила клип гола страны — матч ждёт
    следующего прогона (матч без голов уходит сразу); к крайнему сроку уходит с тем, что
    есть. NHL спрашивается один раз на гол за прогон, сколько бы ни было подписчиков."""
    games = [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")]
    goals = _goals(GAME_A, 2)
    country_env.unpublished = {(GAME_A, 101)}
    await country_env.run([(111, "RUS"), (222, "RUS")], games, goals)

    assert country_env.marked == [(_sub(111), GAME_B), (_sub(222), GAME_B)]
    assert country_env.clip_checks == _goals(GAME_A, 2)
    assert country_env.api.sent(SendMediaGroup) == []

    # Следующий прогон: клип выложен — матч уходит; ушедший GAME_B не повторяется.
    country_env.unpublished = set()
    await country_env.run([(111, "RUS"), (222, "RUS")], games, goals)
    assert country_env.marked[2:] == [(_sub(111), GAME_A), (_sub(222), GAME_A)]
    assert [len(r.media) for r in country_env.api.sent(SendMediaGroup)] == [2, 2]

    # Крайний срок: клипа так и нет — матч уходит без проверки NHL.
    country_env.unpublished = {(GAME_B, 200)}
    country_env.clip_checks.clear()
    await country_env.run([(333, "RUS")], [(GAME_B, "BLOCK B")], [(GAME_B, 200)], final=True)
    assert country_env.marked[4:] == [(_sub(333), GAME_B)]
    assert country_env.clip_checks == []


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
    country_env, push_job, monkeypatch
):
    """Задача 75: альбом из 10 клипов по 8–19 МБ Telegram отверг (лимит 50 МБ на запрос).
    Клипы матча делятся по бюджету загрузки, одиночный остаток — ``send_video``. Второму
    чату клипы идут по ``file_id`` — бюджет не тратят."""
    monkeypatch.setattr(push_job, "_UPLOAD_BUDGET_BYTES", 25)  # клип с превью — 10 байт
    await country_env.run(
        [(111, "RUS"), (222, "RUS")], [(GAME_A, "BLOCK")], _goals(GAME_A, 5), final=True
    )
    api = country_env.api

    assert _order(api) == [
        ("message", 111), ("album", 111), ("album", 111), ("video", 111),
        ("message", 222), ("album", 222),
    ]
    assert [len(r.media) for r in api.sent(SendMediaGroup)] == [2, 2, 5]
    [video] = api.sent(SendVideo)
    assert video.caption == "<b>GOAL</b> 104", "одиночный клип — со своей подписью"
    assert video.parse_mode == "HTML"


@pytest.mark.asyncio
async def test_country_broadcast_failed_game_does_not_stop_the_next(country_env):
    """Ночь 2026-10-13: отказ Telegram на альбоме погасил всю рассылку чату. Теперь сбой
    одного матча логируется и отмечается (повтора не будет), остальные матчи уходят."""
    country_env.api.fail_next(
        SendMediaGroup,
        TelegramBadRequest(method=_method(), message="Bad Request: Request Entity Too Large"),
    )
    await country_env.run(
        [(111, "RUS")],
        [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")],
        _goals(GAME_A, 2) + _goals(GAME_B, 1, first_event=200),
    )

    assert _order(country_env.api) == [("message", 111), ("message", 111), ("video", 111)]
    assert country_env.deactivated == []
    assert country_env.marked == [(_sub(111), GAME_A), (_sub(111), GAME_B)]


@pytest.mark.asyncio
async def test_country_broadcast_night_without_players_sends_nothing(country_env):
    await country_env.run([(111, "RUS")], [], [])

    assert (_order(country_env.api), country_env.downloads, country_env.marked) == ([], [], [])


@pytest.mark.asyncio
async def test_country_broadcast_deactivates_only_the_blocked_subscription(country_env):
    """Заблокировавший бота чат гасится один раз и больше матчей не получает."""
    # Первый запрос рассылки — сообщение чату 111: он «заблокировал» бота.
    country_env.api.fail_next(SendRichMessage, _forbidden())
    await country_env.run(
        [(111, "RUS"), (222, "RUS")], [(GAME_A, "BLOCK A"), (GAME_B, "BLOCK B")], []
    )

    assert country_env.deactivated == [(111, "country_players", None, "RUS")]
    assert _order(country_env.api) == [("message", 222), ("message", 222)]
    assert country_env.marked == [(_sub(222), GAME_A), (_sub(222), GAME_B)]


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
    ходили в NHL, ``previews`` — матчи, для которых собиралось превью, ``sent`` — отметки
    (подписка, матч), ``deactivated`` — погашенные подписки, ``chats`` — (id подписки,
    chat_id) активных подписчиков, ``read_subs`` — сколько раз их читали из БД;
    ``run(bot, now)`` — один прогон."""
    env = SimpleNamespace(days=[], previews=[], sent=set(), deactivated=[], chats=[], read_subs=0)

    def fetch(day):
        env.days.append(day)
        return SLATE

    def preview(game_id, away, home):
        env.previews.append(game_id)
        return f"PREVIEW {away} @ {home}"

    def rows():
        env.read_subs += 1
        return env.chats

    monkeypatch.setattr(push_job, "fetch_score", fetch)
    monkeypatch.setattr(push_job, "matchup_season_preview", preview)
    monkeypatch.setattr(push_job, "list_active_upcoming_rows", rows)
    monkeypatch.setattr(
        push_job, "sent_games", lambda game_ids: {s for s in env.sent if s[1] in game_ids}
    )
    monkeypatch.setattr(push_job, "mark_game_sent", lambda sub, game: env.sent.add((sub, game)))
    monkeypatch.setattr(
        push_job, "mark_subscription_inactive_by_chat_kind_team",
        lambda *a: env.deactivated.append(a),
    )
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
    матчи кончились, прогон ничего не шлёт. Шапка — время начала по МСК; превью
    собирается раз на матч, а не на подписчика."""
    upcoming_env.chats = [(10, 111), (20, 222)]
    by_slot = []
    for hour, minute in ((12, 30), (13, 0), (13, 30), (14, 0)):
        before = len(api.sent_rich)
        await upcoming_env.run(fake_bot, datetime(2026, 10, 24, hour, minute, tzinfo=UTC))
        by_slot.append([(m.chat_id, m.rich_message.html) for m in api.sent_rich[before:]])

    a = rich_html("⏰ <b>Начало в 02:00 МСК</b>\nPREVIEW BOS @ TOR" + FOOTER)
    b = rich_html("⏰ <b>Начало в 02:30 МСК</b>\nPREVIEW NYR @ MTL" + FOOTER)
    c = rich_html("⏰ <b>Начало в 05:00 МСК</b>\nPREVIEW VAN @ SEA" + FOOTER)
    assert by_slot == [[(111, a), (222, a)], [(111, b), (222, b)], [(111, c), (222, c)], []]
    assert api.sent_messages == [], "превью уходят rich-сообщениями"
    assert upcoming_env.previews == [GAME_A, GAME_B, 2026020004]


@pytest.mark.asyncio
async def test_upcoming_next_game_is_per_subscriber(push_job, upcoming_env, fake_bot, api):
    """Следующий матч — свой у каждого подписчика: кому первый уже ушёл, получает второй;
    уже начавшиеся пропускаются."""
    upcoming_env.chats = [(10, 111), (20, 222)]
    upcoming_env.sent = {(20, GAME_A)}
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    assert [(m.chat_id, m.rich_message.html) for m in api.sent_rich] == [
        (111, rich_html("⏰ <b>Начало в 02:00 МСК</b>\nPREVIEW BOS @ TOR" + FOOTER)),
        (222, rich_html("⏰ <b>Начало в 02:30 МСК</b>\nPREVIEW NYR @ MTL" + FOOTER)),
    ]

    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 23, 40, tzinfo=UTC))
    assert [(m.chat_id, m.rich_message.html) for m in api.sent_rich[2:]] == [
        (111, rich_html("⏰ <b>Начало в 05:00 МСК</b>\nPREVIEW VAN @ SEA" + FOOTER)),
        (222, rich_html("⏰ <b>Начало в 05:00 МСК</b>\nPREVIEW VAN @ SEA" + FOOTER)),
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
    """Заблокировавший бота чат гасится (``upcoming_games``), матч ему отмечается;
    следующий подписчик превью получает."""
    upcoming_env.chats = [(10, 111), (20, 222)]
    api.fail_next(SendRichMessage, _forbidden())
    await upcoming_env.run(fake_bot, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))

    assert [m.chat_id for m in api.sent_rich] == [222]
    assert upcoming_env.deactivated == [(111, "upcoming_games")]
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
        rich_html("⏰ <b>Начало в 02:30 МСК</b>\nPREVIEW NYR @ MTL" + FOOTER)
    ]
    assert upcoming_env.deactivated == []


@pytest.mark.asyncio
async def test_country_failure_does_not_stop_upcoming_and_still_fails_the_run(
    push_job, fake_bot
):
    """Страна и превью независимы (ревью Задачи 78): постоянный сбой страны (ffmpeg,
    Replay API) не отключает превью на всю ночь, но прогон всё равно падает — ошибка
    пробрасывается после обеих рассылок. Превью — с моментом своего старта, а не прогона."""
    calls = []

    async def country(*args):
        calls.append("country")
        raise RuntimeError("ffmpeg")

    async def upcoming(bot, now):
        calls.append(("upcoming", now))

    started = _msk(1, 0)
    with patch.object(push_job, "night_is_loaded", return_value=False), patch.object(
        push_job, "run_country_broadcast", country
    ), patch.object(push_job, "run_upcoming_broadcast", upcoming):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            await push_job.broadcast_if_due(fake_bot, started)

    assert calls[0] == "country"
    assert calls[1][0] == "upcoming" and calls[1][1] > started


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
