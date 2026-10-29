"""Unit tests for message assembly: telegram_bot/template_funcs.py and the
HTML-rendering paths of telegram_bot/bot_messages.py. No DB, no network —
bot_messages.fetch_all and bot_messages.cached_fetch_all are patched at the
module boundary; template_funcs is exercised directly against real template
files under telegram_bot/messages/.

Tables are rich-message tables (``_table``); ``_tables()`` below reads them back
into rows of cell texts, so assertions compare structure, not markup.

Special attention goes to HTML escaping (rich-message HTML): player
and team names coming out of the DB can contain `<`, `>`, `&`, quotes, or
ordinary punctuation (`-`, `_`, `*`) and must come back through message
builders exactly once escaped, never re-formatted or dropped.
"""
from __future__ import annotations

import html
import re
from typing import List
from unittest.mock import patch

import pytest


def _tables(text: str) -> List[List[List[str]]]:
    """Таблицы rich-сообщения в тексте экрана: таблица — строки, строка — тексты ячеек
    (теги сняты, HTML-сущности раскрыты); строка пунктира плей-офф — одна ячейка."""
    return [
        [
            [html.unescape(re.sub(r"<[^>]+>", "", cell)) for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row)]
            for row in re.findall(r"<tr>(.*?)</tr>", table)
        ]
        for table in re.findall(r"<table[^>]*>(.*?)</table>", text)
    ]


# ---------------------------------------------------------------------------
# template_funcs.py
# ---------------------------------------------------------------------------

def test_read_template_returns_file_contents_verbatim(bot_module, tmp_path):
    template_funcs = bot_module("template_funcs")
    f = tmp_path / "t.txt"
    f.write_text("Hello {{ name }}!\n", encoding="utf-8")

    assert template_funcs.read_template(str(f)) == "Hello {{ name }}!\n"


def test_output_text_substitutes_jinja_variables(bot_module, tmp_path):
    template_funcs = bot_module("template_funcs")
    f = tmp_path / "t.txt"
    f.write_text("{{ greeting }}, {{ name }}!", encoding="utf-8")

    result = template_funcs.output_text(str(f), {"greeting": "Hi", "name": "World"})

    assert result == "Hi, World!"


def test_output_text_does_not_auto_escape_html(bot_module, tmp_path):
    """bot_messages.py hand-escapes every DB-sourced value with html.escape()
    before it reaches a template. If Template() rendering ever gained
    autoescape=True, every one of those values would be double-escaped
    (`&amp;lt;` instead of `&lt;`) — lock down that it stays raw substitution."""
    template_funcs = bot_module("template_funcs")
    f = tmp_path / "t.txt"
    f.write_text("{{ value }}", encoding="utf-8")

    result = template_funcs.output_text(str(f), {"value": "<b>&already-escaped&lt;</b>"})

    assert result == "<b>&already-escaped&lt;</b>"


# ---------------------------------------------------------------------------
# _table() / rich_message() — разметка rich-сообщения (Bot API 10.1+)
# ---------------------------------------------------------------------------

def test_table_renders_header_alignment_and_escapes_cells(bot_module):
    bot_messages = bot_module("bot_messages")

    table = bot_messages._table([["", "Команда", "Очк"], ["1.", "A&B <Team>", "10"]], "rlr")

    assert table == (
        "<table compact>"
        '<tr><th align="right"></th><th>Команда</th><th align="right">Очк</th></tr>'
        '<tr><td align="right">1.</td><td>A&amp;B &lt;Team&gt;</td><td align="right">10</td></tr>'
        "</table>"
    )
    assert "\n" not in table, "таблица — одна строка текста, один блок rich-сообщения"


def test_table_draws_playoff_line_across_all_columns_after_given_row(bot_module):
    bot_messages = bot_module("bot_messages")

    table = bot_messages._table([["#", "К"], ["1.", "A"], ["2.", "B"], ["3.", "C"]], "rl", playoff_line_after=2)

    line = bot_messages._PLAYOFF_LINE
    assert _tables(table) == [[["#", "К"], ["1.", "A"], ["2.", "B"], [line], ["3.", "C"]]]
    assert f'<td colspan="2" align="center">{line}</td>' in table
    assert set(line) == {"┄"}, "черта — пунктир, без подписи"


def test_table_stripes_only_tables_longer_than_ten_rows(bot_module):
    """Полосы ведут глаз по длинной таблице, на короткой — шум (spike Задачи 88)."""
    bot_messages = bot_module("bot_messages")

    assert bot_messages._table([["#"]] + [["1"]] * 10, "r").startswith("<table compact><tr>")
    assert bot_messages._table([["#"]] + [["1"]] * 11, "r").startswith("<table compact striped><tr>")


def test_rich_message_joins_block_lines_into_one_paragraph(bot_module):
    """В rich-HTML перевод строки — пробел: строки одного блока — один абзац через `<br>`."""
    bot_messages = bot_module("bot_messages")

    message = bot_messages.rich_message("<b>Заголовок</b>\n<i>сезон</i>\nстрока")

    assert message.html == "<p><b>Заголовок</b><br><i>сезон</i><br>строка</p>"
    assert (message.markdown, message.blocks) == (None, None)


def test_rich_message_blank_line_splits_paragraphs_with_visible_gap(bot_module):
    """Граница `<p>` в клиентах отступа не даёт: пустая строка (и несколько подряд) — один
    зазор `<p>&nbsp;</p>` между абзацами; в начале и в конце текста зазора нет."""
    bot_messages = bot_module("bot_messages")

    message = bot_messages.rich_message("\nраздел 1\nещё\n\n\nраздел 2\n\n")

    assert message.html == "<p>раздел 1<br>ещё</p><p>&nbsp;</p><p>раздел 2</p>"


def test_rich_message_keeps_table_as_own_block_without_gap(bot_module):
    """Таблица — свой блок; отступ вокруг неё клиент рисует сам, зазор не добавляется."""
    bot_messages = bot_module("bot_messages")
    table = bot_messages._table([["A"], ["1"]], "r")

    message = bot_messages.rich_message(f"<b>Дивизион</b>\n{table}\n\nИтог: /help\n{table}\nпосле")

    assert message.html == f"<p><b>Дивизион</b></p>{table}<p>Итог: /help</p>{table}<p>после</p>"


def test_rich_message_keeps_footer_details_and_list_as_own_blocks(bot_module):
    """Подвал, свёрнутый блок и список — свои блоки, не внутри абзаца; содержимое
    `<details>` собирается по тем же правилам абзацев, строки подвала — через `<br>`."""
    bot_messages = bot_module("bot_messages")
    details = bot_messages.rich_details("Обозначения", "Г — голы\nО — очки\n\nИ — игры")
    footer = bot_messages.rich_footer("Настроить: /subscriptions\n\nданные на 27.10")
    items = bot_messages.rich_list(["/today — ночь", "/help — справка"])

    message = bot_messages.rich_message(f"Счёт\n{items}\n{details}\n\n{footer}")

    assert message.html == (
        "<p>Счёт</p><ul><li>/today — ночь</li><li>/help — справка</li></ul>"
        "<details><summary>Обозначения</summary>"
        "<p>Г — голы<br>О — очки</p><p>&nbsp;</p><p>И — игры</p></details>"
        "<footer>Настроить: /subscriptions<br>данные на 27.10</footer>"
    )


def test_rich_footer_without_lines_adds_no_empty_footer(bot_module):
    """Пустой подвал (превью несопоставленных команд) — без `<footer></footer>`."""
    bot_messages = bot_module("bot_messages")

    assert bot_messages.rich_message("Текст\n" + bot_messages.rich_footer("\n")).html == "<p>Текст</p>"


# ---------------------------------------------------------------------------
# game_message() — HTML escaping of scorer/team/goalie names
# ---------------------------------------------------------------------------

_EVIL_SCORER = "O'Brien <3> & Co_junior-star*"


def _game_message_fetch_all(query, params=None, columns=None):
    q = str(query)
    if "get_game_stats" in q:
        return {
            "goals": [3, 0],
            "pim": [4, 6],
            "blocks": [5, 5],
            "hits": [20, 10],
            "shots": [30, 20],
            "is_overtime": [False, False],
            "is_shootouts": [False, False],
            "field": [None, None],
            "team_name": ["BOS", "TOR<script>alert(1)</script>"],
            "count_rows": 2,
        }
    if "get_goals_game" in q:
        return {
            "scorer": [_EVIL_SCORER, _EVIL_SCORER, _EVIL_SCORER],
            "scorer_position": ["C", "C", "C"],
            "scorer_nationality": ["CAN", "CAN", "CAN"],
            "assist_1": ["Sm'th", None, None],
            "assist_1_nationality": [None, None, None],
            "assist_2": [None, None, None],
            "assist_2_nationality": [None, None, None],
            "period": [1, 1, 2],
            "goal_time": ["05:30", "10:15", "02:00"],
            "home_score": [1, 2, 3],
            "away_score": [0, 0, 0],
            "is_ppg": [False, True, False],
            "is_shg": [False, False, False],
            "empty_net": [False, False, True],
            "winner_goal": [False, False, True],
            "goal_game_id": [555, 555, 555],
            "goal_event_id": [10, 11, 12],
            "count_rows": 3,
        }
    if "get_goalies_game" in q:
        return {
            "shots": [30],
            "saves": [27],
            "timeonice": ["60:00"],
            "lastname": ["O'Neill<b>"],
            "save_percentage": [90.0],
            "is_home": [True],
            "count_rows": 1,
        }
    if "get_three_stars_game" in q:
        return _THREE_STARS_ROWS
    raise AssertionError(f"unexpected query in game_message(): {q}")


