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
# Сценарий «таблица»: /table
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
async def test_table_command_renders_divisions_with_rows_sorted_by_points(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_TABLE_ROUTES)
    update = make_message_update("/table")

    await bot.cmd_table(update, fake_context)

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
    # Колонки моноширинной таблицы: имя дополнено до 14, очки/игры до 3, %очк до 6.
    assert "Rangers         28  20  70.00" in text
    assert "reply_markup" not in reply, "у /table вне диалога клавиатуры нет"


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

# lastname, position, value, team — 25 игроков, три страницы по 10.
_LEADERS = [(f"Player{i:02d}", "C", 100 - i, "NYR") for i in range(1, 26)]


def _leaders_page(params):
    """Ответ на запрос страницы лидеров: срез по LIMIT/OFFSET плюс COUNT(*) OVER ()."""
    _season_id, limit, offset = params
    return [
        (lastname, position, value, team, len(_LEADERS))
        for lastname, position, value, team in _LEADERS[offset:offset + limit]
    ]


_LEADERS_ROUTES = [("COUNT(*) OVER () AS total", _leaders_page)]


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
    assert "1. Player01 [C] — 99 (NYR)" in text
    assert "10. Player10 [C] — 90 (NYR)" in text
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
    assert "11. Player11 [C] — 89 (NYR)" in text
    assert "20. Player20 [C] — 80 (NYR)" in text
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
# field, team_name — первая строка домашняя, вторая гостевая.
# get_goals_game: scorer, scorer_position, assist_1, assist_2, period, goal_time,
# home_score, away_score, is_ppg, is_shg, empty_net, winner_goal, game_id, event_id.
# get_goalies_game: shots, saves, timeonice, lastname, save_percentage, is_home.
_GAMES = {
    GAME_ONE: {
        "stats": [
            (3, 8, 12, 20, 31, False, False, "home", "NYR"),
            (2, 6, 10, 18, 27, False, False, "away", "BOS"),
        ],
        "teams": [(1, 2)],
        "goals": [
            ("Panarin", "LW", "Fox", None, 1, "05:12", 1, 0, True, False, False, False, GAME_ONE, 101),
            ("Marchand", "LW", None, None, 2, "11:40", 1, 1, False, False, False, False, GAME_ONE, 102),
            ("Zibanejad", "C", "Panarin", "Fox", 2, "15:20", 2, 1, False, True, False, False, GAME_ONE, 103),
            ("Pastrnak", "RW", "Marchand", None, 3, "04:10", 2, 2, False, False, False, False, GAME_ONE, 104),
            ("Panarin", "LW", None, None, 3, "18:03", 3, 2, False, False, False, True, GAME_ONE, 105),
        ],
        "goalies": [
            (27, 25, "60:00", "Shesterkin", 92.59, True),
            (31, 28, "60:00", "Swayman", 90.32, False),
        ],
    },
    GAME_TWO: {
        "stats": [
            (1, 4, 9, 15, 24, False, False, "home", "TOR"),
            (0, 2, 11, 19, 30, False, False, "away", "MTL"),
        ],
        "teams": [(11, 12)],
        "goals": [
            ("Matthews", "C", "Nylander", None, 1, "12:00", 1, 0, False, False, False, True, GAME_TWO, 201),
        ],
        "goalies": [
            (30, 30, "60:00", "Woll", 100.0, True),
            (24, 23, "60:00", "Montembeault", 95.83, False),
        ],
    },
}

# Выборка формы: winner_id, home_team_id, away_team_id, is_overtime,
# is_shootouts. Запрос уходит по одному разу на команду и фильтруется по её
# team_id (`WHERE home_team_id = %s OR away_team_id = %s`), поэтому маршрут
# отвечает по team_id из параметров: иначе команде засчитывались бы игры,
# которых она не играла.
_FORM_BY_TEAM = {
    1: [  # NYR — 3-1-1
        (1, 1, 3, False, False),
        (1, 1, 4, False, False),
        (1, 5, 1, False, False),
        (6, 1, 6, False, False),
        (7, 7, 1, True, False),
    ],
    2: [  # BOS — 1-3-1
        (2, 2, 8, False, False),
        (9, 2, 9, False, False),
        (10, 10, 2, False, False),
        (11, 2, 11, False, False),
        (12, 12, 2, False, True),
    ],
    11: [(11, 11, 13, False, False)],  # TOR — 1-0-0
    12: [(14, 14, 12, False, False)],  # MTL — 0-1-0
}


