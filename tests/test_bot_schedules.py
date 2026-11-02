"""Расписания экранов (Задача 107): расчёт следующей отправки и подпись в `/subscriptions`.

`schedule_repo.next_run_at` — чистая функция на `zoneinfo`, проверяется без БД: частоты,
«последний день месяца», переходы на летнее и зимнее время, граница «ровно сейчас». Тикер,
лимит и удаление — на настоящей таблице в `tests/test_db_nhl.py` (`TestBotSchedulesSql`).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from tests._telegram_fakes import body

UTC = timezone.utc


@pytest.fixture
def repo(bot_module):
    return bot_module("schedule_repo")


@pytest.mark.parametrize(
    ("freq", "weekday", "month_day", "after", "expected"),
    [
        # Каждый день: 10:00 МСК = 07:00 UTC; сегодняшний слот ещё впереди.
        ("day", None, None, datetime(2026, 11, 2, 5, 0, tzinfo=UTC), datetime(2026, 11, 2, 7, 0, tzinfo=UTC)),
        # Ровно в момент слота — следующий слот, а не текущий.
        ("day", None, None, datetime(2026, 11, 2, 7, 0, tzinfo=UTC), datetime(2026, 11, 3, 7, 0, tzinfo=UTC)),
        # По пятницам (5) от понедельника 2 ноября — 6 ноября.
        ("week", 5, None, datetime(2026, 11, 2, 12, 0, tzinfo=UTC), datetime(2026, 11, 6, 7, 0, tzinfo=UTC)),
        # 15-го числа: в ноябре уже прошло — 15 декабря.
        ("month", None, 15, datetime(2026, 11, 20, 0, 0, tzinfo=UTC), datetime(2026, 12, 15, 7, 0, tzinfo=UTC)),
        # Последний день: февраль 2027 — 28-е, ноябрь — 30-е.
        ("month", None, 0, datetime(2027, 2, 1, 0, 0, tzinfo=UTC), datetime(2027, 2, 28, 7, 0, tzinfo=UTC)),
        ("month", None, 0, datetime(2026, 11, 2, 0, 0, tzinfo=UTC), datetime(2026, 11, 30, 7, 0, tzinfo=UTC)),
    ],
)
def test_next_run_in_moscow(repo, freq, weekday, month_day, after, expected):
    got = repo.next_run_at(freq, weekday, month_day, time(10, 0), "Europe/Moscow", after)
    assert got == expected
    assert got.tzinfo == UTC


def test_last_day_in_leap_february(repo):
    after = datetime(2028, 2, 1, tzinfo=UTC)
    assert repo.next_run_at("month", None, 0, time(10, 0), "Europe/Moscow", after) == datetime(
        2028, 2, 29, 7, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("local", "after", "expected"),
    [
        # 8 марта 2026 в Нью-Йорке 02:00–03:00 нет: 02:30 сдвигается на 03:30 EDT (07:30 UTC).
        (time(2, 30), datetime(2026, 3, 8, 0, 0, tzinfo=UTC), datetime(2026, 3, 8, 7, 30, tzinfo=UTC)),
        # 1 ноября 2026 01:30 бывает дважды: первое вхождение — EDT (05:30 UTC), не EST (06:30).
        (time(1, 30), datetime(2026, 11, 1, 0, 0, tzinfo=UTC), datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
        # После первого 01:30 второе вхождение не считается слотом — следующий день.
        (time(1, 30), datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 2, 6, 30, tzinfo=UTC)),
        # Обычное время по обе стороны перехода: 10:00 EST (15:00 UTC) и 10:00 EDT (14:00 UTC).
        (time(10, 0), datetime(2026, 3, 7, 16, 0, tzinfo=UTC), datetime(2026, 3, 8, 14, 0, tzinfo=UTC)),
        (time(10, 0), datetime(2026, 10, 31, 15, 0, tzinfo=UTC), datetime(2026, 11, 1, 15, 0, tzinfo=UTC)),
    ],
)
def test_daylight_saving_in_new_york(repo, local, after, expected):
    assert repo.next_run_at("day", None, None, local, "America/New_York", after) == expected


@pytest.mark.parametrize(("freq", "weekday", "month_day"), [("week", None, None), ("month", None, 31), ("year", None, None)])
def test_inconsistent_frequency_fails_loudly(repo, freq, weekday, month_day):
    with pytest.raises(ValueError, match="Нет дня отправки"):
        repo.next_run_at(freq, weekday, month_day, time(10, 0), "Europe/Moscow", datetime(2026, 11, 1, tzinfo=UTC))


def test_unknown_timezone_fails_on_create(repo):
    with pytest.raises(ValueError, match="Mars/Olympus"):
        repo.create(1, "leaders", {"category": "goals"}, "day", None, None, time(10, 0),
                    "Mars/Olympus", datetime(2026, 11, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    ("screen", "params", "freq", "weekday", "month_day", "tz", "expected"),
    [
        ("leaders", {"category": "goals"}, "week", 5, None, "Europe/Moscow",
         "Лидеры · Лучшие снайперы — по пятницам 10:00 (МСК)"),
        ("open_command", {"command": "standings"}, "day", None, None, "Europe/Moscow",
         "/standings — ежедневно 10:00 (МСК)"),
        ("team_profile", {"team": "WSH", "all_players": True}, "month", None, 0, "America/New_York",
         "/WSH_FULL — в последний день месяца 10:00 (America/New_York)"),
        ("country_players", {"country": "RUS"}, "month", None, 1, "Europe/Moscow",
         "Игроки · 🇷🇺 Россия — 1-го числа 10:00 (МСК)"),
        ("advanced_metric", {"metric": "sat"}, "week", 1, None, "Europe/Moscow",
         "Лидеры · {} — по понедельникам 10:00 (МСК)"),
    ],
)
def test_schedule_label(bot_module, screen, params, freq, weekday, month_day, tz, expected):
    bot = bot_module("bot")
    schedule = bot_module("schedule_repo").Schedule(
        1, 100, screen, params, freq, weekday, month_day, time(10, 0), tz, True
    )
    title = bot_module("leaderboard_specs").PLAYER_STAT_TITLES[("players_advanced_stats", "sat_pct")]
    assert bot._schedule_label(schedule) == expected.format(title)


# ---------------------------------------------------------------------------
# Карточка-черновик (Задача 108): кнопки `sch:…` через `Dispatcher.feed_update`. БД — фейк:
# сохранение и журнал проверяются по запросам, дошедшим до неё.
# ---------------------------------------------------------------------------

MOSCOW = "Europe/Moscow"


@pytest.fixture
def card(bot_module, fake_db_router, monkeypatch, fake_bot, api, make_update):
    """Фабрика: у чата `schedules` — строки активных расписаний, `premium` — премиум.
    Возвращает (bot, курсор БД, press): `press(data)` — нажатие, ответ — (тело, {подпись: data})
    последней правки карточки."""

    def _make(schedules=(), premium=False):
        bot = bot_module("bot")
        cursor = fake_db_router([
            ("FROM premium_access", [(datetime(2099, 1, 1, tzinfo=UTC),)] if premium else []),
            ("INSERT INTO bot_schedules", [(42,)]),
            ("FROM bot_schedules", list(schedules)),
            ("INSERT INTO bot_actions", []),
        ])
        for module in (bot, bot_module("schedule_repo")):
            monkeypatch.setattr(module, "get_connection", bot_module("database").get_connection)
        dispatcher = bot.build_dispatcher()

        async def press(data):
            bot_module("throttle")._hits.clear()
            await dispatcher.feed_update(fake_bot, make_update(data=data))
            edit = api.edited_texts[-1]
            markup = edit.reply_markup.inline_keyboard if edit.reply_markup else []
            return body(edit), {b.text: b.callback_data for row in markup for b in row}

        return bot, cursor, press

    return _make


def _draft(bot, freq="week", day=5, slot=20, tz=MOSCOW):
    """Лидеры по голам; по умолчанию — по пятницам 10:00 МСК."""
    return bot.Draft("leaders", {"category": "goals"}, freq, day, slot, tz)


@pytest.mark.asyncio
async def test_frequency_redraws_the_when_row(card):
    bot, _, press = card()
    text, buttons = await press(bot._encode("e", _draft(bot)))
    assert "Когда: по пятницам" in text and "•Пт" in buttons and "•Неделя" in buttons

    text, buttons = await press(buttons["Месяц"])
    assert "Когда: <i>выбрать</i>" in text
    assert [str(i) for i in range(1, 29)] + ["Последний день"] == [k for k in buttons if k[0].isdigit() or k == "Последний день"]
    assert "Пт" not in buttons and "✅ Сохранить" not in buttons

    text, buttons = await press(buttons["Последний день"])
    assert "Когда: в последний день месяца" in text and "✅ Сохранить" in buttons

    text, buttons = await press(buttons["День"])
    assert "Когда" not in text and "Последний день" not in buttons and "✅ Сохранить" in buttons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot", "button", "expected"),
    [(47, "+1ч", "00:30"), (0, "−1ч", "23:00"), (47, ":00/:30", "23:00"), (20, ":00/:30", "10:30")],
)
async def test_time_buttons_wrap_around_the_day(card, slot, button, expected):
    bot, _, press = card()
    _, buttons = await press(bot._encode("e", _draft(bot, slot=slot)))
    text, _ = await press(buttons[button])
    assert f"Время: {expected}" in text


@pytest.mark.asyncio
async def test_save_is_hidden_until_everything_is_chosen(card):
    bot, _, press = card()
    text, buttons = await press(bot._encode("e", _draft(bot, freq=None, day=None, slot=None)))
    assert text.count("<i>выбрать</i>") == 2, "частота и время"
    assert "✅ Сохранить" not in buttons and "✖ Отмена" in buttons

    _, buttons = await press(buttons["Неделя"])
    _, buttons = await press(buttons["Пт"])
    assert "✅ Сохранить" not in buttons, "время не выбрано"
    text, buttons = await press(buttons["09:00"])
    assert "Время: 09:00" in text and "✅ Сохранить" in buttons


@pytest.mark.asyncio
async def test_zone_picker_changes_the_zone(card):
    bot, _, press = card()
    _, buttons = await press(bot._encode("e", _draft(bot, tz="America/New_York")))
    text, buttons = await press(buttons["Сменить пояс"])
    assert "Часовой пояс" in text and len(buttons) == 9 + 1
    text, _ = await press(buttons["Екатеринбург"])
    assert "Пояс: Екатеринбург (UTC+5)" in text


@pytest.mark.asyncio
async def test_save_writes_the_row_with_next_run_and_logs_it(card, repo):
    bot, cursor, press = card()
    before = datetime.now(UTC)
    text, buttons = await press(bot._encode("s", _draft(bot)))

    assert text.startswith("<p>Готово: Лидеры · Лучшие снайперы — по пятницам 10:00 (МСК)</p>")
    assert "/subscriptions" in text and buttons == {}
    [(_, params)] = [(q, p) for q, p in cursor.executed if q.startswith("INSERT INTO bot_schedules")]
    assert params[1:8] == ("leaders", params[2], "week", 5, None, time(10, 0), MOSCOW)
    assert params[2].adapted == {"category": "goals"}
    assert params[8] == repo.next_run_at("week", 5, None, time(10, 0), MOSCOW, before)
    actions = [p for q, p in cursor.executed if q.startswith("INSERT INTO bot_actions")]
    assert actions[-1][2:] == ("schedule_created", "42:leaders")


def _row():
    """Строка `_ACTIVE` — уже есть ежедневное расписание /standings."""
    return (7, 1, "open_command", {"command": "standings"}, "day", None, None, time(9, 0), MOSCOW, True,
            datetime(2026, 11, 3, 6, 0, tzinfo=UTC))


@pytest.mark.asyncio
async def test_free_chat_with_a_schedule_is_refused_and_keeps_the_draft(card, api):
    bot, cursor, press = card(schedules=[_row()])
    await press(bot._encode("e", _draft(bot)))
    edits = len(api.edited_texts)
    await press(bot._encode("s", _draft(bot)))
    assert len(api.edited_texts) == edits, "карточка не тронута"
    assert body(api.sent_rich[-1]) == f"<p>{bot._SCHEDULE_LIMIT_TEXT}</p>"
    assert not [q for q, _ in cursor.executed if q.startswith("INSERT INTO bot_schedules")]


@pytest.mark.asyncio
async def test_premium_chat_saves_a_second_schedule(card):
    bot, cursor, press = card(schedules=[_row()], premium=True)
    text, _ = await press(bot._encode("s", _draft(bot)))
    assert text.startswith("<p>Готово:")
    assert [q for q, _ in cursor.executed if q.startswith("INSERT INTO bot_schedules")]


@pytest.mark.asyncio
async def test_cancel_drops_the_card(card):
    bot, _, press = card()
    text, buttons = await press("sch:x")
    assert text == "<p>Расписание не создано.</p>" and buttons == {}


def test_longest_callback_fits_telegram_limit(bot_module):
    import re
    import zoneinfo

    bot = bot_module("bot")
    longest_zone = max(zoneinfo.available_timezones(), key=len)
    draft = bot.Draft("open_command", {"command": "team_stats"}, "month", 28, 47, longest_zone)
    for view in "ezs":
        data = bot._encode(view, draft)
        assert len(data.encode()) <= 64, data
        assert re.match(bot.SCHEDULE_CALLBACK_PATTERN, data)
        assert bot._decode(data) == (view, draft)
    full = bot.Draft("team_profile", {"team": "WSH", "all_players": True}, "day", None, None, MOSCOW)
    assert bot._decode(bot._encode("e", full)) == ("e", full)
