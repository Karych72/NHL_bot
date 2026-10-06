"""Тесты cron-скрипта рассылки ``telegram_bot/push_digest_job.py``.

Модуль годами был сломан незамеченным (сначала ``ImportError`` на
``telegram.error.Forbidden``, затем несделанные ``await`` после перехода на
PTB 21.x) ровно потому, что ни один тест его не импортировал. Здесь он
именно **вызывается**: рассылка прогоняется через настоящий
``dispatch_day_digest_messages`` на фальшивом ``CallbackContext`` из conftest,
поэтому забытый ``await`` виден как «сообщение не отправлено», а не как тихо
брошенная корутина. Границы — БД (``subscription_repo``, ``bot_messages``)
и Telegram (``FakeBot``) — подменяются, сеть не используется.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest
from telegram.error import Forbidden, RetryAfter
from telegram.ext import Application
from telegram.warnings import PTBUserWarning

# Один «настоящий» матч дня: dispatch_day_digest_messages шлёт карточку матча,
# а следом, при attach_conv_nav_on_last=False, подсказку «Ещё: …».
ONE_GAME_DAY = ("2026-01-15", [(2026020001, "<b>BOS 3 : 2 TOR</b>", [])])

# Ночь игровой даты 2026-10-06: прогоны sync — с 18:00 UTC 06.10 до 08:00 UTC 07.10.
NIGHT = date(2026, 10, 6)
# Момент после крайнего срока ночи (11:00 МСК 07.10): рассылка точно пора.
AFTER_DEADLINE = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)

# Синтаксически валидный, но заведомо несуществующий токен: никуда не ходим.
FAKE_TOKEN = "123456789:TEST-TOKEN-NOT-A-REAL-SECRET"


@pytest.fixture
def push_job(bot_module):
    """Импортированный ``push_digest_job`` (импорт модуля — уже часть проверки)."""
    return bot_module("push_digest_job")


def _rows(*chat_ids, send_time=time(11, 0), last_sent=None):
    """Строки ``list_active_morning_digest_rows`` для чатов с одинаковой подпиской."""
    return [(chat_id, send_time, last_sent) for chat_id in chat_ids]


# ---------------------------------------------------------------------------
# Утренний дайджест
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_digest_reaches_every_active_subscriber(push_job, fake_context):
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, 222)
    ), patch.object(push_job, "mark_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    sent = fake_context.bot.sent_messages
    assert [m["chat_id"] for m in sent] == [111, 111, 222, 222]
    assert sent[0]["text"] == "<b>BOS 3 : 2 TOR</b>"
    assert sent[1]["text"].startswith("Ещё:")


@pytest.mark.asyncio
async def test_morning_digest_deactivates_chat_that_blocked_the_bot(push_job, fake_context):
    """``Forbidden`` ловится, только если корутина рассылки действительно ждётся."""

    async def blocked(*args, **kwargs):
        raise Forbidden("bot was blocked by the user")

    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(
        push_job, "dispatch_day_digest_messages", side_effect=blocked
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive, patch.object(push_job, "mark_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    mark_inactive.assert_called_once_with(111, "morning_digest", None)


@pytest.mark.asyncio
async def test_morning_digest_retries_once_after_retry_after(push_job, fake_context):
    """429 означает «не доставлено» — рассылка обязана повторить попытку."""
    attempts = []

    async def flaky(context, chat_id, *args, **kwargs):
        attempts.append(chat_id)
        if len(attempts) == 1:
            raise RetryAfter(0)
        await context.bot.send_message(chat_id=chat_id, text="ok")

    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "dispatch_day_digest_messages", side_effect=flaky), patch.object(
        push_job, "mark_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    assert attempts == [111, 111]
    assert [m["chat_id"] for m in fake_context.bot.sent_messages] == [111]


# ---------------------------------------------------------------------------
# Итоги по командам
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_scores_sends_first_line_of_every_game_of_the_night(push_job, fake_context):
    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1, 2]
    ), patch.object(
        push_job,
        "game_message",
        side_effect=[("BOS 3 : 2 TOR\nподробности", {}), ("BOS 1 : 4 MTL\nещё", {})],
    ):
        await push_job.run_team_scores_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    [sent] = fake_context.bot.sent_messages
    assert sent["chat_id"] == 111
    assert sent["parse_mode"] == "HTML"
    assert "BOS 3 : 2 TOR\nBOS 1 : 4 MTL" in sent["text"]
    assert "подробности" not in sent["text"]


@pytest.mark.asyncio
async def test_team_scores_skips_chat_without_games_that_night(push_job, fake_context):
    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[]):
        await push_job.run_team_scores_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    assert fake_context.bot.sent_messages == []


@pytest.mark.asyncio
async def test_team_scores_resends_once_after_retry_after(push_job, fake_context):
    """``_send_throttled`` повторяет отправку; без ``await`` 429 вообще не всплывёт."""
    calls = []
    real_send = fake_context.bot.send_message

    async def flaky_send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RetryAfter(0)
        return await real_send(**kwargs)

    fake_context.bot.send_message = flaky_send

    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
    ), patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", {})):
        await push_job.run_team_scores_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    assert len(calls) == 2
    assert [m["chat_id"] for m in fake_context.bot.sent_messages] == [111]


@pytest.mark.asyncio
async def test_team_scores_deactivates_chat_that_blocked_the_bot(push_job, fake_context):
    async def blocked(**kwargs):
        raise Forbidden("bot was blocked by the user")

    fake_context.bot.send_message = blocked

    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
    ), patch.object(
        push_job, "game_message", return_value=("BOS 3 : 2 TOR", {})
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive:
        await push_job.run_team_scores_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    mark_inactive.assert_called_once_with(111, "team_scores", 6)


# ---------------------------------------------------------------------------
# Точка входа cron-скрипта
# ---------------------------------------------------------------------------

class _ForbiddenApplication:
    """Заглушка ``Application``: обращение к ней означает лишний выход в сеть."""

    @staticmethod
    def builder():
        raise AssertionError("Application must not be built when the job is a no-op")


@pytest.mark.asyncio
async def test_main_raises_and_never_builds_application_without_token(
    push_job, monkeypatch, set_required_bot_env
):
    """``main()`` падает на ``validate_env()`` до сборки ``Application``, если
    токена нет — раньше это было мягкое ``sys.exit(1)`` внутри самого скрипта."""
    set_required_bot_env(TELEGRAM_BOT_TOKEN="")
    monkeypatch.setattr(push_job, "Application", _ForbiddenApplication)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        await push_job.main()


@pytest.mark.asyncio
async def test_main_does_nothing_while_enable_push_digest_is_off(
    push_job, bot_module, monkeypatch, set_required_bot_env
):
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", False)
    monkeypatch.setattr(push_job, "Application", _ForbiddenApplication)

    await push_job.main()


@pytest.mark.asyncio
async def test_main_runs_both_broadcasts_on_a_context_bound_to_its_application(
    push_job, bot_module, monkeypatch, set_required_bot_env
):
    """Обвязка ``main()``: builder → ``Application`` → ``CallbackContext``.

    ``Application`` собирается настоящий, подменены только ``initialize`` /
    ``shutdown``: единственный сетевой шаг PTB — ``get_me()`` внутри
    ``initialize()`` — так не выполняется, и тест не ходит в api.telegram.org.
    """
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", True)

    steps = []

    async def fake_initialize(self):
        steps.append("initialize")

    async def fake_shutdown(self):
        steps.append("shutdown")

    monkeypatch.setattr(Application, "initialize", fake_initialize)
    monkeypatch.setattr(Application, "shutdown", fake_shutdown)

    contexts = []

    async def record(context, now, night, night_loaded):
        steps.append("broadcast")
        contexts.append(context)
        assert night == push_job.night_of(now)
        assert night_loaded is True

    monkeypatch.setattr(push_job, "night_is_loaded", lambda night: True)
    monkeypatch.setattr(push_job, "run_morning_digest_broadcast", record)
    monkeypatch.setattr(push_job, "run_team_scores_broadcast", record)

    await push_job.main()

    # Рассылки идут строго между initialize и shutdown — иначе HTTP-клиент бота
    # либо не поднят, либо уже погашен.
    assert steps == ["initialize", "broadcast", "broadcast", "shutdown"]

    morning_context, team_context = contexts
    application = morning_context.application
    assert team_context.application is application
    assert morning_context.bot is application.bot
    assert application.bot.token == FAKE_TOKEN
    # Скрипт ничего не принимает и ничего не планирует.
    assert application.updater is None
    with pytest.warns(PTBUserWarning, match="No `JobQueue` set up"):
        assert application.job_queue is None


# ---------------------------------------------------------------------------
# Время доставки (Задача 60): более позднее из «время подписчика» и «ночь загружена»
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


async def _run_night(push_job, fake_context, subscriptions, loaded_at):
    """Прогоняет рассылку дайджеста после каждого слота ночи.

    *subscriptions* — ``{chat_id: digest_time}``; отметка ``last_sent_night``
    живёт в словаре так же, как в ``bot_subscriptions``. *loaded_at* — с какого
    слота ночь загружена целиком (``None`` — не загружается до утра).

    Возвращает ``{chat_id: [моменты МСК, когда чату ушёл дайджест]}``.
    """
    last_sent = {chat_id: None for chat_id in subscriptions}
    sent_at = {chat_id: [] for chat_id in subscriptions}

    def mark(chat_id, kind, team_id, night):
        assert (kind, team_id) == ("morning_digest", None)
        last_sent[chat_id] = night

    for slot in _night_slots():
        rows = [(c, t, last_sent[c]) for c, t in subscriptions.items()]
        before = len(fake_context.bot.sent_messages)
        with patch.object(push_job, "list_active_morning_digest_rows", return_value=rows), patch.object(
            push_job, "mark_night_sent", side_effect=mark
        ), patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY):
            loaded = loaded_at is not None and slot >= loaded_at
            await push_job.run_morning_digest_broadcast(fake_context, slot, NIGHT, loaded)
        for message in fake_context.bot.sent_messages[before:]:
            if not message["text"].startswith("Ещё:"):
                sent_at[message["chat_id"]].append(slot)
    return sent_at


def test_night_of_covers_the_whole_sync_window(push_job):
    """Все прогоны ночи — с 21:00 МСК до 11:00 МСК следующего дня — одна ночь."""
    assert {push_job.night_of(slot) for slot in _night_slots()} == {NIGHT}


@pytest.mark.asyncio
async def test_time_before_night_end_waits_for_the_last_game(push_job, fake_context):
    """07:00 выбрано, ночь загрузилась к 08:30 — дайджест в 08:30, один раз."""
    sent_at = await _run_night(push_job, fake_context, {111: time(7, 0)}, loaded_at=_msk(8, 30))
    assert sent_at == {111: [_msk(8, 30)]}


@pytest.mark.asyncio
async def test_time_after_night_end_is_kept_exactly(push_job, fake_context):
    """Ночь загрузилась к 06:00, выбрано 09:30 — дайджест ровно в 09:30, один раз."""
    sent_at = await _run_night(push_job, fake_context, {111: time(9, 30)}, loaded_at=_msk(6, 0))
    assert sent_at == {111: [_msk(9, 30)]}


@pytest.mark.asyncio
async def test_unloaded_night_goes_out_at_the_deadline(push_job, fake_context):
    """Ночь так и не загрузилась целиком (перенос, сбой) — дайджест в 11:00 с тем, что есть."""
    sent_at = await _run_night(push_job, fake_context, {111: time(6, 0)}, loaded_at=None)
    assert sent_at == {111: [_msk(11, 0)]}


@pytest.mark.asyncio
async def test_repeated_tick_after_sending_sends_nothing(push_job, fake_context):
    """Ночь уже отмечена отправленной — повторный тик (рестарт, ручной once) молчит."""
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, last_sent=NIGHT)
    ), patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "mark_night_sent"
    ) as mark:
        await push_job.run_morning_digest_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    assert fake_context.bot.sent_messages == []
    mark.assert_not_called()


@pytest.mark.asyncio
async def test_night_without_games_is_not_sent(push_job, fake_context):
    """Ночь без матчей в БД не рассылается — раньше уходил дайджест прошлого игрового дня."""
    no_games = ("2026-10-06", [(0, "За 2026-10-06 завершенных матчей не найдено.", [])])
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111)
    ), patch.object(push_job, "day_digest", return_value=no_games) as digest, patch.object(
        push_job, "mark_night_sent"
    ):
        await push_job.run_morning_digest_broadcast(fake_context, AFTER_DEADLINE, NIGHT, True)

    digest.assert_called_once_with(NIGHT)
    assert fake_context.bot.sent_messages == []


@pytest.mark.asyncio
async def test_team_scores_wait_for_the_night_and_are_not_repeated(push_job, fake_context):
    """У ``team_scores`` своего времени нет: ушёл, как только ночь загружена, и не повторяется."""
    patches = (
        patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]),
        patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", {})),
    )
    with patches[0], patches[1], patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(push_job, "mark_night_sent") as mark:
        await push_job.run_team_scores_broadcast(fake_context, _msk(6, 0), NIGHT, False)
        assert fake_context.bot.sent_messages == []
        await push_job.run_team_scores_broadcast(fake_context, _msk(6, 30), NIGHT, True)
    mark.assert_called_once_with(111, "team_scores", 6, NIGHT)
    assert [m["text"] for m in fake_context.bot.sent_messages] == [
        "<b>Ваши матчи (2026-10-06)</b>\n\nBOS 3 : 2 TOR"
    ]

    with patches[0], patches[1], patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, NIGHT)]
    ), patch.object(push_job, "mark_night_sent"):
        await push_job.run_team_scores_broadcast(fake_context, _msk(7, 0), NIGHT, True)
    assert len(fake_context.bot.sent_messages) == 1


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
