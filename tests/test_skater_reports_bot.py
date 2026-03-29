"""Unit tests for skater leaderboard messages (stage 5.3-style: no DB required)."""
import importlib
import os
import sys
import unittest
from unittest.mock import patch

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


class SkaterReportsLeaderboardsTest(unittest.TestCase):
    def test_new_skater_leaderboards_render(self):
        bot_messages = _load_bot_messages()
        fake_rows = {
            "lastname": ["Ovechkin"],
            "points": [58.2],
            "team": ["WSH"],
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
                with patch.object(bot_messages, "fetch_all", return_value=fake_rows) as mock_fetch:
                    text = bot_messages.player_stats(title, table, col)
                self.assertIn("Ovechkin", text)
                self.assertIn(title, text)
                self.assertTrue(mock_fetch.called)


class Phase1UxCommandsTest(unittest.TestCase):
    def test_help_lists_core_commands(self):
        help_text = _load_help_text()
        for fragment in (
            "/today",
            "/table",
            "/leaders",
            "/stats",
            "/cancel",
            "/help",
            "/game",
            "/advanced",
            "/shottypes",
        ):
            with self.subTest(cmd=fragment):
                self.assertIn(fragment, help_text.HELP_MESSAGE)

    def test_team_table_includes_season_and_as_of(self):
        bot_messages = _load_bot_messages()
        standings_row = {
            "short_name": ["BOS"],
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

        with patch.object(bot_messages, "fetch_all", side_effect=fake_fetch):
            text = bot_messages.team_table()
        self.assertIn("25/26", text)
        self.assertIn("2025-11-15", text)
        self.assertIn("BOS", text)
        self.assertIn("*EASTERN CONFERENCE*", text)
        self.assertIn("*ATLANTIC DIVISION*", text)
        self.assertIn("```", text)

    def test_leaders_compact_combines_points_and_goals(self):
        bot_messages = _load_bot_messages()
        fake_rows = {
            "lastname": ["Ovechkin"],
            "points": [99],
            "team": ["WSH"],
            "count_rows": 1,
        }
        with patch.object(bot_messages, "fetch_all", return_value=fake_rows):
            text = bot_messages.leaders_compact()
        self.assertIn("Очки", text)
        self.assertIn("Голы", text)
        self.assertIn("Ovechkin", text)


def _load_stats_handlers():
    os.chdir(TELEGRAM_BOT)
    if TELEGRAM_BOT not in sys.path:
        sys.path.insert(0, TELEGRAM_BOT)
    return importlib.import_module("stats_handlers")


class Phase2UxNavigationTest(unittest.TestCase):
    def test_day_digest_single_game_sends_full_card_with_markup(self):
        stats_handlers = _load_stats_handlers()
        sent = []

        class FakeBot:
            def send_message(self, **kwargs):
                sent.append(kwargs)

        class FakeContext:
            bot = FakeBot()

        games = [
            (
                1,
                "Полная карточка",
                [{"label": "Гол", "game_id": 1, "event_id": 2}],
            ),
        ]
        stats_handlers.dispatch_day_digest_messages(
            FakeContext(), chat_id=42, day_label="2025-03-01", games=games, attach_conv_nav_on_last=True
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "Полная карточка")
        self.assertIsNotNone(sent[0].get("reply_markup"))

    def test_day_digest_multi_sends_one_summary_with_expand_buttons(self):
        """Несколько матчей — одно сообщение-сводка и кнопки dg:<game_id>."""
        stats_handlers = _load_stats_handlers()
        sent = []

        class FakeBot:
            def send_message(self, **kwargs):
                sent.append(kwargs)

        class FakeContext:
            bot = FakeBot()

        games = [
            (101, "*Home Away* 1:0\n\ngoal", []),
            (102, "*B C* 2:2\n", []),
        ]
        stats_handlers.dispatch_day_digest_messages(
            FakeContext(), chat_id=42, day_label="2025-03-01", games=games, attach_conv_nav_on_last=True
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("2 игр", sent[0]["text"])
        self.assertIn("*Home Away* 1:0", sent[0]["text"])
        kb = sent[0]["reply_markup"].inline_keyboard
        self.assertTrue(any("Матч 1" in row[0].text for row in kb if row))
        ds = importlib.import_module("dialog_states")
        self.assertEqual(kb[-1][0].callback_data, str(ds.CHOOSE_STATS))
        self.assertEqual(kb[-1][1].callback_data, str(ds.END_CONVERSATION))

    def test_day_digest_synthetic_message_only(self):
        stats_handlers = _load_stats_handlers()
        sent = []

        class FakeBot:
            def send_message(self, **kwargs):
                sent.append(kwargs)

        class FakeContext:
            bot = FakeBot()

        games = [(0, "Нет матчей", [])]
        stats_handlers.dispatch_day_digest_messages(
            FakeContext(), chat_id=7, day_label="2025-01-01", games=games, attach_conv_nav_on_last=False
        )
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["text"], "Нет матчей")
        self.assertIn("/stats", sent[1]["text"])


if __name__ == "__main__":
    unittest.main()
