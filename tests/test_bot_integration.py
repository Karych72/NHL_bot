"""Сквозные сценарии «запрос → ответ» для основных веток бота (Задача 7).

Остальные tests/test_bot_*.py рвут стек посередине: тесты хендлеров подменяют
функции ``bot_messages``, тесты сообщений — ``fetch_all``. Здесь подменяется
только граница БД (фикстура ``fake_db_router``: соединение psycopg2 и TTL-обёртка
над ним) и граница Telegram (``fake_bot`` на ``FakeSession``, который записывает
запросы Bot API), а всё между хендлером и этими границами работает по-настоящему:
разбор callback_data, композиция SQL, маппинг строк на колонки, jinja-шаблоны,
сборка клавиатур, сериализация запросов aiogram. Postgres не нужен, сети нет.

Кнопку для следующего шага сценарий берёт из клавиатуры, которую вернул
предыдущий шаг, и сверяет её callback_data с паттерном регистрации хендлера —
так проверяется, что нажатие кнопки действительно попадёт в тот хендлер,
которому её адресуют. Паттерн для хендлера с `match` тест собирает так же, как
aiogram при маршрутизации: `re.match(<PATTERN>, callback_data)`.
"""
from __future__ import annotations

import re
from typing import Any, List

import pytest
from aiogram.filters import CommandObject

from tests._telegram_fakes import body, rich_html


def _tr(align: str, *cells: str) -> str:
    """Ожидаемая строка тела rich-таблицы: ячейки `cells`, выравнивание — по символу
    `align` на колонку (`r` — `align="right"`, `l` — без атрибута)."""
    return "<tr>" + "".join(
        f'<td align="right">{c}</td>' if a == "r" else f"<td>{c}</td>"
        for c, a in zip(cells, align)
    ) + "</tr>"


def _flat_buttons(markup: Any) -> List[Any]:
    """Кнопки клавиатуры одним списком, без разбиения по рядам."""
    return [button for row in markup.inline_keyboard for button in row]


def _callback_data(markup: Any) -> List[str]:
    return [button.callback_data for button in _flat_buttons(markup)]


def _start_command(args: str) -> CommandObject:
    """`/start <args>` так, как его разбирает aiogram для хендлера `cmd_start`."""
    return CommandObject(prefix="/", command="start", args=args)


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
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router(_TABLE_ROUTES)
    message = make_message("/standings")

    await bot.cmd_standings(message, fake_bot)

    assert api.sent_messages == [], "таблица уходит rich-сообщением, не parse_mode=HTML"
    (reply,) = api.sent_rich
    text = body(reply)
    assert "Турнирная таблица NHL" in text
    assert "данные на 2026-04-01" in text
    for title in ("EASTERN CONFERENCE", "WESTERN CONFERENCE",
                  "METROPOLITAN DIVISION", "ATLANTIC DIVISION",
                  "CENTRAL DIVISION", "PACIFIC DIVISION"):
        assert f"<p><b>{title}</b></p>" in text
    assert "<pre>" not in text
    # Строки дивизиона отсортированы по очкам, а не по порядку выдачи БД.
    rows = [
        _tr("rlrrr", "1.", "Rangers", "28", "20", "70.0"),
        _tr("rlrrr", "2.", "Devils", "22", "20", "55.0"),
        _tr("rlrrr", "3.", "Islanders", "19", "20", "47.5"),
    ]
    assert all(row in text for row in rows)
    assert [text.index(row) for row in rows] == sorted(text.index(row) for row in rows)
    assert (
        '<table compact><tr><th align="right"></th><th>Команда</th>'
        '<th align="right">Очк</th><th align="right">Игр</th><th align="right">%очк</th></tr>'
    ) in text
    assert reply.reply_markup is None, "у /standings клавиатуры нет"


# ---------------------------------------------------------------------------
# Сценарий «лидеры с пагинацией»: /stats → «Очки» → следующая страница
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


async def _press_stat(bot_module, make_callback, fake_bot, data):
    """Нажатие кнопки таблицы `/stats`/`/team_stats` с аргументами, которые подставил бы aiogram."""
    stats_handlers = bot_module("stats_handlers")
    match = re.match(stats_handlers.STAT_SCREEN_CALLBACK_PATTERN, data)
    assert match
    await stats_handlers.callback_stat_screen(make_callback(data), fake_bot, match)


async def _open_points(bot_module, make_message, make_callback, fake_bot, api):
    """/stats и первая кнопка меню — «Очки»."""
    await bot_module("bot").cmd_stats(make_message("/stats"), fake_bot)
    points_button = _flat_buttons(api.sent_rich[0].reply_markup)[0]
    assert points_button.text == "Очки"
    await _press_stat(bot_module, make_callback, fake_bot, points_button.callback_data)


