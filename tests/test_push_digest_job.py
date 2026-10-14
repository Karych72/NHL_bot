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

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application
from telegram.warnings import PTBUserWarning

# Один «настоящий» матч дня: dispatch_day_digest_messages шлёт карточку матча,
# а следом, при attach_conv_nav_on_last=False, подсказку «Ещё: …».
ONE_GAME_DAY = ("2026-01-15", [(2026020001, "<b>BOS 3 : 2 TOR</b>", [])])

# Ночь игровой даты 2026-10-06: прогоны sync — с 18:00 UTC 06.10 до 08:00 UTC 07.10.
NIGHT = date(2026, 10, 6)

# Синтаксически валидный, но заведомо несуществующий токен: никуда не ходим.
FAKE_TOKEN = "123456789:TEST-TOKEN-NOT-A-REAL-SECRET"


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
async def test_morning_digest_reaches_every_active_subscriber(push_job, fake_context):
    with patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, 222)
    ), patch.object(push_job, "mark_night_sent"):
        await push_job.run_morning_digest_broadcast(fake_context, NIGHT)

    sent = fake_context.bot.sent_messages
    assert [m["chat_id"] for m in sent] == [111, 111, 222, 222]
    assert sent[0]["text"] == "<b>BOS 3 : 2 TOR</b>"
    assert sent[1]["text"].startswith("Ещё:")
    assert sent[1]["text"].endswith("Настроить подписки: /subscriptions")


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
        await push_job.run_morning_digest_broadcast(fake_context, NIGHT)

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
        await push_job.run_morning_digest_broadcast(fake_context, NIGHT)

    assert attempts == [111, 111]
    assert [m["chat_id"] for m in fake_context.bot.sent_messages] == [111]


# ---------------------------------------------------------------------------
# Итоги по командам
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_scores_sends_full_card_of_every_game_of_the_night(push_job, fake_context):
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
        await push_job.run_team_scores_broadcast(fake_context, NIGHT)

    first, second = fake_context.bot.sent_messages
    assert [m["chat_id"] for m in (first, second)] == [111, 111]
    assert first["parse_mode"] == "HTML"
    assert first["text"] == (
        "<b>Матч вашей команды (2026-10-06)</b>\n\nBOS 3 : 2 TOR\nподробности"
        "\n\nНастроить подписки: /subscriptions"
    )
    [[button]] = first["reply_markup"].inline_keyboard
    assert (button.text, button.callback_data) == ("▶ 1:0 Pastrnak 6:33", "gv:1:55")
    assert "BOS 1 : 4 MTL\nещё" in second["text"]
    assert second["reply_markup"] is None


@pytest.mark.asyncio
async def test_team_scores_skips_chat_without_games_that_night(push_job, fake_context):
    with patch.object(push_job, "mark_night_sent"), patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[]):
        await push_job.run_team_scores_broadcast(fake_context, NIGHT)

    assert fake_context.bot.sent_messages == []