# Три звезды по умолчанию: полевой игрок, вратарь, и звезда с экранируемыми
# фамилией/аббревиатурой команды и без статистики (NULL) — хвост после «—»
# в этом случае не выводится.
_THREE_STARS_ROWS = {
    "star": [1, 2, 3],
    "lastname": ["Kaprizov", "Thompson", _EVIL_SCORER],
    "player_position": ["LW", "G", "C"],
    "abbreviation": ["MIN", "VGK", "TOR<script>alert(1)</script>"],
    "goals": [1, None, None],
    "assists": [0, None, None],
    "saves": [None, 33, None],
    "shots": [None, 34, None],
    "save_percentage": [None, 97.1, None],
    "count_rows": 3,
}


@pytest.fixture
def game_message_text(bot_module):
    """game_message() calls only fetch_all; cached_fetch_all is patched to the
    same stub too, so a query that sneaks in through the cache fails loudly
    (the stub's terminal AssertionError) instead of silently escaping to the
    real database.get_connection()."""
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "fetch_all", side_effect=_game_message_fetch_all), \
            patch.object(bot_messages, "cached_fetch_all", side_effect=_game_message_fetch_all):
        text, goals_meta = bot_messages.game_message(555)
    return text, goals_meta


def test_game_message_escapes_script_tag_in_team_name(game_message_text):
    text, _ = game_message_text
    assert "TOR&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>" not in text


def test_game_message_escapes_apostrophe_ampersand_angle_brackets_in_scorer(game_message_text):
    text, _ = game_message_text
    assert "O&#x27;Brien &lt;3&gt; &amp; Co_junior-star*" in text
    assert _EVIL_SCORER not in text


def test_game_message_preserves_hyphen_underscore_asterisk_unescaped(game_message_text):
    """html.escape only touches &, <, >, and quotes — punctuation used in real
    player names (hyphenated surnames, nicknames) must survive unchanged."""
    text, _ = game_message_text
    assert "Co_junior-star*" in text


def test_game_message_escapes_assist_name_with_apostrophe(game_message_text):
    text, _ = game_message_text
    assert "Sm&#x27;th" in text
    assert "Sm'th" not in text


def test_game_message_renders_hat_trick_line_with_escaped_name(game_message_text):
    text, _ = game_message_text
    assert "\nХет-трик: O&#x27;Brien &lt;3&gt; &amp; Co_junior-star* (×3)\n" in text
    assert "<b>Хет-трик" not in text, "хет-трик — обычный текст, не жирный (стиль §9)"


def test_game_message_escapes_goalie_lastname(game_message_text):
    text, _ = game_message_text
    assert "O&#x27;Neill&lt;b&gt;" in text
    assert "O'Neill<b>" not in text


def test_game_message_returns_goals_meta_for_video_buttons(game_message_text):
    _, goals_meta = game_message_text
    assert [g["event_id"] for g in goals_meta] == [10, 11, 12]
    assert all(g["game_id"] == 555 for g in goals_meta)


# ---------------------------------------------------------------------------
# game_message() — голы по периодам (Задача 84)
# ---------------------------------------------------------------------------

def test_game_message_groups_goals_by_period_and_skips_period_without_goals(
    game_message_text,
):
    """Голы фикстуры — в 1-м и 2-м периодах: заголовка 3-го нет, время внутри периода."""
    text, _ = game_message_text
    scorer = "O&#x27;Brien &lt;3&gt; &amp; Co_junior-star*"
    assert (
        "<b>1-й период</b>\n"
        f"05:30 · 1:0 — {scorer} [C]\n"
        "Sm&#x27;th\n"
        f"10:15 · 2:0 — {scorer} [C] (ББ)\n"
        "\n"
        "<b>2-й период</b>\n"
        f"02:00 · 3:0 — {scorer} [C] (ПВ, ПШ)\n"
    ) in text
    assert "3-й период" not in text
    assert "<pre>" not in text and "Форма" not in text


def _overtime_fetch_all(shootout: bool):
    """Заглушка матча 2:2 после основного времени, решённого в овертайме
    (`shootout=False`, гол в периоде 4) или буллитами (гол в периоде 5, 00:00)."""
    def fetch_all(query, params=None, columns=None):
        q = str(query)
        if "get_game_stats" in q:
            stats = _game_message_fetch_all(query, params, columns)
            return {
                **stats,
                "goals": [3, 2],
                "is_overtime": [True, True],
                "is_shootouts": [shootout, shootout],
            }
        if "get_goals_game" in q:
            goals = _game_message_fetch_all(query, params, columns)
            return {
                **goals,
                "scorer": ["Kaprizov", "Hughes", "Boldy"],
                "period": [1, 3, 5 if shootout else 4],
                "goal_time": ["06:09", "19:39", "00:00" if shootout else "03:12"],
                "home_score": [1, 1, 2],
                "away_score": [0, 1, 1],
                "is_ppg": [False, False, False],
                "empty_net": [False, False, False],
                "winner_goal": [False, False, True],
            }
        return _game_message_fetch_all(query, params, columns)

    return fetch_all


@pytest.mark.parametrize(
    "shootout, expected_block, expected_label",
    [
        (False, "<b>Овертайм</b>\n03:12 · 2:1 — Boldy [C] (ПШ)", "▶ 2:1 Boldy ОТ 03:12"),
        # Время буллита (всегда 00:00) не выводится — ни в строке, ни в кнопке.
        (True, "<b>Буллиты</b>\n2:1 — Boldy [C] (ПШ)", "▶ 2:1 Boldy Б"),
    ],
)
def test_game_message_titles_overtime_and_shootout_goal(
    bot_module, shootout, expected_block, expected_label
):
    bot_messages = bot_module("bot_messages")
    stub = _overtime_fetch_all(shootout)
    with patch.object(bot_messages, "fetch_all", side_effect=stub), \
            patch.object(bot_messages, "cached_fetch_all", side_effect=stub):
        text, goals_meta = bot_messages.game_message(555)

    assert "<b>3-й период</b>\n19:39 · 1:1 — Hughes [C]\n\n" + expected_block in text
    assert "2-й период" not in text
    assert [g["label"] for g in goals_meta] == [
        "▶ 1:0 Kaprizov 1п 06:09", "▶ 1:1 Hughes 3п 19:39", expected_label,
    ]


def test_period_names_count_playoff_overtimes_from_period_number(bot_module):
    """Плей-офф (Задача 37C): период 5 без буллитов — второй овертайм, а не «Буллиты»."""
    bot_messages = bot_module("bot_messages")
    assert bot_messages._period_names(4, False) == ("Овертайм", "ОТ")
    assert bot_messages._period_names(5, False) == ("2-й овертайм", "2ОТ")
    assert bot_messages._period_names(6, False) == ("3-й овертайм", "3ОТ")
    assert bot_messages._period_names(5, True) == ("Буллиты", "Б")


# ---------------------------------------------------------------------------
# game_message() — блок «Звёзды матча»
# ---------------------------------------------------------------------------

def test_game_message_renders_three_stars_heading_and_three_lines(game_message_text):
    text, _ = game_message_text
    assert "<b>Звёзды матча</b>" in text
    # Звёзды — нумерованные строки «1. …», без «★» (символов в теле сообщения нет).
    block = text.split("<b>Звёзды матча</b>", 1)[1]
    assert [line[:3] for line in block.strip().splitlines()] == ["1. ", "2. ", "3. "]
    assert "★" not in block


def test_game_message_formats_field_player_star_as_goals_plus_assists(game_message_text):
    text, _ = game_message_text
    assert "\n1. Kaprizov (MIN) — 1+0\n" in text


def test_game_message_formats_goalie_star_as_saves_over_shots_and_percentage(
    game_message_text,
):
    text, _ = game_message_text
    assert "\n2. Thompson (VGK) — 33/34, 97.1%\n" in text


def test_game_message_escapes_html_in_three_star_lastname_and_abbreviation(
    game_message_text,
):
    text, _ = game_message_text
    assert (
        "3. O&#x27;Brien &lt;3&gt; &amp; Co_junior-star* "
        "(TOR&lt;script&gt;alert(1)&lt;/script&gt;)"
    ) in text
    assert "<script>alert(1)</script>" not in text


def test_game_message_omits_tail_when_three_star_stats_are_null(game_message_text):
    """Третья звезда фикстуры без статистики: строка обрывается перед «—»
    вместо `— None+None`."""
    text, _ = game_message_text
    assert "TOR&lt;script&gt;alert(1)&lt;/script&gt;) —" not in text


def test_game_message_omits_three_stars_block_when_no_rows(bot_module):
    def fetch_all_no_stars(query, params=None, columns=None):
        q = str(query)
        if "get_three_stars_game" in q:
            return {"count_rows": 0}
        return _game_message_fetch_all(query, params, columns)

    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "fetch_all", side_effect=fetch_all_no_stars), \
            patch.object(bot_messages, "cached_fetch_all", side_effect=fetch_all_no_stars):
        text, _ = bot_messages.game_message(555)

    assert "Звёзды матча" not in text
    assert "Kaprizov" not in text and "Thompson" not in text


# ---------------------------------------------------------------------------
# game_exists()
# ---------------------------------------------------------------------------

def test_game_exists_true_only_when_a_row_is_found(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "fetch_all", return_value={"count_rows": 0, "o": []}):
        assert bot_messages.game_exists(1) is False
    with patch.object(bot_messages, "fetch_all", return_value={"count_rows": 1, "o": [1]}):
        assert bot_messages.game_exists(1) is True


# ---------------------------------------------------------------------------
# _last_n_form_record() / _current_streak() — Задача 24, включая правило NHL 84.2
# ---------------------------------------------------------------------------

_STREAK_TEAM_ID = 10
_STREAK_OPP_ID = 20


