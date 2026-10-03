"""Tests for NHL scoreboard (/tonight), no network."""
import json
import os
import sys
import unittest
import importlib

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TELEGRAM_BOT = os.path.join(REPO_ROOT, "telegram_bot")
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "score_now_sample.json")


def _load_nhl_scoreboard():
    os.chdir(TELEGRAM_BOT)
    if TELEGRAM_BOT not in sys.path:
        sys.path.insert(0, TELEGRAM_BOT)
    return importlib.import_module("nhl_scoreboard")


class TonightIntroAndButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_nhl_scoreboard()
        with open(FIXTURE, encoding="utf-8") as f:
            cls.fixture = json.load(f)

    def test_intro_mentions_date_and_moscow(self):
        text = self.mod.tonight_reply_intro(self.fixture)
        self.assertIn("2026-04-04", text)
        self.assertIn("Москва", text)
        self.assertIn("впереди ET", text)
        self.assertNotIn("DET @ NYR", text)

    def test_button_labels_match_and_time(self):
        games = self.mod.slate_games_sorted(self.fixture)
        self.assertEqual(len(games), 2)
        t0 = self.mod.tonight_match_button_label(games[0])
        t1 = self.mod.tonight_match_button_label(games[1])
        self.assertIn("DET", t0)
        self.assertIn("NYR", t0)
        self.assertIn("12:30 ET", t0)
        self.assertIn("MIN", t1)
        self.assertIn("OTT", t1)
        self.assertIn("13:00 ET", t1)
        self.assertLessEqual(len(t0), 64)
        self.assertLessEqual(len(t1), 64)
        idx_det = t0.index("DET")
        self.assertLess(idx_det, t0.index("NYR"))

    def test_tonight_callback_data_within_telegram_limit(self):
        games = self.mod.slate_games_sorted(self.fixture)
        for g in games:
            gid = g.get("id")
            away = ((g.get("awayTeam") or {}).get("abbrev") or "?").strip()
            home = ((g.get("homeTeam") or {}).get("abbrev") or "?").strip()
            cb = f"tn:{int(gid)}:{away}:{home}"
            self.assertLessEqual(len(cb.encode("utf-8")), 64, msg=cb)

    def test_empty_games_list(self):
        payload = {"currentDate": "2026-04-04", "games": []}
        text = self.mod.tonight_reply_intro(payload)
        self.assertIn("2026-04-04", text)
        self.assertIn("расписании NHL", text)
        self.assertIn("матчей нет", text)

    def test_all_filtered_out_by_date(self):
        payload = {
            "currentDate": "2026-04-04",
            "games": [
                {
                    "gameDate": "2026-04-03",
                    "startTimeUTC": "2026-04-03T23:00:00Z",
                    "gameState": "FINAL",
                    "awayTeam": {"abbrev": "BOS", "score": 1},
                    "homeTeam": {"abbrev": "TOR", "score": 2},
                }
            ],
        }
        text = self.mod.tonight_reply_intro(payload)
        self.assertIn("матчей нет", text)


class LeagueTodayTest(unittest.TestCase):
    """Игровой день лиги — дата по ET со сменой в 03:00 ET, а не currentDate из score/now."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_nhl_scoreboard()

    def _at_utc(self, iso):
        from datetime import datetime
        return datetime.fromisoformat(iso + "+00:00")

    def test_moscow_morning_is_already_the_new_league_day(self):
        # 2026-10-03 13:55 MSK = 06:55 ET: score/now ещё отдавал 2026-10-02 (бага).
        self.assertEqual(str(self.mod.league_today(self._at_utc("2026-10-03T10:55:00"))), "2026-10-03")

    def test_after_midnight_et_late_games_keep_previous_day(self):
        # 01:30 ET — западные матчи вчерашнего слейта ещё могут идти.
        self.assertEqual(str(self.mod.league_today(self._at_utc("2026-10-03T05:30:00"))), "2026-10-02")

    def test_rollover_at_three_am_et(self):
        self.assertEqual(str(self.mod.league_today(self._at_utc("2026-10-03T06:59:00"))), "2026-10-02")
        self.assertEqual(str(self.mod.league_today(self._at_utc("2026-10-03T07:00:00"))), "2026-10-03")

    def test_fetch_score_requests_explicit_day(self):
        from datetime import date
        from unittest import mock

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"currentDate": "2026-10-03", "games": []}
        with mock.patch.object(self.mod.requests, "get", return_value=resp) as get:
            payload = self.mod.fetch_score(date(2026, 10, 3))
        self.assertEqual(get.call_args.args[0], "https://api-web.nhle.com/v1/score/2026-10-03")
        self.assertEqual(payload["currentDate"], "2026-10-03")


if __name__ == "__main__":
    unittest.main()
