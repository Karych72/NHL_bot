"""Loader contract: missing source fields must surface as Python ``None``.

Two levels of ``docs/pipeline_nulls_and_explicit_null_tz.md`` §1–§2 that need no
captured payload: the single-value coercers (``to_int``'s eager default for key
columns, and the ``optional_*`` / ``safe_pct`` family that stores ``NULL``
instead of legacy 0 / "00:00" / -9999 sentinels), plus two hand-written
play-by-play cases the real fixtures cannot show — a penalty without
``duration`` and a goal with no player ids at all. Row assembly on real payloads
lives in ``tests/test_pipeline_*_rows.py``.
"""

from __future__ import annotations

import unittest
from datetime import date

# The shared module puts pipeline/ and telegram_bot/ on sys.path and re-exports
# the loader, so this file does not repeat that bootstrap.
from tests._pipeline_fixtures import (
    LoaderApiTestCase,
    field,
    loader,
    make_loader,
    stub_api,
)


class EagerDefaultCoercerTest(unittest.TestCase):
    """The other half of §1: ``to_int`` keeps an eager default for key columns."""

    def test_to_int_falls_back_to_default_for_missing_or_invalid(self):
        self.assertEqual(loader.to_int(None), 0)
        self.assertEqual(loader.to_int(""), 0)
        self.assertEqual(loader.to_int("abc"), 0)
        self.assertEqual(loader.to_int(object()), 0)
        self.assertEqual(loader.to_int(None, 5), 5)

    def test_to_int_parses_present_values(self):
        self.assertEqual(loader.to_int(0), 0)
        self.assertEqual(loader.to_int("42"), 42)
        self.assertEqual(loader.to_int(7.9), 7)


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


class GoalAggregationTest(LoaderApiTestCase):
    """Sentinels removed: aggregations must use ``is not None`` filters.

    Hand-written payloads on purpose: the NHL API does send these shapes
    (a penalty without ``duration``, a goal with no player ids), but no single
    captured game contains them, so they cannot come from ``tests/fixtures``.
    """

    def test_goal_with_missing_assist_id_is_null_and_skipped_in_aggregations(self):
        instance = make_loader()

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

        stub_api(instance, json_routes={"play-by-play": pbp, "boxscore": box})
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
        self.assertEqual(field(goal, "all_goals", "goal_player_id"), 8000001)
        self.assertIsNone(
            field(goal, "all_goals", "assist_player1_id"),
            "missing assist1PlayerId must surface as NULL, not -9999",
        )
        self.assertIsNone(
            field(goal, "all_goals", "assist_total_1"),
            "missing assist1PlayerTotal must surface as NULL",
        )
        self.assertEqual(field(goal, "all_goals", "assist_player2_id"), 8000003)
        self.assertTrue(
            field(goal, "all_goals", "is_ppg"),
            "PPG flag must be set when home outnumbers away",
        )

        # Per-team PIM: the only penalty had no duration → contributes 0 (skipped),
        # not a fabricated 2-minute default.
        by_team = {field(r, "game_team_stats", "team_id"): r for r in game_team_rows}
        self.assertEqual(field(by_team[10], "game_team_stats", "pim"), 0)
        self.assertEqual(field(by_team[20], "game_team_stats", "pim"), 0)

        # Goalie row: missing toi / saves / decision → NULL.
        self.assertEqual(len(game_goalie_rows), 1)
        gk = game_goalie_rows[0]
        self.assertIsNone(
            field(gk, "game_goalie_stats", "timeonice"), "missing toi must surface as NULL"
        )
        self.assertIsNone(
            field(gk, "game_goalie_stats", "saves"), "missing saves must surface as NULL"
        )
        self.assertIsNone(
            field(gk, "game_goalie_stats", "decision"),
            "missing decision must surface as NULL",
        )

    def test_no_minus_9999_in_goal_rows(self):
        instance = make_loader()
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
        stub_api(instance, json_routes={"play-by-play": pbp, "boxscore": box})
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
        for column in (
            "goal_player_id",
            "total_goals",
            "assist_player1_id",
            "assist_total_1",
            "assist_player2_id",
            "assist_total_2",
        ):
            value = field(all_goals_rows[0], "all_goals", column)
            self.assertNotEqual(value, -9999, column)
            self.assertIsNone(value, column)


if __name__ == "__main__":
    unittest.main()