def _recent_games_rows(outcomes):
    """Строит фикстуру `cached_fetch_all()` для запроса «последние игры команды»
    (`_recent_team_outcomes()`, общий для `_last_n_form_record()`/`_current_streak()`).

    outcomes: список `(winner_id, is_overtime, is_shootouts, ot_empty_net_win)`,
    от новой игры к старой — как их отдаёт `ORDER BY day DESC, game_id DESC`.
    """
    return {
        "winner_id": [o[0] for o in outcomes],
        "is_overtime": [o[1] for o in outcomes],
        "is_shootouts": [o[2] for o in outcomes],
        "ot_empty_net_win": [o[3] for o in outcomes],
        "count_rows": len(outcomes),
    }


@pytest.mark.parametrize(
    "points, word",
    [(1, "очко"), (2, "очка"), (4, "очка"), (5, "очков"), (11, "очков"),
     (12, "очков"), (21, "очко"), (22, "очка"), (0, "очков")],
)
def test_plural_word_agrees_with_number(bot_module, points, word):
    """Отзыв UI: «1 очков» в превью — слово согласуется с числом."""
    assert bot_module("bot_messages")._plural_word(points, ("очко", "очка", "очков")) == word


def test_current_streak_three_wins_in_a_row(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = _recent_games_rows([
        (_STREAK_TEAM_ID, False, False, False),
        (_STREAK_TEAM_ID, False, False, False),
        (_STREAK_TEAM_ID, False, False, False),
        (_STREAK_OPP_ID, False, False, False),
    ])
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        assert bot_messages._current_streak(_STREAK_TEAM_ID) == "W3"


def test_current_streak_two_losses_in_a_row(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = _recent_games_rows([
        (_STREAK_OPP_ID, False, False, False),
        (_STREAK_OPP_ID, False, False, False),
        (_STREAK_TEAM_ID, False, False, False),
    ])
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        assert bot_messages._current_streak(_STREAK_TEAM_ID) == "L2"


def test_current_streak_one_shootout_loss(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = _recent_games_rows([
        (_STREAK_OPP_ID, True, True, False),
        (_STREAK_TEAM_ID, False, False, False),
    ])
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        assert bot_messages._current_streak(_STREAK_TEAM_ID) == "OTL1"


def test_current_streak_stops_at_first_different_outcome(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = _recent_games_rows([
        (_STREAK_TEAM_ID, False, False, False),   # W
        (_STREAK_TEAM_ID, False, False, False),   # W
        (_STREAK_OPP_ID, True, False, False),      # OTL — обрывает серию
        (_STREAK_TEAM_ID, False, False, False),   # W (старее, в серию не входит)
    ])
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        assert bot_messages._current_streak(_STREAK_TEAM_ID) == "W2"


def test_current_streak_no_games_returns_dash(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "cached_fetch_all", return_value=_recent_games_rows([])):
        assert bot_messages._current_streak(_STREAK_TEAM_ID) == "—"


def test_current_streak_rule_84_2_ot_empty_net_goal_is_loss_not_otl(bot_module):
    """Правило NHL 84.2: is_overtime=True, без буллитов, но с победным голом в
    пустые ворота в периоде >= 4 (`ot_empty_net_win=True`) — для проигравшей
    команды это L, а не OTL, поэтому серия здесь L2, а не OTL2."""
    bot_messages = bot_module("bot_messages")
    rows = _recent_games_rows([
        (_STREAK_OPP_ID, True, False, True),
        (_STREAK_OPP_ID, True, False, True),
        (_STREAK_TEAM_ID, False, False, False),
    ])
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        assert bot_messages._current_streak(_STREAK_TEAM_ID) == "L2"


def test_last_n_form_record_rule_84_2_ot_empty_net_goal_counts_as_loss(bot_module):
    """Тот же случай 84.2 для записи формы W-L-OTL: случай из брифа Задачи 24
    (game_id 2023021166, MIN 1 – VGK 2) — 0-1-0 для проигравшей команды,
    а не 0-0-1."""
    bot_messages = bot_module("bot_messages")
    rows = _recent_games_rows([(_STREAK_OPP_ID, True, False, True)])
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        assert bot_messages._last_n_form_record(_STREAK_TEAM_ID, 5) == "0-1-0"


# ---------------------------------------------------------------------------
# matchup_season_preview() — escaping and missing-data branches
# ---------------------------------------------------------------------------

def test_matchup_season_preview_rejects_blank_or_placeholder_abbrev(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "cached_fetch_all") as mock_fetch:
        text, footer = bot_messages.matchup_season_preview(1, "", "NYR")
    assert "Не удалось сопоставить" in text
    assert footer == ""
    mock_fetch.assert_not_called()

    with patch.object(bot_messages, "cached_fetch_all") as mock_fetch:
        text, footer = bot_messages.matchup_season_preview(1, "?", "?")
    assert "Не удалось сопоставить" in text
    assert footer == ""
    mock_fetch.assert_not_called()


def test_matchup_season_preview_reports_when_neither_team_in_db(bot_module):
    bot_messages = bot_module("bot_messages")
    empty = {"count_rows": 0}
    with patch.object(bot_messages, "cached_fetch_all", return_value=empty):
        text, footer = bot_messages.matchup_season_preview(1, "AAA", "BBB")
    assert text.startswith("<b>AAA @ BBB</b>\n")
    assert "нет в базе бота" in text
    assert "<b>AAA</b>" not in text and "<b>BBB</b>" not in text, "аббревиатуры в тексте — без <b>"
    assert footer.startswith("Сезон ")


def test_matchup_season_preview_escapes_html_in_abbreviations(bot_module):
    bot_messages = bot_module("bot_messages")
    empty = {"count_rows": 0}
    with patch.object(bot_messages, "cached_fetch_all", return_value=empty):
        text, _footer = bot_messages.matchup_season_preview(1, "<i>AB", "CD&EF")
    assert "&lt;i&gt;AB" in text
    assert "CD&amp;EF" in text
    assert "<i>AB" not in text


def _preview_stats_row(home="BBB"):
    """Ответ запроса `teams_stats` для пары AAA @ home (общий для тестов превью)."""
    return {
        "abbr": ["AAA", home],
        "games_played": [10, 10],
        "wins": [7, 3],
        "losses": [3, 7],
        "ot": [0, 0],
        "points": [14, 6],
        "procent_points": [70.0, 30.0],
        "goals_per_game": [3.5, 2.1],
        "goals_against_per_game": [2.0, 3.0],
        "power_play_percentage": [25.0, 15.0],
        "penalty_kill_percentage": [80.0, 75.0],
        "shots_per_game": [32.0, 28.0],
        "face_off_win_percentage": [51.0, 49.0],
        "count_rows": 2,
    }


def _render_preview(bot_messages, predictions=None, home="BBB", game_id=777):
    """Превью AAA @ home; `predictions` — строки `game_predictions` {task: p} (None = записей нет).

    Возвращает ((тело, подвал), mock uncached `fetch_all`).
    """
    stats_row = _preview_stats_row(home)

    def fake_cached(query, params=None, columns=None):
        q = str(query)
        if "teams_stats ts" in q:
            return stats_row
        # _team_id_for_abbrev / _last_n_form_record / _h2h_season_wins
        if "team_id FROM teams" in q:
            return {"count_rows": 0, "team_id": []}
        raise AssertionError(f"unexpected query: {q}")

    predictions = predictions or {}
    pred = {
        "count_rows": len(predictions),
        "task": list(predictions),
        "probability": list(predictions.values()),
    }
    with patch.object(bot_messages, "cached_fetch_all", side_effect=fake_cached), \
            patch.object(bot_messages, "fetch_all", return_value=pred) as mock_fetch:
        parts = bot_messages.matchup_season_preview(game_id, "AAA", home)
    return parts, mock_fetch


def test_matchup_season_preview_full_comparison_when_both_teams_known(bot_module):
    bot_messages = bot_module("bot_messages")
    (text, footer), _ = _render_preview(bot_messages)

    # Тело открывает матч без «— сезон …»: сезон ушёл в подвал.
    assert text.startswith("<b>AAA @ BBB</b>")
    assert "сезон" not in text.splitlines()[0]
    assert footer.splitlines()[0].startswith("Сезон ")
    # Одна таблица «метрика · гости · хозяева», аббревиатуры — в шапке колонок,
    # разница шайб со знаком.
    [table] = _tables(text)
    assert table[:4] == [
        ["", "AAA", "BBB"],
        ["Очки", "14", "6"],
        ["% очков", "70%", "30%"],
        ["Баланс W-L-OT", "7-3-0", "3-7-0"],
    ]
    assert table[-1] == ["Разница шайб", "+1.5", "-0.9"]
    # Нет записи в game_predictions -> ни строки оценки, ни заглушки.
    assert "Оценка модели" not in text + footer
    assert "н/д" not in text


def test_matchup_season_preview_bolds_best_value_per_metric(bot_module):
    """Лучшее значение метрики — `<b>` внутри ячейки; у «Пропущено/игра» лучшее — меньшее;
    равные значения и строки баланса/формы/серии без выделения."""
    bot_messages = bot_module("bot_messages")
    (text, _footer), _ = _render_preview(bot_messages)

    rows = {
        re.sub(r"<[^>]+>", "", cells[0]): cells[1:]
        for cells in (re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row)
                      for row in re.findall(r"<tr>(.*?)</tr>", text))
        if cells
    }
    assert rows["Очки"] == ["<b>14</b>", "6"]
    assert rows["Пропущено/игра"] == ["<b>2</b>", "3"], "меньше пропущено — лучше"
    assert rows["Баланс W-L-OT"] == ["7-3-0", "3-7-0"], "баланс не выделяется"


def test_matchup_season_preview_does_not_bold_equal_values(bot_module):
    bot_messages = bot_module("bot_messages")
    stats_row = {**_preview_stats_row(), "points": [10, 10]}

    def fake_cached(query, params=None, columns=None):
        q = str(query)
        if "teams_stats ts" in q:
            return stats_row
        if "team_id FROM teams" in q:
            return {"count_rows": 0, "team_id": []}
        raise AssertionError(f"unexpected query: {q}")

    pred = {"count_rows": 0, "task": [], "probability": []}
    with patch.object(bot_messages, "cached_fetch_all", side_effect=fake_cached), \
            patch.object(bot_messages, "fetch_all", return_value=pred):
        text, _footer = bot_messages.matchup_season_preview(1, "AAA", "BBB")

    [points] = [r for r in re.findall(r"<tr>(.*?)</tr>", text) if ">Очки<" in r]
    assert "<b>" not in points


def test_matchup_season_preview_appends_win_and_total_to_footer(bot_module):
    bot_messages = bot_module("bot_messages")
    (text, footer), mock_fetch = _render_preview(
        bot_messages, {"over_5_5": 0.468, "home_win": 0.574}
    )

    assert footer.splitlines()[-1] == "Оценка модели (не совет): победа BBB — 57%, тотал больше 5.5 — 47%"
    assert "Оценка модели" not in text and "🤖" not in footer
    # Запрос параметризован game_id из кнопки, берёт обе задачи, без кэша.
    query, params = mock_fetch.call_args.args[:2]
    assert "game_predictions" in query and "task IN ('home_win', 'over_5_5')" in query
    assert params == (777,)


def test_matchup_season_preview_shows_total_without_win(bot_module):
    """Гейт победы не пройден — строки home_win нет, тотал выходит один."""
    bot_messages = bot_module("bot_messages")
    (_text, footer), _ = _render_preview(bot_messages, {"over_5_5": 0.55})

    assert footer.splitlines()[-1] == "Оценка модели (не совет): тотал больше 5.5 — 55%"


def test_matchup_season_preview_escapes_home_abbrev_in_prediction_line(bot_module):
    bot_messages = bot_module("bot_messages")
    (_text, footer), _ = _render_preview(bot_messages, {"home_win": 0.5}, home="B&B")

    assert footer.splitlines()[-1] == "Оценка модели (не совет): победа B&amp;B — 50%"


def test_matchup_season_preview_shows_form_streak_and_head_to_head_rows(bot_module):
    """Задача 24: форма и серия — строками таблицы сравнения; у команды без
    игр форма 0-0-0, а серии нет («—»). Личные встречи — строка с победами."""
    bot_messages = bot_module("bot_messages")
    stats_row = _preview_stats_row()
    team_ids = {"AAA": 111, "BBB": 222}
    # AAA: 2 победы подряд, затем поражение -> форма 2-1-0, серия W2.
    aaa_games = _recent_games_rows([
        (111, False, False, False),
        (111, False, False, False),
        (222, False, False, False),
    ])
    # BBB: в сезоне пока нет завершённых игр -> форма 0-0-0, серия "—".
    bbb_games = _recent_games_rows([])

    def fake_fetch(query, params=None, columns=None):
        q = str(query)
        if "teams_stats ts" in q:
            return stats_row
        if "FROM games g WHERE" in q:
            return aaa_games if params[1] == 111 else bbb_games
        if "team_id FROM teams" in q:
            tid = team_ids.get(params[1])
            return {"count_rows": 1 if tid else 0, "team_id": [tid] if tid else []}
        if "((home_team_id" in q:
            return {"count_rows": 1, "winner_id": [222]}
        raise AssertionError(f"unexpected query: {q}")

    with patch.object(bot_messages, "cached_fetch_all", side_effect=fake_fetch), \
            patch.object(bot_messages, "fetch_all", return_value={"count_rows": 0, "task": [], "probability": []}):
        text, _footer = bot_messages.matchup_season_preview(1, "AAA", "BBB")

    [table] = _tables(text)
    assert ["Форма (5 игр)", "2-1-0", "0-0-0"] in table
    assert ["Серия", "W2", "—"] in table
    assert ["Личные встречи", "0", "1"] in table


# ---------------------------------------------------------------------------
# player_stat_leaderboard_page() / team_stat_leaderboard_page() — pagination
# ---------------------------------------------------------------------------

def _leader_rows(n: int, total: int | None = None):
    # COUNT(*) OVER () отдаёт одно и то же total в каждой строке страницы;
    # при n == 0 (пустая выборка) окно тоже не возвращает строк — total: [].
    if total is None:
        total = n
    return {
        "lastname": [f"Player{i}" for i in range(n)],
        "roster_position": ["C"] * n,
        "points": [100 - i for i in range(n)],
        "team": ["WSH"] * n,
        # Задача 18 — «хвост» строки лидерборда (games/shifts), обязателен в
        # каждой фикстуре players_season_stats: player_stats_with_count читает
        # эти ключи напрямую (без запасного None), их отсутствие — KeyError.
        "tail_games": [82] * n,
        "tail_shifts": [1500 + i for i in range(n)],
        "total": [total] * n,
        "count_rows": n,
    }


def test_player_stat_leaderboard_page_full_page_has_next_no_prev(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "cached_fetch_all", return_value=_leader_rows(10, total=25)
    ):
        text, has_prev, has_next = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 0
        )
    assert has_prev is False
    assert has_next is True
    # Колонка показателя подписана им самим, а не «Знач».
    [table] = _tables(text)
    assert table[:2] == [["", "Игрок", "Ком", "Очки", "И", "Смен"], ["1.", "Player0 [C]", "WSH", "100", "82", "1500"]]
    assert len(table) == 11
    assert "Показаны 1–10 из 25" in text


def test_player_stat_leaderboard_page_middle_page_has_prev_and_next(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "cached_fetch_all", return_value=_leader_rows(10, total=25)
    ):
        text, has_prev, has_next = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 10
        )
    assert has_prev is True
    assert has_next is True
    assert "Показаны 11–20 из 25" in text
    assert _tables(text)[0][1] == ["11.", "Player0 [C]", "WSH", "100", "82", "1500"]


def test_player_stat_leaderboard_page_last_partial_page_has_no_next(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "cached_fetch_all", return_value=_leader_rows(3, total=13)
    ):
        text, has_prev, has_next = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 10
        )
    assert has_prev is True
    assert has_next is False
    assert "Показаны 11–13 из 13" in text
    assert _tables(text)[0][1] == ["11.", "Player0 [C]", "WSH", "100", "82", "1500"]


