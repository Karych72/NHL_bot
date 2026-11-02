"""Расписания экранов (Задача 107): расчёт следующей отправки и подпись в `/subscriptions`.

`schedule_repo.next_run_at` — чистая функция на `zoneinfo`, проверяется без БД: частоты,
«последний день месяца», переходы на летнее и зимнее время, граница «ровно сейчас». Тикер,
лимит и удаление — на настоящей таблице в `tests/test_db_nhl.py` (`TestBotSchedulesSql`).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

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