@pytest.mark.asyncio
async def test_team_scores_resends_once_after_retry_after(push_job, fake_context):
    """``_throttled`` повторяет отправку; без ``await`` 429 вообще не всплывёт."""
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
    ), patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])):
        await push_job.run_team_scores_broadcast(fake_context, NIGHT)

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
        push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])
    ), patch.object(
        push_job, "mark_subscription_inactive_by_chat_kind_team"
    ) as mark_inactive:
        await push_job.run_team_scores_broadcast(fake_context, NIGHT)

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
async def test_main_runs_all_broadcasts_on_a_context_bound_to_its_application(
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
    nights = []
    before = push_job.night_of(datetime.now(timezone.utc))

    async def record(context, night):
        steps.append("broadcast")
        contexts.append(context)
        nights.append(night)

    monkeypatch.setattr(push_job, "night_is_loaded", lambda night: True)
    monkeypatch.setattr(push_job, "run_morning_digest_broadcast", record)
    monkeypatch.setattr(push_job, "run_team_scores_broadcast", record)
    monkeypatch.setattr(push_job, "run_country_broadcast", record)

    await push_job.main()

    # Рассылки идут строго между initialize и shutdown — иначе HTTP-клиент бота
    # либо не поднят, либо уже погашен.
    assert steps == ["initialize", "broadcast", "broadcast", "broadcast", "shutdown"]

    after = push_job.night_of(datetime.now(timezone.utc))
    assert len(nights) == 3 and len(set(nights)) == 1 and nights[0] in (before, after)
    morning_context, team_context, country_context = contexts
    application = morning_context.application
    assert team_context.application is application
    assert country_context.application is application
    assert morning_context.bot is application.bot
    assert application.bot.token == FAKE_TOKEN
    # Скрипт ничего не принимает и ничего не планирует.
    assert application.updater is None
    with pytest.warns(PTBUserWarning, match="No `JobQueue` set up"):
        assert application.job_queue is None


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


async def _run_night(push_job, fake_context, loaded_at):
    """Прогоняет ``broadcast_if_due`` после каждого слота ночи для трёх видов подписки.

    Чат 111 — дайджест, 222 — команда, 333 — страна; отметка ``last_sent_night``
    живёт в словаре так же, как в ``bot_subscriptions``. *loaded_at* — с какого
    слота ночь загружена целиком (``None`` — не загружается до утра).

    Возвращает ``{chat_id: [моменты МСК, когда чату ушла рассылка]}``.
    """
    last_sent = {111: None, 222: None, 333: None}
    sent_at = {chat_id: [] for chat_id in last_sent}

    def mark(chat_id, kind, team_id, night, country=None):
        last_sent[chat_id] = night

    for slot in _night_slots():
        before = len(fake_context.bot.sent_messages)
        loaded = loaded_at is not None and slot >= loaded_at
        with patch.object(
            push_job, "list_active_morning_digest_rows", return_value=[(111, last_sent[111])]
        ), patch.object(
            push_job, "list_active_team_scores_rows", return_value=[(222, 6, last_sent[222])]
        ), patch.object(
            push_job, "list_active_country_rows", return_value=[(333, "RUS", last_sent[333])]
        ), patch.object(push_job, "mark_night_sent", side_effect=mark), patch.object(
            push_job, "day_digest", return_value=ONE_GAME_DAY
        ), patch.object(
            push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]
        ), patch.object(
            push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])
        ), patch.object(
            push_job, "country_night_games", return_value=[(1, "RUS TEXT")]
        ), patch.object(
            push_job, "country_night_goals", return_value=[]
        ), patch.object(push_job, "night_is_loaded", return_value=loaded):
            await push_job.broadcast_if_due(fake_context, slot)
        for message in fake_context.bot.sent_messages[before:]:
            if not message["text"].startswith("Ещё:"):
                sent_at[message["chat_id"]].append(slot)
    return sent_at


def test_night_of_covers_the_whole_sync_window(push_job):
    """Все прогоны ночи — с 21:00 МСК до 11:00 МСК следующего дня — одна ночь."""
    assert {push_job.night_of(slot) for slot in _night_slots()} == {NIGHT}


@pytest.mark.asyncio
async def test_nothing_goes_out_while_the_night_is_not_loaded(push_job, fake_context):
    """Ночь не загружена, срок не наступил — ни один вид подписки не шлётся."""
    with patch.object(push_job, "night_is_loaded", return_value=False), patch.object(
        push_job, "run_morning_digest_broadcast"
    ) as digest, patch.object(push_job, "run_team_scores_broadcast") as team, patch.object(
        push_job, "run_country_broadcast"
    ) as country:
        await push_job.broadcast_if_due(fake_context, _msk(10, 30))

    digest.assert_not_called()
    team.assert_not_called()
    country.assert_not_called()


@pytest.mark.asyncio
async def test_all_kinds_go_out_once_at_the_first_run_after_the_night_is_loaded(
    push_job, fake_context
):
    """Ночь загрузилась к 08:30 — все три вида уходят в 08:30 и не повторяются до утра."""
    sent_at = await _run_night(push_job, fake_context, loaded_at=_msk(8, 30))
    assert sent_at == {111: [_msk(8, 30)], 222: [_msk(8, 30)], 333: [_msk(8, 30)]}


@pytest.mark.asyncio
async def test_unloaded_night_goes_out_at_the_deadline(push_job, fake_context):
    """Ночь так и не загрузилась целиком (перенос, сбой) — всё уходит в 11:00 с тем, что есть."""
    sent_at = await _run_night(push_job, fake_context, loaded_at=None)
    assert sent_at == {111: [_msk(11, 0)], 222: [_msk(11, 0)], 333: [_msk(11, 0)]}