def test_player_stat_leaderboard_page_full_last_page_has_no_next(bot_module):
    """`has_next` смотрит на total, а не на «страница пришла полной»: последняя
    полная страница (offset + n == total) не должна рисовать кнопку «вперёд»
    на пустой диапазон — маркер тут же говорит «показаны 741–750 из 750»."""
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "cached_fetch_all", return_value=_leader_rows(10, total=10)
    ):
        text, _, has_next = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 0
        )
    assert has_next is False
    assert "Показаны 1–10 из 10" in text
    assert _tables(text)[0][1] == ["1.", "Player0 [C]", "WSH", "100", "82", "1500"]


def test_player_stat_leaderboard_page_empty_range_shows_placeholder(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "cached_fetch_all", return_value=_leader_rows(0)):
        text, has_prev, has_next = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 50
        )
    assert "Нет данных в этом диапазоне." in text
    assert has_next is False
    assert "Показаны" not in text


def test_team_stat_leaderboard_page_renders_heading_and_rows(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": ["BOS"], "v0": [55.5], "sort": [55.5], "games_played": [40],
        # Задача 18 — хвост wins-losses-ot/points, обязателен в фикстуре:
        # team_stats_with_count читает эти ключи напрямую (без запасного None).
        "wins": [30], "losses": [8], "ot": [2], "record_points": [62],
        "total": [12], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, has_prev, has_next = bot_messages.team_stat_leaderboard_page(
            "Статистика большинства", ("power_play_percentage",), 0
        )
    assert "Статистика большинства" in text
    assert _tables(text) == [[["", "Ком", "%бол", "И", "В-П-ОТ", "О"], ["1.", "BOS", "55.5", "40", "30-8-2", "62"]]]
    assert has_prev is False
    assert has_next is True
    assert "Показаны 1 из 12" in text


def test_team_stat_leaderboard_page_full_last_page_has_no_next(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": [f"T{i}" for i in range(10)],
        "v0": [float(i) for i in range(10)],
        "sort": [float(i) for i in range(10)],
        "games_played": [40] * 10,
        "wins": [10 + i for i in range(10)],
        "losses": [5] * 10,
        "ot": [1] * 10,
        "record_points": [float(i) for i in range(10)],
        "total": [10] * 10,
        "count_rows": 10,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, has_next = bot_messages.team_stat_leaderboard_page("Топ", ("power_play_percentage",), 0)
    assert has_next is False
    assert "Показаны 1–10 из 10" in text
    assert _tables(text)[0][1] == ["1.", "T0", "0", "40", "10-5-1", "0"]


def test_stat_column_labels_cover_every_leaderboard_column(bot_module):
    """Шапка колонки показателя берётся по ключу без запасного значения —
    у каждого лидерборда меню должна быть своя подпись вместо «Знач»."""
    specs = bot_module("leaderboard_specs")
    team_single = {cols[0] for _title, cols in specs.TEAM_STAT_SPECS.values() if len(cols) == 1}
    columns = {col for _table, col in specs.PLAYER_STAT_TITLES} | team_single
    assert columns <= set(specs.STAT_COLUMN_LABELS)
    assert all(len(label) <= 5 for label in specs.STAT_COLUMN_LABELS.values())


def test_team_stat_leaderboard_page_for_against_pair_shows_both_difference_and_games(bot_module):
    """Пара колонок «за/против» (`/team_stats` → «Голы», «Броски»): средние за игру,
    разница со знаком, игры; без хвоста баланса — шире строка на
    телефоне не помещается."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": ["WSH", "BOS"], "v0": [3.4, 2.9], "v1": [2.5, None], "sort": [0.9, None],
        "games_played": [17, 20], "wins": [10, 12], "losses": [5, 6], "ot": [2, 1],
        "record_points": [22, 25], "total": [32, 32], "count_rows": 2,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, has_prev, has_next = bot_messages.team_stat_leaderboard_page(
            "Голы", ("goals_per_game", "goals_against_per_game"), 10
        )
    assert _tables(text) == [[
        ["", "Ком", "За", "Пр", "Разн", "И"],
        ["11.", "WSH", "3.4", "2.5", "+0.9", "17"],
        ["12.", "BOS", "2.9", "—", "—", "20"],
    ]]
    assert (has_prev, has_next) == (True, True)


def test_team_stats_with_count_rejects_column_outside_allowlist(bot_module):
    """Колонки идут в SQL идентификаторами — только из белого списка, в паре тоже."""
    bot_messages = bot_module("bot_messages")
    with pytest.raises(ValueError, match="Column not allowed"):
        bot_messages.team_stats_with_count(("goals_per_game", "goals; DROP TABLE teams"))


# ---------------------------------------------------------------------------
# Задача 18 — «спящие» данные: постоянный хвост строки лидерборда
# (games/shifts у игроков, games/saves/shots_against у вратарей,
# wins-losses-ot/points у команд). Одна и та же механика на всех трёх
# поверхностях, нулевой риск неверной сортировки — сама сортировка не меняется.
# ---------------------------------------------------------------------------

def test_team_stat_leaderboard_page_shows_record_tail(bot_module):
    """(A) Лидерборд команд: хвост игр:/W-L-OT/очков поверх текущей строки."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": ["BOS"], "v0": [55], "sort": [55], "games_played": [40],
        "wins": [25], "losses": [10], "ot": [5], "record_points": [55],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.team_stat_leaderboard_page("Топ", ("power_play_percentage",), 0)
    assert _tables(text)[0][1] == ["1.", "BOS", "55", "40", "25-10-5", "55"]


def test_team_stat_leaderboard_page_null_record_fields_render_dash(
    bot_module, monkeypatch: pytest.MonkeyPatch
):
    """Хвост обязан переживать NULL (Задача 18: «не заполнены везде»)."""
    bot_messages = bot_module("bot_messages")
    monkeypatch.setattr(bot_messages.config, "CURRENT_SEASON", "25/26")
    rows = {
        "team": ["BOS"], "v0": [None], "sort": [None], "games_played": [40],
        "wins": [None], "losses": [None], "ot": [None], "record_points": [None],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.team_stat_leaderboard_page("Топ", ("power_play_percentage",), 0)
    assert _tables(text)[0][1] == ["1.", "BOS", "—", "40", "—-—-—", "—"]
    assert "None" not in text


def test_player_stat_leaderboard_page_season_end_goalie_keeps_every_column(bot_module):
    """Конец сезона, 10-е место: в rich-таблице колонки раскладывает Telegram — сейвы
    и полная фамилия остаются (в `<pre>` строка шире телефона теряла сейвы, фамилия
    обрезалась до «Vasilevsk…»)."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "lastname": ["Vasilevskiy"], "roster_position": ["G"],
        "points": [92.52], "team": ["TBL"],
        "tail_games": [58], "tail_saves": [1353], "tail_shots_against": [1483],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Топ", "goalies_season_stats", "save_percentage", 9
        )
    assert _tables(text) == [[
        ["", "Вратарь", "Ком", "%ОБ", "И", "Сейвы"],
        ["10.", "Vasilevskiy", "TBL", "92.52", "58", "1353/1483"],
    ]]


def test_player_stat_leaderboard_page_shows_games_and_shifts_tail(bot_module):
    """(B) Лидерборд полевых игроков (players_season_stats): игр/смен."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "lastname": ["McDavid"], "roster_position": ["C"],
        "points": [153], "team": ["EDM"],
        "tail_games": [82], "tail_shifts": [1800],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 0
        )
    assert _tables(text)[0][1] == ["1.", "McDavid [C]", "EDM", "153", "82", "1800"]


def test_player_stat_leaderboard_page_null_shifts_renders_dash_not_none(
    bot_module, monkeypatch: pytest.MonkeyPatch
):
    """Реальный сценарий из БД (Задача 18): shifts NULL в 7 строках на 2
    сезона — рендер обязан отдать «—», а не «None» и не упасть."""
    bot_messages = bot_module("bot_messages")
    monkeypatch.setattr(bot_messages.config, "CURRENT_SEASON", "25/26")
    rows = {
        "lastname": ["Rookie"], "roster_position": ["C"],
        "points": [10], "team": ["CHI"],
        "tail_games": [5], "tail_shifts": [None],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Топ", "players_season_stats", "points", 0
        )
    assert _tables(text)[0][1] == ["1.", "Rookie [C]", "CHI", "10", "5", "—"]
    assert "None" not in text


def test_player_stat_leaderboard_page_advanced_stats_also_shows_games_shifts(bot_module):
    """(B) Лидерборд players_advanced_stats («Расширенная статистика») тоже
    отдаёт игр/смен — через LEFT/INNER JOIN players_season_stats, не только
    для players_season_stats напрямую."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "lastname": ["Makar"], "roster_position": ["D"],
        "points": [58.2], "team": ["COL"],
        "tail_games": [70], "tail_shifts": [1600],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Лидеры по Corsi", "players_advanced_stats", "sat_pct", 0
        )
    assert _tables(text) == [[["", "Игрок", "Ком", "SAT%", "И", "Смен"], ["1.", "Makar [D]", "COL", "58.2", "70", "1600"]]]


def test_player_stat_leaderboard_page_shot_types_also_shows_games_shifts(bot_module):
    """(B) Лидерборд players_shot_types тоже отдаёт игр/смен через JOIN."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "lastname": ["Kucherov"], "roster_position": ["RW"],
        "points": [12], "team": ["TBL"],
        "tail_games": [78], "tail_shifts": [1700],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Голы с кистевого", "players_shot_types", "goals_wrist", 0
        )
    assert _tables(text) == [[
        ["", "Игрок", "Ком", "Голы", "И", "Смен"], ["1.", "Kucherov [RW]", "TBL", "12", "78", "1700"],
    ]]


# ---------------------------------------------------------------------------
# JOIN-доказательство (ревью Задачи 18, раунд 2): два теста выше мокают
# cached_fetch_all и никогда не смотрят на составленный SQL — если бы
# _leaderboard_tail_columns/_pss_join_sql молча свалили хвост игроков на
# алиас `pl` вместо `pss` (или потеряли ветку JOIN для players_shot_types),
# они остались бы зелёными, а расширенная статистика упала бы в проде ошибкой Postgres
# «missing FROM-clause entry for table pss». Ниже — fake_db_router
# (tests/conftest.py:202): патчит только границу БД, композиция SQL реальная,
# и `cursor.executed` отдаёт фактический текст запроса на assert.
# ---------------------------------------------------------------------------

def test_player_stat_leaderboard_page_advanced_stats_joins_pss_for_tail(
    bot_module, fake_db_router
):
    """(B) players_advanced_stats: хвост игр/смен реально приходит через
    INNER JOIN players_season_stats pss, колонки хвоста qualified алиасом
    `pss`, а не `pl`."""
    bot_messages = bot_module("bot_messages")
    # lastname, roster_position, points(pl.sat_pct), team, tail_games, tail_shifts, total
    row = ("Makar", "D", 58.2, "COL", 70, 1600, 1)
    cursor = fake_db_router([("COUNT(*) OVER () AS total", [row])])

    bot_messages.player_stat_leaderboard_page(
        "Лидеры по Corsi", "players_advanced_stats", "sat_pct", 0
    )

    query_text, _params = cursor.executed[-1]
    assert "INNER JOIN players_season_stats pss" in query_text
    assert "Identifier('pss'), SQL('.'), Identifier('games')" in query_text
    assert "Identifier('pss'), SQL('.'), Identifier('shifts')" in query_text


@pytest.mark.parametrize(
    "column, by_seconds",
    [("time_on_ice_per_game", True), ("points", False), ("hits", False)],
)
def test_player_stat_leaderboard_page_sorts_mmss_time_by_seconds(
    bot_module, fake_db_router, column, by_seconds
):
    """Задача 44: «м:сс» строкой сортируется неверно («9:57» > «25:00») — для
    колонки времени ORDER BY идёт по секундам, числовые колонки не меняются."""
    bot_messages = bot_module("bot_messages")
    row = ("Hughes", "D", "27:44", "NJD", 17, 400, 1)
    cursor = fake_db_router([("COUNT(*) OVER () AS total", [row])])

    bot_messages.player_stat_leaderboard_page("x", "players_season_stats", column, 0)

    query_text, _params = cursor.executed[-1]
    order_by = query_text.split("ORDER BY", 1)[1]
    seconds_sort = (
        f"SQL('(split_part('), Composed([Identifier('pl'), SQL('.'), Identifier('{column}')])"
    )
    plain_sort = f"Composed([Identifier('pl'), SQL('.'), Identifier('{column}')]), SQL(' DESC NULLS LAST"
    # Первый ключ ORDER BY — секунды для «м:сс» и сама колонка для остальных.
    first_key = f"Composed([{seconds_sort}" if by_seconds else plain_sort
    assert order_by.startswith(f" '), {first_key}")
    assert ("split_part" in query_text) is by_seconds


def test_player_stat_leaderboard_page_shot_types_joins_pss_for_tail(
    bot_module, fake_db_router
):
    """(B) players_shot_types: хвост игр/смен реально приходит через
    LEFT JOIN players_season_stats pss, колонки хвоста qualified алиасом
    `pss`, а не `pl`."""
    bot_messages = bot_module("bot_messages")
    row = ("Kucherov", "RW", 12, "TBL", 78, 1700, 1)
    cursor = fake_db_router([("COUNT(*) OVER () AS total", [row])])

    bot_messages.player_stat_leaderboard_page(
        "Голы с кистевого", "players_shot_types", "goals_wrist", 0
    )

    query_text, _params = cursor.executed[-1]
    assert "LEFT JOIN players_season_stats pss" in query_text
    assert "Identifier('pss'), SQL('.'), Identifier('games')" in query_text
    assert "Identifier('pss'), SQL('.'), Identifier('shifts')" in query_text


def test_player_stat_leaderboard_page_goalies_no_join_pl_qualified_tail(
    bot_module, fake_db_router
):
    """(C) goalies_season_stats: все поля хвоста уже в самой таблице — нет
    JOIN на players_season_stats, хвост qualified алиасом `pl`."""
    bot_messages = bot_module("bot_messages")
    # lastname, roster_position, points(pl.wins), team,
    # tail_games, tail_saves, tail_shots_against, total
    row = ("Vasilevskiy", "G", 39, "TBL", 58, 1353, 1483, 1)
    cursor = fake_db_router([("COUNT(*) OVER () AS total", [row])])

    bot_messages.player_stat_leaderboard_page(
        "Топ", "goalies_season_stats", "wins", 0
    )

    query_text, _params = cursor.executed[-1]
    assert "players_season_stats pss" not in query_text
    assert "Identifier('pl'), SQL('.'), Identifier('games')" in query_text
    assert "Identifier('pl'), SQL('.'), Identifier('shots_against')" in query_text
    assert "time_on_ice" not in query_text, "время вратаря в лидерборде не показывается"


def test_player_stat_leaderboard_page_shows_goalie_tail(bot_module):
    """(C) Лидерборд вратарей: игры и сейвы/броски; позиции [G] и времени нет."""
    bot_messages = bot_module("bot_messages")
    rows = {
        "lastname": ["Shesterkin"], "roster_position": ["G"],
        "points": [0.925], "team": ["NYR"],
        "tail_games": [55], "tail_saves": [1500], "tail_shots_against": [1620],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Топ", "goalies_season_stats", "save_percentage", 0
        )
    assert _tables(text) == [[
        ["", "Вратарь", "Ком", "%ОБ", "И", "Сейвы"], ["1.", "Shesterkin", "NYR", "0.925", "55", "1500/1620"],
    ]]


def test_player_stat_leaderboard_page_goalie_null_tail_fields_render_dash(
    bot_module, monkeypatch: pytest.MonkeyPatch
):
    """Рендер не должен полагаться на заполненность хвоста вратаря и обязан
    пережить NULL в любом его поле."""
    bot_messages = bot_module("bot_messages")
    monkeypatch.setattr(bot_messages.config, "CURRENT_SEASON", "25/26")
    rows = {
        "lastname": ["Backup"], "roster_position": ["G"],
        "points": [0.0], "team": ["CHI"],
        "tail_games": [1], "tail_saves": [None], "tail_shots_against": [None],
        "total": [1], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, _ = bot_messages.player_stat_leaderboard_page(
            "Топ", "goalies_season_stats", "save_percentage", 0
        )
    assert _tables(text)[0][1] == ["1.", "Backup", "CHI", "0", "1", "—/—"]
    assert "None" not in text


# ---------------------------------------------------------------------------
# truncation_marker() — единственный хелпер сноски об усечении (Задача 11)
# ---------------------------------------------------------------------------

def test_truncation_marker_with_counts_names_shown_and_total(bot_module):
    bot_messages = bot_module("bot_messages")
    marker = bot_messages.truncation_marker(5, 20, item_word="строк")
    assert marker == "<i>Показаны 5 из 20 строк.</i>"


def test_truncation_marker_accepts_range_string_as_shown(bot_module):
    bot_messages = bot_module("bot_messages")
    marker = bot_messages.truncation_marker("11–20", 750, item_word="строк")
    assert marker == "<i>Показаны 11–20 из 750 строк.</i>"


def test_truncation_marker_without_counts_is_plain_telegram_limit_note(bot_module):
    bot_messages = bot_module("bot_messages")
    assert bot_messages.truncation_marker() == "<i>Текст обрезан (лимит Telegram).</i>"


def test_truncation_marker_rejects_shown_without_total(bot_module):
    """GC4 («падать громко»): shown без total не имеет однозначного смысла —
    тихий рендер «Показаны 5 из None.» запрещён, функция должна упасть."""
    bot_messages = bot_module("bot_messages")
    with pytest.raises(ValueError, match="total"):
        bot_messages.truncation_marker(5)


# ---------------------------------------------------------------------------
# truncate_telegram_text()
# ---------------------------------------------------------------------------

def test_truncate_telegram_text_leaves_short_text_untouched(bot_module):
    bot_messages = bot_module("bot_messages")
    text = "short message"
    assert bot_messages.truncate_telegram_text(text) == text


def _lines_over_limit(bot_messages, line: str) -> str:
    """Построчный текст длиннее лимита rich-сообщения на несколько строк."""
    return "\n".join([line] * (bot_messages.RICH_MESSAGE_MAX_LENGTH // len(line) + 3))


def test_truncate_telegram_text_cuts_whole_lines_and_appends_default_note(bot_module):
    """Дефолтная сноска (без footer_note) приходит из truncation_marker() — тот же
    хелпер, что и все остальные ветки усечения, а не свой строковый литерал. Режется
    целыми строками: строка — абзац или таблица rich-сообщения, обрывок оставил бы
    незакрытый тег."""
    bot_messages = bot_module("bot_messages")
    line = "<b>" + "x" * 90 + "</b>"
    long_text = _lines_over_limit(bot_messages, line)
    note = "\n\n" + bot_messages.truncation_marker()

    result = bot_messages.truncate_telegram_text(long_text)

    assert result.endswith(note)
    kept = result[: -len(note)].split("\n")
    assert set(kept) == {line}, "только целые строки"
    assert len(result) <= bot_messages.RICH_MESSAGE_MAX_LENGTH
    # Ещё одна строка уже не влезла бы — режется не больше необходимого.
    assert len("\n".join(kept + [line])) > bot_messages._telegram_cut_budget(note)


def test_truncate_telegram_text_uses_custom_footer_note(bot_module):
    bot_messages = bot_module("bot_messages")
    long_text = _lines_over_limit(bot_messages, "y" * 100)
    footer_note = "\n\nCUT"
    result = bot_messages.truncate_telegram_text(long_text, footer_note=footer_note)
    assert result.endswith("y\n\nCUT")
    assert len(result) <= bot_messages.RICH_MESSAGE_MAX_LENGTH


def test_truncate_telegram_text_keeps_last_line_that_fits_exactly(bot_module):
    """Строка, конец которой ровно на границе бюджета, помещается — не отбрасывается."""
    bot_messages = bot_module("bot_messages")
    text = "\n".join(["a" * 9] * 20)
    result = bot_messages.truncate_telegram_text(text, max_len=102, footer_note="\n\nN")
    assert result == "\n".join(["a" * 9] * 10) + "\n\nN"
    assert len(result) == 102


def test_truncate_telegram_text_keeps_a_table_line_whole(bot_module):
    """Таблица — одна строка текста: её нельзя разрезать посередине, она либо
    помещается целиком, либо уходит целиком."""
    bot_messages = bot_module("bot_messages")
    table = bot_messages._table([["A"]] + [["x" * 50]] * 200, "l")
    text = "<b>Заголовок</b>\n" + table
    result = bot_messages.truncate_telegram_text(text, max_len=len(text) - 10)
    assert "<table" not in result
    assert result.startswith("<b>Заголовок</b>")


# ---------------------------------------------------------------------------
# digest_shown_match_count() — сколько заголовков матчей уместилось после
# обрезки truncate_telegram_text по лимиту Telegram
# ---------------------------------------------------------------------------

def test_digest_shown_match_count_returns_full_total_when_everything_fits(bot_module):
    bot_messages = bot_module("bot_messages")
    lines = ["Матч 1", "Матч 2", "Матч 3"]
    assert bot_messages.digest_shown_match_count("Header\n\n", lines, 3) == 3


def test_digest_shown_match_count_drops_lines_that_do_not_fit_budget(bot_module):
    bot_messages = bot_module("bot_messages")
    long_line = "X" * 500
    lines = [long_line] * 80  # 80 * 500 = 40000 символов, заведомо больше лимита rich
    shown = bot_messages.digest_shown_match_count("Header\n\n", lines, 80)
    assert 0 < shown < 80

    # ровно shown строк должны укладываться в бюджет truncate_telegram_text с
    # footer_note, построенным на этом же shown …
    note = "\n\n" + bot_messages.truncation_marker(shown, 80, item_word="матчей")
    cut = bot_messages._telegram_cut_budget(note)
    assert len("Header\n\n" + "\n".join(lines[:shown])) <= cut

    # … а на одну строку больше — уже нет (иначе маркер солгал бы про N).
    note_next = "\n\n" + bot_messages.truncation_marker(shown + 1, 80, item_word="матчей")
    cut_next = bot_messages._telegram_cut_budget(note_next)
    assert len("Header\n\n" + "\n".join(lines[: shown + 1])) > cut_next


def test_digest_shown_match_count_empty_lines_returns_zero(bot_module):
    bot_messages = bot_module("bot_messages")
    assert bot_messages.digest_shown_match_count("Header\n\n", [], 0) == 0


# ---------------------------------------------------------------------------
# day_digest_summary_body()
# ---------------------------------------------------------------------------

def _goal_row(period, home, away):
    """Строка get_goals_game: важны только период и счёт после гола."""
    return ("X", "C", None, None, None, None, None, period, "01:00", home, away,
            False, False, False, False, 1, 1)


def test_day_digest_summary_body_plain_matchup_and_italic_indented_periods(bot_module, fake_db_router):
    """Матч — «N. Хозяева счёт Гости» обычным шрифтом по левому краю, без <b>
    (моноширинное выравнивание по счёту выглядело криво, отзыв 2026-10-03);
    периоды и OT — вторая строка курсивом с отступом figure space."""
    bot_messages = bot_module("bot_messages")
    stats = {
        1: [(0, 0, 0, 0, 0, False, False, "home", "Flyers"),
            (2, 0, 0, 0, 0, False, False, "away", "Penguins")],
        2: [(2, 0, 0, 0, 0, True, False, "home", "Maple Leafs"),
            (1, 0, 0, 0, 0, True, False, "away", "Kings")],
    }
    goals = {
        1: [_goal_row(1, 0, 1), _goal_row(3, 0, 2)],
        2: [_goal_row(1, 1, 0), _goal_row(2, 1, 1), _goal_row(4, 2, 1)],
    }
    fake_db_router([
        ("get_game_stats", lambda params: stats[params[0]]),
        ("get_goals_game", lambda params: goals[params[0]]),
    ])

    body = bot_messages.day_digest_summary_body([1, 2])

    assert body == [
        "1. Flyers 0:2 Penguins\n  <i>0:1, 0:0, 0:1</i>",
        "2. Maple Leafs 2:1 Kings\n  <i>1:0, 0:1, 0:0, 1:0 (OT)</i>",
    ]


def test_day_digest_summary_body_keeps_scores_unpadded_and_escapes_names(
    bot_module, fake_db_router
):
    """Без выравнивания по колонке «10:2» и «3:2» идут как есть — никакой
    добивки; имена команд из БД экранируются."""
    bot_messages = bot_module("bot_messages")
    stats = {
        1: [(10, 0, 0, 0, 0, False, False, "home", "A"), (2, 0, 0, 0, 0, False, False, "away", "B")],
        2: [(3, 0, 0, 0, 0, False, False, "home", "C&C"), (2, 0, 0, 0, 0, False, False, "away", "D")],
    }
    fake_db_router([
        ("get_game_stats", lambda params: stats[params[0]]),
        ("get_goals_game", []),
    ])

    lines = bot_messages.day_digest_summary_body([1, 2])

    assert lines[0].startswith("1. A 10:2 B\n")
    assert lines[1].startswith("2. C&amp;C 3:2 D\n")
    assert all("<b>" not in line for line in lines), "строка матча без <b> (стиль §9)"


def test_digest_game_button_labels_number_games_in_summary_order_with_home_first(
    bot_module, fake_db_router
):
    """Подпись — «N. HOME – AWAY» в порядке game_ids (не в порядке выдачи БД),
    одним запросом на все матчи; номер тот же, что в сводке."""
    bot_messages = bot_module("bot_messages")
    # БД отдаёт строки в другом порядке, чем запрошены game_id.
    cursor = fake_db_router([("JOIN teams th", [(2, "TOR ", "MTL"), (1, "NYR", "BOS")])])

    labels = bot_messages.digest_game_button_labels([1, 2])

    assert labels == ["1. NYR – BOS", "2. TOR – MTL"]
    assert len(cursor.executed) == 1
    assert cursor.executed[0][1] == ([1, 2],)


# ---------------------------------------------------------------------------
# last_night_day() — игровой день NHL прошедшей ночи по Москве (UTC+3)
# ---------------------------------------------------------------------------

def _freeze_utc_now(monkeypatch, bot_messages, frozen_utc):
    """Подменяет `bot_messages.datetime`: `now(tz)` отдаёт замороженный момент в
    переданном поясе — как настоящий `datetime.now(tz)`."""
    class _FrozenDatetime(bot_messages.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_utc.astimezone(tz)

    monkeypatch.setattr(bot_messages, "datetime", _FrozenDatetime)


@pytest.mark.parametrize(
    "utc_now, nights_back, expected",
    [
        ((2026, 10, 2, 9, 0), 0, "2026-10-01"),   # 12:00 МСК, 2 октября → день NHL 1 октября
        ((2026, 10, 2, 9, 0), 1, "2026-09-30"),   # ночь раньше
        ((2026, 10, 1, 23, 30), 0, "2026-10-01"),  # 02:30 МСК 2 октября — в МСК уже новый день
        ((2026, 10, 1, 20, 59), 0, "2026-09-30"),  # 23:59 МСК 1 октября — ещё 1 октября
    ],
)
def test_last_night_day_is_moscow_date_minus_one_night(
    bot_module, monkeypatch, utc_now, nights_back, expected
):
    from datetime import datetime, timezone

    bot_messages = bot_module("bot_messages")
    _freeze_utc_now(monkeypatch, bot_messages, datetime(*utc_now, tzinfo=timezone.utc))

    assert bot_messages.last_night_day(nights_back) == expected


# ---------------------------------------------------------------------------
# day_digest() — branch coverage around fetch_all + game_message collaboration
# ---------------------------------------------------------------------------

def test_day_digest_reports_no_completed_games_when_day_omitted(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "fetch_all", return_value={"day": [None], "count_rows": 1}
    ):
        day_label, games = bot_messages.day_digest(None)
    assert day_label is None
    assert games == [(0, "В базе пока нет завершенных матчей.", [])]


def test_day_digest_reports_none_found_for_given_day_with_no_games(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "fetch_all", return_value={"game_id": [], "count_rows": 0}
    ):
        day_label, games = bot_messages.day_digest("2025-01-01")
    assert day_label == "2025-01-01"
    assert games == [(0, "За 2025-01-01 завершенных матчей не найдено.", [])]


def test_day_digest_builds_one_result_per_game_id(bot_module):
    bot_messages = bot_module("bot_messages")
    game_ids_row = {"game_id": [10, 20], "count_rows": 2}

    def fake_game_message(game_id):
        return f"text-{game_id}", [{"game_id": game_id, "event_id": 1, "label": "x"}]

    with patch.object(bot_messages, "fetch_all", return_value=game_ids_row), \
            patch.object(bot_messages, "game_message", side_effect=fake_game_message):
        day_label, games = bot_messages.day_digest("2025-01-01")

    assert day_label == "2025-01-01"
    assert games == [
        (10, "text-10", [{"game_id": 10, "event_id": 1, "label": "x"}]),
        (20, "text-20", [{"game_id": 20, "event_id": 1, "label": "x"}]),
    ]


def test_day_digest_falls_back_to_not_found_when_all_game_texts_empty(bot_module):
    bot_messages = bot_module("bot_messages")
    game_ids_row = {"game_id": [10], "count_rows": 1}

    with patch.object(bot_messages, "fetch_all", return_value=game_ids_row), \
            patch.object(bot_messages, "game_message", return_value=("", [])):
        day_label, games = bot_messages.day_digest("2025-01-01")

    assert games == [(0, "За 2025-01-01 завершенных матчей не найдено.", [])]


# ---------------------------------------------------------------------------
# player_stats_with_count() — identifier validation is not bypassed
# ---------------------------------------------------------------------------

def test_player_stats_with_count_rejects_unwhitelisted_table(bot_module):
    bot_messages = bot_module("bot_messages")
    with pytest.raises(ValueError, match="Table not allowed"):
        bot_messages.player_stats_with_count("Title", "users_secret", "points")


def test_player_stats_with_count_returns_total_from_window_function(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(
        bot_messages, "cached_fetch_all", return_value=_leader_rows(3, total=42)
    ):
        _, n, total = bot_messages.player_stats_with_count(
            "Title", "players_season_stats", "points"
        )
    assert n == 3
    assert total == 42


def test_player_stats_with_count_total_is_zero_for_empty_page(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "cached_fetch_all", return_value=_leader_rows(0)):
        _, n, total = bot_messages.player_stats_with_count(
            "Title", "players_season_stats", "points", offset=500
        )
    assert n == 0
    assert total == 0


# ---------------------------------------------------------------------------
# team_stats_with_count() — same total-from-window-function contract
# ---------------------------------------------------------------------------

def test_team_stats_with_count_returns_total_from_window_function(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": ["BOS"], "v0": [55.5], "sort": [55.5], "games_played": [40],
        # Задача 18 — хвост wins-losses-ot/points, обязателен в фикстуре:
        # team_stats_with_count читает эти ключи напрямую (без запасного None).
        "wins": [30], "losses": [8], "ot": [2], "record_points": [62],
        "total": [7], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        _, n, total = bot_messages.team_stats_with_count(("power_play_percentage",))
    assert n == 1
    assert total == 7


def test_team_stats_with_count_total_is_zero_for_empty_page(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": [], "v0": [], "sort": [], "games_played": [],
        "wins": [], "losses": [], "ot": [], "record_points": [],
        "total": [], "count_rows": 0,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        _, n, total = bot_messages.team_stats_with_count(("power_play_percentage",))
    assert n == 0
    assert total == 0


# ---------------------------------------------------------------------------
# team_list_text() — команды сезона по дивизионам кликабельными /ABBR
# ---------------------------------------------------------------------------

def test_team_list_text_groups_teams_by_division_as_clickable_commands(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {
        "abbr": ["BOS", "TOR", "NYR"],
        "name": ["Boston Bruins", "Toronto Maple Leafs", "New York Rangers"],
        "division": ["Atlantic", "Atlantic", "Metropolitan"],
        "count_rows": 3,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text = bot_messages.team_list_text()
    assert "/BOS — Boston Bruins\n/TOR — Toronto Maple Leafs" in text
    assert text.index("<b>Atlantic</b>") < text.index("/BOS") < text.index("<b>Metropolitan</b>")
    assert "<b>Metropolitan</b>\n/NYR — New York Rangers" in text
    assert "<code>" not in text, "в <code> команда не кликабельна"


def test_team_list_text_reports_empty_season(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {"abbr": [], "name": [], "division": [], "count_rows": 0}
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text = bot_messages.team_list_text()
    assert "В базе пока нет списка команд для этого сезона." in text


# ---------------------------------------------------------------------------
# Wild Card: черта плей-офф после WC2
# ---------------------------------------------------------------------------

def test_wild_card_block_draws_cut_line_after_second_wild_card_team(
    bot_module, fake_db_router
):
    """Метрополитен из шести команд: топ-3 дивизиона вне гонки, остальные три —
    WC1, WC2, затем черта плей-офф и третий претендент с обычным номером."""
    bot_messages = bot_module("bot_messages")
    # name, games_played, points, procent_points, wins, losses, ot,
    # division_name, conference_name — очки убывают с номером команды.
    rows = [
        (f"T{i}", 20, 40 - i, 60.0, 15 - i, 5, 0, "Metropolitan", "Eastern") for i in range(6)
    ]
    fake_db_router([
        ("FROM teams_stats ts", rows),
        ("SELECT max(day)::text AS d", [("2026-04-01",)]),
    ])

    text = bot_messages.team_table()

    wc = text.split("WILD CARD — EASTERN", 1)[1].split("WILD CARD — WESTERN", 1)[0]
    [table] = _tables(wc)
    assert [row[:2] for row in table] == [
        ["", "Команда"], ["WC1", "T3"], ["WC2", "T4"], [bot_messages._PLAYOFF_LINE], ["3.", "T5"],
    ]
    assert "\n\n<b>WILD CARD" in text, "секции разделены пустой строкой"
    assert text.startswith("<b>Турнирная таблица NHL</b>\n\n")
    assert text.rstrip().endswith(" · данные на 2026-04-01</footer>"), "дата — в подвале"



def test_division_block_draws_cut_line_after_top_three_and_wild_card_shows_five(
    bot_module, fake_db_router
):
    """Дивизион из девяти команд: черта плей-офф после 3-го места; в Wild Card
    из шести оставшихся показаны пять — WC1, WC2 и три претендента."""
    bot_messages = bot_module("bot_messages")
    rows = [
        (f"T{i}", 20, 40 - i, 60.0, 15 - i, 5, 0, "Metropolitan", "Eastern") for i in range(9)
    ]
    fake_db_router([
        ("FROM teams_stats ts", rows),
        ("SELECT max(day)::text AS d", [("2026-04-01",)]),
    ])

    text = bot_messages.team_table()

    div = text.split("METROPOLITAN DIVISION", 1)[1].split("WILD CARD — EASTERN", 1)[0]
    [table] = _tables(div)
    assert table[:6] == [
        ["", "Команда", "Очк", "Игр", "%очк"],
        ["1.", "T0", "40", "20", "60.0"],
        ["2.", "T1", "39", "20", "60.0"],
        ["3.", "T2", "38", "20", "60.0"],
        [bot_messages._PLAYOFF_LINE],
        ["4.", "T3", "37", "20", "60.0"],
    ]
    assert len(table) == 1 + 9 + 1, "шапка, девять команд и одна линия"
    wc = text.split("WILD CARD — EASTERN", 1)[1].split("WILD CARD — WESTERN", 1)[0]
    assert [row[:2] for row in _tables(wc)[0]][-1] == ["5.", "T7"]
    assert "T8" not in wc, "Wild Card обрезан до пяти команд"

# ---------------------------------------------------------------------------
# Порог расширенной статистики: смягчается в начале сезона
# ---------------------------------------------------------------------------

def test_advanced_stats_join_threshold_is_least_of_20_and_half_of_leader_games(
    bot_module, fake_db_router
):
    bot_messages = bot_module("bot_messages")
    cursor = fake_db_router([("COUNT(*) OVER () AS total", [("Makar", "D", 58.2, "COL", 5, 120, 1)])])

    bot_messages.player_stat_leaderboard_page(
        "Лидеры по Corsi", "players_advanced_stats", "sat_pct", 0
    )

    query_text, _params = cursor.executed[-1]
    assert (
        "pss.games >= LEAST(20, (SELECT CEIL(MAX(ts.games_played) / 2.0) "
        "FROM teams_stats ts WHERE ts.season_id = pl.season_id))"
    ) in query_text


# ---------------------------------------------------------------------------
# Задачи 42, 49: статистика по странам — country_rankings / country_page
# (БД подменена fake_db_router)
# ---------------------------------------------------------------------------

_COUNTRY_RANKING = ("GROUP BY r.nationality", [("CAN", 4, 100, 40), ("XXX", 3, 30, 9)])


def test_country_rankings_lists_countries_with_label_unknown_code_and_null_footnote(
    bot_module, fake_db_router
):
    bot_messages = bot_module("bot_messages")
    cursor = fake_db_router([_COUNTRY_RANKING, ("r.nationality IS NULL", [(7,)])])

    text, codes = bot_messages.country_rankings()

    assert codes == ["CAN", "XXX"]
    # Неизвестный код показывается как есть, без исключения.
    assert _tables(text) == [[
        ["", "Страна", "Игр", "О", "Г", "О/и"],
        ["1.", "Канада", "4", "100", "40", "25.0"],
        ["2.", "XXX", "3", "30", "9", "10.0"],
    ]]
    assert "Страна не указана в данных NHL у игроков: 7 (не учтены)." in text
    # Порог уходит в SQL параметром, вратари исключены.
    query, params = cursor.executed[0]
    assert params[1] == bot_messages.COUNTRY_MIN_PLAYERS
    assert "r.position <> 'G'" in query


def test_country_rankings_empty_season_returns_reason_and_no_codes(bot_module, fake_db_router):
    bot_messages = bot_module("bot_messages")
    fake_db_router([("GROUP BY r.nationality", [])])

    # Простой текст без разметки: причину показывает и экран подписок, а он не rich.
    assert bot_messages.country_rankings() == (
        "В базе нет данных по странам для этого сезона.",
        [],
    )


def _country_skater_row(lastname, goals, points, games, hits, shots, blocked):
    # lastname, team, goals, points, games, toi, hits, shots, blocked, total (total=25)
    return (lastname, "EDM", goals, points, games, "21:30", hits, shots, blocked, 25)


@pytest.mark.parametrize(
    "group, position_sql, extra_header",
    [("F", "r.position IN ('C', 'L', 'R')", "Бр"), ("D", "r.position = 'D'", "Бл")],
)
def test_country_page_skaters_show_group_stats_and_has_next(
    bot_module, fake_db_router, group, position_sql, extra_header
):
    """Нападающие — броски, защитники — блоки; группа уходит в SQL условием."""
    bot_messages = bot_module("bot_messages")
    cursor = fake_db_router([
        _COUNTRY_RANKING,
        ("OVER ()", [_country_skater_row("McDavid", 4, 8, 7, 5, 20, 2)]),
    ])

    text, has_prev, has_next = bot_messages.country_page("CAN", group, 10)

    assert "🇨🇦 Канада" in text
    assert "Показаны 11 из 25 строк." in text
    # Игры — сразу после голов и очков (Задача 50).
    extra = "20" if group == "F" else "2"
    assert _tables(text) == [[
        ["", "Игрок", "Ком", "Г", "О", "И", "ВП", "Хит", extra_header],
        ["11.", "McDavid", "EDM", "4", "8", "7", "21:30", "5", extra],
    ]]
    assert (has_prev, has_next) == (True, True)
    assert position_sql in cursor.executed[-1][0]


def test_country_page_keeps_every_column_and_full_last_name(
    bot_module, fake_db_router
):
    """Rich-таблицу раскладывает Telegram: длинная фамилия и все колонки остаются
    (в `<pre>` строка шире телефона теряла броски и силовые, фамилия обрезалась)."""
    bot_messages = bot_module("bot_messages")
    fake_db_router([
        _COUNTRY_RANKING,
        ("OVER ()", [_country_skater_row("Nugent-Hopkins", 64, 153, 82, 150, 300, 20)]),
    ])

    text, _, _ = bot_messages.country_page("CAN", "F", 0)

    assert _tables(text) == [[
        ["", "Игрок", "Ком", "Г", "О", "И", "ВП", "Хит", "Бр"],
        ["1.", "Nugent-Hopkins", "EDM", "64", "153", "82", "21:30", "150", "300"],
    ]]


def test_country_pages_reject_code_outside_ranking_without_touching_sql(
    bot_module, fake_db_router
):
    bot_messages = bot_module("bot_messages")
    cursor = fake_db_router([_COUNTRY_RANKING])

    for group in ("F", "G"):
        assert bot_messages.country_page("'; DROP", group, 0) == (
            "Страна не найдена в рейтинге этого сезона.", False, False,
        )
    assert len(cursor.executed) == 1  # только запрос рейтинга (кэш), без страницы


def test_country_page_goalies_formats_rows_and_reports_empty(bot_module, fake_db_router):
    bot_messages = bot_module("bot_messages")
    fake_db_router([
        _COUNTRY_RANKING,
        # lastname, team, wins, save_pct, gaa, games, shutouts, total
        ("FROM goalies_season_stats", [("Vejmelka", "UTA", 38, 89.67, 2.74592, 64, 3, 1)]),
    ])
    text, _, _ = bot_messages.country_page("CAN", "G", 0)
    assert _tables(text) == [[
        ["", "Вратарь", "Ком", "В", "%ОБ", "КН", "И", "Сух"],
        ["1.", "Vejmelka", "UTA", "38", "89.67%", "2.75", "64", "3"],
    ]]

    fake_db_router([_COUNTRY_RANKING, ("FROM goalies_season_stats", [])])
    text, _, _ = bot_messages.country_page("CAN", "G", 0)
    assert "Нет данных в этом диапазоне." in text
