"""Unit tests for skater leaderboard messages (stage 5.3-style: no DB required)."""
import importlib
import os
import sys
import unittest
from unittest.mock import patch

from tests._telegram_fakes import rich_html

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TELEGRAM_BOT = os.path.join(REPO_ROOT, "telegram_bot")


def _load_bot_messages():
    os.chdir(TELEGRAM_BOT)
    if TELEGRAM_BOT not in sys.path:
        sys.path.insert(0, TELEGRAM_BOT)
    return importlib.import_module("bot_messages")


def _load_help_text():
    os.chdir(TELEGRAM_BOT)
    if TELEGRAM_BOT not in sys.path:
        sys.path.insert(0, TELEGRAM_BOT)
    return importlib.import_module("help_text")


def _load_nhl_scoreboard():
    os.chdir(TELEGRAM_BOT)
    if TELEGRAM_BOT not in sys.path:
        sys.path.insert(0, TELEGRAM_BOT)
    return importlib.import_module("nhl_scoreboard")


class SkaterReportsLeaderboardsTest(unittest.TestCase):
    def test_new_skater_leaderboards_render(self):
        bot_messages = _load_bot_messages()
        fake_rows = {
            "lastname": ["Ovechkin"],
            "roster_position": ["LW"],
            "points": [58.2],
            "team": ["WSH"],
            # Задача 18 — хвост игр/смен, обязателен в фикстуре: player_stats_with_count
            # читает эти ключи напрямую (без запасного None).
            "tail_games": [82],
            "tail_shifts": [1700],
            "total": [1],
            "count_rows": 1,
        }
        cases = [
            ("Лидеры по Corsi", "players_advanced_stats", "sat_pct"),
            ("Лидеры по Fenwick", "players_advanced_stats", "usat_pct"),
            ("Лидеры по GF%", "players_advanced_stats", "goals_pct"),
            ("Лидеры по OZ Start", "players_advanced_stats", "oz_start_pct"),
            ("Лидеры по буллитам", "players_season_stats", "shootout_pct"),
        ]
        for title, table, col in cases:
            with self.subTest(table=table, column=col):
                with patch.object(bot_messages, "cached_fetch_all", return_value=fake_rows) as mock_fetch:
                    text, _, _ = bot_messages.player_stat_leaderboard_page(title, table, col, 0)
                self.assertIn("Ovechkin", text)
                self.assertIn(title, text)
                self.assertIn(
                    '<tr><td align="right">1.</td><td>Ovechkin [LW]</td><td>WSH</td>'
                    '<td align="right">58.2</td><td align="right">82</td>'
                    '<td align="right">1700</td></tr>',
                    text,
                )
                self.assertTrue(mock_fetch.called)