@pytest.mark.asyncio
async def test_deadline_does_not_ask_nhl_api(push_job, fake_context):
    """После крайнего срока готовность не нужна: сбой NHL API не срывает отправку."""
    with patch.object(push_job, "night_is_loaded", side_effect=AssertionError("API")), patch.object(
        push_job, "run_morning_digest_broadcast"
    ) as digest, patch.object(push_job, "run_team_scores_broadcast") as team, patch.object(
        push_job, "run_country_broadcast"
    ) as country:
        await push_job.broadcast_if_due(fake_context, _msk(11, 0))

    digest.assert_awaited_once_with(fake_context, NIGHT)
    team.assert_awaited_once_with(fake_context, NIGHT)
    country.assert_awaited_once_with(fake_context, NIGHT)


@pytest.mark.asyncio
async def test_repeated_tick_after_sending_sends_nothing(push_job, fake_context):
    """Ночь уже отмечена отправленной — повторный тик (рестарт, ручной once) молчит."""
    with patch.object(
        push_job, "list_active_morning_digest_rows", return_value=_rows(111, last_sent=NIGHT)
    ), patch.object(push_job, "day_digest", return_value=ONE_GAME_DAY), patch.object(
        push_job, "mark_night_sent"
    ) as mark:
        await push_job.run_morning_digest_broadcast(fake_context, NIGHT)

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
        await push_job.run_morning_digest_broadcast(fake_context, NIGHT)

    digest.assert_called_once_with(NIGHT)
    assert fake_context.bot.sent_messages == []


@pytest.mark.asyncio
async def test_team_scores_are_not_repeated(push_job, fake_context):
    """``team_scores`` не повторяется: ночь с отметкой ``last_sent_night`` молчит."""
    patches = (
        patch.object(push_job, "_game_ids_for_team_on_calendar_day", return_value=[1]),
        patch.object(push_job, "game_message", return_value=("BOS 3 : 2 TOR", [])),
    )
    with patches[0], patches[1], patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, None)]
    ), patch.object(push_job, "mark_night_sent") as mark:
        await push_job.run_team_scores_broadcast(fake_context, NIGHT)
    mark.assert_called_once_with(111, "team_scores", 6, NIGHT)
    assert [m["text"] for m in fake_context.bot.sent_messages] == [
        "<b>Матч вашей команды (2026-10-06)</b>\n\nBOS 3 : 2 TOR\n\nНастроить подписки: /subscriptions"
    ]

    with patches[0], patches[1], patch.object(
        push_job, "list_active_team_scores_rows", return_value=[(111, 6, NIGHT)]
    ), patch.object(push_job, "mark_night_sent"):
        await push_job.run_team_scores_broadcast(fake_context, NIGHT)
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


# ---------------------------------------------------------------------------
# Подписка на страну (Задача 61): сообщение, альбомы видео, кэш file_id
# ---------------------------------------------------------------------------

class CountryBot:
    """Bot, запоминающий сообщения, альбомы и одиночные видео и их порядок (``order``);
    каждому загруженному файлу выдаёт ``file_id``, как Telegram."""

    def __init__(self) -> None:
        self.messages: list = []
        self.albums: list = []
        self.videos: list = []
        self.order: list = []
        self.uploads = 0

    def _answer(self, media) -> SimpleNamespace:
        if isinstance(media, str):
            return SimpleNamespace(video=SimpleNamespace(file_id=media))
        self.uploads += 1
        return SimpleNamespace(video=SimpleNamespace(file_id=f"fid-{self.uploads}"))

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        self.order.append(("message", kwargs["chat_id"]))

    async def send_media_group(self, chat_id, media):
        self.albums.append((chat_id, list(media)))
        self.order.append(("album", chat_id))
        return tuple(self._answer(m.media) for m in media)

    async def send_video(self, chat_id, video, **kwargs):
        self.videos.append((chat_id, video, kwargs))
        self.order.append(("video", chat_id))
        return self._answer(video)


GAME_A, GAME_B = 2026020001, 2026020002
FOOTER = "\n\nНастроить подписки: /subscriptions"


def _goals(game_id, n, first_event=100):
    return [(game_id, first_event + i) for i in range(n)]


