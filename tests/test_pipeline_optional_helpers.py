"""Loader contract: missing source fields must surface as Python ``None``.

Targets the helpers introduced for ``docs/pipeline_nulls_and_explicit_null_tz.md``
so PostgreSQL stores ``NULL`` instead of legacy 0 / "00:00" / -9999 sentinels
when the NHL API omits a field.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PIPELINE_DIR = os.path.join(REPO_ROOT, "pipeline")
TELEGRAM_BOT = os.path.join(REPO_ROOT, "telegram_bot")
for path in (TELEGRAM_BOT, PIPELINE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import load_season_modern as loader  # noqa: E402


class OptionalCoercersTest(unittest.TestCase):
    def test_optional_int_returns_none_for_missing_or_invalid(self):
        self.assertIsNone(loader.optional_int(None))
        self.assertIsNone(loader.optional_int(""))
        self.assertIsNone(loader.optional_int("abc"))
        self.assertIsNone(loader.optional_int(object()))

    def test_optional_int_preserves_real_zero(self):
        # Real "0 wins" must persist as 0; only missing values become NULL.
        self.assertEqual(loader.optional_int(0), 0)
        self.assertEqual(loader.optional_int("0"), 0)
        self.assertEqual(loader.optional_int(7), 7)

    def test_optional_float_zero_vs_missing(self):
        self.assertIsNone(loader.optional_float(None))
        self.assertIsNone(loader.optional_float(""))
        self.assertIsNone(loader.optional_float("abc"))
        self.assertEqual(loader.optional_float(0), 0.0)
        self.assertEqual(loader.optional_float("3.14"), 3.14)

    def test_optional_str_strips_and_nulls_empty(self):
        self.assertIsNone(loader.optional_str(None))
        self.assertIsNone(loader.optional_str(""))
        self.assertIsNone(loader.optional_str("   "))
        self.assertEqual(loader.optional_str("  ABC  "), "ABC")

    def test_optional_pct_from_ratio_normalises_and_nulls(self):
        self.assertIsNone(loader.optional_pct_from_ratio(None))
        self.assertIsNone(loader.optional_pct_from_ratio(""))
        self.assertEqual(loader.optional_pct_from_ratio(0.5), 50.0)
        self.assertEqual(loader.optional_pct_from_ratio(91.23), 91.23)

    def test_optional_split_sv_handles_missing(self):
        self.assertEqual(loader.optional_split_sv(None), (None, None))
        self.assertEqual(loader.optional_split_sv(""), (None, None))
        self.assertEqual(loader.optional_split_sv("not-a-pair"), (None, None))
        self.assertEqual(loader.optional_split_sv("12/15"), (12, 15))

    def test_optional_seconds_to_mmss_treats_zero_as_unknown(self):
        self.assertIsNone(loader.optional_seconds_to_mmss(None))
        self.assertIsNone(loader.optional_seconds_to_mmss(""))
        self.assertIsNone(loader.optional_seconds_to_mmss(0))
        self.assertIsNone(loader.optional_seconds_to_mmss(-5))
        self.assertEqual(loader.optional_seconds_to_mmss(125), "2:05")

    def test_optional_mmss_treats_legacy_sentinel_as_null(self):
        self.assertIsNone(loader.optional_mmss(None))
        self.assertIsNone(loader.optional_mmss(""))
        self.assertIsNone(loader.optional_mmss("00:00"))
        self.assertEqual(loader.optional_mmss("12:34"), "12:34")

    def test_optional_age_from_birthdate(self):
        self.assertIsNone(loader.optional_age_from_birthdate(None))
        self.assertIsNone(loader.optional_age_from_birthdate(""))
        self.assertIsNone(loader.optional_age_from_birthdate("1985-13-40"))
        # Sanity: age is non-negative for an adult past birthdate.
        self.assertGreaterEqual(
            loader.optional_age_from_birthdate("2000-01-01"),
            date.today().year - 2000 - 1,
        )

    def test_safe_pct_returns_none_when_no_attempts(self):
        self.assertIsNone(loader.safe_pct(0, 0))
        self.assertIsNone(loader.safe_pct(None, 5))
        self.assertIsNone(loader.safe_pct(2, None))
        self.assertIsNone(loader.safe_pct(2, 0))
        self.assertEqual(loader.safe_pct(8, 10), 80.0)


class GoalAggregationTest(unittest.TestCase):
    """Sentinels removed: aggregations must use ``is not None`` filters."""

    def _build_loader(self):
        # ModernNhlLoader.__init__ reads config.* — bypass it via __new__.
        return loader.ModernNhlLoader.__new__(loader.ModernNhlLoader)

    def _patched_get_json(self, pbp, box):
        def _get_json(url):
            if "play-by-play" in url:
                return pbp
            if "boxscore" in url:
                return box
            raise AssertionError(f"unexpected URL in test: {url}")

        return _get_json

    def test_goal_with_missing_assist_id_is_null_and_skipped_in_aggregations(self):
        instance = self._build_loader()
        instance.season_id = 20252026
        instance.current_season_label = "25/26"

        pbp = {
            "periodDescriptor": {"number": 1, "periodType": "REG"},
            "shootoutInUse": False,
            "plays": [
                {
                    "typeDescKey": "goal",
                    "periodDescriptor": {"number": 1},
                    "timeInPeriod": "05:00",
                    "eventId": 101,
                    # Missing assist1PlayerId — must persist as NULL, not -9999.
                    "details": {
                        "eventOwnerTeamId": 10,
                        "scoringPlayerId": 8000001,
                        "scoringPlayerTotal": 12,
                        "assist2PlayerId": 8000003,
                        "assist2PlayerTotal": 4,
                        "awayScore": 0,
                        "homeScore": 1,
                    },
                    # Home team scored on a PP (away has 4 skaters + goalie).
                    "situationCode": "1451",
                },
                {
                    "typeDescKey": "faceoff",
                    "details": {
                        # Loser id missing — must not increment a fake "0" player.
                        "eventOwnerTeamId": 10,
                        "winningPlayerId": 8000001,
                    },
                },
                {
                    "typeDescKey": "penalty",
                    "details": {
                        "eventOwnerTeamId": 20,
                        # No duration field: must skip rather than fabricate "2".
                    },
                },
            ],
        }
        box = {
            "homeTeam": {"sog": 25},
            "awayTeam": {"sog": 18},
            "playerByGameStats": {
                "homeTeam": {
                    "forwards": [
                        {
                            "playerId": 8000001,
                            "toi": "18:30",
                            "assists": 0,
                            "goals": 1,
                            "sog": 4,
                            "hits": 2,
                            "powerPlayGoals": 1,
                            "pim": 0,
                            "takeaways": 1,
                            "giveaways": 0,
                            "blockedShots": 0,
                            "plusMinus": 1,
                            "faceoffWinningPctg": 0.5,
                        }
                    ],
                    "defense": [],
                    "goalies": [
                        {
                            "playerId": 9000001,
                            # Missing toi / saves / shots / decision: each must be NULL.
                            "powerPlayShotsAgainst": "0/0",
                            "shorthandedShotsAgainst": "0/0",
                            "evenStrengthShotsAgainst": "18/19",
                        }
                    ],
                },
                "awayTeam": {"forwards": [], "defense": [], "goalies": []},
            },
        }

        instance.get_json = self._patched_get_json(pbp, box)
        games_meta = [
            {
                "id": 2025020001,
                "gameDate": "2025-10-01",
                "homeTeamId": 10,
                "visitingTeamId": 20,
                "homeScore": 1,
                "visitingScore": 0,
            }
        ]

        games_rows, all_goals_rows, game_team_rows, game_player_rows, game_goalie_rows = (
            instance.build_game_rows(games_meta)
        )

        self.assertEqual(len(all_goals_rows), 1)
        goal = all_goals_rows[0]
        # Tuple positions: 0=scorer, 1=scorerTotal, 2=assist1, 3=assist1Total,
        # 4=assist2, 5=assist2Total, 8=is_ppg, 9=is_shg, 10=team_id.
        self.assertEqual(goal[0], 8000001)
        self.assertIsNone(
            goal[2], "missing assist1PlayerId must surface as NULL, not -9999"
        )
        self.assertIsNone(
            goal[3], "missing assist1PlayerTotal must surface as NULL"
        )
        self.assertEqual(goal[4], 8000003)
        self.assertTrue(goal[8], "PPG flag must be set when home outnumbers away")

        # Per-team PIM: the only penalty had no duration → contributes 0 (skipped),
        # not a fabricated 2-minute default.
        home_team_row = next(r for r in game_team_rows if r[13] == 10)
        away_team_row = next(r for r in game_team_rows if r[13] == 20)
        self.assertEqual(home_team_row[2], 0)
        self.assertEqual(away_team_row[2], 0)

        # Goalie row: missing toi / saves / decision → NULL.
        self.assertEqual(len(game_goalie_rows), 1)
        gk = game_goalie_rows[0]
        self.assertIsNone(gk[3], "missing toi must surface as NULL")
        self.assertIsNone(gk[8], "missing saves must surface as NULL")
        self.assertIsNone(gk[15], "missing decision must surface as NULL")

    def test_no_minus_9999_in_goal_rows(self):
        instance = self._build_loader()
        instance.season_id = 20252026
        instance.current_season_label = "25/26"
        pbp = {
            "periodDescriptor": {"number": 1, "periodType": "REG"},
            "plays": [
                {
                    "typeDescKey": "goal",
                    "periodDescriptor": {"number": 1},
                    "timeInPeriod": "01:23",
                    "eventId": 1,
                    "details": {
                        "eventOwnerTeamId": 10,
                        # All player IDs missing.
                    },
                    "situationCode": "1551",
                }
            ],
        }
        box = {
            "homeTeam": {"sog": 0},
            "awayTeam": {"sog": 0},
            "playerByGameStats": {
                "homeTeam": {"forwards": [], "defense": [], "goalies": []},
                "awayTeam": {"forwards": [], "defense": [], "goalies": []},
            },
        }
        instance.get_json = self._patched_get_json(pbp, box)
        _, all_goals_rows, *_ = instance.build_game_rows(
            [
                {
                    "id": 2025020002,
                    "gameDate": "2025-10-02",
                    "homeTeamId": 10,
                    "visitingTeamId": 20,
                    "homeScore": 1,
                    "visitingScore": 0,
                }
            ]
        )
        for col in (0, 1, 2, 3, 4, 5):
            self.assertNotEqual(all_goals_rows[0][col], -9999)
            self.assertIsNone(all_goals_rows[0][col])


if __name__ == "__main__":
    unittest.main()