class Phase1UxCommandsTest(unittest.TestCase):
    def test_help_lists_core_commands(self):
        help_text = _load_help_text()
        hm = help_text.HELP_MESSAGE
        self.assertIn("<b>Команды</b>", hm)
        self.assertNotIn("/day\\_games", hm)
        self.assertNotIn("\\_", hm)
        for fragment in (
            "/today",
            "/tonight",
            "/standings",
            "/team",
            "/stats",
            "/team_stats",
            "/help",
            "/countries",
            "/subscriptions",
        ):
            with self.subTest(cmd=fragment):
                self.assertIn(fragment, hm)
        self.assertNotIn("/shottypes", hm)
        for removed in ("/advanced", "/day_games", "/table", "/game", "/leaders", "/cancel"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, hm)
        self.assertIn("Форма до матча", hm)
        # Пометки голов карточки, в том числе ПШ вместо прежней «★».
        self.assertIn("ПШ — победная шайба", hm)
        self.assertIn("/BOS_FULL", hm)
        # Расшифровка SAT/USAT/GF живёт в подменю расширенной статистики /stats, в /help её не дублируем.
        self.assertNotIn(help_text.ADVANCED_STATS_EXPLAINED, hm)
        self.assertIn("Corsi", help_text.ADVANCED_COMMAND_INTRO)
        self.assertIn("Fenwick", help_text.ADVANCED_COMMAND_INTRO)

    def test_reference_texts_fold_legends_and_footer_links(self):
        """Стиль справочных экранов (Задача 89): команды /help — список, легенда и
        расшифровка метрик свёрнуты, ссылка на /help из /start — в подвале."""
        help_text = _load_help_text()
        hm = help_text.HELP_MESSAGE
        self.assertTrue(hm.startswith("<b>Справка</b>\n"))
        self.assertIn("\n<ul><li>/today — ", hm)
        legend = hm.split("<details><summary>Обозначения</summary>", 1)[1]
        self.assertIn("ПШ — победная шайба", legend)
        self.assertTrue(help_text.START_MESSAGE.endswith("<footer>Все команды — /help</footer>"))
        explained = help_text.ADVANCED_COMMAND_INTRO.split("<details><summary>", 1)[1]
        self.assertIn("Corsi", explained)

    def test_start_and_help_use_plain_clickable_commands(self):
        """`<code>/cmd</code>` в Telegram не кликается — команды пишутся голыми."""
        help_text = _load_help_text()
        for name in ("START_MESSAGE", "HELP_MESSAGE"):
            with self.subTest(message=name):
                self.assertNotIn("<code>/", getattr(help_text, name))
        for cmd, _desc in help_text.BOT_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIn(f"/{cmd} — ", help_text.HELP_MESSAGE)

    def test_help_notes_regular_season_only(self):
        """Оговорка про плей-офф — только в /help: /start её не несёт (решение человека 2026-10-31)."""
        help_text = _load_help_text()
        self.assertIn("регулярный чемпионат", help_text.HELP_MESSAGE)
        self.assertIn("плей-офф", help_text.HELP_MESSAGE)
        self.assertNotIn("плей-офф", help_text.START_MESSAGE)

    def test_tonight_reply_intro_escapes_league_date(self):
        nhl = _load_nhl_scoreboard()
        raw_date = "2025-01-01<script>"
        text_empty = nhl.tonight_reply_intro({"currentDate": raw_date, "games": []})
        self.assertNotIn("<script>", text_empty)
        self.assertIn("script", text_empty)
        g = {
            "gameDate": raw_date,
            "startTimeUTC": "2025-01-01T00:00:00Z",
            "awayTeam": {"abbrev": "A"},
            "homeTeam": {"abbrev": "B"},
        }
        text_games = nhl.tonight_reply_intro({"currentDate": raw_date, "games": [g]})
        self.assertIn("&lt;script&gt;", text_games)
        self.assertNotIn("<script>", text_games)

    def test_team_table_includes_season_and_as_of(self):
        bot_messages = _load_bot_messages()
        standings_row = {
            "name": ["Boston Bruins"],
            "games_played": [82],
            "points": [110],
            "procent_points": [0.671],
            "wins": [50],
            "losses": [20],
            "ot": [12],
            "division_name": ["Atlantic"],
            "conference_name": ["Eastern Conference"],
            "count_rows": 1,
        }
        max_day_row = {"d": ["2025-11-15"], "count_rows": 1}

        def fake_fetch(query, *args, **kwargs):
            q = str(query)
            if "max(day)" in q:
                return max_day_row
            return standings_row

        with patch.object(bot_messages, "cached_fetch_all", side_effect=fake_fetch), \
             patch.object(bot_messages.config, "CURRENT_SEASON", "25/26"):
            text = bot_messages.team_table()
        self.assertIn("25/26", text)
        self.assertIn("2025-11-15", text)
        self.assertIn("Boston Bruins", text)
        self.assertIn("<table compact>", text)
        self.assertNotIn("<pre>", text)
        self.assertIn(
            '<tr><td align="right">1.</td><td>Boston Bruins</td><td align="right">110</td>'
            '<td align="right">82</td><td align="right">0.7</td></tr>',
            text,
        )
        self.assertIn("<b>ATLANTIC DIVISION</b>", text)
        self.assertIn("<b>EASTERN CONFERENCE</b>", text)

    def test_player_stat_leaderboard_page_points(self):
        bot_messages = _load_bot_messages()
        fake_rows = {
            "lastname": ["Ovechkin"],
            "roster_position": ["LW"],
            "points": [99],
            "team": ["WSH"],
            # Задача 18 — хвост игр/смен, обязателен в фикстуре: player_stats_with_count
            # читает эти ключи напрямую (без запасного None).
            "tail_games": [79],
            "tail_shifts": [1650],
            "total": [1],
            "count_rows": 1,
        }
        with patch.object(bot_messages, "cached_fetch_all", return_value=fake_rows):
            text, has_prev, has_next = bot_messages.player_stat_leaderboard_page(
                "Лучшие бомбардиры", "players_season_stats", "points", 0
            )
        self.assertIn("Лучшие бомбардиры", text)
        self.assertIn("Ovechkin", text)
        self.assertIn(
            '<tr><td align="right">1.</td><td>Ovechkin [LW]</td><td>WSH</td>'
            '<td align="right">99</td><td align="right">79</td>'
            '<td align="right">1650</td></tr>',
            text,
        )
        self.assertFalse(has_prev)
        self.assertFalse(has_next)