@pytest.fixture
def country_env(push_job, monkeypatch, tmp_path):
    """Рассылка по стране с подменой БД и скачивания: бот, журнал скачиваний,
    отметки, погашенные подписки и ``run(rows, games, goals)``; ``games`` —
    (game_id, текст) матчей ночи, ``goals`` — (game_id, event_id) голов страны."""
    env = SimpleNamespace(bot=CountryBot(), downloads=[], marked=[], deactivated=[], missing=set())
    env.context = SimpleNamespace(bot=env.bot)

    def download(game_id, event_id):
        env.downloads.append((game_id, event_id))
        if (game_id, event_id) in env.missing:
            return None
        video, thumb = tmp_path / f"{event_id}.mp4", tmp_path / f"{event_id}.jpg"
        video.write_bytes(b"video")
        thumb.write_bytes(b"thumb")
        return SimpleNamespace(
            path=str(video), width=1280, height=720, duration=9, thumb_path=str(thumb)
        )

    def run(rows, games, goals):
        monkeypatch.setattr(push_job, "list_active_country_rows", lambda: [
            (chat, country, None) for chat, country in rows
        ])
        monkeypatch.setattr(push_job, "country_night_games", lambda *a: games)
        monkeypatch.setattr(push_job, "country_night_goals", lambda *a: goals)
        monkeypatch.setattr(push_job, "download_goal_video", download)
        monkeypatch.setattr(
            push_job, "mark_night_sent", lambda *a, **kw: env.marked.append((a, kw))
        )
        monkeypatch.setattr(
            push_job, "mark_subscription_inactive_by_chat_kind_team",
            lambda *a: env.deactivated.append(a),
        )
        return push_job.run_country_broadcast(env.context, NIGHT)

    env.run = run
    env.tmp_path = tmp_path
    return env


@pytest.mark.asyncio
async def test_country_broadcast_sends_one_message_per_game_and_reuses_file_ids(country_env):
    """Задача 75: матч — одно сообщение. Голы матча — альбомом, подпись у первого клипа —
    карточка с игроками страны; матч без голов страны — просто текстом."""
    country_env.missing = {(GAME_A, 101)}
    await country_env.run(
        [(111, "RUS"), (222, "RUS")],
        [(GAME_A, "CARD A"), (GAME_B, "CARD B")],
        _goals(GAME_A, 3),
    )
    bot = country_env.bot

    assert bot.order == [("album", 111), ("message", 111), ("album", 222), ("message", 222)]
    # Гол без клипа пропущен: в альбоме два видео, подпись только у первого.
    first = bot.albums[0][1]
    assert [(m.caption, m.parse_mode) for m in first] == [
        ("CARD A" + FOOTER, "HTML"), (None, "HTML"),
    ]
    assert [m["text"] for m in bot.messages] == ["CARD B" + FOOTER] * 2
    assert {m["parse_mode"] for m in bot.messages} == {"HTML"}
    assert not any(isinstance(m.media, str) for m in first), "первому чату — загрузка"
    # PTB пишет в альбом media/thumbnail только у файлов с attach_uri (attach://…):
    # без него поля выпадают из запроса и Telegram альбом не принимает.
    uris = [f.attach_uri for m in first for f in (m.media, m.thumbnail)]
    assert all(uris) and len(set(uris)) == 4, "у каждого видео и превью свой attach://"
    # Второму чату — file_id, без повторной загрузки; каждый клип скачан один раз.
    assert [m.media for m in bot.albums[1][1]] == ["fid-1", "fid-2"]
    assert bot.albums[1][1][0].caption == "CARD A" + FOOTER
    assert sorted(country_env.downloads) == _goals(GAME_A, 3)
    assert list(country_env.tmp_path.iterdir()) == [], "временные файлы удалены"
    assert country_env.marked == [
        ((111, "country_players", None, NIGHT), {"country": "RUS"}),
        ((222, "country_players", None, NIGHT), {"country": "RUS"}),
    ]


@pytest.mark.asyncio
async def test_country_game_over_upload_budget_goes_out_in_several_albums(
    country_env, push_job, monkeypatch
):
    """Задача 75: альбом из 10 клипов по 8–19 МБ Telegram отверг (лимит 50 МБ на запрос).
    Клипы матча делятся по бюджету загрузки, подпись — у первого альбома, одиночный
    остаток — ``send_video``. Второму чату клипы идут по ``file_id`` — бюджет не тратят."""
    monkeypatch.setattr(push_job, "_UPLOAD_BUDGET_BYTES", 25)  # клип с превью — 10 байт
    await country_env.run([(111, "RUS"), (222, "RUS")], [(GAME_A, "CARD")], _goals(GAME_A, 5))
    bot = country_env.bot

    assert bot.order == [("album", 111), ("album", 111), ("video", 111), ("album", 222)]
    assert [len(media) for _, media in bot.albums] == [2, 2, 5]
    captions = [m.caption for _, media in bot.albums[:2] for m in media]
    assert captions == ["CARD" + FOOTER, None, None, None]
    assert bot.videos[0][2]["caption"] is None
    assert bot.albums[2][1][0].caption == "CARD" + FOOTER


