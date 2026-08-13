"""Unit tests for message assembly: telegram_bot/template_funcs.py and the
HTML-rendering paths of telegram_bot/bot_messages.py. No DB, no network —
bot_messages.fetch_all and bot_messages.cached_fetch_all are patched at the
module boundary; template_funcs is exercised directly against real template
files under telegram_bot/messages/.

Special attention goes to HTML escaping (Telegram parse_mode="HTML"): player
and team names coming out of the DB can contain `<`, `>`, `&`, quotes, or
ordinary punctuation (`-`, `_`, `*`) and must come back through message
builders exactly once escaped, never re-formatted or dropped.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


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
    if "away_team_id FROM games" in q:
        return {"count_rows": 0}
    if "get_goals_game" in q:
        return {
            "scorer": [_EVIL_SCORER, _EVIL_SCORER, _EVIL_SCORER],
            "scorer_position": ["C", "C", "C"],
            "assist_1": ["Sm'th", None, None],
            "assist_2": [None, None, None],
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
    raise AssertionError(f"unexpected query in game_message(): {q}")


@pytest.fixture
def game_message_text(bot_module):
    """game_message() itself only calls fetch_all (:106,111,126,145), but it
    also calls _last_n_form_record() (:236-237), which since Task 9 goes
    through cached_fetch_all — patch both to the same stub, so an unmocked
    query fails loudly (the stub's terminal AssertionError) instead of
    silently escaping to the real database.get_connection()."""
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
    assert "O&#x27;Brien &lt;3&gt; &amp; Co_junior-star*: хет-трик (×3)" in text


def test_game_message_escapes_goalie_lastname(game_message_text):
    text, _ = game_message_text
    assert "O&#x27;Neill&lt;b&gt;" in text
    assert "O'Neill<b>" not in text


def test_game_message_returns_goals_meta_for_video_buttons(game_message_text):
    _, goals_meta = game_message_text
    assert [g["event_id"] for g in goals_meta] == [10, 11, 12]
    assert all(g["game_id"] == 555 for g in goals_meta)


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
# matchup_season_preview() — escaping and missing-data branches
# ---------------------------------------------------------------------------

def test_matchup_season_preview_rejects_blank_or_placeholder_abbrev(bot_module):
    bot_messages = bot_module("bot_messages")
    with patch.object(bot_messages, "cached_fetch_all") as mock_fetch:
        text = bot_messages.matchup_season_preview("", "NYR")
    assert "Не удалось сопоставить" in text
    mock_fetch.assert_not_called()

    with patch.object(bot_messages, "cached_fetch_all") as mock_fetch:
        text = bot_messages.matchup_season_preview("?", "?")
    assert "Не удалось сопоставить" in text
    mock_fetch.assert_not_called()


def test_matchup_season_preview_reports_when_neither_team_in_db(bot_module):
    bot_messages = bot_module("bot_messages")
    empty = {"count_rows": 0}
    with patch.object(bot_messages, "cached_fetch_all", return_value=empty):
        text = bot_messages.matchup_season_preview("AAA", "BBB")
    assert "нет в базе бота" in text
    assert "<b>AAA</b>" in text and "<b>BBB</b>" in text


def test_matchup_season_preview_escapes_html_in_abbreviations(bot_module):
    bot_messages = bot_module("bot_messages")
    empty = {"count_rows": 0}
    with patch.object(bot_messages, "cached_fetch_all", return_value=empty):
        text = bot_messages.matchup_season_preview("<i>AB", "CD&EF")
    assert "&lt;i&gt;AB" in text
    assert "CD&amp;EF" in text
    assert "<i>AB" not in text


def test_matchup_season_preview_full_comparison_when_both_teams_known(bot_module):
    bot_messages = bot_module("bot_messages")
    stats_row = {
        "abbr": ["AAA", "BBB"],
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

    def fake_fetch(query, params=None, columns=None):
        q = str(query)
        if "teams_stats ts" in q:
            return stats_row
        # _team_id_for_abbrev / _last_n_form_record / _h2h_season_wins
        if "team_id FROM teams" in q:
            return {"count_rows": 0, "team_id": []}
        raise AssertionError(f"unexpected query: {q}")

    with patch.object(bot_messages, "cached_fetch_all", side_effect=fake_fetch):
        text = bot_messages.matchup_season_preview("AAA", "BBB")

    assert "<b>AAA</b> — 7-3-0, 14 очков (70%)" in text
    assert "<b>BBB</b> — 3-7-0, 6 очков (30%)" in text
    assert "<b>Сравнение</b>" in text
    assert "<pre>" in text


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
    assert "Player0" in text
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
        "team": ["BOS"], "points": [55.5], "games_played": [40],
        "total": [12], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, has_prev, has_next = bot_messages.team_stat_leaderboard_page(
            "Статистика большинства", "power_play_percentage", 0
        )
    assert "Статистика большинства" in text
    assert "BOS" in text
    assert has_prev is False
    assert has_next is True
    assert "Показаны 1 из 12" in text


def test_team_stat_leaderboard_page_full_last_page_has_no_next(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {
        "team": [f"T{i}" for i in range(10)],
        "points": [float(i) for i in range(10)],
        "games_played": [40] * 10,
        "total": [10] * 10,
        "count_rows": 10,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text, _, has_next = bot_messages.team_stat_leaderboard_page("Топ", "points", 0)
    assert has_next is False
    assert "Показаны 1–10 из 10" in text


def test_stat_leaderboard_for_kind_rejects_unknown_kind(bot_module):
    bot_messages = bot_module("bot_messages")
    with pytest.raises(ValueError, match="unknown leaderboard kind"):
        bot_messages.stat_leaderboard_for_kind("wins", 0)


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


def test_truncate_telegram_text_truncates_and_appends_default_note(bot_module):
    """Дефолтная сноска (без footer_note) приходит из truncation_marker() — тот же
    хелпер, что и все остальные ветки усечения, а не свой строковый литерал."""
    bot_messages = bot_module("bot_messages")
    long_text = "x" * 5000
    note = "\n\n" + bot_messages.truncation_marker()
    result = bot_messages.truncate_telegram_text(long_text)
    cut = bot_messages._telegram_cut_budget(note)
    assert result == long_text[:cut] + "..." + note
    assert len(result) <= bot_messages.TELEGRAM_MAX_MESSAGE_LENGTH


def test_truncate_telegram_text_uses_custom_footer_note(bot_module):
    bot_messages = bot_module("bot_messages")
    long_text = "y" * 5000
    footer_note = "\n\nCUT"
    result = bot_messages.truncate_telegram_text(long_text, footer_note=footer_note)
    cut = bot_messages.TELEGRAM_MAX_MESSAGE_LENGTH - len(footer_note) - 3
    assert result == long_text[:cut] + "..." + footer_note
    assert len(result) <= bot_messages.TELEGRAM_MAX_MESSAGE_LENGTH


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
    lines = [long_line] * 20  # 20 * 500 = 10000 символов, заведомо больше 4096
    shown = bot_messages.digest_shown_match_count("Header\n\n", lines, 20)
    assert 0 < shown < 20

    # ровно shown строк должны укладываться в бюджет truncate_telegram_text с
    # footer_note, построенным на этом же shown …
    note = "\n\n" + bot_messages.truncation_marker(shown, 20, item_word="матчей")
    cut = bot_messages._telegram_cut_budget(note)
    assert len("Header\n\n" + "\n".join(lines[:shown])) <= cut

    # … а на одну строку больше — уже нет (иначе маркер солгал бы про N).
    note_next = "\n\n" + bot_messages.truncation_marker(shown + 1, 20, item_word="матчей")
    cut_next = bot_messages._telegram_cut_budget(note_next)
    assert len("Header\n\n" + "\n".join(lines[: shown + 1])) > cut_next


def test_digest_shown_match_count_empty_lines_returns_zero(bot_module):
    bot_messages = bot_module("bot_messages")
    assert bot_messages.digest_shown_match_count("Header\n\n", [], 0) == 0


# ---------------------------------------------------------------------------
# day_digest_summary_body()
# ---------------------------------------------------------------------------

def test_day_digest_summary_body_takes_first_line_of_each_game(bot_module):
    bot_messages = bot_module("bot_messages")
    games = [
        (1, "<b>BOS TOR</b> 3:2\n\n<i>details...</i>", []),
        (2, "<b>NYR OTT</b> 1:1 (OT)\nmore stuff", []),
    ]
    body = bot_messages.day_digest_summary_body(games)
    assert body == "<b>BOS TOR</b> 3:2\n<b>NYR OTT</b> 1:1 (OT)"


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
        "team": ["BOS"], "points": [55.5], "games_played": [40],
        "total": [7], "count_rows": 1,
    }
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        _, n, total = bot_messages.team_stats_with_count("Title", "power_play_percentage")
    assert n == 1
    assert total == 7


def test_team_stats_with_count_total_is_zero_for_empty_page(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {"team": [], "points": [], "games_played": [], "total": [], "count_rows": 0}
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        _, n, total = bot_messages.team_stats_with_count("Title", "power_play_percentage")
    assert n == 0
    assert total == 0


# ---------------------------------------------------------------------------
# season_team_abbrev_help_text() — маркер на молчаливо обрезанный список
# ---------------------------------------------------------------------------

def test_season_team_abbrev_help_text_marks_dropped_teams_when_over_budget(bot_module):
    bot_messages = bot_module("bot_messages")
    abbrevs = [f"AB{i:03d}" for i in range(250)]  # шире бюджета в 3500 символов
    rows = {"ab": abbrevs, "count_rows": len(abbrevs)}
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text = bot_messages.season_team_abbrev_help_text()
    assert "Показаны" in text
    assert f"из {len(abbrevs)} команд" in text


def test_season_team_abbrev_help_text_no_marker_when_everything_fits(bot_module):
    bot_messages = bot_module("bot_messages")
    rows = {"ab": ["BOS", "TOR", "NYR"], "count_rows": 3}
    with patch.object(bot_messages, "cached_fetch_all", return_value=rows):
        text = bot_messages.season_team_abbrev_help_text()
    assert "Показаны" not in text
    assert "BOS" in text
