"""Per-game row assembly in ``pipeline/load_season_modern.py``.

Runs ``ModernNhlLoader.build_game_rows`` over the trimmed real play-by-play and
boxscore of WSH 4:1 OTT (2026-03-18) and checks the five tuples it produces —
``games``, ``all_goals``, ``game_team_stats``, ``game_player_stats``,
``game_goalie_stats``. Both directions of
``docs/pipeline_nulls_and_explicit_null_tz.md`` §2 are asserted on real payloads
(a value the API sent, a real 0 included, survives; a field it omits is
``None``), as are the PBP-derived non-NULL defaults of §3.
"""

from __future__ import annotations

import unittest

from tests._pipeline_fixtures import (
    GAME_ID,
    OTT,
    OVECHKIN,
    PINTO,
    SEASON_ID,
    SEASON_LABEL,
    SOURDIF,
    THOMPSON,
    ULLMARK,
    WSH,
    LoaderApiTestCase,
    by_player,
    field,
    load_fixture,
    make_loader,
    stub_api,
    without,
)

PLAY_BY_PLAY = "play-by-play"
BOXSCORE = "boxscore"

# Every boxscore key ``build_game_rows`` reads for a nullable column.
SKATER_BOXSCORE_KEYS = (
    "toi", "assists", "goals", "sog", "hits", "powerPlayGoals", "pim", "takeaways",
    "giveaways", "blockedShots", "plusMinus", "faceoffWinningPctg",
)
GOALIE_BOXSCORE_KEYS = (
    "toi", "assists", "goals", "pim", "shotsAgainst", "saves", "decision", "savePctg",
    "powerPlayShotsAgainst", "shorthandedShotsAgainst", "evenStrengthShotsAgainst",
)