@pytest.mark.asyncio
async def test_leaders_first_page_shows_ranks_one_to_ten_and_only_next_button(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    fake_db_router(_LEADERS_ROUTES)

    await _open_points(bot_module, make_message, make_callback, fake_bot, api)

    (menu,) = api.sent_rich
    assert body(menu).startswith("<p><b>Лидеры сезона</b><br>")
    (edited,) = api.edited_texts
    text = body(edited)
    # Заголовок — одна строка, сезон и маркер страницы — подвал.
    assert text.startswith("<p><b>Лучшие бомбардиры</b></p><table")
    assert text.endswith(" · <i>Показаны 1–10 из 25 строк.</i></footer>")
    assert (
        '<table compact><tr><th align="right"></th><th>Игрок</th><th>Ком</th>'
        '<th align="right">Очки</th><th align="right">И</th><th align="right">Смен</th></tr>'
    ) in text
    assert _tr("rllrrr", "1.", "Player01 [C]", "NYR", "99", "82", "1501") in text
    assert _tr("rllrrr", "10.", "Player10 [C]", "NYR", "90", "82", "1510") in text
    assert "Player11" not in text
    assert _callback_data(edited.reply_markup) == [
        "st:players_season_stats:points:10", "st:menu", "st:save",
    ], "первая страница — только «вперёд»"


@pytest.mark.asyncio
async def test_leaders_next_page_asks_db_for_offset_ten_and_renders_ranks_eleven_to_twenty(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    cursor = fake_db_router(_LEADERS_ROUTES)

    await _open_points(bot_module, make_message, make_callback, fake_bot, api)
    next_button = _flat_buttons(api.edited_texts[0].reply_markup)[0]
    await _press_stat(bot_module, make_callback, fake_bot, next_button.callback_data)

    _first, edited = api.edited_texts
    text = body(edited)
    assert "<i>Показаны 11–20 из 25 строк.</i>" in text
    assert _tr("rllrrr", "11.", "Player11 [C]", "NYR", "89", "82", "1511") in text
    assert _tr("rllrrr", "20.", "Player20 [C]", "NYR", "80", "82", "1520") in text
    assert "Player10" not in text
    assert _callback_data(edited.reply_markup)[:2] == [
        "st:players_season_stats:points:0", "st:players_season_stats:points:20",
    ]
    # Смещение не «нарисовано» в тексте, а действительно ушло в БД параметром.
    assert [params[1:] for _query, params in cursor.executed] == [(10, 0), (10, 10)]


# ---------------------------------------------------------------------------
# Сценарии «карточка игры» и «дайджест дня» — общие данные двух матчей
# ---------------------------------------------------------------------------

GAME_ONE = 2026020001
GAME_TWO = 2026020002

# get_game_stats: goals, pim, blocks, hits, shots, is_overtime, is_shootouts,
# field, team_name — первая строка домашняя, вторая гостевая. Пара
# (is_overtime=False, is_shootouts=True) — то, что лежит в БД сегодня (дефект Д1
# Задачи 28: загрузчик пишет туда `shootoutInUse`, флаг регламента сезона); на
# карточку матча она не влияет, потому что game_message читает is_shootouts
# только при is_overtime.
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

def _game_card_routes(games_by_id):
    """Шесть общих маршрутов карточки матча (`/start game_<id>`, дайджест дня) по словарю
    игр `games_by_id` (`game_id` → ответы формы `_GAMES`) — общий строитель для
    любого сценария (не копировать построчно под каждый)."""
    def by_game(key):
        def rows(params):
            return games_by_id[params[0]][key]

        return rows

    return [
        ("SELECT 1 AS o FROM games", [(1,)]),
        ("SELECT * FROM get_game_stats", by_game("stats")),
        ("SELECT * FROM get_goals_game", by_game("goals")),
        ("SELECT * FROM get_goalies_game", by_game("goalies")),
        ("SELECT * FROM get_three_stars_game", by_game("three_stars")),
        # Подписки чата — блок страны под карточкой (Задача 76); у чата сценария их нет.
        ("FROM bot_subscriptions", []),
    ]


_GAME_CARD_ROUTES = _game_card_routes(_GAMES)

# `/today` берёт игровой день не из БД, а у `bot.last_night_day()` — тесты
# фиксируют его (сам расчёт — в tests/test_bot_messages.py).
_TODAY = "2026-04-01"

_DIGEST_ROUTES = [
    ("SELECT DISTINCT game_id FROM games", [(GAME_ONE,), (GAME_TWO,)]),
    # Подписи кнопок: game_id, хозяева, гости — одним запросом на все матчи дня.
    ("JOIN teams th", [(GAME_ONE, "NYR", "BOS"), (GAME_TWO, "TOR", "MTL")]),
] + _GAME_CARD_ROUTES


# ---------------------------------------------------------------------------
# Сценарий «карточка игры»: deep link /start game_<id>
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_game_card_renders_full_card(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    cursor = fake_db_router(_GAME_CARD_ROUTES)
    message = make_message(f"/start game_{GAME_ONE}")

    await bot.cmd_start(message, fake_bot, _start_command(f"game_{GAME_ONE}"))

    assert api.sent_messages == [], "карточка уходит rich-сообщением, не parse_mode=HTML"
    (sent,) = api.sent_rich
    text = body(sent)
    # Шесть разных запросов, по разу; подписки чата — ради блока страны (Задача 76).
    assert len(cursor.executed) == 6
    # Формы до матча в итоге матча нет (Задача 84) — голы сразу под периодами.
    assert (
        "<p>🏒 <b>NYR 3:2 BOS</b><br><i>Периоды: 1:0, 1:1, 1:1</i></p><p>&nbsp;</p>"
        "<p><b>1-й период</b><br>"
    ) in text
    assert "Форма" not in text
    assert "<b>Хет-трик</b>: Panarin" not in text, "у Panarin два гола — хет-трика нет"
    # Голы — обычным текстом по периодам, без <pre>: время внутри периода, счёт
    # после гола, автор; ассистенты — строкой ниже. Российский игрок (в том числе
    # в передачах) капсом; победная шайба — «ПШ», а не «★».
    assert "<pre>" not in text
    assert rich_html(
        "<b>1-й период</b>\n"
        "05:12 · 1:0 — PANARIN [LW] (ББ)\n"
        "Fox\n"
        "\n"
        "<b>2-й период</b>\n"
        "11:40 · 1:1 — Marchand [LW]\n"
        "15:20 · 2:1 — Zibanejad [C] (МБ)\n"
        "PANARIN, Fox\n"
        "\n"
        "<b>3-й период</b>\n"
        "04:10 · 2:2 — Pastrnak [RW]\n"
        "Marchand\n"
        "18:03 · 3:2 — PANARIN [LW] (ПШ)"
    ) in text, "голы по периодам — отдельные абзацы"
    # Итоги матча — один абзац: броски и штрафное время одной строкой, затем вратари.
    assert rich_html(
        "Броски: 31 - 27 · штрафное время: 8 - 6\n"
        "Вратари:\n"
        "Shesterkin — 25/27, 92.59%, 60:00\n"
        "Swayman — 28/31, 90.32%, 60:00"
    ) in text
    assert "<b>Броски" not in text and "<b>Штрафное" not in text and "<b>Вратари" not in text
    assert text.index("<b>3-й период</b>") < text.index("Броски:") < text.index("<b>Звёзды матча</b>")
    assert text.endswith(
        "<p><b>Звёзды матча</b><br>1. Panarin (NYR) — 2+1<br>"
        "2. Shesterkin (NYR) — 25/27, 92.59%<br>3. Marchand (BOS) — 1+0</p>"
    ), "без подписки на страну блока страны нет"


@pytest.mark.asyncio
async def test_game_card_of_country_subscriber_ends_with_country_block(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    """Задача 76: подписчику страны под полной карточкой — блок игроков страны в матче."""
    bot = bot_module("bot")
    cursor = fake_db_router([
        ("FROM bot_subscriptions", [("country_players", None, "RUS")]),
        # (game_id, фамилия, команда, позиция, Г, П, броски, ±, хиты, блоки, вбр., время)
        ("FROM game_player_stats s", [
            (GAME_ONE, "Panarin", "NYR", "L", 2, 1, 5, 2, 0, 0, None, None, "20:15"),
        ]),
        ("FROM game_goalie_stats s", []),
    ] + _GAME_CARD_ROUTES)
    message = make_message(f"/start game_{GAME_ONE}")

    await bot.cmd_start(message, fake_bot, _start_command(f"game_{GAME_ONE}"))

    (sent,) = api.sent_rich
    text = body(sent)
    assert text.endswith("<br>" + rich_html(
        "3. Marchand (BOS) — 1+0\n\n"
        "<b>Россия</b>\n\n"
        "Panarin (NYR)\n"
        "2+1 · +2 · 5 бр. · 0 хитов · 20:15"
    ).removeprefix("<p>")), "блок страны — после звёзд матча, отдельным разделом"
    assert "🇷🇺" not in text, "заголовок страны без флага"
    block_queries = [p for q, p in cursor.executed if "r.nationality" in q]
    assert block_queries == [(GAME_ONE, "RUS")] * 2, "блок — по этому матчу и этой стране"


@pytest.mark.asyncio
async def test_game_card_attaches_one_video_button_per_goal(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router(_GAME_CARD_ROUTES)
    message = make_message(f"/start game_{GAME_ONE}")

    await bot.cmd_start(message, fake_bot, _start_command(f"game_{GAME_ONE}"))

    (sent,) = api.sent_rich
    buttons = _flat_buttons(sent.reply_markup)
    assert _callback_data(sent.reply_markup) == [
        f"gv:{GAME_ONE}:{event_id}" for event_id in (101, 102, 103, 104, 105)
    ]
    # Время кнопки — как в строке гола: внутри периода, с периодом коротко.
    assert buttons[0].text == "▶ 1:0 PANARIN 1п 05:12"
    assert buttons[4].text == "▶ 3:2 PANARIN 3п 18:03"
    # Два столбца: столбик на всю ширину сливался со следующей карточкой.
    assert [len(row) for row in sent.reply_markup.inline_keyboard] == [2, 2, 1]


@pytest.mark.asyncio
async def test_game_card_reports_missing_game_without_running_card_queries(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    cursor = fake_db_router([("SELECT 1 AS o FROM games", [])])
    message = make_message("/start game_1")

    await bot.cmd_start(message, fake_bot, _start_command("game_1"))

    (sent,) = api.sent_messages
    assert sent.text == "Такого матча нет в базе бота."
    assert sent.reply_markup is None
    assert len(cursor.executed) == 1, "после game_exists() == False запросов карточки быть не должно"


# ---------------------------------------------------------------------------
# Сценарий «дайджест дня»: /today → разворот матча кнопкой
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_today_summarises_both_matches_with_numbered_team_buttons(
    bot_module, fake_db_router, make_message, fake_bot, api, monkeypatch
):
    bot = bot_module("bot")
    cursor = fake_db_router(_DIGEST_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: _TODAY)
    message = make_message("/today")

    await bot.cmd_today(message, fake_bot)

    (summary,) = api.sent_rich
    assert api.sent_messages == [], "подсказка — подвал сводки, не отдельное сообщение"
    summary_html = body(summary)
    assert summary_html.startswith("<p><b>Матчи ночи</b></p>")
    # Матчи — строкой без <b>, периоды под ней курсивом (абзац на матч).
    assert rich_html(
        "1. NYR 3:2 BOS\n  <i>1:0, 1:1, 1:1</i>\n\n"
        "2. TOR 1:0 MTL\n  <i>1:0, 0:0, 0:0</i>"
    ) in summary_html
    # Подвал в конце: игровой день и число матчей, пояснение кнопок, подсказка «Ещё: …».
    footer = summary_html.rsplit("<footer>", 1)[1]
    assert summary_html.endswith("</footer>")
    assert footer.startswith("Игровой день 2026-04-01 · матчей: 2<br>")
    assert "Кнопка матча — полная карточка и видео голов." in footer
    assert "Ещё: /tonight" in footer
    # Кнопки несут те же номера, что и сводка, и команды вместо «Матч N».
    buttons = _flat_buttons(summary.reply_markup)
    assert [b.text for b in buttons] == ["1. NYR – BOS", "2. TOR – MTL"]
    assert [b.callback_data for b in buttons] == [f"dg:{GAME_ONE}", f"dg:{GAME_TWO}"]
    day_query = next(q for q in cursor.executed if "SELECT DISTINCT game_id" in q[0])
    assert day_query[1][0] == _TODAY


@pytest.mark.asyncio
async def test_digest_expand_button_opens_the_full_card_of_that_match(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api, monkeypatch
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_DIGEST_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: _TODAY)

    await bot.cmd_today(make_message("/today"), fake_bot)
    second_match = _flat_buttons(api.sent_rich[0].reply_markup)[1]
    match = re.match(stats_handlers.DIGEST_EXPAND_CALLBACK_PATTERN, second_match.callback_data)
    assert match

    await stats_handlers.callback_expand_digest_game(
        make_callback(second_match.callback_data), fake_bot, match
    )

    card = api.sent_rich[-1]
    assert "<b>TOR 1:0 MTL</b>" in body(card)
    assert rich_html(
        "<b>1-й период</b>\n12:00 · 1:0 — Matthews [C] (ПШ)\nNylander\n"
    ) in body(card)
    assert _callback_data(card.reply_markup) == [f"gv:{GAME_TWO}:201"]


# ---------------------------------------------------------------------------
# Задача 36: сезон с 0 и с 1–3 игровыми днями — /standings, /stats, /today,
# карточка матча, /advanced.
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
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router(_EMPTY_SEASON_ROUTES)
    message = make_message("/standings")

    await bot.cmd_standings(message, fake_bot)

    (reply,) = api.sent_rich
    assert "Сезон ещё не начался" in body(reply)
    assert "EASTERN CONFERENCE" not in body(reply), "пустая таблица с заголовками — не текст-причина"
    assert "<table" not in body(reply)


@pytest.mark.asyncio
async def test_stats_points_on_empty_season_reports_no_data(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    fake_db_router(_EMPTY_SEASON_ROUTES)

    await _open_points(bot_module, make_message, make_callback, fake_bot, api)

    (edited,) = api.edited_texts
    assert "Нет данных в этом диапазоне." in body(edited)


@pytest.mark.asyncio
async def test_today_on_empty_season_reports_no_finished_matches(
    bot_module, fake_db_router, make_message, fake_bot, api, monkeypatch
):
    bot = bot_module("bot")
    fake_db_router(_EMPTY_SEASON_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: "2026-10-02")
    message = make_message("/today")

    await bot.cmd_today(message, fake_bot)

    # Пустая ночь — пометка и откат на последний игровой день в базе; в пустом
    # сезоне нет и его.
    notice, sent = api.outgoing
    assert notice.text == (
        "Прошедшей ночью (2026-10-02) матчей в базе нет — показываю последний игровой день."
    )
    text = body(sent)
    assert text.startswith(rich_html("В базе пока нет завершенных матчей."))
    assert sent in api.sent_rich and notice in api.sent_messages
    assert text.endswith("</footer>") and "/stats" in text.rsplit("<footer>", 1)[1]


@pytest.mark.asyncio
async def test_game_card_on_empty_season_reports_missing_game(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router(_EMPTY_SEASON_ROUTES)
    message = make_message("/start game_1")

    await bot.cmd_start(message, fake_bot, _start_command("game_1"))

    (sent,) = api.sent_messages
    assert sent.text == "Такого матча нет в базе бота."


@pytest.mark.asyncio
async def test_advanced_pick_on_empty_season_reports_no_data(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_EMPTY_SEASON_ROUTES)

    await bot.cmd_advanced(make_message("/advanced"), fake_bot)
    sat_button = _flat_buttons(api.sent_rich[0].reply_markup)[0]
    assert sat_button.callback_data == "adv:sat"
    match = re.match(stats_handlers.ADVANCED_CALLBACK_PATTERN, sat_button.callback_data)
    assert match

    await stats_handlers.callback_standalone_adv(make_callback(sat_button.callback_data), match)

    (edited,) = api.edited_texts
    assert "Нет данных в этом диапазоне." in body(edited)


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
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router(_PARTIAL_TABLE_ROUTES)
    message = make_message("/standings")

    await bot.cmd_standings(message, fake_bot)

    (reply,) = api.sent_rich
    text = body(reply)
    assert "Сезон ещё не начался" not in text
    assert _tr("rlrrr", "1.", "Rangers", "4", "2", "100.0") in text
    # Разделы западной конференции остаются на месте (нет исключения), но
    # каждый дивизион без единой сыгранной игры несёт текст-причину, а не
    # заголовки пустой таблицы.
    assert "<p><b>WESTERN CONFERENCE</b></p>" in text
    assert "<p><b>CENTRAL DIVISION</b><br>(в дивизионе ещё никто не сыграл)</p>" in text
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
async def test_stats_points_on_partial_season_shows_short_page_without_next_button(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    fake_db_router(_PARTIAL_LEADERS_ROUTES)

    await _open_points(bot_module, make_message, make_callback, fake_bot, api)

    (edited,) = api.edited_texts
    text = body(edited)
    assert "<i>Показаны 1–4 из 4 строк.</i>" in text
    assert _tr("rllrrr", "1.", "Panarin [LW]", "NYR", "5", "2", "45") in text
    assert _tr("rllrrr", "4.", "Matthews [C]", "TOR", "1", "1", "18") in text
    # Ни prev, ни next — показаны все 4 строки; остаются меню и «Сохранить».
    assert _callback_data(edited.reply_markup) == ["st:menu", "st:save"]


# Ранняя сборная (games < 20) отфильтровывается INNER JOIN players_season_stats
# в самом запросе (bot_messages._pss_join_sql) — на 1–3 играх сезона выборка
# players_advanced_stats пуста для *всех* игроков, не только по недостатку строк.
_PARTIAL_ADVANCED_ROUTES = [("players_advanced_stats", [])]


@pytest.mark.asyncio
async def test_advanced_pick_on_partial_season_reports_no_data_below_games_threshold(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router(_PARTIAL_ADVANCED_ROUTES)

    await bot.cmd_advanced(make_message("/advanced"), fake_bot)
    sat_button = _flat_buttons(api.sent_rich[0].reply_markup)[0]
    match = re.match(stats_handlers.ADVANCED_CALLBACK_PATTERN, sat_button.callback_data)
    assert match

    await stats_handlers.callback_standalone_adv(make_callback(sat_button.callback_data), match)

    (edited,) = api.edited_texts
    assert "Нет данных в этом диапазоне." in body(edited)
    # «Нет данных» — реально из-за порога games >= 20 в самом запросе
    # (_pss_join_sql), а не по случайному совпадению пустого маршрута. Порог
    # смягчается в начале сезона до половины игр лидера по числу матчей.
    assert "LEAST(20, (SELECT CEIL(MAX(ts.games_played) / 2.0)" in cursor.executed[-1][0]


# Вторая игра сезона — TOR 2026-10-03 (первая у TOR). /today в тесте показывает
# ночь, закреплённую на 2026-10-03.
PARTIAL_GAME_B = 2026020102  # TOR (home) 1 : 3 NYR (away) — 2026-10-03

_PARTIAL_GAME_B_DATA = {
    "stats": [
        (1, 4, 8, 15, 22, False, False, "home", "TOR"),
        (3, 6, 9, 24, 30, False, False, "away", "NYR"),
    ],
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

_PARTIAL_DIGEST_ROUTES = [
    ("SELECT DISTINCT game_id FROM games", [(PARTIAL_GAME_B,)]),
] + _game_card_routes({PARTIAL_GAME_B: _PARTIAL_GAME_B_DATA})


@pytest.mark.asyncio
async def test_today_on_partial_season_renders_the_single_game_card(
    bot_module, fake_db_router, make_message, fake_bot, api, monkeypatch
):
    bot = bot_module("bot")
    fake_db_router(_PARTIAL_DIGEST_ROUTES)
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: "2026-10-03")
    message = make_message("/today")

    await bot.cmd_today(message, fake_bot)

    (sent,) = api.sent_rich
    assert api.sent_messages == []
    text = body(sent)
    assert "<b>TOR 1:3 NYR</b>" in text
    assert text.endswith("</footer>") and "/stats" in text.rsplit("<footer>", 1)[1]


# ---------------------------------------------------------------------------
# Сценарий «статистика команд»: /team_stats → «Голы» (за, против, разница за игру)
# ---------------------------------------------------------------------------

# abbreviation, goals_per_game, goals_against_per_game, разница (сортировка),
# games_played, wins, losses, ot, points — уже по разнице: сортирует БД.
_TEAM_GOALS = [
    ("WSH", 3.4, 2.5, 0.9, 17, 10, 5, 2, 22),
    ("BOS", 3.0, 3.1, -0.1, 20, 12, 6, 1, 25),
]


@pytest.mark.asyncio
async def test_team_stats_goals_button_ranks_teams_by_goal_difference_per_game(
    bot_module, fake_db_router, make_message, make_callback, fake_bot, api
):
    cursor = fake_db_router([("COUNT(*) OVER () AS total", _leaderboard_page_route(_TEAM_GOALS))])

    await bot_module("bot").cmd_team_stats(make_message("/team_stats"), fake_bot)
    goals_button = _flat_buttons(api.sent_rich[0].reply_markup)[0]
    assert goals_button.text == "Голы"
    await _press_stat(bot_module, make_callback, fake_bot, goals_button.callback_data)

    (edited,) = api.edited_texts
    text = body(edited)
    assert text.startswith("<p><b>Голы за и против в среднем за игру</b></p>")
    assert (
        '<table compact><tr><th align="right"></th><th>Ком</th><th align="right">За</th>'
        '<th align="right">Пр</th><th align="right">Разн</th><th align="right">И</th></tr>'
        + _tr("rlrrrr", "1.", "WSH", "3.4", "2.5", "+0.9", "17")
        + _tr("rlrrrr", "2.", "BOS", "3", "3.1", "-0.1", "20")
        + "</table>"
    ) in text
    [(query, params)] = cursor.executed
    # Порядок даёт БД: сортировка — по разнице «за − против», а не по одной из колонок.
    assert (
        "SQL(' DESC NULLS LAST, t.abbreviation LIMIT %s OFFSET %s')" in query
        and "Composed([SQL('('), Identifier('goals_per_game'), SQL(' - '), "
        "Identifier('goals_against_per_game'), SQL(')')]), SQL(' DESC" in query
    )
    assert params[1:] == (10, 0), "первая страница по 10 строк"
    assert _callback_data(edited.reply_markup) == ["tm:menu", "tm:save"]


# ---------------------------------------------------------------------------
# Сценарий «профиль команды»: подменю команд → «Профиль команды» → выбор
# аббревиатуры → карточка состава (Задача 41, Фаза D)
# ---------------------------------------------------------------------------

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
async def test_team_profile_command_shows_places_scorers_and_goalies(
    bot_module, fake_db_router, make_message, api
):
    """`/WSH`: места в дивизионе/конференции/лиге, баланс, форма и серия, спецбригады,
    топ бомбардиров и вратарь."""
    bot = bot_module("bot")
    fake_db_router(_PROFILE_ROUTES)

    await bot.cmd_team_profile(make_message("/WSH"), re.match(bot.TEAM_COMMAND_PATTERN, "/WSH"))

    (reply,) = api.sent_rich
    text = body(reply)
    # Заголовок — одна строка, сезон — в подвале.
    assert text.startswith("<p><b>Washington Capitals</b> (WSH)<br>")
    assert "Metropolitan: 2-е место · Eastern: 3-е · лига: 4-е из 4 сыгравших" in text
    assert "Игр: 17 · 10-5-2, 22 очка (64.71%)" in text
    assert "Форма (5 игр, W-L-OTL): 3-2-0 · серия W3" in text
    assert "Голы за игру: 3.1 забито / 2.65 пропущено" in text
    assert "Большинство: 21.5% · Меньшинство: 80% · Вбрасывания: 51.25%" in text
    # Бомбардиры — в порядке выдачи БД (по очкам), rich-таблицей.
    assert (
        "<p><b>Бомбардиры</b></p>"
        '<table compact><tr><th></th><th>Игрок</th><th align="right">И</th>'
        '<th align="right">Г</th><th align="right">П</th><th align="right">О</th></tr>'
        + _tr("llrrrr", "1.", "Ovechkin [L]", "17", "38", "4", "42")
        + _tr("llrrrr", "2.", "Backstrom [C]", "17", "10", "20", "30")
        + _tr("llrrrr", "3.", "Carlson [D]", "16", "5", "20", "25")
        + "</table>"
    ) in text
    assert (
        "<p><b>Игровое время</b></p>"
        '<table compact><tr><th></th><th>Игрок</th><th align="right">И</th>'
        '<th align="right">ВП</th></tr>'
        + _tr("llrr", "1.", "Carlson [D]", "16", "24:31")
        + _tr("llrr", "2.", "Ovechkin [L]", "17", "19:05")
        + _tr("llrr", "3.", "Backstrom [C]", "17", "17:40")
        + "</table>"
    ) in text
    assert (
        "<p><b>Вратари</b></p>"
        '<table compact><tr><th>Вратарь</th><th align="right">И</th>'
        '<th align="right">В-П-ОТ</th><th align="right">%ОБ</th><th align="right">КН</th></tr>'
        + _tr("lrrrr", "Samsonov", "15", "9-4-2", "91.5%", "2.6")
        + "</table>"
    ) in text
    assert "<details><summary>Обозначения</summary><p>И — игры" in text
    assert text.endswith(" · все игроки: /WSH_FULL · все команды: /team</footer>")


@pytest.mark.asyncio
async def test_team_profile_reports_team_that_has_not_played_yet(
    bot_module, fake_db_router, make_message, api
):
    """Команда есть в `teams`, но без строки в `teams_stats` (ещё не сыграла,
    Задача 36) — текст с причиной, не пустая карточка и не исключение."""
    bot = bot_module("bot")
    fake_db_router([
        ("SELECT team_id FROM teams WHERE season_id", [(99,)]),
        ("FROM teams_stats ts", _LEAGUE_ROWS),
    ])

    await bot.cmd_team_profile(make_message("/WSH"), re.match(bot.TEAM_COMMAND_PATTERN, "/WSH"))

    (reply,) = api.sent_rich
    assert "Команда ещё не сыграла в этом сезоне — статистики нет." in body(reply)


@pytest.mark.asyncio
async def test_team_command_lists_teams_by_division_as_clickable_commands(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router([(
        "ORDER BY division_name, name",
        [("WSH", "Washington Capitals", "Metropolitan"), ("BOS", "Boston Bruins", "Atlantic")],
    )])
    message = make_message("/team")

    await bot.cmd_team(message, fake_bot)

    (reply,) = api.sent_rich
    assert rich_html("<b>Atlantic</b>\n/BOS — Boston Bruins") in body(reply)
    assert rich_html("<b>Metropolitan</b>\n/WSH — Washington Capitals") in body(reply)


@pytest.mark.asyncio
async def test_team_abbreviation_command_replies_with_profile_and_link_to_team_list(
    bot_module, fake_db_router, make_message, api
):
    """`/wsh`: аббревиатуру берёт regex-хендлер (`match`), в профиль она уходит
    в верхнем регистре; в подвале — ссылка на список команд."""
    bot = bot_module("bot")
    cursor = fake_db_router(_PROFILE_ROUTES)
    message = make_message("/wsh")
    match = re.match(bot.TEAM_COMMAND_PATTERN, "/wsh")

    await bot.cmd_team_profile(message, match)

    (reply,) = api.sent_rich
    assert "<b>Washington Capitals</b> (WSH)" in body(reply)
    assert body(reply).endswith("все команды: /team</footer>")
    team_id_query = next(q for q in cursor.executed if "SELECT team_id FROM teams" in q[0])
    assert "WSH" in team_id_query[1]


@pytest.mark.asyncio
async def test_team_full_command_lists_every_skater_and_goalie(
    bot_module, fake_db_router, make_message, api
):
    """`/WSH_FULL` из конца профиля: тот же regex-хендлер; нападающие,
    защитники и вратари отдельными таблицами с колонками экрана страны, полный
    список без LIMIT (параметр `None`); легенды групп — свёрнутым блоком, ссылка
    назад на профиль — в подвале."""
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
    message = make_message("/WSH_FULL")
    match = re.match(bot.TEAM_COMMAND_PATTERN, "/WSH_FULL")

    await bot.cmd_team_profile(message, match)

    (reply,) = api.sent_rich
    text = body(reply)
    assert "<p><b>WSH: все игроки</b>" in text
    # Без колонок места и команды: игрок и показатели.
    assert (
        "<p><b>Нападающие</b></p>"
        '<table compact><tr><th>Игрок</th><th align="right">Г</th>'
        '<th align="right">О</th><th align="right">И</th><th align="right">ВП</th>'
        '<th align="right">Хит</th><th align="right">Бр</th></tr>'
        + _tr("lrrrrrr", "Ovechkin", "38", "42", "17", "19:05", "30", "120")
        + _tr("lrrrrrr", "Backstrom", "10", "30", "17", "17:40", "8", "40")
        + "</table>"
    ) in text
    assert (
        "<p><b>Защитники</b></p>"
        '<table compact><tr><th>Игрок</th><th align="right">Г</th>'
        '<th align="right">О</th><th align="right">И</th><th align="right">ВП</th>'
        '<th align="right">Хит</th><th align="right">Бл</th></tr>'
        + _tr("lrrrrrr", "Carlson", "5", "25", "16", "24:31", "20", "33")
        + "</table>"
    ) in text
    assert (
        "<p><b>Вратари</b></p>"
        '<table compact><tr><th>Вратарь</th><th align="right">В</th>'
        '<th align="right">%ОБ</th><th align="right">КН</th><th align="right">И</th>'
        '<th align="right">Сух</th></tr>'
        + _tr("lrrrrr", "Samsonov", "9", "91.5%", "2.6", "15", "1")
        + "</table>"
    ) in text
    assert text.index("Нападающие") < text.index("Защитники") < text.index("Вратари")
    legend = text.split("<details><summary>Обозначения</summary>", 1)[1].split("</details>", 1)[0]
    assert legend.startswith("<p>Нападающие: Г — голы, О — очки")
    assert "<br>Защитники: " in legend and "<br>Вратари: В — победы" in legend
    assert text.endswith(" · профиль клуба: /WSH · все команды: /team</footer>")
    group_queries = [q for q in cursor.executed if "r.current_team_id = %s" in q[0]]
    assert len(group_queries) == 3
    # Игроки клуба — по team_id параметром, полный список — без LIMIT.
    assert all(params[1:3] == (5, None) for _sql, params in group_queries)
    goalie_sql = next(q for q, _ in group_queries if "goalies_season_stats" in q)
    assert "g.games > 0" in goalie_sql, "вратарь без игр в списке клуба не нужен"


@pytest.mark.asyncio
async def test_team_full_command_skips_empty_groups(
    bot_module, fake_db_router, make_message, api
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
    match = re.match(bot.TEAM_COMMAND_PATTERN, "/WSH_FULL")

    await bot.cmd_team_profile(make_message("/WSH_FULL"), match)

    text = body(api.sent_rich[0])
    assert "<b>Нападающие</b>" in text
    assert "Защитники" not in text and "Вратари" not in text

    fake_db_router([
        ("SELECT team_id FROM teams WHERE season_id", [(5,)]),
        ("r.position IN ('C', 'L', 'R')", []),
        ("r.position = 'D'", []),
        ("FROM goalies_season_stats g", []),
    ])
    await bot.cmd_team_profile(make_message("/WSH_FULL"), match)
    assert "У команды пока нет статистики игроков в этом сезоне." in body(api.sent_rich[1])


@pytest.mark.asyncio
async def test_team_profile_reports_unknown_abbreviation(
    bot_module, fake_db_router, make_message, api
):
    """`/ZZZ` прошла TEAM_COMMAND_PATTERN, но не резолвится в team_id (не найдена
    в teams для сезона) — текст с причиной, не исключение."""
    bot = bot_module("bot")
    fake_db_router([("SELECT team_id FROM teams WHERE season_id", [])])

    await bot.cmd_team_profile(make_message("/ZZZ"), re.match(bot.TEAM_COMMAND_PATTERN, "/ZZZ"))

    (reply,) = api.sent_rich
    assert "Команда не найдена в базе для этого сезона." in body(reply)


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
    bot_module, fake_db_router, make_message, fake_bot, api
):
    bot = bot_module("bot")
    fake_db_router(_COUNTRY_ROUTES)
    message = make_message("/countries")

    await bot.cmd_countries(message, fake_bot)

    (reply,) = api.sent_rich
    text = body(reply)
    # Флаг — только на кнопке: эмодзи в ячейках таблиц не ставим.
    assert _tr("rlrrrr", "1.", "Канада", "5", "100", "40", "20.0") in text
    assert text.startswith("<p><b>Рейтинг стран</b></p><table")
    assert "<details><summary>Обозначения</summary><p>Полевые игроки;" in text
    assert text.endswith("<br>Страна не указана в данных NHL у игроков: 2 (не учтены).</footer>")
    assert [(b.text, b.callback_data) for b in _flat_buttons(reply.reply_markup)] == [
        ("🇨🇦 Канада", "cn:CAN:F:0"),
        ("🇺🇸 США", "cn:USA:F:0"),
    ]


@pytest.mark.asyncio
async def test_countries_command_reports_empty_season_without_buttons(
    bot_module, fake_db_router, make_message, fake_bot, api
):
    """Пустой сезон (Задача 36): текст с причиной, без кнопок стран."""
    bot = bot_module("bot")
    fake_db_router([("HAVING COUNT", [])])
    message = make_message("/countries")

    await bot.cmd_countries(message, fake_bot)

    (reply,) = api.sent_rich
    assert "В базе нет данных по странам для этого сезона." in body(reply)
    assert _flat_buttons(reply.reply_markup) == []


@pytest.mark.asyncio
async def test_country_page_pages_and_switches_groups(
    bot_module, fake_db_router, make_callback, api
):
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router(_COUNTRY_ROUTES)
    callback = make_callback("cn:CAN:D:10")
    match = re.match(stats_handlers.COUNTRY_CALLBACK_PATTERN, "cn:CAN:D:10")

    await stats_handlers.callback_country(callback, match)

    (edited,) = api.edited_texts
    text = body(edited)
    assert text.startswith("<p><b>🇨🇦 Канада: защитники</b></p><table")
    assert "<details><summary>Обозначения</summary><p>Г — голы" in text
    assert text.endswith(" · <i>Показаны 11 из 25 строк.</i></footer>")
    # Все колонки на месте, включая время и силовые.
    assert _tr(
        "rllrrrrrr", "11.", "McDavid", "EDM", "30", "90", "70", "21:30", "40", "30"
    ) in body(edited)
    buttons = _flat_buttons(edited.reply_markup)
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
    bot_module, fake_db_router, make_callback, api
):
    """Код прошёл паттерн, но не в рейтинге сезона — текст, не исключение и не
    запрос по стране; «« Страны»» на рейтинг."""
    stats_handlers = bot_module("stats_handlers")
    cursor = fake_db_router([("HAVING COUNT", _COUNTRY_RANKING_ROWS)])
    callback = make_callback("cn:ZZZ:F:0")
    match = re.match(stats_handlers.COUNTRY_CALLBACK_PATTERN, "cn:ZZZ:F:0")

    await stats_handlers.callback_country(callback, match)

    (edited,) = api.edited_texts
    assert "Страна не найдена в рейтинге этого сезона." in body(edited)
    assert "cn:list" in _callback_data(edited.reply_markup)
    assert len(cursor.executed) == 1


@pytest.mark.asyncio
async def test_country_goalies_and_back_to_rankings(
    bot_module, fake_db_router, make_callback, api
):
    stats_handlers = bot_module("stats_handlers")
    fake_db_router(_COUNTRY_ROUTES)

    await stats_handlers.callback_country(
        make_callback("cn:CAN:G:0"),
        re.match(stats_handlers.COUNTRY_CALLBACK_PATTERN, "cn:CAN:G:0"),
    )
    assert _tr(
        "rllrrrrr", "1.", "Hellebuyck", "WPG", "35", "92.1%", "2.4", "60", "5"
    ) in body(api.edited_texts[0])

    await stats_handlers.callback_country(
        make_callback("cn:list"), re.match(stats_handlers.COUNTRY_CALLBACK_PATTERN, "cn:list")
    )
    back = api.edited_texts[1]
    assert "<b>Рейтинг стран</b>" in body(back)
    assert _callback_data(back.reply_markup) == ["cn:CAN:F:0", "cn:USA:F:0"]