def _form_rows(params):
    _season_id, team_id, _same_team_id, _limit = params
    return _FORM_BY_TEAM[team_id]


def _by_game(key):
    """Маршрут, отвечающий данными того матча, чей game_id пришёл в параметрах."""
    def rows(params):
        return _GAMES[params[0]][key]

    return rows


_GAME_CARD_ROUTES = [
    ("SELECT 1 AS o FROM games", [(1,)]),
    ("SELECT * FROM get_game_stats", _by_game("stats")),
    ("SELECT home_team_id, away_team_id FROM games", _by_game("teams")),
    ("SELECT * FROM get_goals_game", _by_game("goals")),
    ("SELECT * FROM get_goalies_game", _by_game("goalies")),
    ("ORDER BY day DESC NULLS LAST", _form_rows),
]

_DIGEST_ROUTES = [
    ("SELECT max(day) AS day FROM games", [("2026-04-01",)]),
    ("SELECT DISTINCT game_id FROM games", [(GAME_ONE,), (GAME_TWO,)]),
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
    # Шесть разных запросов, семь обращений: форма спрашивается на каждую команду.
    assert len(cursor.executed) == 7
    assert sent["parse_mode"] == "HTML"
    assert "<b>NYR BOS 3:2</b> (1:0, 1:1, 1:1)" in text
    assert "<b>BOS</b> 1-3-1 — <b>NYR</b> 3-1-1" in text
    assert "1:0 Panarin [LW](Fox) (ББ) P1 5:12" in text
    assert "2:1 Zibanejad [C](Panarin, Fox) (МБ) P2 35:20" in text
    assert "3:2 Panarin [LW] ★ P3 58:03" in text
    assert "<b>Броски</b>: 31 - 27" in text
    assert "<b>Штрафное время</b>: 8 - 6" in text
    assert (
        "<b>Вратари</b>: Shesterkin (25/27, 92.59%, 60:00) - "
        "Swayman (28/31, 90.32%, 60:00)"
    ) in text


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
    assert buttons[0].text == "1:0 Panarin(Fox) 5:12"


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
# Сценарий «дайджест дня»: /day_games → разворот матча кнопкой
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_day_games_summarises_both_matches_with_expand_buttons(
    bot_module, fake_db_router, make_message_update, fake_context
):
    bot = bot_module("bot")
    fake_db_router(_DIGEST_ROUTES)
    update = make_message_update("/day_games")

    await bot.cmd_day_games(update, fake_context)

    summary, hint = fake_context.bot.sent_messages
    assert "<b>Матчи 2026-04-01</b> (2 игр)" in summary["text"]
    assert "<b>NYR BOS 3:2</b> (1:0, 1:1, 1:1)" in summary["text"]
    assert "<b>TOR MTL 1:0</b> (1:0, 0:0, 0:0)" in summary["text"]
    assert "Нажмите «Матч N»" in summary["text"]
    assert _callback_data(summary["reply_markup"]) == [f"dg:{GAME_ONE}", f"dg:{GAME_TWO}"]
    assert "/stats" in hint["text"]


@pytest.mark.asyncio
async def test_digest_expand_button_opens_the_full_card_of_that_match(
    bot_module, fake_db_router, make_message_update, make_callback_update, fake_context
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_DIGEST_ROUTES)

    digest = make_message_update("/day_games")
    await bot.cmd_day_games(digest, fake_context)
    second_match = _flat_buttons(fake_context.bot.sent_messages[0]["reply_markup"])[1]
    assert second_match.callback_data.startswith(stats_handlers.DIGEST_EXPAND_PREFIX)

    update = make_callback_update(second_match.callback_data)
    await stats_handlers.callback_expand_digest_game(update, fake_context)

    card = fake_context.bot.sent_messages[-1]
    assert "<b>TOR MTL 1:0</b>" in card["text"]
    assert "1:0 Matthews [C](Nylander) ★ P1 12:00" in card["text"]
    assert _callback_data(card["reply_markup"]) == [f"gv:{GAME_TWO}:201"]
