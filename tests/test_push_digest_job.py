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

from unittest.mock import patch

import pytest
from telegram.error import Forbidden, RetryAfter
from telegram.ext import Application
from telegram.warnings import PTBUserWarning

# Один «настоящий» матч дня: dispatch_day_digest_messages шлёт карточку матча,
# а следом, при attach_conv_nav_on_last=False, подсказку «Ещё: …».
ONE_GAME_DAY = ("2026-01-15", [(2026020001, "<b>BOS 3 : 2 TOR</b>", [])])

# Синтаксически валидный, но заведомо несуществующий токен: никуда не ходим.
FAKE_TOKEN = "123456789:TEST-TOKEN-NOT-A-REAL-SECRET"


@pytest.fixture
def push_job(bot_module):
    """Импортированный ``push_digest_job`` (импорт модуля — уже часть проверки)."""
    return bot_module("push_digest_job")


# ---------------------------------------------------------------------------
# Утренний дайджест
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_digest_reaches_every_active_subscriber(push_job, fake_context):
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_chat_ids", return_value=[111, 222]
    ):
        await push_job.run_morning_digest_broadcast(fake_context)

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
        push_job, "list_active_morning_digest_chat_ids", return_value=[111]
    ), patch.object(
        push_job, "dispatch_day_digest_messages", side_effect=blocked
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive:
        await push_job.run_morning_digest_broadcast(fake_context)

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
        push_job, "list_active_morning_digest_chat_ids", return_value=[111]
    ), patch.object(push_job, "dispatch_day_digest_messages", side_effect=flaky):
        await push_job.run_morning_digest_broadcast(fake_context)

    assert attempts == [111, 111]
    assert [m["chat_id"] for m in fake_context.bot.sent_messages] == [111]


# ---------------------------------------------------------------------------
# Итоги по командам
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_scores_sends_first_line_of_every_yesterday_game(push_job, fake_context):
    with patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1, 2]
    ), patch.object(
        push_job,
        "game_message",
        side_effect=[("BOS 3 : 2 TOR\nподробности", {}), ("BOS 1 : 4 MTL\nещё", {})],
    ):
        await push_job.run_team_scores_broadcast(fake_context)

    [sent] = fake_context.bot.sent_messages
    assert sent["chat_id"] == 111
    assert sent["parse_mode"] == "HTML"
    assert "BOS 3 : 2 TOR\nBOS 1 : 4 MTL" in sent["text"]
    assert "подробности" not in sent["text"]


@pytest.mark.asyncio
async def test_team_scores_skips_chat_without_games_yesterday(push_job, fake_context):
    with patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6)]
    ), patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[]):
        await push_job.run_team_scores_broadcast(fake_context)

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

    with patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
    ), patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", {})):
        await push_job.run_team_scores_broadcast(fake_context)

    assert len(calls) == 2
    assert [m["chat_id"] for m in fake_context.bot.sent_messages] == [111]


@pytest.mark.asyncio
async def test_team_scores_deactivates_chat_that_blocked_the_bot(push_job, fake_context):
    async def blocked(**kwargs):
        raise Forbidden("bot was blocked by the user")

    fake_context.bot.send_message = blocked

    with patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6)]
    ), patch.object(
        push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
    ), patch.object(
        push_job, "game_message", return_value=("BOS 3 : 2 TOR", {})
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive:
        await push_job.run_team_scores_broadcast(fake_context)

    mark_inactive.assert_called_once_with(111, "team_scores", 6)


# ---------------------------------------------------------------------------
# Точка входа cron-скрипта
# ---------------------------------------------------------------------------

class _ForbiddenApplication:
    """Заглушка ``Application``: обращение к ней означает лишний выход в сеть."""

    @staticmethod
    def builder():
        raise AssertionError("Application must not be built when the job is a no-op")


# config.validate_env() смотрит на сырое окружение, а не на уже задефолченные
# config.PG_*, поэтому тесты на main() выставляют все пять обязательных
# переменных явно — не полагаясь на то, чем их снабдил make.
_REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    "PG_HOST": "localhost",
    "PG_PORT": "5432",
    "PG_USER": "nhl_bot_test",
    "PG_DATABASE": "nhl_bot_test",
}


def _set_required_env(monkeypatch, **overrides: str) -> None:
    """Выставляет все переменные, обязательные для ``config.validate_env()``,
    с точечными переопределениями через ``overrides``."""
    for name, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.asyncio
async def test_main_raises_and_never_builds_application_without_token(
    push_job, bot_module, monkeypatch
):
    """``main()`` падает на ``validate_env()`` до сборки ``Application``, если
    токена нет — раньше это было мягкое ``sys.exit(1)`` внутри самого скрипта."""
    _set_required_env(monkeypatch, TELEGRAM_BOT_TOKEN="")
    monkeypatch.setattr(bot_module("config"), "TOKEN", "")
    monkeypatch.setattr(push_job, "Application", _ForbiddenApplication)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        await push_job.main()


@pytest.mark.asyncio
async def test_main_does_nothing_while_enable_push_digest_is_off(
    push_job, bot_module, monkeypatch
):
    config = bot_module("config")
    _set_required_env(monkeypatch)
    monkeypatch.setattr(config, "TOKEN", "123456789:TEST-TOKEN-NOT-A-REAL-SECRET")
    monkeypatch.setattr(config, "ENABLE_PUSH_DIGEST", False)
    monkeypatch.setattr(push_job, "Application", _ForbiddenApplication)

    await push_job.main()


@pytest.mark.asyncio
async def test_main_runs_both_broadcasts_on_a_context_bound_to_its_application(
    push_job, bot_module, monkeypatch
):
    """Обвязка ``main()``: builder → ``Application`` → ``CallbackContext``.

    ``Application`` собирается настоящий, подменены только ``initialize`` /
    ``shutdown``: единственный сетевой шаг PTB — ``get_me()`` внутри
    ``initialize()`` — так не выполняется, и тест не ходит в api.telegram.org.
    """
    config = bot_module("config")
    _set_required_env(monkeypatch)
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

    async def record(context):
        steps.append("broadcast")
        contexts.append(context)

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