@pytest.mark.asyncio
async def test_country_game_text_over_caption_limit_goes_before_the_album(country_env):
    """Подпись длиннее 1024 видимых символов Telegram не примет: текст уходит отдельным
    сообщением, альбом — без подписи."""
    long_text = "<b>" + "x" * 1100 + "</b>"
    await country_env.run([(111, "RUS")], [(GAME_A, long_text)], _goals(GAME_A, 2))
    bot = country_env.bot

    assert bot.order == [("message", 111), ("album", 111)]
    assert bot.messages[0]["text"] == long_text + FOOTER
    assert [m.caption for m in bot.albums[0][1]] == [None, None]


def test_caption_length_counts_visible_utf16_characters(push_job):
    """Разметка не считается, сущности — по символу, эмодзи — два UTF-16-символа."""
    assert push_job._caption_length("<b>&lt;</b> <pre>ab</pre>🏒") == 1 + 1 + 2 + 2


def test_albums_respect_album_size_and_upload_budget(push_job, monkeypatch):
    """Альбом — не больше 10 клипов и не больше бюджета загружаемых байт;
    клип по ``file_id`` бюджета не тратит, клип тяжелее бюджета уходит один."""
    monkeypatch.setattr(push_job, "_UPLOAD_BUDGET_BYTES", 100)

    def clip(n, size):
        return ((GAME_A, n), b"v" * size, {"thumbnail": b""})

    cached = [((GAME_A, n), f"fid-{n}", {}) for n in range(12)]
    assert [len(a) for a in push_job._albums(cached)] == [10, 2]
    heavy = [clip(0, 60), clip(1, 40), clip(2, 1), clip(3, 150), clip(4, 10)]
    assert [[c[0][1] for c in a] for a in push_job._albums(heavy)] == [[0, 1], [2], [3], [4]]


@pytest.mark.asyncio
async def test_country_broadcast_failed_game_does_not_stop_the_next(country_env):
    """Ночь 2026-10-13: отказ Telegram на альбоме погасил всю рассылку чату. Теперь сбой
    одного матча логируется, остальные матчи уходят."""
    async def rejected(chat_id, media):
        raise BadRequest("Request Entity Too Large")

    country_env.bot.send_media_group = rejected
    await country_env.run(
        [(111, "RUS")],
        [(GAME_A, "CARD A"), (GAME_B, "CARD B")],
        _goals(GAME_A, 2) + _goals(GAME_B, 1, first_event=200),
    )
    bot = country_env.bot

    assert bot.order == [("video", 111)]
    assert bot.videos[0][2]["caption"] == "CARD B" + FOOTER
    assert country_env.deactivated == []
    assert country_env.marked == [((111, "country_players", None, NIGHT), {"country": "RUS"})]


@pytest.mark.asyncio
async def test_country_broadcast_night_without_players_sends_nothing_but_marks_night(country_env):
    await country_env.run([(111, "RUS")], [], [])

    bot = country_env.bot
    assert (bot.order, country_env.downloads) == ([], [])
    assert country_env.marked == [((111, "country_players", None, NIGHT), {"country": "RUS"})]


@pytest.mark.asyncio
async def test_country_broadcast_deactivates_only_the_blocked_subscription(country_env):
    """Заблокировавший бота чат гасится один раз и больше матчей не получает."""
    send_message = country_env.bot.send_message

    async def blocked(**kwargs):
        if kwargs["chat_id"] == 111:
            raise Forbidden("blocked")
        await send_message(**kwargs)

    country_env.bot.send_message = blocked
    await country_env.run(
        [(111, "RUS"), (222, "RUS")], [(GAME_A, "CARD A"), (GAME_B, "CARD B")], []
    )

    assert country_env.deactivated == [(111, "country_players", None, "RUS")]
    assert country_env.bot.order == [("message", 222), ("message", 222)]
    assert [a[0][0] for a in country_env.marked] == [111, 222]
