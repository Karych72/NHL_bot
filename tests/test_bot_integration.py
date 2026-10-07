"""Сквозные сценарии «запрос → ответ» для основных веток бота (Задача 7).

Остальные tests/test_bot_*.py рвут стек посередине: тесты хендлеров подменяют
функции ``bot_messages``, тесты сообщений — ``fetch_all``. Здесь подменяется
только граница БД (фикстура ``fake_db_router``: соединение psycopg2 и TTL-обёртка
над ним), а всё между хендлером и этой границей работает по-настоящему: разбор
callback_data, композиция SQL, маппинг строк на колонки, jinja-шаблоны, сборка
клавиатур. Postgres не нужен, сети нет.

Кнопку для следующего шага сценарий берёт из клавиатуры, которую вернул
предыдущий шаг, и сверяет её callback_data с паттерном регистрации хендлера —
так проверяется, что нажатие кнопки действительно попадёт в тот хендлер,
которому её адресуют.
"""
from __future__ import annotations

import re
from typing import Any, List

import pytest


def _flat_buttons(markup: Any) -> List[Any]:
    """Кнопки клавиатуры одним списком, без разбиения по рядам."""
    return [button for row in markup.inline_keyboard for button in row]


def _callback_data(markup: Any) -> List[str]:
    return [button.callback_data for button in _flat_buttons(markup)]


# ---------------------------------------------------------------------------
# Сценарий «таблица»: /standings
# ---------------------------------------------------------------------------

# short_name, games_played, points, procent_points, wins, losses, ot,
# division_name, conference_name
# Метрополитен идёт вперемешку: сортировку делает бот, а не порядок выдачи БД.
_STANDINGS_ROWS = [
    ("Islanders", 20, 19, 47.5, 8, 8, 3, "Metropolitan", "Eastern"),
    ("Rangers", 20, 28, 70.0, 13, 5, 2, "Metropolitan", "Eastern"),
    ("Devils", 20, 22, 55.0, 10, 7, 2, "Metropolitan", "Eastern"),
    ("Bruins", 20, 25, 62.5, 12, 6, 1, "Atlantic", "Eastern"),
    ("Avalanche", 20, 27, 67.5, 13, 6, 1, "Central", "Western"),
    ("Kings", 20, 24, 60.0, 11, 6, 2, "Pacific", "Western"),
]

_TABLE_ROUTES = [
    ("FROM teams_stats ts", _STANDINGS_ROWS),
    ("SELECT max(day)::text AS d", [("2026-04-01",)]),
]


@pytest.mark.asyncio
async def test_standings_command_renders_divisions_with_ranked_rows_sorted_by_points(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_TABLE_ROUTES)
    update = make_message_update("/standings")

    await bot.cmd_standings(update, fake_context)

    (reply,) = update.message.replies
    text = reply["text"]
    assert reply["parse_mode"] == "HTML"
    assert "Турнирная таблица NHL" in text
    assert "данные на 2026-04-01" in text
    for title in ("EASTERN CONFERENCE", "WESTERN CONFERENCE",
                  "METROPOLITAN DIVISION", "ATLANTIC DIVISION",
                  "CENTRAL DIVISION", "PACIFIC DIVISION"):
        assert f"<b>{title}</b>" in text
    # Строки дивизиона отсортированы по очкам, а не по порядку выдачи БД.
    assert text.index("Rangers") < text.index("Devils") < text.index("Islanders")
    # Колонки моноширинной таблицы через «|»: место « 1.», имя дополнено до 14,
    # очки/игры до 3, %очк до 5.
    assert " 1.|Rangers       | 28| 20| 70.0" in text
    assert " 2.|Devils        | 22| 20| 55.0" in text
    assert " 3.|Islanders     | 19| 20| 47.5" in text
    assert "reply_markup" not in reply, "у /standings вне диалога клавиатуры нет"