class GameRowsTest(LoaderApiTestCase):
    def _build(self, pbp=None, box=None):
        instance = make_loader()
        stub_api(
            instance,
            json_routes={
                PLAY_BY_PLAY: (
                    load_fixture("nhl_game_play_by_play.json") if pbp is None else pbp
                ),
                BOXSCORE: load_fixture("nhl_game_boxscore.json") if box is None else box,
            },
        )
        return instance.build_game_rows(load_fixture("nhl_games_meta.json"))

    def _team_rows(self, **kwargs):
        game_team_rows = self._build(**kwargs)[2]
        return {field(r, "game_team_stats", "team_id"): r for r in game_team_rows}

    def test_games_row_from_meta_and_play_by_play(self):
        games_rows = self._build()[0]
        self.assertEqual(len(games_rows), 1)
        row = games_rows[0]

        self.assertEqual(field(row, "games", "game_id"), GAME_ID)
        self.assertEqual(field(row, "games", "day"), "2026-03-18")
        self.assertEqual(field(row, "games", "home_team_id"), WSH)
        self.assertEqual(field(row, "games", "away_team_id"), OTT)
        self.assertEqual(field(row, "games", "winner_id"), WSH)
        self.assertEqual(field(row, "games", "season"), SEASON_LABEL)
        self.assertEqual(field(row, "games", "season_id"), SEASON_ID)
        # §3: both flags are derived from the PBP payload, never NULL.
        self.assertIs(field(row, "games", "is_overtime"), False)
        # Current behaviour: taken from the payload's ``shootoutInUse``, which
        # this regulation game (gameOutcome.lastPeriodType == "REG") also sets.
        self.assertIs(field(row, "games", "is_shootouts"), True)

    def test_goal_row_keeps_sent_values_including_zero_and_nulls_absent_ones(self):
        goals = self._build()[1]
        self.assertEqual(len(goals), 5)
        first = goals[0]  # 2nd period, 08:09 — Ovechkin, unassisted second helper

        self.assertEqual(field(first, "all_goals", "goal_player_id"), OVECHKIN)
        self.assertEqual(field(first, "all_goals", "total_goals"), 25)
        self.assertEqual(field(first, "all_goals", "assist_player1_id"), 8480873)
        self.assertEqual(field(first, "all_goals", "assist_total_1"), 21)
        self.assertEqual(field(first, "all_goals", "team_id"), WSH)
        self.assertEqual(field(first, "all_goals", "game_id"), GAME_ID)
        self.assertEqual(field(first, "all_goals", "period"), 2)
        self.assertEqual(field(first, "all_goals", "time"), "08:09")
        self.assertEqual(field(first, "all_goals", "event_id"), 710)
        self.assertEqual(field(first, "all_goals", "goals_home"), 1)
        # A real 0 on the scoreboard, not a missing field.
        self.assertEqual(field(first, "all_goals", "goals_away"), 0)
        # §2.1: only one assist was credited — the second stays NULL, not -9999/0.
        self.assertIsNone(field(first, "all_goals", "assist_player2_id"))
        self.assertIsNone(field(first, "all_goals", "assist_total_2"))
        # §3: even-strength goal at 5-on-5, no situation flag set.
        self.assertIs(field(first, "all_goals", "empty_net"), False)
        self.assertIs(field(first, "all_goals", "is_ppg"), False)
        self.assertIs(field(first, "all_goals", "is_shg"), False)
        self.assertIs(field(first, "all_goals", "winner_goal"), False)

    def test_goal_flags_follow_situation_code_and_final_score(self):
        goals = self._build()[1]

        # Winner goal = the one that put WSH one past OTT's final score of 1.
        winners = [g for g in goals if field(g, "all_goals", "winner_goal")]
        self.assertEqual(len(winners), 1)
        self.assertEqual(field(winners[0], "all_goals", "event_id"), 841)

        # 0551: OTT pulled its goalie, so WSH scored short-side into an empty net
        # while outnumbering the skaters on ice.
        last = goals[-1]
        self.assertEqual(field(last, "all_goals", "event_id"), 1140)
        self.assertIs(field(last, "all_goals", "empty_net"), True)
        self.assertIs(field(last, "all_goals", "is_ppg"), True)
        self.assertIsNone(field(last, "all_goals", "assist_player1_id"))

        # 0651 for the away side: OTT scored 6-on-5 with its own net empty, which
        # is not an empty-net goal for the scoring team.
        ott_goal = next(g for g in goals if field(g, "all_goals", "team_id") == OTT)
        self.assertIs(field(ott_goal, "all_goals", "empty_net"), False)
        self.assertIs(field(ott_goal, "all_goals", "is_ppg"), False)
        self.assertEqual(field(ott_goal, "all_goals", "goals_away"), 1)

    def test_team_rows_aggregate_the_play_by_play(self):
        rows = self._team_rows()
        home, away = rows[WSH], rows[OTT]
        table = "game_team_stats"

        self.assertEqual(field(home, table, "goals"), 4)
        self.assertEqual(field(home, table, "field"), "home")
        self.assertEqual(field(away, table, "field"), "away")
        self.assertEqual(field(home, table, "game_id"), GAME_ID)
        self.assertEqual(field(home, table, "shots"), 25)
        self.assertEqual(field(away, table, "shots"), 35)
        # §3: PBP event counts — a real 0 means "no such event in this game".
        self.assertEqual(field(home, table, "pim"), 2)
        self.assertEqual(field(away, table, "pim"), 2)
        self.assertEqual(field(home, table, "hits"), 1)
        self.assertEqual(field(away, table, "hits"), 0)
        self.assertEqual(field(home, table, "blocked"), 0)
        self.assertEqual(field(away, table, "blocked"), 1)
        self.assertEqual(field(home, table, "giveaways"), 0)
        self.assertEqual(field(away, table, "giveaways"), 1)
        self.assertEqual(field(home, table, "takeaways"), 0)
        self.assertEqual(field(away, table, "takeaways"), 1)
        self.assertEqual(field(home, table, "fst_period_goals"), 0)
        self.assertEqual(field(home, table, "snd_period_goals"), 2)
        self.assertEqual(field(home, table, "trd_period_goals"), 2)
        self.assertEqual(field(away, table, "trd_period_goals"), 1)
        # Each side took one penalty, so each side had one power play; WSH
        # converted its own, OTT did not — 0.0% is a real number, not "unknown".
        self.assertEqual(field(home, table, "power_play_opportunities"), 1.0)
        self.assertEqual(field(home, table, "power_play_goals"), 1.0)
        self.assertEqual(field(home, table, "power_play_percentage"), 100.0)
        self.assertEqual(field(away, table, "power_play_percentage"), 0.0)
        # Both kept faceoffs went to OTT.
        self.assertEqual(field(home, table, "face_off_win_percentage"), 0.0)
        self.assertEqual(field(away, table, "face_off_win_percentage"), 100.0)

    def test_team_row_nulls_shots_and_percentages_without_source_events(self):
        box = load_fixture("nhl_game_boxscore.json")
        box["homeTeam"] = without(box["homeTeam"], "sog")
        pbp = load_fixture("nhl_game_play_by_play.json")
        pbp["plays"] = [
            p for p in pbp["plays"] if p.get("typeDescKey") not in ("penalty", "faceoff")
        ]
        rows = self._team_rows(pbp=pbp, box=box)
        home = rows[WSH]
        table = "game_team_stats"

        # §2.4: no sog in the boxscore → NULL, not 0 shots.
        self.assertIsNone(field(home, table, "shots"))
        # No power play and no faceoff was taken → the percentages are undefined.
        self.assertIsNone(field(home, table, "power_play_percentage"))
        self.assertIsNone(field(home, table, "face_off_win_percentage"))
        # §2.4 / §3: no penalties at all is a real 0, not missing data.
        self.assertEqual(field(home, table, "pim"), 0)

    def test_player_rows_keep_real_zeros_and_count_pbp_events(self):
        rows = by_player(self._build()[3], "game_player_stats")
        table = "game_player_stats"
        ovi = rows[OVECHKIN]

        self.assertEqual(field(ovi, table, "team_id"), WSH)
        self.assertEqual(field(ovi, table, "game_id"), GAME_ID)
        self.assertEqual(field(ovi, table, "time_on_ice"), "12:57")
        self.assertEqual(field(ovi, table, "goals"), 1)
        self.assertEqual(field(ovi, table, "shots"), 3)
        self.assertEqual(field(ovi, table, "hits"), 1)
        self.assertEqual(field(ovi, table, "giveaways"), 1)
        self.assertEqual(field(ovi, table, "plus_minus"), 1)
        # Real zeros from the boxscore.
        self.assertEqual(field(ovi, table, "assists"), 0)
        self.assertEqual(field(ovi, table, "penalty_minutes"), 0)
        self.assertEqual(field(ovi, table, "power_play_goals"), 0)
        self.assertEqual(field(ovi, table, "blocked"), 0)
        self.assertEqual(field(ovi, table, "takeaways"), 0)
        self.assertEqual(field(ovi, table, "face_off_pct"), 0.0)
        # §3: PBP-derived counters stay 0 when the player had no such event.
        self.assertEqual(field(ovi, table, "power_play_assists"), 0)
        self.assertEqual(field(ovi, table, "short_handed_goals"), 0)
        self.assertEqual(field(ovi, table, "short_handed_assists"), 0)
        self.assertEqual(field(ovi, table, "face_off_wins"), 0)
        self.assertEqual(field(ovi, table, "face_off_taken"), 0)

        # Faceoffs are counted for both sides of every draw in the PBP.
        self.assertEqual(field(rows[PINTO], table, "face_off_wins"), 2)
        self.assertEqual(field(rows[PINTO], table, "face_off_taken"), 2)
        self.assertEqual(field(rows[SOURDIF], table, "face_off_wins"), 0)
        self.assertEqual(field(rows[SOURDIF], table, "face_off_taken"), 1)
        # Defencemen land in the same table as forwards.
        self.assertEqual(field(rows[8480873], table, "assists"), 1)

    def test_boxscore_rows_null_every_field_the_api_omits(self):
        box = load_fixture("nhl_game_boxscore.json")
        home = box["playerByGameStats"]["homeTeam"]
        home["forwards"] = [without(home["forwards"][0], *SKATER_BOXSCORE_KEYS)]
        home["goalies"] = [without(home["goalies"][0], *GOALIE_BOXSCORE_KEYS)]
        _, _, _, player_rows, goalie_rows = self._build(box=box)

        # §2.2: nothing left but the keys — no 0 / "00:00" stand-ins. The PBP
        # aggregates of §3 keep their 0: they never came from the boxscore.
        ovi = by_player(player_rows, "game_player_stats")[OVECHKIN]
        pbp_counters = (
            "power_play_assists",
            "face_off_wins",
            "face_off_taken",
            "short_handed_goals",
            "short_handed_assists",
        )
        self.assertColumnsNone(
            ovi,
            "game_player_stats",
            keep=("team_id", "game_id", "player_id") + pbp_counters,
        )
        for column in pbp_counters:
            self.assertEqual(field(ovi, "game_player_stats", column), 0, column)

        # §2.3, same for the goalie side (the synthetic-payload variant of
        # timeonice / saves / decision lives in test_pipeline_optional_helpers).
        thompson = by_player(goalie_rows, "game_goalie_stats")[THOMPSON]
        self.assertColumnsNone(
            thompson, "game_goalie_stats", keep=("team_id", "game_id", "player_id")
        )

    def test_goalie_rows_split_shots_against_and_derive_percentages(self):
        rows = by_player(self._build()[4], "game_goalie_stats")
        table = "game_goalie_stats"
        thompson, ullmark = rows[THOMPSON], rows[ULLMARK]

        self.assertEqual(field(thompson, table, "team_id"), WSH)
        self.assertEqual(field(thompson, table, "game_id"), GAME_ID)
        self.assertEqual(field(thompson, table, "timeonice"), "60:00")
        self.assertEqual(field(thompson, table, "shots"), 35)
        self.assertEqual(field(thompson, table, "saves"), 34)
        self.assertEqual(field(thompson, table, "pim"), 0)
        self.assertEqual(field(thompson, table, "save_percentage"), 97.14)
        # "saves/shots" strings are split into two columns.
        self.assertEqual(field(thompson, table, "power_play_saves"), 8)
        self.assertEqual(field(thompson, table, "power_play_shots_against"), 8)
        self.assertEqual(field(thompson, table, "even_saves"), 26)
        self.assertEqual(field(thompson, table, "even_shots_against"), 27)
        self.assertEqual(field(thompson, table, "power_play_save_percentage"), 100.0)
        self.assertEqual(field(thompson, table, "even_strength_save_percentage"), 96.3)
        # "0/0": a real zero for the counters, undefined for the percentage.
        self.assertEqual(field(thompson, table, "short_handed_saves"), 0)
        self.assertEqual(field(thompson, table, "short_handed_shots_against"), 0)
        self.assertIsNone(field(thompson, table, "short_handed_save_percentage"))
        # §2.3: the boxscore goalie record has no assists / goals fields at all.
        self.assertIsNone(field(thompson, table, "assists"))
        self.assertIsNone(field(thompson, table, "goals"))
        # decision is stored as "did this goalie win?".
        self.assertIs(field(thompson, table, "decision"), True)
        self.assertIs(field(ullmark, table, "decision"), False)
        self.assertEqual(field(ullmark, table, "team_id"), OTT)
        self.assertEqual(field(ullmark, table, "save_percentage"), 91.3)
        self.assertEqual(field(ullmark, table, "even_strength_save_percentage"), 89.47)


if __name__ == "__main__":
    unittest.main()