def _load_stats_handlers():
    os.chdir(TELEGRAM_BOT)
    if TELEGRAM_BOT not in sys.path:
        sys.path.insert(0, TELEGRAM_BOT)
    return importlib.import_module("stats_handlers")


def _fake_bot():
    """aiogram ``Bot`` на записывающей сессии (``tests/_telegram_fakes.py``) — запросы к Bot API без сети."""
    from aiogram import Bot

    from tests._telegram_fakes import FAKE_TOKEN, FakeSession

    return Bot(FAKE_TOKEN, session=FakeSession())


class Phase2UxNavigationTest(unittest.IsolatedAsyncioTestCase):
    async def test_day_digest_single_game_sends_full_card_with_markup(self):
        stats_handlers = _load_stats_handlers()
        bot = _fake_bot()
        games = [
            (
                1,
                "Полная карточка",
                [{"label": "Гол", "game_id": 1, "event_id": 2}],
            ),
        ]
        await stats_handlers.dispatch_day_digest_messages(
            bot, chat_id=42, day_label="2025-03-01", games=games
        )
        sent = bot.session.sent_rich
        self.assertEqual(len(sent), 1)
        self.assertEqual(bot.session.sent_messages, [], "подсказка «Ещё: …» — подвал, не сообщение")
        html_ = sent[0].rich_message.html
        self.assertTrue(html_.startswith(rich_html("Полная карточка")))
        self.assertTrue(html_.endswith("</footer>"))
        self.assertIn("<footer>Ещё: /tonight", html_)
        [[video]] = sent[0].reply_markup.inline_keyboard
        self.assertEqual(video.callback_data, "gv:1:2")

    async def test_day_digest_multi_sends_one_summary_with_expand_buttons(self):
        """Несколько матчей — одно сообщение-сводка и кнопки dg:<game_id>."""
        stats_handlers = _load_stats_handlers()
        bot = _fake_bot()
        games = [
            (101, "<b>Home Away</b> 1:0\n\n<i>x</i>", []),
            (102, "<b>B C</b> 2:2\n", []),
        ]
        with patch.object(
            stats_handlers, "digest_game_button_labels", return_value=["1. HOM – AWY", "2. B – C"]
        ), patch.object(
            stats_handlers, "day_digest_summary_body", return_value=["1. Home Away", "2. B C"]
        ):
            await stats_handlers.dispatch_day_digest_messages(
                bot, chat_id=42, day_label="2025-03-01", games=games
            )
        sent = bot.session.sent_rich
        self.assertEqual(len(sent), 1)
        self.assertEqual(bot.session.sent_messages, [], "подсказка «Ещё: …» — подвал, не сообщение")
        html_ = sent[0].rich_message.html
        self.assertTrue(html_.startswith("<p><b>Матчи ночи</b></p>"))
        self.assertIn("<p>1. Home Away</p>", html_)
        self.assertTrue(html_.endswith("</footer>"))
        self.assertIn("<footer>Игровой день 2025-03-01 · матчей: 2<br>", html_)
        kb = sent[0].reply_markup.inline_keyboard
        self.assertEqual([[b.text for b in row] for row in kb], [["1. HOM – AWY", "2. B – C"]])

    async def test_day_digest_synthetic_message_only(self):
        stats_handlers = _load_stats_handlers()
        bot = _fake_bot()
        games = [(0, "Нет матчей", [])]
        await stats_handlers.dispatch_day_digest_messages(
            bot, chat_id=7, day_label="2025-01-01", games=games
        )
        self.assertEqual(len(bot.session.outgoing), 1)
        (rich,) = bot.session.sent_rich
        self.assertEqual(bot.session.sent_messages, [])
        html_ = rich.rich_message.html
        self.assertTrue(html_.startswith(rich_html("Нет матчей")))
        self.assertTrue(html_.endswith("</footer>"))
        self.assertIn("/stats", html_.rsplit("<footer>", 1)[1])


if __name__ == "__main__":
    unittest.main()