@pytest.mark.asyncio
async def test_stats_menu_standings_branch_sends_table_with_menu_button(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Та же таблица, но веткой диалога `/stats`, а не командой.

    Отдельный сценарий, потому что путь отличается всем, кроме рендера: новое
    сообщение вместо `reply_text`, кнопка возврата в корень меню и запись id
    сообщения в `user_data` — с неё `/cancel` снимает клавиатуру.
    """
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router(_TABLE_ROUTES)
    update = make_callback_update(str(dialog_states.LEAGUE_STANDINGS))

    state = await stats_handlers.bot_league_standings(update, fake_context)

    (sent,) = fake_context.bot.sent_messages
    assert state == dialog_states.FIRST
    assert "<b>METROPOLITAN DIVISION</b>" in sent["text"]
    assert _callback_data(sent["reply_markup"]) == [str(dialog_states.CHOOSE_STATS)]
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


# ---------------------------------------------------------------------------
# Сценарий «лидеры с пагинацией»: /leaders → категория → следующая страница
# ---------------------------------------------------------------------------

# lastname, position, value, team, games, shifts (Задача 18 — «хвост» строки
# лидерборда) — 25 игроков, три страницы по 10.
_LEADERS = [
    (f"Player{i:02d}", "C", 100 - i, "NYR", 82, 1500 + i) for i in range(1, 26)
]


def _leaderboard_page_route(rows):
    """Маршрут `COUNT(*) OVER () AS total`: срез `rows` по LIMIT/OFFSET плюс
    размер выборки — параметризован по списку строк (не копировать под каждый сценарий)."""
    def page(params):
        _season_id, limit, offset = params
        return [(*row, len(rows)) for row in rows[offset:offset + limit]]

    return page


_LEADERS_ROUTES = [("COUNT(*) OVER () AS total", _leaderboard_page_route(_LEADERS))]


@pytest.mark.asyncio
async def test_leaders_first_page_shows_ranks_one_to_ten_and_only_next_button(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_LEADERS_ROUTES)

    menu = make_message_update("/leaders")
    await bot.cmd_leaders(menu, fake_context)
    assert "Лидеры сезона" in menu.message.replies[0]["text"]
    points_button = _flat_buttons(menu.message.replies[0]["reply_markup"])[0]
    assert re.match(stats_handlers.LEADERS_PICK_CALLBACK_PATTERN, points_button.callback_data)

    update = make_callback_update(points_button.callback_data)
    await stats_handlers.callback_leaders_pick(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    text = edited["text"]
    assert edited["parse_mode"] == "HTML"
    assert "<b>Топ бомбардиров</b>" in text
    assert "<i>Показаны 1–10 из 25 строк.</i>" in text
    assert "<pre>   |Игрок       |Ком|Очки| И|Смен\n" in text
    assert " 1.|Player01 [C]|NYR|  99|82|1501" in text
    assert "10.|Player10 [C]|NYR|  90|82|1510" in text
    assert "11. Player11" not in text
    assert _callback_data(edited["reply_markup"])[0] == "pl:points:10", "первая страница — только «вперёд»"


@pytest.mark.asyncio
async def test_leaders_next_page_asks_db_for_offset_ten_and_renders_ranks_eleven_to_twenty(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router(_LEADERS_ROUTES)

    first = make_callback_update("pl:pick:points")
    await stats_handlers.callback_leaders_pick(first, fake_context)
    next_button = _flat_buttons(first.callback_query.edited_texts[0]["reply_markup"])[0]
    assert re.match(stats_handlers.LEADERBOARD_PAGE_CALLBACK_PATTERN, next_button.callback_data)

    update = make_callback_update(next_button.callback_data)
    await stats_handlers.callback_leaderboard_page(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    text = edited["text"]
    assert "<i>Показаны 11–20 из 25 строк.</i>" in text
    assert "11.|Player11 [C]|NYR|  89|82|1511" in text
    assert "20.|Player20 [C]|NYR|  80|82|1520" in text
    assert "Player10" not in text
    assert _callback_data(edited["reply_markup"])[:2] == ["pl:points:0", "pl:points:20"]
    # Смещение не «нарисовано» в тексте, а действительно ушло в БД параметром.
    assert [params[1:] for _query, params in cursor.executed] == [(10, 0), (10, 10)]


# ---------------------------------------------------------------------------
# Сценарии «карточка игры» и «дайджест дня» — общие данные двух матчей
# ---------------------------------------------------------------------------

GAME_ONE = 2026020001
GAME_TWO = 2026020002

# get_game_stats: goals, pim, blocks, hits, shots, is_overtime, is_shootouts,
# field, team_name — первая строка домашняя, вторая гостевая. Пара
# (is_overtime=False, is_shootouts=True) — то, что лежит в БД сегодня, см.
# комментарий к _FORM_BY_TEAM; на карточку матча она не влияет, потому что
# game_message читает is_shootouts только при is_overtime.
# get_goals_game: scorer, scorer_position, scorer_nationality, assist_1,
# assist_1_nationality, assist_2, assist_2_nationality, period, goal_time,
# home_score, away_score, is_ppg, is_shg, empty_net, winner_goal, game_id, event_id.
# get_goalies_game: shots, saves, timeonice, lastname, save_percentage, is_home.
# get_three_stars_game: star, lastname, player_position, abbreviation, goals,
# assists, saves, shots, save_percentage.
_GAMES = {
    GAME_ONE: {
        "stats": [
            (3, 8, 12, 20, 31, False, True, "home", "NYR"),
            (2, 6, 10, 18, 27, False, True, "away", "BOS"),
        ],
        "teams": [(1, 2, "2026-04-01")],
        "goals": [
            ("Panarin", "LW", "RUS", "Fox", None, None, None, 1, "05:12", 1, 0, True, False, False, False, GAME_ONE, 101),
            ("Marchand", "LW", None, None, None, None, None, 2, "11:40", 1, 1, False, False, False, False, GAME_ONE, 102),
            ("Zibanejad", "C", None, "Panarin", "RUS", "Fox", None, 2, "15:20", 2, 1, False, True, False, False, GAME_ONE, 103),
            ("Pastrnak", "RW", None, "Marchand", None, None, None, 3, "04:10", 2, 2, False, False, False, False, GAME_ONE, 104),
            ("Panarin", "LW", "RUS", None, None, None, None, 3, "18:03", 3, 2, False, False, False, True, GAME_ONE, 105),
        ],
        "goalies": [
            (27, 25, "60:00", "Shesterkin", 92.59, True),
            (31, 28, "60:00", "Swayman", 90.32, False),
        ],
        "three_stars": [
            (1, "Panarin", "LW", "NYR", 2, 1, None, None, None),
            (2, "Shesterkin", "G", "NYR", None, None, 25, 27, 92.59),
            (3, "Marchand", "LW", "BOS", 1, 0, None, None, None),
        ],
    },
    GAME_TWO: {
        "stats": [
            (1, 4, 9, 15, 24, False, True, "home", "TOR"),
            (0, 2, 11, 19, 30, False, True, "away", "MTL"),
        ],
        "teams": [(11, 12, "2026-04-01")],
        "goals": [
            ("Matthews", "C", None, "Nylander", None, None, None, 1, "12:00", 1, 0, False, False, False, True, GAME_TWO, 201),
        ],
        "goalies": [
            (30, 30, "60:00", "Woll", 100.0, True),
            (24, 23, "60:00", "Montembeault", 95.83, False),
        ],
        "three_stars": [
            (1, "Matthews", "C", "TOR", 1, 1, None, None, None),
            (2, "Woll", "G", "TOR", None, None, 30, 30, 100.0),
            (3, "Nylander", "RW", "TOR", 0, 1, None, None, None),
        ],
    },
}

# Выборка формы: winner_id, is_overtime, is_shootouts, ot_empty_net_win
# (Задача 24 — признак правила NHL 84.2 из EXISTS-подзапроса по all_goals;
# здесь всегда False и на исход не влияет, см. ниже). Запрос уходит по
# одному разу на команду и фильтруется по её team_id (`WHERE home_team_id
# = %s OR away_team_id = %s`), поэтому маршрут отвечает по team_id из
# параметров: иначе команде засчитывались бы игры, которых она не играла.
#
# is_shootouts = True у всех строк — это НЕ произвол фикстуры, а текущее
# состояние БД (дефект Д1 Задачи 28: загрузчик пишет туда `shootoutInUse`,
# флаг регламента сезона, истинный и для игры, доигранной в основное время).
# Из-за него `_team_game_outcome()` (Задача 24) уходит в ветку `is_shootouts`
# на каждом незачётном исходе, и форма читается как «В-0-ПО»: ожидания ниже
# фиксируют то, что видит пользователь сегодня. При исправлении Д1 править
# здесь же — см. «Область» Задачи 28 в плане.
_FORM_BY_TEAM = {
    1: [  # NYR — 3 победы, 2 поражения; рендерится как 3-0-2
        ("2026-03-31", (1, False, True, False)),
        ("2026-03-31", (1, False, True, False)),
        ("2026-03-31", (1, False, True, False)),
        ("2026-03-31", (6, False, True, False)),
        ("2026-03-31", (7, True, True, False)),
    ],
    2: [  # BOS — 1 победа, 4 поражения; рендерится как 1-0-4
        ("2026-03-31", (2, False, True, False)),
        ("2026-03-31", (9, False, True, False)),
        ("2026-03-31", (10, False, True, False)),
        ("2026-03-31", (11, False, True, False)),
        ("2026-03-31", (12, False, True, False)),
    ],
    11: [("2026-03-31", (11, False, True, False))],  # TOR — 1-0-0
    12: [("2026-03-31", (14, False, True, False))],  # MTL — 0-0-1
}


def _form_route(form_by_team):
    """Маршрут «форма команды» (`ORDER BY day DESC NULLS LAST`) по `team_id`
    из параметров — параметризован по словарю формы (не копировать под
    каждый сценарий). Значения словаря — пары (игровой день, строка выборки):
    маршрут, как и SQL, отбрасывает игры не раньше `before_day` карточки."""
    def rows(params):
        _season_id, team_id, _same_team_id, before_day, _before_day, _limit = params
        return [
            row for day, row in form_by_team[team_id]
            if before_day is None or day < before_day
        ]

    return rows


def _game_card_routes(games_by_id, form_by_team):
    """Семь общих маршрутов карточки матча (`/game`, дайджест дня) по словарю
    игр `games_by_id` (`game_id` → ответы формы `_GAMES`) и форме
    `form_by_team` — общий строитель для любого сценария (не копировать
    построчно под каждый)."""
    def by_game(key):
        def rows(params):
            return games_by_id[params[0]][key]

        return rows

    return [
        ("SELECT 1 AS o FROM games", [(1,)]),
        ("SELECT * FROM get_game_stats", by_game("stats")),
        ("SELECT home_team_id, away_team_id, day", by_game("teams")),
        ("SELECT * FROM get_goals_game", by_game("goals")),
        ("SELECT * FROM get_goalies_game", by_game("goalies")),
        ("SELECT * FROM get_three_stars_game", by_game("three_stars")),
        ("ORDER BY day DESC NULLS LAST", _form_route(form_by_team)),
    ]


_GAME_CARD_ROUTES = _game_card_routes(_GAMES, _FORM_BY_TEAM)

# `/today` берёт игровой день не из БД, а у `bot.last_night_day()` — тесты
# фиксируют его (сам расчёт — в tests/test_bot_messages.py).
_TODAY = "2026-04-01"

_DIGEST_ROUTES = [
    ("SELECT DISTINCT game_id FROM games", [(GAME_ONE,), (GAME_TWO,)]),
    # Подписи кнопок: game_id, хозяева, гости — одним запросом на все матчи дня.
    ("JOIN teams th", [(GAME_ONE, "NYR", "BOS"), (GAME_TWO, "TOR", "MTL")]),
] + _GAME_CARD_ROUTES


# ---------------------------------------------------------------------------
# Сценарий «карточка игры»: /game <id>
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_game_command_renders_full_card(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    cursor = fake_db_router(_GAME_CARD_ROUTES)
    update = make_message_update(f"/game {GAME_ONE}")
    fake_context.args = [str(GAME_ONE)]

    await bot.cmd_game(update, fake_context)

    (sent,) = fake_context.bot.sent_messages
    text = sent["text"]
    # Семь разных запросов, восемь обращений: форма спрашивается на каждую команду.
    assert len(cursor.executed) == 8
    assert sent["parse_mode"] == "HTML"
    assert "🏒 <b>NYR 3:2 BOS</b>\n<i>Периоды: 1:0, 1:1, 1:1</i>" in text
    # 3-0-2 / 1-0-4, а не 3-1-1 / 1-3-1: см. комментарий к _FORM_BY_TEAM (Д1).
    # Хозяева первыми, как в шапке карточки.
    assert "<i>Форма (5 игр, W-L-OTL)</i>:\nNYR 3-0-2 · BOS 1-0-4" in text
    assert "<b>Хет-трик</b>: Panarin" not in text, "у Panarin два гола — хет-трика нет"
    # Голы — выровненный <pre> в ширину телефона: время без номера периода,
    # счёт, автор; ассистенты — строкой ниже. Российский игрок (в том числе в
    # передачах) капсом; победная шайба — «ПШ», а не «★».
    assert (
        "<pre> 5:12 1:0 PANARIN [LW] (ББ)\n"
        "          Fox\n"
        "31:40 1:1 Marchand [LW]\n"
        "35:20 2:1 Zibanejad [C] (МБ)\n"
        "          PANARIN, Fox\n"
        "44:10 2:2 Pastrnak [RW]\n"
        "          Marchand\n"
        "58:03 3:2 PANARIN [LW] (ПШ)</pre>"
    ) in text
    assert "<b>Броски</b>: 31 - 27" in text
    assert "<b>Штрафное время</b>: 8 - 6" in text
    assert (
        "<b>Вратари</b>\nShesterkin — 25/27, 92.59%, 60:00\n"
        "Swayman — 28/31, 90.32%, 60:00"
    ) in text
    assert "<b>Звёзды матча</b>" in text
    assert "★1 Panarin (NYR) — 2+1" in text
    assert "★2 Shesterkin (NYR) — 25/27, 92.59%" in text
    assert "★3 Marchand (BOS) — 1+0" in text


@pytest.mark.asyncio
async def test_game_command_attaches_one_video_button_per_goal(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_GAME_CARD_ROUTES)
    update = make_message_update(f"/game {GAME_ONE}")
    fake_context.args = [str(GAME_ONE)]

    await bot.cmd_game(update, fake_context)

    (sent,) = fake_context.bot.sent_messages
    buttons = _flat_buttons(sent["reply_markup"])
    assert _callback_data(sent["reply_markup"]) == [
        f"gv:{GAME_ONE}:{event_id}" for event_id in (101, 102, 103, 104, 105)
    ]
    assert buttons[0].text == "▶ 1:0 PANARIN 5:12"
    # Два столбца: столбик на всю ширину сливался со следующей карточкой.
    assert [len(row) for row in sent["reply_markup"].inline_keyboard] == [2, 2, 1]


@pytest.mark.asyncio
async def test_game_command_reports_missing_game_without_running_card_queries(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    cursor = fake_db_router([("SELECT 1 AS o FROM games", [])])
    update = make_message_update("/game 1")
    fake_context.args = ["1"]

    await bot.cmd_game(update, fake_context)

    (sent,) = fake_context.bot.sent_messages
    assert sent["text"] == "Такого матча нет в базе бота."
    assert "reply_markup" not in sent
    assert len(cursor.executed) == 1, "после game_exists() == False запросов карточки быть не должно"


# ---------------------------------------------------------------------------
# Сценарий «дайджест дня»: /today → разворот матча кнопкой
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_today_summarises_both_matches_with_numbered_team_buttons(
    bot_module, fake_db_router, make_message_update, fake_context, monkeypatch
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router(_DIGEST_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: _TODAY)
    update = make_message_update("/today")

    await bot.cmd_today(update, fake_context)

    summary, hint = fake_context.bot.sent_messages
    assert "<b>Матчи 2026-04-01</b> (2 игр)" in summary["text"]
    # Матчи — жирной строкой, периоды под ней; между матчами пустая строка.
    assert (
        "1. <b>NYR 3:2 BOS</b>\n  1:0, 1:1, 1:1\n\n"
        "2. <b>TOR 1:0 MTL</b>\n  1:0, 0:0, 0:0\n\n"
    ) in summary["text"]
    assert "Кнопка матча — полная карточка и видео голов." in summary["text"]
    # Кнопки несут те же номера, что и сводка, и команды вместо «Матч N».
    buttons = _flat_buttons(summary["reply_markup"])
    assert [b.text for b in buttons] == ["1. NYR – BOS", "2. TOR – MTL"]
    assert [b.callback_data for b in buttons] == [f"dg:{GAME_ONE}", f"dg:{GAME_TWO}"]
    assert hint["text"] == stats_handlers._DIGEST_MORE_HINT
    day_query = next(q for q in cursor.executed if "SELECT DISTINCT game_id" in q[0])
    assert day_query[1][0] == _TODAY


@pytest.mark.asyncio
async def test_digest_expand_button_opens_the_full_card_of_that_match(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context,
    monkeypatch
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_DIGEST_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: _TODAY)

    digest = make_message_update("/today")
    await bot.cmd_today(digest, fake_context)
    second_match = _flat_buttons(fake_context.bot.sent_messages[0]["reply_markup"])[1]
    assert re.match(stats_handlers.DIGEST_EXPAND_CALLBACK_PATTERN, second_match.callback_data)

    update = make_callback_update(second_match.callback_data)
    await stats_handlers.callback_expand_digest_game(update, fake_context)

    card = fake_context.bot.sent_messages[-1]
    assert "<b>TOR 1:0 MTL</b>" in card["text"]
    assert "<pre>12:00 1:0 Matthews [C] (ПШ)\n          Nylander</pre>" in card["text"]
    assert _callback_data(card["reply_markup"]) == [f"gv:{GAME_TWO}:201"]


# ---------------------------------------------------------------------------
# Задача 36: сезон с 0 и с 1–3 игровыми днями — /standings, /leaders, /today,
# /game, /advanced.
#
# Факт из прогона загрузчика на скретч-БД (season-load-full, SEASON_ID=20262027,
# 2026-09-28, до старта сезона): при 0 сыгранных играх лиги пустует не только
# `games` — `teams`/`teams_stats`/`rosters`/`players_season_stats` тоже, потому
# что `build_teams_and_stats()` строит их из `team/summary` (агрегат по уже
# сыгранным играм), а `build_rosters()` идёт только по командам из этого же
# списка (`pipeline/load_season_modern.py:263-338`). Маршрут-заглушка "" ниже
# отвечает пустой выборкой на любой запрос — это и есть реальное состояние БД
# в первый день после переключения сезона, а не гипотеза.
#
# Для «1–3 игровых дня» живых данных получить нельзя (сезон 2026/27 на дату
# работы над задачей ещё не начался), поэтому фикстура собрана по этому же
# факту: только команды, уже сыгравшие матч, попадают в teams_stats/rosters/
# players_season_stats — команды без единой игры в сезоне отсутствуют в
# строках целиком, а не приходят строкой с нулями.
# ---------------------------------------------------------------------------

_EMPTY_SEASON_ROUTES = [
    # MAX() без GROUP BY в Postgres всегда отдаёт ровно одну строку (NULL на
    # пустой таблице), а не ноль строк — маршрут-заглушка ниже отвечает [] на
    # всё остальное, но day_digest()'s `MAX(day)` нужно перечислить отдельно
    # (team_table() на пустом teams_stats возвращается раньше своего
    # `MAX(day)::text` в `_standings_as_of_day()` — тот запрос сюда не доходит).
    ("SELECT max(day) AS day FROM games", [(None,)]),
    ("", []),
]


@pytest.mark.asyncio
async def test_standings_command_on_empty_season_reports_season_not_started(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_EMPTY_SEASON_ROUTES)
    update = make_message_update("/standings")

    await bot.cmd_standings(update, fake_context)

    (reply,) = update.message.replies
    assert reply["parse_mode"] == "HTML"
    assert "Сезон ещё не начался" in reply["text"]
    assert "EASTERN CONFERENCE" not in reply["text"], "пустая таблица с заголовками — не текст-причина"


@pytest.mark.asyncio
async def test_leaders_pick_on_empty_season_reports_no_data(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_EMPTY_SEASON_ROUTES)

    menu = make_message_update("/leaders")
    await bot.cmd_leaders(menu, fake_context)
    points_button = _flat_buttons(menu.message.replies[0]["reply_markup"])[0]

    update = make_callback_update(points_button.callback_data)
    await stats_handlers.callback_leaders_pick(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "Нет данных в этом диапазоне." in edited["text"]


@pytest.mark.asyncio
async def test_today_on_empty_season_reports_no_finished_matches(
    bot_module, fake_db_router, make_message_update, fake_context, monkeypatch
):
    bot = bot_module("bot")
    fake_db_router(_EMPTY_SEASON_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: "2026-10-02")
    update = make_message_update("/today")

    await bot.cmd_today(update, fake_context)

    # Пустая ночь — пометка и откат на последний игровой день в базе; в пустом
    # сезоне нет и его.
    (notice,) = update.message.replies
    assert notice["text"] == (
        "Прошедшей ночью (2026-10-02) матчей в базе нет — показываю последний игровой день."
    )
    sent, hint = fake_context.bot.sent_messages
    assert sent["text"] == "В базе пока нет завершенных матчей."
    assert "/stats" in hint["text"]


@pytest.mark.asyncio
async def test_game_command_on_empty_season_reports_missing_game(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_EMPTY_SEASON_ROUTES)
    update = make_message_update("/game 1")
    fake_context.args = ["1"]

    await bot.cmd_game(update, fake_context)

    (sent,) = fake_context.bot.sent_messages
    assert sent["text"] == "Такого матча нет в базе бота."


@pytest.mark.asyncio
async def test_advanced_pick_on_empty_season_reports_no_data(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_EMPTY_SEASON_ROUTES)

    intro = make_message_update("/advanced")
    await bot.cmd_advanced(intro, fake_context)
    sat_button = _flat_buttons(intro.message.replies[0]["reply_markup"])[0]
    assert sat_button.callback_data == "adv:sat"

    update = make_callback_update(sat_button.callback_data)
    await stats_handlers.callback_standalone_adv(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "Нет данных в этом диапазоне." in edited["text"]


# ---------------------------------------------------------------------------
# «1–3 игровых дня»: три команды сыграли (NYR — 2 игры, BOS и TOR — по одной),
# остальные 29 команд отсутствуют в teams_stats целиком (см. факт выше).
# ---------------------------------------------------------------------------

# short_name, games_played, points, procent_points, wins, losses, ot,
# division_name, conference_name — ни одной команды с Западного побережья:
# там пока никто не сыграл ни одного матча.
_PARTIAL_STANDINGS_ROWS = [
    ("Rangers", 2, 4, 100.0, 2, 0, 0, "Metropolitan", "Eastern"),
    ("Bruins", 1, 0, 0.0, 0, 1, 0, "Atlantic", "Eastern"),
    ("Maple Leafs", 1, 0, 0.0, 0, 1, 0, "Atlantic", "Eastern"),
]

_PARTIAL_TABLE_ROUTES = [
    ("FROM teams_stats ts", _PARTIAL_STANDINGS_ROWS),
    ("SELECT max(day)::text AS d", [("2026-10-03",)]),
]


@pytest.mark.asyncio
async def test_standings_command_on_partial_season_shows_only_teams_with_games(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_PARTIAL_TABLE_ROUTES)
    update = make_message_update("/standings")

    await bot.cmd_standings(update, fake_context)

    (reply,) = update.message.replies
    text = reply["text"]
    assert "Сезон ещё не начался" not in text
    assert " 1.|Rangers       |  4|  2|100.0" in text
    # Разделы западной конференции остаются на месте (нет исключения), но
    # каждый дивизион без единой сыгранной игры несёт текст-причину, а не
    # заголовки пустой таблицы.
    assert "<b>WESTERN CONFERENCE</b>" in text
    assert "<b>CENTRAL DIVISION</b>" in text
    # Ровно два пустых дивизиона (Central и Pacific) — оба западных, оба с
    # текстом-причиной, ни одного лишнего или пропущенного.
    assert text.count("(в дивизионе ещё никто не сыграл)") == 2
    assert "Avalanche" not in text and "Kings" not in text


# lastname, position, value(points), team, games, shifts — только игроки трёх
# команд, уже сыгравших матч.
_PARTIAL_LEADERS = [
    ("Panarin", "LW", 5, "NYR", 2, 45),
    ("Zibanejad", "C", 3, "NYR", 2, 40),
    ("Marchand", "LW", 2, "BOS", 1, 20),
    ("Matthews", "C", 1, "TOR", 1, 18),
]


_PARTIAL_LEADERS_ROUTES = [("COUNT(*) OVER () AS total", _leaderboard_page_route(_PARTIAL_LEADERS))]


@pytest.mark.asyncio
async def test_leaders_pick_on_partial_season_shows_short_page_without_next_button(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_PARTIAL_LEADERS_ROUTES)

    menu = make_message_update("/leaders")
    await bot.cmd_leaders(menu, fake_context)
    points_button = _flat_buttons(menu.message.replies[0]["reply_markup"])[0]

    update = make_callback_update(points_button.callback_data)
    await stats_handlers.callback_leaders_pick(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    text = edited["text"]
    assert "<i>Показаны 1–4 из 4 строк.</i>" in text
    assert "1.|Panarin [LW] |NYR|   5|2|  45" in text
    assert "4.|Matthews [C] |TOR|   1|1|  18" in text
    # Ни prev, ни next — показаны все 4 строки; остаётся только смена категории.
    assert _callback_data(edited["reply_markup"]) == [
        "pl:pick:points", "pl:pick:goals", "pl:pick:assists",
    ]


# Ранняя сборная (games < 20) отфильтровывается INNER JOIN players_season_stats
# в самом запросе (bot_messages._pss_join_sql) — на 1–3 играх сезона выборка
# players_advanced_stats пуста для *всех* игроков, не только по недостатку строк.
_PARTIAL_ADVANCED_ROUTES = [("players_advanced_stats", [])]


@pytest.mark.asyncio
async def test_advanced_pick_on_partial_season_reports_no_data_below_games_threshold(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router(_PARTIAL_ADVANCED_ROUTES)

    intro = make_message_update("/advanced")
    await bot.cmd_advanced(intro, fake_context)
    sat_button = _flat_buttons(intro.message.replies[0]["reply_markup"])[0]

    update = make_callback_update(sat_button.callback_data)
    await stats_handlers.callback_standalone_adv(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "Нет данных в этом диапазоне." in edited["text"]
    # «Нет данных» — реально из-за порога games >= 20 в самом запросе
    # (_pss_join_sql), а не по случайному совпадению пустого маршрута. Порог
    # смягчается в начале сезона до половины игр лидера по числу матчей.
    assert "LEAST(20, (SELECT CEIL(MAX(ts.games_played) / 2.0)" in cursor.executed[-1][0]


# Игры сезона: NYR обыгрывает BOS 2026-10-01 (в фикстуре не нужна отдельно —
# участвует только как первая игра NYR в её форме, `_PARTIAL_FORM_BY_TEAM`),
# затем TOR — 2026-10-03 (первая игра сезона у TOR). /today в тесте показывает
# ночь, закреплённую на 2026-10-03, — вторую игру.
PARTIAL_GAME_B = 2026020102  # TOR (home) 1 : 3 NYR (away) — 2026-10-03

_PARTIAL_GAME_B_DATA = {
    "stats": [
        (1, 4, 8, 15, 22, False, False, "home", "TOR"),
        (3, 6, 9, 24, 30, False, False, "away", "NYR"),
    ],
    "teams": [(3, 1, "2026-10-03")],  # home_team_id=TOR(3), away_team_id=NYR(1), day
    "goals": [
        ("Matthews", "C", None, "Marner", None, None, None, 1, "05:00", 1, 0, False, False, False, False, PARTIAL_GAME_B, 301),
        ("Panarin", "LW", "RUS", "Zibanejad", None, None, None, 2, "10:00", 1, 1, False, False, False, False, PARTIAL_GAME_B, 302),
        ("Zibanejad", "C", None, None, None, None, None, 3, "15:00", 1, 2, False, False, False, True, PARTIAL_GAME_B, 303),
        ("Panarin", "LW", "RUS", None, None, None, None, 3, "18:00", 1, 3, False, False, False, False, PARTIAL_GAME_B, 304),
    ],
    "goalies": [
        (22, 21, "60:00", "Woll", 95.45, True),
        (15, 14, "60:00", "Shesterkin", 93.33, False),
    ],
    "three_stars": [
        (1, "Panarin", "LW", "NYR", 2, 1, None, None, None),
        (2, "Matthews", "C", "TOR", 1, 0, None, None, None),
        (3, "Zibanejad", "C", "NYR", 1, 1, None, None, None),
    ],
}

# winner_id, is_overtime, is_shootouts, ot_empty_net_win — форма читает
# games ещё раз, независимо от карточки; TOR играет свой первый матч сезона
# (одна строка вместо «—»), NYR — уже вторую подряд победу.
_PARTIAL_FORM_BY_TEAM = {
    # TOR: 0-0-0 — в выборке только эта же игра, а форма считается до неё.
    3: [("2026-10-03", (1, False, False, False))],
    # NYR: 1-0-0 — победа 2026-10-01; эта игра в форму до неё не входит.
    1: [("2026-10-03", (1, False, False, False)), ("2026-10-01", (1, False, False, False))],
}


_PARTIAL_GAME_CARD_ROUTES = _game_card_routes(
    {PARTIAL_GAME_B: _PARTIAL_GAME_B_DATA}, _PARTIAL_FORM_BY_TEAM
)

_PARTIAL_DIGEST_ROUTES = [
    ("SELECT DISTINCT game_id FROM games", [(PARTIAL_GAME_B,)]),
] + _PARTIAL_GAME_CARD_ROUTES


@pytest.mark.asyncio
async def test_game_command_on_partial_season_renders_card_with_low_game_count_form(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_PARTIAL_GAME_CARD_ROUTES)
    update = make_message_update(f"/game {PARTIAL_GAME_B}")
    fake_context.args = [str(PARTIAL_GAME_B)]

    await bot.cmd_game(update, fake_context)

    (sent,) = fake_context.bot.sent_messages
    text = sent["text"]
    assert "<b>TOR 1:3 NYR</b>\n<i>Периоды: 1:0, 0:1, 0:2</i>" in text
    # Форма до матча: у TOR это первая игра сезона (0-0-0, не «—» и не
    # засчитанное поражение в этой же игре), у NYR — одна победа до неё.
    assert "<i>Форма (5 игр, W-L-OTL)</i>:\nTOR 0-0-0 · NYR 1-0-0" in text


@pytest.mark.asyncio
async def test_today_on_partial_season_renders_the_single_game_card(
    bot_module, fake_db_router, make_message_update, fake_context, monkeypatch
):
    bot = bot_module("bot")
    fake_db_router(_PARTIAL_DIGEST_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: "2026-10-03")
    update = make_message_update("/today")

    await bot.cmd_today(update, fake_context)

    sent, hint = fake_context.bot.sent_messages
    assert "<b>TOR 1:3 NYR</b>" in sent["text"]
    assert "/stats" in hint["text"]


# ---------------------------------------------------------------------------
# Сценарий «сводки по конференциям/дивизионам»: подменю команд → кнопка
# «По конференциям»/«По дивизионам» (Задача 41, Фаза B)
# ---------------------------------------------------------------------------

# conference_name, team_count, avg_goals_per_game, avg_power_play_percentage,
# avg_penalty_kill_percentage, avg_points
_CONFERENCE_ROWS = [
    ("Eastern", 16, 3.12, 21.5, 79.5, 55.25),
    ("Western", 16, 2.98, 19.9, 80.25, 50.1),
]

# conference_name, division_name, team_count, avg_goals_per_game,
# avg_power_play_percentage, avg_penalty_kill_percentage, avg_points
_DIVISION_ROWS = [
    # Значения конца сезона: с двумя знаками строка была бы 37 символов.
    ("Eastern", "Metropolitan", 8, 3.0812, 21.4567, 79.8811, 91.375),
]


@pytest.mark.asyncio
async def test_team_submenu_conference_button_renders_summary_and_back_to_team_stats(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router([("FROM teams_stats ts", _CONFERENCE_ROWS)])
    update = make_callback_update(str(dialog_states.TEAM_CONFERENCE_STATS))

    state = await stats_handlers.bot_team_conference_stats(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    text = edited["text"]
    assert state == dialog_states.SECOND
    assert edited["parse_mode"] == "HTML"
    assert "<b>Сводка по конференциям</b>" in text
    # Выровненная таблица в ширину телефона, легенда сокращений под ней.
    assert (
        "<pre>       |Ком|   О|Г/и|  Бол|  Мен\n"
        "Eastern| 16|55.2|3.1|21.5%|79.5%\n"
        "Western| 16|50.1|3.0|19.9%|80.2%</pre>"
    ) in text
    assert "Бол — реализация большинства" in text
    # «« Назад»» ведёт на родительское подменю команд, как у TEAM_PROCENT_WINS и соседей.
    assert _callback_data(edited["reply_markup"]) == [
        str(dialog_states.TEAM_STATS),
        str(dialog_states.CHOOSE_STATS),
        str(dialog_states.END_CONVERSATION),
    ]


@pytest.mark.asyncio
async def test_team_submenu_division_button_renders_summary_table(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router([("FROM teams_stats ts", _DIVISION_ROWS)])
    update = make_callback_update(str(dialog_states.TEAM_DIVISION_STATS))

    state = await stats_handlers.bot_team_division_stats(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    text = edited["text"]
    assert state == dialog_states.SECOND
    assert "<b>Сводка по дивизионам</b>" in text
    # Шире экрана телефона с числом команд — оно уходит, средние остаются.
    table = "            |   О|Г/и|  Бол|  Мен\nMetropolitan|91.4|3.1|21.5%|79.9%"
    assert f"<pre>{table}</pre>" in text
    assert all(len(line) <= 36 for line in table.splitlines())
    assert _callback_data(edited["reply_markup"])[0] == str(dialog_states.TEAM_STATS)


@pytest.mark.asyncio
async def test_team_submenu_conference_button_reports_empty_season_without_traceback(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Пустой сезон (Задача 36): текст с причиной, не пустая таблица и не исключение,
    и «« Назад»» на подменю команд по-прежнему на месте."""
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router([("FROM teams_stats ts", [])])
    update = make_callback_update(str(dialog_states.TEAM_CONFERENCE_STATS))

    state = await stats_handlers.bot_team_conference_stats(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert state == dialog_states.SECOND
    assert "В базе нет командной статистики для этого сезона." in edited["text"]
    assert _callback_data(edited["reply_markup"])[0] == str(dialog_states.TEAM_STATS)


# ---------------------------------------------------------------------------
# Сценарий «профиль команды»: подменю команд → «Профиль команды» → выбор
# аббревиатуры → карточка состава (Задача 41, Фаза D)
# ---------------------------------------------------------------------------

_SEASON_ABBREVS = [("WSH",), ("WPG",)]

# team_id, name, division_name, conference_name, games_played, wins, losses, ot,
# points, procent_points, goals_per_game, goals_against_per_game, shots_per_game,
# shots_allowed, power_play_percentage, penalty_kill_percentage,
# face_off_win_percentage — вся лига сезона: места считаются по всем строкам.
# WSH (team_id=5) — 2-я в дивизионе (Rangers впереди), 3-я в конференции
# (Rangers и Bruins впереди), 4-я в лиге из четырёх.
_LEAGUE_ROWS = [
    (5, "Washington Capitals", "Capitals", "Metropolitan", "Eastern", 17, 10, 5, 2, 22, 64.71, 3.1, 2.65, 30.5, 28, 21.5, 80, 51.25),
    (6, "New York Rangers", "Rangers", "Metropolitan", "Eastern", 20, 13, 5, 2, 28, 70.0, 3.3, 2.5, 31, 27, 22, 82, 52),
    (7, "Boston Bruins", "Bruins", "Atlantic", "Eastern", 20, 12, 6, 1, 25, 62.5, 3.0, 2.9, 29, 30, 20, 79, 49),
    (8, "Los Angeles Kings", "Kings", "Pacific", "Western", 20, 14, 4, 2, 30, 75.0, 3.4, 2.4, 32, 26, 23, 83, 50),
]

# lastname, position, games, goals, assists, points, plus_minus,
# time_on_ice_per_game — уже отсортировано БД (по очкам), порядок строк бот
# сохраняет как есть. position — однобуквенный NHL API positionCode
# (C/L/R/D/G), как пишет загрузчик.
_SCORER_ROWS = [
    ("Ovechkin", "L", 17, 38, 4, 42, 12, "19:05"),
    ("Backstrom", "C", 17, 10, 20, 30, -3, "17:40"),
    ("Carlson", "D", 16, 5, 20, 25, 4, "24:31"),
]
# Те же игроки в порядке среднего времени на льду (сортировка по `split_part`).
_TOI_ROWS = [_SCORER_ROWS[2], _SCORER_ROWS[0], _SCORER_ROWS[1]]

# lastname, games, wins, losses, ot, save_percentage, goal_against_average
_GOALIE_ROWS = [("Samsonov", 15, 9, 4, 2, 91.5, 2.6)]

# winner_id, is_overtime, is_shootouts, ot_empty_net_win — свежие игры первыми:
# три победы WSH подряд, затем два поражения.
_WSH_FORM = [(5, False, False, False)] * 3 + [(9, False, False, False)] * 2

_PROFILE_ROUTES = [
    ("SELECT team_id FROM teams WHERE season_id", [(5,)]),
    ("FROM teams_stats ts", _LEAGUE_ROWS),
    ("ORDER BY (split_part", _TOI_ROWS),
    ("JOIN players_season_stats pss", _SCORER_ROWS),
    ("JOIN goalies_season_stats gs", _GOALIE_ROWS),
    ("ORDER BY day DESC NULLS LAST", _WSH_FORM),
]


@pytest.mark.asyncio
async def test_team_submenu_profile_button_lists_season_abbreviations(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router([("SELECT DISTINCT trim(COALESCE(NULLIF(trim(abbreviation)", _SEASON_ABBREVS)])
    update = make_callback_update(str(dialog_states.TEAM_PROFILE_PICK))

    state = await stats_handlers.bot_team_profile_pick(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert state == dialog_states.SECOND
    assert edited["text"] == "Выберите команду:"
    callbacks = _callback_data(edited["reply_markup"])
    assert callbacks[:2] == ["tp:WSH", "tp:WPG"]
    # Футер — «« Назад»» на подменю команд, как у соседних командных экранов.
    assert callbacks[2:] == [
        str(dialog_states.TEAM_STATS),
        str(dialog_states.CHOOSE_STATS),
        str(dialog_states.END_CONVERSATION),
    ]


@pytest.mark.asyncio
async def test_team_submenu_profile_button_reports_empty_season_without_traceback(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Пустой сезон (Задача 36): нет команд в базе — текст с причиной, не
    клавиатура без единой кнопки, и без исключения."""
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router([("SELECT DISTINCT trim(COALESCE(NULLIF(trim(abbreviation)", [])])
    update = make_callback_update(str(dialog_states.TEAM_PROFILE_PICK))

    state = await stats_handlers.bot_team_profile_pick(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert state == dialog_states.SECOND
    assert edited["text"] == "В базе нет команд для этого сезона."
    assert _callback_data(edited["reply_markup"])[0] == str(dialog_states.TEAM_STATS)


@pytest.mark.asyncio
async def test_team_profile_abbreviation_button_shows_places_scorers_and_goalies(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Выбор аббревиатуры на экране профиля: места в дивизионе/конференции/лиге,
    баланс, форма и серия, спецбригады, топ бомбардиров и вратарь; «« Назад»» —
    на список команд (TEAM_PROFILE_PICK), не сразу в подменю команд."""
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router(_PROFILE_ROUTES)
    update = make_callback_update("tp:WSH")

    state = await stats_handlers.bot_team_profile_show(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    text = edited["text"]
    assert state == dialog_states.SECOND
    assert edited["parse_mode"] == "HTML"
    assert "<b>Washington Capitals</b> (WSH) · сезон" in text
    assert "Metropolitan: 2-е место · Eastern: 3-е · лига: 4-е из 4 сыгравших" in text
    assert "Игр: 17 · 10-5-2, 22 очка (64.71%)" in text
    assert "Форма (5 игр, W-L-OTL): 3-2-0 · серия W3" in text
    assert "Голы за игру: 3.1 забито / 2.65 пропущено" in text
    assert "Большинство: 21.5% · Меньшинство: 80% · Вбрасывания: 51.25%" in text
    # Бомбардиры — в порядке выдачи БД (по очкам), колонки выровнены в <pre>.
    assert (
        "<b>Бомбардиры</b>\n<pre>  |Игрок        | И| Г| П| О\n"
        "1.|Ovechkin [L] |17|38| 4|42\n"
        "2.|Backstrom [C]|17|10|20|30\n"
        "3.|Carlson [D]  |16| 5|20|25</pre>"
    ) in text
    assert (
        "<b>Игровое время</b>\n<pre>  |Игрок        | И|   ВП\n"
        "1.|Carlson [D]  |16|24:31\n"
        "2.|Ovechkin [L] |17|19:05\n"
        "3.|Backstrom [C]|17|17:40</pre>"
    ) in text
    assert (
        "<b>Вратари</b>\n<pre>Вратарь | И|В-П-ОТ|  %ОБ| КН\n"
        "Samsonov|15| 9-4-2|91.5%|2.6</pre>"
    ) in text
    assert "Все игроки: /WSH_FULL" in text
    assert _callback_data(edited["reply_markup"]) == [
        str(dialog_states.TEAM_PROFILE_PICK),
        str(dialog_states.CHOOSE_STATS),
        str(dialog_states.END_CONVERSATION),
    ]


@pytest.mark.asyncio
async def test_team_profile_reports_team_that_has_not_played_yet(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Команда есть в `teams`, но без строки в `teams_stats` (ещё не сыграла,
    Задача 36) — текст с причиной, не пустая карточка и не исключение."""
    stats_handlers = bot_module("stats_handlers")
    fake_db_router([
        ("SELECT team_id FROM teams WHERE season_id", [(99,)]),
        ("FROM teams_stats ts", _LEAGUE_ROWS),
    ])
    update = make_callback_update("tp:WSH")

    await stats_handlers.bot_team_profile_show(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "Команда ещё не сыграла в этом сезоне — статистики нет." in edited["text"]


@pytest.mark.asyncio
async def test_team_command_lists_teams_by_division_as_clickable_commands(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router([(
        "ORDER BY division_name, name",
        [("WSH", "Washington Capitals", "Metropolitan"), ("BOS", "Boston Bruins", "Atlantic")],
    )])
    update = make_message_update("/team")

    await bot.cmd_team(update, fake_context)

    (reply,) = update.message.replies
    assert reply["parse_mode"] == "HTML"
    assert "<b>Atlantic</b>\n/BOS — Boston Bruins" in reply["text"]
    assert "<b>Metropolitan</b>\n/WSH — Washington Capitals" in reply["text"]


@pytest.mark.asyncio
async def test_team_abbreviation_command_replies_with_profile_and_link_to_team_list(
    bot_module, fake_db_router, make_message_update, fake_context
):
    """`/wsh`: аббревиатуру берёт regex-хендлер (`context.matches`), в
    профиль она уходит в верхнем регистре; в конце — ссылка на список команд."""
    bot = bot_module("bot")
    cursor = fake_db_router(_PROFILE_ROUTES)
    update = make_message_update("/wsh")
    fake_context.matches = [re.match(bot.TEAM_COMMAND_PATTERN, "/wsh")]

    await bot.cmd_team_profile(update, fake_context)

    (reply,) = update.message.replies
    assert reply["parse_mode"] == "HTML"
    assert "<b>Washington Capitals</b> (WSH)" in reply["text"]
    assert reply["text"].endswith("\n\nВсе команды: /team")
    team_id_query = next(q for q in cursor.executed if "SELECT team_id FROM teams" in q[0])
    assert "WSH" in team_id_query[1]


@pytest.mark.asyncio
async def test_team_full_command_lists_every_skater_and_goalie(
    bot_module, fake_db_router, make_message_update, fake_context
):
    """`/WSH_FULL` из конца профиля: тот же regex-хендлер; нападающие,
    защитники и вратари отдельными таблицами с колонками экрана страны, полный
    список без LIMIT (параметр `None`); ссылка назад на профиль."""
    bot = bot_module("bot")
    cursor = fake_db_router([
        ("SELECT team_id FROM teams WHERE season_id", [(5,)]),
        # lastname, team, goals, points, games, toi, hits, shots, blocked, total
        ("r.position IN ('C', 'L', 'R')", [
            ("Ovechkin", "WSH", 38, 42, 17, "19:05", 30, 120, 5, 2),
            ("Backstrom", "WSH", 10, 30, 17, "17:40", 8, 40, 9, 2),
        ]),
        ("r.position = 'D'", [("Carlson", "WSH", 5, 25, 16, "24:31", 20, 50, 33, 1)]),
        # lastname, team, wins, save_pct, gaa, games, shutouts, total
        ("FROM goalies_season_stats g", [("Samsonov", "WSH", 9, 91.5, 2.6, 15, 1, 1)]),
    ])
    update = make_message_update("/WSH_FULL")
    fake_context.matches = [re.match(bot.TEAM_COMMAND_PATTERN, "/WSH_FULL")]

    await bot.cmd_team_profile(update, fake_context)

    (reply,) = update.message.replies
    text = reply["text"]
    assert "<b>WSH: все игроки</b>" in text
    # Без колонок места и команды все показатели страны влезают в ширину телефона.
    assert (
        "<b>Нападающие</b>\n<pre>Игрок    | Г| О| И|   ВП|Хит| Бр\n"
        "Ovechkin |38|42|17|19:05| 30|120\n"
        "Backstrom|10|30|17|17:40|  8| 40</pre>\n"
        "<i>Г — голы, О — очки, И — игры, ВП — время на льду за игру, Хит — силовые, "
        "Бр — броски.</i>"
    ) in text
    assert (
        "<b>Защитники</b>\n<pre>Игрок  |Г| О| И|   ВП|Хит|Бл\n"
        "Carlson|5|25|16|24:31| 20|33</pre>"
    ) in text
    assert (
        "<b>Вратари</b>\n<pre>Вратарь |В|  %ОБ| КН| И|Сух\n"
        "Samsonov|9|91.5%|2.6|15|  1</pre>"
    ) in text
    assert text.index("Нападающие") < text.index("Защитники") < text.index("Вратари")
    assert "Профиль клуба: /WSH" in text
    group_queries = [q for q in cursor.executed if "r.current_team_id = %s" in q[0]]
    assert len(group_queries) == 3
    # Игроки клуба — по team_id параметром, полный список — без LIMIT.
    assert all(params[1:3] == (5, None) for _sql, params in group_queries)
    goalie_sql = next(q for q, _ in group_queries if "goalies_season_stats" in q)
    assert "g.games > 0" in goalie_sql, "вратарь без игр в списке клуба не нужен"


@pytest.mark.asyncio
async def test_team_full_command_skips_empty_groups(
    bot_module, fake_db_router, make_message_update, fake_context
):
    """Группа без игроков (у клуба ещё не сыграл ни один вратарь) — без
    заголовка и пустой таблицы; совсем пустой клуб — текст с причиной."""
    bot = bot_module("bot")
    fake_db_router([
        ("SELECT team_id FROM teams WHERE season_id", [(5,)]),
        ("r.position IN ('C', 'L', 'R')", [("Ovechkin", "WSH", 38, 42, 17, "19:05", 30, 120, 5, 1)]),
        ("r.position = 'D'", []),
        ("FROM goalies_season_stats g", []),
    ])
    update = make_message_update("/WSH_FULL")
    fake_context.matches = [re.match(bot.TEAM_COMMAND_PATTERN, "/WSH_FULL")]

    await bot.cmd_team_profile(update, fake_context)

    text = update.message.replies[0]["text"]
    assert "<b>Нападающие</b>" in text
    assert "Защитники" not in text and "Вратари" not in text

    fake_db_router([
        ("SELECT team_id FROM teams WHERE season_id", [(5,)]),
        ("r.position IN ('C', 'L', 'R')", []),
        ("r.position = 'D'", []),
        ("FROM goalies_season_stats g", []),
    ])
    update = make_message_update("/WSH_FULL")
    await bot.cmd_team_profile(update, fake_context)
    assert "У команды пока нет статистики игроков в этом сезоне." in update.message.replies[0]["text"]


@pytest.mark.asyncio
async def test_team_profile_reports_unknown_abbreviation(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Аббревиатура прошла TEAM_PROFILE_CALLBACK_PATTERN (кнопка), но не
    резолвится в team_id (не найдена в teams для сезона) — текст с причиной,
    «« Назад»» на список команд по-прежнему на месте."""
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    fake_db_router([("SELECT team_id FROM teams WHERE season_id", [])])
    update = make_callback_update("tp:ZZZ")

    state = await stats_handlers.bot_team_profile_show(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert state == dialog_states.SECOND
    assert "Команда не найдена в базе для этого сезона." in edited["text"]
    assert _callback_data(edited["reply_markup"])[0] == str(dialog_states.TEAM_PROFILE_PICK)


# ---------------------------------------------------------------------------
# Сценарий «по странам»: /countries → рейтинг → страна → группа игроков
# (Задача 49)
# ---------------------------------------------------------------------------

# code, players, points, goals — рейтинг стран, прошедших порог
_COUNTRY_RANKING_ROWS = [("CAN", 5, 100, 40), ("USA", 3, 60, 20)]
_COUNTRY_ROUTES = [
    ("r.nationality IS NULL", [(2,)]),
    ("HAVING COUNT", _COUNTRY_RANKING_ROWS),
    # lastname, team, wins, save_pct, gaa, games, shutouts, total
    ("FROM goalies_season_stats g", [("Hellebuyck", "WPG", 35, 92.1, 2.4, 60, 5, 1)]),
    # lastname, team, goals, points, games, toi, hits, shots, blocked, total; total=25
    # при LIMIT 10 — страница со смещением 10 имеет и prev, и next.
    ("COUNT(*) OVER ()", [("McDavid", "EDM", 30, 90, 70, "21:30", 40, 200, 30, 25)]),
]


@pytest.mark.asyncio
async def test_countries_command_lists_countries_with_buttons(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_COUNTRY_ROUTES)
    update = make_message_update("/countries")

    await bot.cmd_countries(update, fake_context)

    (reply,) = update.message.replies
    assert reply["parse_mode"] == "HTML"
    assert "1.|🇨🇦 Канада|  5|100|40|20.0" in reply["text"]
    assert "Страна не указана в данных NHL у игроков: 2 (не учтены)" in reply["text"]
    assert [(b.text, b.callback_data) for b in _flat_buttons(reply["reply_markup"])] == [
        ("🇨🇦 Канада", "cn:CAN:F:0"),
        ("🇺🇸 США", "cn:USA:F:0"),
    ]


@pytest.mark.asyncio
async def test_countries_command_reports_empty_season_without_buttons(
    bot_module, fake_db_router, make_message_update, fake_context
):
    """Пустой сезон (Задача 36): текст с причиной, без кнопок стран."""
    bot = bot_module("bot")
    fake_db_router([("HAVING COUNT", [])])
    update = make_message_update("/countries")

    await bot.cmd_countries(update, fake_context)

    (reply,) = update.message.replies
    assert "В базе нет данных по странам для этого сезона." in reply["text"]
    assert _flat_buttons(reply["reply_markup"]) == []


@pytest.mark.asyncio
async def test_country_page_pages_and_switches_groups(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router(_COUNTRY_ROUTES)
    update = make_callback_update("cn:CAN:D:10")

    await stats_handlers.callback_country(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "<b>Защитники: 🇨🇦 Канада</b>" in edited["text"]
    # С блоками строка 37 символов — они уходят первыми, игры остаются.
    assert "11.|McDavid|EDM|30|90|70|21:30| 40</pre>" in edited["text"]
    buttons = _flat_buttons(edited["reply_markup"])
    assert [(b.text, b.callback_data) for b in buttons] == [
        ("← 1–10", "cn:CAN:D:0"),
        ("21–30 →", "cn:CAN:D:20"),
        ("Нападающие", "cn:CAN:F:0"),
        ("• Защитники", "cn:CAN:D:0"),
        ("Вратари", "cn:CAN:G:0"),
        ("« Страны", "cn:list"),
    ]
    # Код страны дошёл до БД только параметром, смещение — из callback_data.
    assert cursor.executed[-1][1] == (bot_module("config").SEASON_ID, "CAN", 10, 10)


@pytest.mark.asyncio
async def test_country_page_rejects_code_outside_ranking(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    """Код прошёл паттерн, но не в рейтинге сезона — текст, не исключение и не
    запрос по стране; «« Страны»» на рейтинг."""
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router([("HAVING COUNT", _COUNTRY_RANKING_ROWS)])
    update = make_callback_update("cn:ZZZ:F:0")

    await stats_handlers.callback_country(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "Страна не найдена в рейтинге этого сезона." in edited["text"]
    assert "cn:list" in _callback_data(edited["reply_markup"])
    assert len(cursor.executed) == 1


@pytest.mark.asyncio
async def test_country_goalies_and_back_to_rankings(
    bot_module, fake_db_router, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_COUNTRY_ROUTES)
    update = make_callback_update("cn:CAN:G:0")

    await stats_handlers.callback_country(update, fake_context)

    (edited,) = update.callback_query.edited_texts
    assert "1.|Hellebuyck|WPG|35|92.1%|2.4|60</pre>" in edited["text"]

    back = make_callback_update("cn:list")
    await stats_handlers.callback_country(back, fake_context)
    (edited,) = back.callback_query.edited_texts
    assert "<b>Рейтинг стран</b>" in edited["text"]
    assert _callback_data(edited["reply_markup"]) == ["cn:CAN:F:0", "cn:USA:F:0"]


# ---------------------------------------------------------------------------
# Сценарий «время дайджеста»: /digest_time → кнопка → сохранение (Задача 60)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_time_buttons_save_the_chosen_time(
    bot_module, make_message_update, make_callback_update, monkeypatch
):
    from datetime import time
    from types import SimpleNamespace

    bot = bot_module("bot")
    repo = bot_module("subscription_repo")
    saved = []
    monkeypatch.setattr(repo, "get_digest_time", lambda chat_id: time(11, 0))
    monkeypatch.setattr(repo, "set_digest_time", lambda *args: saved.append(args) or True)

    update = make_message_update("/digest_time", chat_id=42)
    update.effective_chat = SimpleNamespace(id=42)
    await bot.cmd_digest_time(update, SimpleNamespace(args=[]))

    [reply] = update.message.replies
    assert "11:00 МСК" in reply["text"]
    labels = [b.text for b in _flat_buttons(reply["reply_markup"])]
    assert labels[0] == "05:00" and labels[-1] == "• 11:00" and len(labels) == 13
    data = _callback_data(reply["reply_markup"])
    assert all(re.match(bot.DIGEST_TIME_CALLBACK_PATTERN, d) for d in data)

    press = make_callback_update(data[5], chat_id=42)
    press.effective_chat = SimpleNamespace(id=42)
    await bot.callback_digest_time(press, SimpleNamespace())

    assert saved == [(42, time(7, 30))]
    [edited] = press.callback_query.edited_texts
    assert edited["text"].startswith("Дайджест придёт в 07:30 МСК")


@pytest.mark.asyncio
async def test_digest_time_rejects_time_outside_choices(bot_module, make_callback_update):
    """Позже 11:00 прогонов sync нет — такое время из подделанной кнопки не сохраняется."""
    bot = bot_module("bot")
    press = make_callback_update("dt:15:00")
    with pytest.raises(ValueError, match="outside of choices"):
        await bot.callback_digest_time(press, None)


@pytest.mark.asyncio
async def test_digest_time_without_subscription_points_to_subscribe(
    bot_module, make_message_update, monkeypatch
):
    from types import SimpleNamespace

    bot = bot_module("bot")
    monkeypatch.setattr(bot_module("subscription_repo"), "get_digest_time", lambda chat_id: None)
    update = make_message_update("/digest_time")
    update.effective_chat = SimpleNamespace(id=100)
    await bot.cmd_digest_time(update, SimpleNamespace(args=[]))

    [reply] = update.message.replies
    assert "/subscribe_digest" in reply["text"]
    assert "reply_markup" not in reply


# ---------------------------------------------------------------------------
# Сценарий «подписка на страну»: кнопки стран, время, отписка (Задача 61)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_country_buttons_subscribe_and_report_delivery_time(
    bot_module, make_message_update, make_callback_update, monkeypatch
):
    from datetime import time
    from types import SimpleNamespace

    bot = bot_module("bot")
    repo = bot_module("subscription_repo")
    saved = []
    monkeypatch.setattr(bot, "country_rankings", lambda: ("", ["CAN", "RUS"]))
    monkeypatch.setattr(repo, "upsert_country", lambda *args: saved.append(args) or time(9, 0))

    update = make_message_update("/subscribe_country", chat_id=42)
    await bot.cmd_subscribe_country(update, SimpleNamespace())
    [reply] = update.message.replies
    assert _callback_data(reply["reply_markup"]) == ["sc:CAN", "sc:RUS"]
    assert [b.text for b in _flat_buttons(reply["reply_markup"])] == ["🇨🇦 Канада", "🇷🇺 Россия"]

    press = make_callback_update("sc:RUS", chat_id=42)
    press.effective_chat = SimpleNamespace(id=42)
    await bot.callback_country_pick(press, SimpleNamespace())

    assert saved == [(42, "RUS")]
    [edited] = press.callback_query.edited_texts
    assert edited["text"].startswith("Вы подписаны на 🇷🇺 Россия")
    assert "09:00 МСК" in edited["text"] and "/country_time" in edited["text"]

    # Код вне рейтинга (устаревшая кнопка) не подписывает.
    stale = make_callback_update("sc:ZZZ", chat_id=42)
    stale.effective_chat = SimpleNamespace(id=42)
    await bot.callback_country_pick(stale, SimpleNamespace())
    assert saved == [(42, "RUS")]
    assert stale.callback_query.edited_texts[0]["text"] == "Страна не найдена в рейтинге этого сезона."


@pytest.mark.asyncio
async def test_unsubscribe_country_offers_only_subscribed_countries(
    bot_module, make_message_update, make_callback_update, monkeypatch
):
    from types import SimpleNamespace

    bot = bot_module("bot")
    repo = bot_module("subscription_repo")
    dropped = []
    chat_countries = {42: ["FIN", "RUS"], 43: []}
    monkeypatch.setattr(repo, "list_chat_countries", lambda chat_id: chat_countries[chat_id])
    monkeypatch.setattr(repo, "mark_subscription_inactive_by_chat_kind_team",
                        lambda *args: dropped.append(args))

    update = make_message_update("/unsubscribe_country", chat_id=42)
    update.effective_chat = SimpleNamespace(id=42)
    await bot.cmd_unsubscribe_country(update, SimpleNamespace())
    [reply] = update.message.replies
    assert _callback_data(reply["reply_markup"]) == ["uc:FIN", "uc:RUS"]

    press = make_callback_update("uc:FIN", chat_id=42)
    press.effective_chat = SimpleNamespace(id=42)
    await bot.callback_country_pick(press, SimpleNamespace())
    assert dropped == [(42, "country_players", None, "FIN")]
    assert press.callback_query.edited_texts[0]["text"] == "Подписка на 🇫🇮 Финляндия отключена."

    empty = make_message_update("/unsubscribe_country", chat_id=43)
    empty.effective_chat = SimpleNamespace(id=43)
    await bot.cmd_unsubscribe_country(empty, SimpleNamespace())
    [hint] = empty.message.replies
    assert "/subscribe_country" in hint["text"] and "reply_markup" not in hint


@pytest.mark.asyncio
async def test_country_time_buttons_save_the_chosen_time_for_all_countries(
    bot_module, make_message_update, make_callback_update, monkeypatch
):
    from datetime import time
    from types import SimpleNamespace

    bot = bot_module("bot")
    repo = bot_module("subscription_repo")
    saved = []
    monkeypatch.setattr(repo, "get_country_time", lambda chat_id: time(11, 0))
    monkeypatch.setattr(repo, "set_country_time", lambda *args: saved.append(args) or True)

    update = make_message_update("/country_time", chat_id=42)
    update.effective_chat = SimpleNamespace(id=42)
    await bot.cmd_country_time(update, SimpleNamespace())
    [reply] = update.message.replies
    data = _callback_data(reply["reply_markup"])
    assert all(re.match(bot.COUNTRY_TIME_CALLBACK_PATTERN, d) for d in data)
    assert [b.text for b in _flat_buttons(reply["reply_markup"])][-1] == "• 11:00"

    press = make_callback_update(data[5], chat_id=42)
    press.effective_chat = SimpleNamespace(id=42)
    await bot.callback_country_time(press, SimpleNamespace())
    assert saved == [(42, time(7, 30))]
    assert "07:30 МСК" in press.callback_query.edited_texts[0]["text"]
