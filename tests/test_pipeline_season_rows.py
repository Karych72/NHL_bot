"""Season-scoped row assembly in ``pipeline/load_season_modern.py``.

Feeds ``build_teams_and_stats`` / ``build_rosters`` /
``supplement_rosters_from_reports`` / ``build_player_season_stats`` /
``build_goalie_season_stats`` / ``build_player_advanced_stats`` /
``build_player_shot_types`` the trimmed real API payloads from
``tests/fixtures/nhl_*.json`` and checks the tuples they hand to the season
UPSERTs: real values (a real 0 included) survive, fields the API omits arrive as
``None`` (``docs/pipeline_nulls_and_explicit_null_tz.md`` §2), and the deliberate
non-NULL defaults of §3 stay non-``None``.
"""

from __future__ import annotations

import unittest
from datetime import date

import requests

from tests._pipeline_fixtures import (
    MCMICHAEL,
    OTT,
    OVECHKIN,
    SEASON_ID,
    SOURDIF,
    THOMPSON,
    WSH,
    LoaderApiTestCase,
    NetworkBlockedError,
    by_player,
    field,
    load_fixture,
    make_loader,
    stub_api,
    without,
)

TEAM_REFERENCE = "stats/rest/en/team"
TEAM_SUMMARY = "stats/rest/en/team/summary"
STANDINGS = "standings/now"
ROSTER = "/v1/roster/"
LANDING = "/landing"

# Every source key the build_* methods read for a nullable column, per report.
# Dropping them models a payload that still lists the entity but carries none of
# its numbers — the second half of the NULL contract.
TEAM_SUMMARY_STAT_KEYS = (
    "gamesPlayed", "wins", "losses", "otLosses", "points", "pointPct",
    "goalsForPerGame", "goalsAgainstPerGame", "powerPlayPct", "penaltyKillPct",
    "shotsForPerGame", "shotsAgainstPerGame", "faceoffWinPct",
)
SKATER_SUMMARY_STAT_KEYS = (
    "assists", "goals", "penaltyMinutes", "shots", "gamesPlayed", "ppGoals",
    "ppPoints", "faceoffWinPct", "shootingPct", "gameWinningGoals", "otGoals",
    "shGoals", "shPoints", "plusMinus", "points", "shifts",
    # Read off the summary row only as a fallback when the realtime report has
    # no such key (load_season_modern.py:609-612).
    "hits", "blockedShots",
)
GOALIE_SUMMARY_STAT_KEYS = (
    "otLosses", "shutouts", "wins", "losses", "saves", "savePct",
    "goalsAgainstAverage", "gamesPlayed", "gamesStarted", "shotsAgainst",
    "goalsAgainst",
)
PUCK_POSSESSION_KEYS = (
    "satPct", "usatPct", "goalsPct", "offensiveZoneStartPct", "defensiveZoneStartPct",
    "neutralZoneStartPct", "onIceShootingPct",
)
SHOT_TYPE_KEYS = tuple(
    f"{prefix}{shot}"
    for shot in ("Wrist", "Slap", "Snap", "Backhand", "TipIn", "Deflected", "WrapAround")
    for prefix in ("goals", "shotsOnNet")
)


class NetworkGuardTest(LoaderApiTestCase):
    """The guard every loader test inherits: no test may reach the NHL API."""

    def test_real_http_call_fails(self):
        with self.assertRaises(NetworkBlockedError):
            requests.Session().get("https://api-web.nhle.com/v1/standings/now")


class TeamRowsTest(LoaderApiTestCase):
    def _loader(self, summary_rows=None):
        instance = make_loader()
        stub_api(
            instance,
            json_routes={STANDINGS: load_fixture("nhl_standings_now.json")},
            paginated_routes={
                TEAM_REFERENCE: load_fixture("nhl_team_reference.json"),
                TEAM_SUMMARY: (
                    load_fixture("nhl_team_summary.json")
                    if summary_rows is None
                    else summary_rows
                ),
            },
        )
        instance.load_team_reference()
        return instance

    def test_teams_row_merges_summary_meta_and_standings(self):
        teams_rows, _ = self._loader().build_teams_and_stats()
        wsh = next(r for r in teams_rows if field(r, "teams", "team_id") == WSH)

        self.assertEqual(field(wsh, "teams", "season_id"), SEASON_ID)
        self.assertEqual(field(wsh, "teams", "name"), "Washington Capitals")
        self.assertEqual(field(wsh, "teams", "abbreviation"), "WSH")
        self.assertEqual(field(wsh, "teams", "division_name"), "Metropolitan")
        self.assertEqual(field(wsh, "teams", "conference_name"), "Eastern")
        self.assertEqual(field(wsh, "teams", "city"), "Washington")
        self.assertEqual(field(wsh, "teams", "short_name"), "Capitals")
        # §3: written because the team appeared in this season's summary.
        self.assertIs(field(wsh, "teams", "active"), True)
        # §2.8: neither endpoint carries these two.
        self.assertIsNone(field(wsh, "teams", "arena"))
        self.assertIsNone(field(wsh, "teams", "first_year_of_play"))

    def test_teams_stats_row_scales_ratios_and_nulls_absent_columns(self):
        _, stats_rows = self._loader().build_teams_and_stats()
        wsh = next(r for r in stats_rows if field(r, "teams_stats", "team_id") == WSH)

        self.assertEqual(field(wsh, "teams_stats", "games_played"), 82)
        self.assertEqual(field(wsh, "teams_stats", "wins"), 43)
        self.assertEqual(field(wsh, "teams_stats", "losses"), 30)
        self.assertEqual(field(wsh, "teams_stats", "ot"), 9)
        self.assertEqual(field(wsh, "teams_stats", "points"), 95)
        self.assertEqual(field(wsh, "teams_stats", "goals_per_game"), 3.18292)
        self.assertEqual(field(wsh, "teams_stats", "goals_against_per_game"), 2.90243)
        self.assertEqual(field(wsh, "teams_stats", "shots_per_game"), 28.04878)
        self.assertEqual(field(wsh, "teams_stats", "shots_allowed"), 28.13414)
        # Ratios from the API are stored as percentages.
        self.assertEqual(field(wsh, "teams_stats", "procent_points"), 57.93)
        self.assertEqual(field(wsh, "teams_stats", "power_play_percentage"), 17.84)
        self.assertEqual(field(wsh, "teams_stats", "penalty_kill_percentage"), 80.08)
        self.assertEqual(field(wsh, "teams_stats", "face_off_win_percentage"), 49.32)
        # team/summary does not report PP goals / opportunities at all → NULL.
        self.assertIsNone(field(wsh, "teams_stats", "power_play_goals"))
        self.assertIsNone(field(wsh, "teams_stats", "power_play_goals_against"))
        self.assertIsNone(field(wsh, "teams_stats", "power_play_opportunities"))

    def test_teams_stats_keeps_real_zero_and_nulls_missing_fields(self):
        summary = load_fixture("nhl_team_summary.json")
        wsh_row = next(r for r in summary if r["teamId"] == WSH)
        ott_row = next(r for r in summary if r["teamId"] == OTT)
        _, stats_rows = self._loader(
            [
                dict(wsh_row, wins=0, faceoffWinPct=0.0),  # the API really sent 0
                without(ott_row, *TEAM_SUMMARY_STAT_KEYS),  # the API sent nothing
            ]
        ).build_teams_and_stats()
        by_team = {field(r, "teams_stats", "team_id"): r for r in stats_rows}

        self.assertEqual(field(by_team[WSH], "teams_stats", "wins"), 0)
        self.assertEqual(field(by_team[WSH], "teams_stats", "face_off_win_percentage"), 0.0)
        self.assertColumnsNone(by_team[OTT], "teams_stats", keep=("team_id", "season_id"))


class RosterRowsTest(LoaderApiTestCase):
    def _loader(self, roster_payload=None):
        instance = make_loader()
        stub_api(
            instance,
            json_routes={
                STANDINGS: load_fixture("nhl_standings_now.json"),
                ROSTER: (
                    load_fixture("nhl_roster_wsh.json")
                    if roster_payload is None
                    else roster_payload
                ),
                LANDING: load_fixture("nhl_player_landing.json"),
            },
            paginated_routes={
                TEAM_REFERENCE: load_fixture("nhl_team_reference.json"),
                TEAM_SUMMARY: load_fixture("nhl_team_summary.json"),
            },
        )
        instance.load_team_reference()
        return instance

    def _wsh_roster(self, instance):
        teams_rows, _ = instance.build_teams_and_stats()
        wsh_only = [r for r in teams_rows if field(r, "teams", "team_id") == WSH]
        return instance.build_rosters(wsh_only)

    def test_roster_row_from_team_roster_endpoint(self):
        rows = by_player(self._wsh_roster(self._loader()), "rosters")
        ovi = rows[OVECHKIN]

        self.assertEqual(field(ovi, "rosters", "season_id"), SEASON_ID)
        self.assertEqual(field(ovi, "rosters", "name"), "Alex Ovechkin")
        self.assertEqual(field(ovi, "rosters", "lastname"), "Ovechkin")
        self.assertEqual(field(ovi, "rosters", "position"), "L")
        self.assertEqual(field(ovi, "rosters", "abbreviation"), "L")
        self.assertEqual(field(ovi, "rosters", "jersey_number"), 8)
        self.assertEqual(field(ovi, "rosters", "nationality"), "RUS")
        self.assertEqual(field(ovi, "rosters", "current_team_id"), WSH)
        # Born 1985-09-17: exact age today, so an off-by-one around the birthday
        # in optional_age_from_birthdate fails here.
        today = date.today()
        self.assertEqual(
            field(ovi, "rosters", "currentage"),
            today.year - 1985 - ((today.month, today.day) < (9, 17)),
        )
        # §2.7: /v1/roster never exposes these, so "unknown" ≠ False.
        self.assertIsNone(field(ovi, "rosters", "captain"))
        self.assertIsNone(field(ovi, "rosters", "alternate_captain"))
        self.assertIsNone(field(ovi, "rosters", "rookie"))
        # All three roster groups land in the same table.
        self.assertEqual(field(rows[THOMPSON], "rosters", "position"), "G")

    def test_roster_row_nulls_fields_the_payload_omits(self):
        payload = load_fixture("nhl_roster_wsh.json")
        payload["forwards"] = [
            without(
                payload["forwards"][0],
                "sweaterNumber", "birthDate", "birthCountry", "firstName", "lastName",
                "positionCode",
            )
        ]
        rows = by_player(self._wsh_roster(self._loader(payload)), "rosters")

        # §2.7: nothing but the keys and the team the record was listed under.
        self.assertColumnsNone(
            rows[OVECHKIN], "rosters", keep=("player_id", "season_id", "current_team_id")
        )
        self.assertEqual(field(rows[OVECHKIN], "rosters", "current_team_id"), WSH)

    def test_supplement_adds_report_only_and_landing_only_players(self):
        instance = self._loader()
        roster_rows = self._wsh_roster(instance)
        supplemented = instance.supplement_rosters_from_reports(
            roster_rows,
            load_fixture("nhl_skater_summary.json"),
            load_fixture("nhl_goalie_summary.json"),
            {MCMICHAEL},
        )
        rows = by_player(supplemented, "rosters")

        # Sourdif plays the season but is not on the trimmed roster response.
        sourdif = rows[SOURDIF]
        self.assertEqual(field(sourdif, "rosters", "name"), "Justin Sourdif")
        self.assertEqual(field(sourdif, "rosters", "lastname"), "Sourdif")
        self.assertEqual(field(sourdif, "rosters", "position"), "C")
        self.assertEqual(field(sourdif, "rosters", "current_team_id"), WSH)
        # Stats summary has no jersey / birth data → NULL, not 0 or "".
        self.assertIsNone(field(sourdif, "rosters", "jersey_number"))
        self.assertIsNone(field(sourdif, "rosters", "currentage"))
        self.assertIsNone(field(sourdif, "rosters", "nationality"))

        # Neither on the roster nor in the summaries → player landing fallback.
        mcmichael = rows[MCMICHAEL]
        self.assertEqual(field(mcmichael, "rosters", "name"), "Connor McMichael")
        self.assertEqual(field(mcmichael, "rosters", "jersey_number"), 77)
        self.assertEqual(field(mcmichael, "rosters", "nationality"), "CAN")
        self.assertEqual(field(mcmichael, "rosters", "current_team_id"), 19)

        # A player already on the roster keeps the richer roster row.
        self.assertEqual(field(rows[OVECHKIN], "rosters", "jersey_number"), 8)


class SkaterSeasonStatsTest(LoaderApiTestCase):
    REPORTS = {
        "skater/summary": "nhl_skater_summary.json",
        "skater/timeonice": "nhl_skater_timeonice.json",
        "skater/faceoffpercentages": "nhl_skater_faceoff_percentages.json",
        "skater/shootout": "nhl_skater_shootout.json",
        "skater/realtime": "nhl_skater_realtime.json",
    }

    def _rows(self, overrides=None):
        instance = make_loader()
        routes = {frag: load_fixture(name) for frag, name in self.REPORTS.items()}
        routes.update(overrides or {})
        stub_api(instance, paginated_routes=routes)
        rows, _ = instance.build_player_season_stats()
        return by_player(rows, "players_season_stats")

    def test_row_joins_every_report_and_keeps_real_zeros(self):
        ovi = self._rows()[OVECHKIN]
        table = "players_season_stats"

        self.assertEqual(field(ovi, table, "season_id"), SEASON_ID)
        self.assertEqual(field(ovi, table, "assists"), 32)
        self.assertEqual(field(ovi, table, "goals"), 32)
        self.assertEqual(field(ovi, table, "points"), 64)
        self.assertEqual(field(ovi, table, "shots"), 244)
        self.assertEqual(field(ovi, table, "games"), 82)
        self.assertEqual(field(ovi, table, "pim"), 26)
        self.assertEqual(field(ovi, table, "penalty_minutes"), 26)
        self.assertEqual(field(ovi, table, "plus_minus"), -5)
        self.assertEqual(field(ovi, table, "power_play_goals"), 5)
        self.assertEqual(field(ovi, table, "power_play_points"), 19)
        self.assertEqual(field(ovi, table, "game_winning_goals"), 5)
        self.assertEqual(field(ovi, table, "shot_pct"), 13.11)
        self.assertEqual(field(ovi, table, "face_off_pct"), 50.0)
        # hits / blocked come from the realtime report (summary dropped them).
        self.assertEqual(field(ovi, table, "hits"), 134)
        self.assertEqual(field(ovi, table, "blocked"), 16)
        # Seconds from the timeonice report become mm:ss.
        self.assertEqual(field(ovi, table, "time_on_ice"), "1430:46")
        self.assertEqual(field(ovi, table, "even_time_on_ice"), "1060:57")
        self.assertEqual(field(ovi, table, "power_play_time_on_ice"), "367:51")
        self.assertEqual(field(ovi, table, "short_handed_time_on_ice"), "1:58")
        self.assertEqual(field(ovi, table, "time_on_ice_per_game"), "17:27")
        self.assertEqual(field(ovi, table, "even_time_on_ice_per_game"), "12:56")
        self.assertEqual(field(ovi, table, "power_play_time_on_ice_per_game"), "4:29")
        self.assertEqual(field(ovi, table, "short_handed_time_on_ice_per_game"), "0:01")
        # Real zeros the API did send must not turn into NULL.
        self.assertEqual(field(ovi, table, "over_time_goals"), 0)
        self.assertEqual(field(ovi, table, "short_handed_goals"), 0)
        self.assertEqual(field(ovi, table, "short_handed_points"), 0)
        self.assertEqual(field(ovi, table, "shootout_goals"), 0)
        self.assertEqual(field(ovi, table, "shootout_gd_goals"), 0)
        self.assertEqual(field(ovi, table, "shootout_shots"), 3)
        self.assertEqual(field(ovi, table, "shootout_pct"), 0.0)
        self.assertEqual(field(ovi, table, "oz_faceoff_pct"), 100.0)
        self.assertEqual(field(ovi, table, "nz_faceoff_pct"), 33.33)
        # The faceoff report itself sends null for a zone he never started in.
        self.assertIsNone(field(ovi, table, "dz_faceoff_pct"))
        # Current behaviour, not a wish: the loader reads ``shifts`` off the
        # summary row, and skater/summary stopped carrying it — the value lives
        # in the timeonice report (1615 here), so the column loads as NULL.
        self.assertIsNone(field(ovi, table, "shifts"))

    def test_row_nulls_columns_whose_report_row_or_field_is_absent(self):
        # Sourdif keeps his summary row (he played the season) but it carries no
        # numbers, and none of the joined reports lists him at all.
        overrides = {
            frag: [r for r in load_fixture(name) if r["playerId"] != SOURDIF]
            for frag, name in self.REPORTS.items()
            if frag != "skater/summary"
        }
        overrides["skater/summary"] = [
            r if r["playerId"] != SOURDIF else without(r, *SKATER_SUMMARY_STAT_KEYS)
            for r in load_fixture(self.REPORTS["skater/summary"])
        ]
        sourdif = self._rows(overrides)[SOURDIF]

        # §2.5: unknown everywhere, not 0 / "00:00" / 0.0.
        self.assertColumnsNone(
            sourdif, "players_season_stats", keep=("player_id", "season_id")
        )


class GoalieSeasonStatsTest(LoaderApiTestCase):
    def _rows(self, summary=None, saves_by_strength=None):
        instance = make_loader()
        stub_api(
            instance,
            paginated_routes={
                "goalie/summary": (
                    load_fixture("nhl_goalie_summary.json") if summary is None else summary
                ),
                "goalie/savesByStrength": (
                    load_fixture("nhl_goalie_saves_by_strength.json")
                    if saves_by_strength is None
                    else saves_by_strength
                ),
            },
        )
        rows, _ = instance.build_goalie_season_stats()
        return by_player(rows, "goalies_season_stats")

    def test_row_joins_saves_by_strength_and_derives_percentages(self):
        gk = self._rows()[THOMPSON]
        table = "goalies_season_stats"

        self.assertEqual(field(gk, table, "season_id"), SEASON_ID)
        self.assertEqual(field(gk, table, "games"), 58)
        self.assertEqual(field(gk, table, "games_started"), 58)
        self.assertEqual(field(gk, table, "wins"), 31)
        self.assertEqual(field(gk, table, "losses"), 21)
        self.assertEqual(field(gk, table, "ot"), 6)
        self.assertEqual(field(gk, table, "shutouts"), 4)
        self.assertEqual(field(gk, table, "saves"), 1447)
        self.assertEqual(field(gk, table, "shots_against"), 1587)
        self.assertEqual(field(gk, table, "goals_against"), 140)
        self.assertEqual(field(gk, table, "save_percentage"), 91.18)
        self.assertEqual(field(gk, table, "goal_against_average"), 2.43828)
        self.assertEqual(field(gk, table, "power_play_saves"), 218)
        self.assertEqual(field(gk, table, "power_play_shots"), 247)
        self.assertEqual(field(gk, table, "short_handed_saves"), 43)
        self.assertEqual(field(gk, table, "short_handed_shots"), 52)
        self.assertEqual(field(gk, table, "even_saves"), 1186)
        self.assertEqual(field(gk, table, "even_shots"), 1288)
        self.assertEqual(field(gk, table, "power_play_save_percentage"), 88.26)
        self.assertEqual(field(gk, table, "short_handed_save_percentage"), 82.69)
        self.assertEqual(field(gk, table, "even_strength_save_percentage"), 92.08)
        # §3: the API has had no ties stat since 2005, 0 is a known business fact.
        self.assertEqual(field(gk, table, "ties"), 0)
        # Current behaviour, and a contradiction worth naming: the loader hard-codes
        # both ice-time columns to None (load_season_modern.py:684, 706) claiming the
        # reports have no ice time — but goalie/summary does send ``timeOnIce``
        # (206703 seconds in this very fixture, i.e. "3445:03"), so the column loads
        # as NULL although the data is right there. Only time_on_ice_per_game is
        # genuinely absent from both reports (it would be timeOnIce / gamesPlayed).
        self.assertIsNone(field(gk, table, "time_on_ice"))
        self.assertIsNone(field(gk, table, "time_on_ice_per_game"))

    def test_row_keeps_real_zero_when_the_api_sends_one(self):
        summary = load_fixture("nhl_goalie_summary.json")
        gk = self._rows(summary=[dict(summary[0], shutouts=0, wins=0)])[THOMPSON]

        self.assertEqual(field(gk, "goalies_season_stats", "shutouts"), 0)
        self.assertEqual(field(gk, "goalies_season_stats", "wins"), 0)

    def test_row_nulls_every_column_the_reports_do_not_cover(self):
        summary = load_fixture("nhl_goalie_summary.json")
        gk = self._rows(
            summary=[without(summary[0], *GOALIE_SUMMARY_STAT_KEYS)],
            saves_by_strength=[],  # no row for this goalie at all
        )[THOMPSON]

        # §3: ties stays 0 even with nothing in the payload.
        self.assertEqual(field(gk, "goalies_season_stats", "ties"), 0)
        self.assertColumnsNone(
            gk, "goalies_season_stats", keep=("player_id", "season_id", "ties")
        )


class AdvancedAndShotTypeRowsTest(LoaderApiTestCase):
    def test_advanced_row_keeps_real_zeros_and_nulls_the_missing_report(self):
        instance = make_loader()
        goals_for_against = [
            r for r in load_fixture("nhl_skater_goals_for_against.json")
            if r["playerId"] != SOURDIF
        ]
        puck_possessions = [
            r if r["playerId"] != SOURDIF else without(r, *PUCK_POSSESSION_KEYS)
            for r in load_fixture("nhl_skater_puck_possessions.json")
        ]
        stub_api(
            instance,
            paginated_routes={
                "skater/goalsForAgainst": goals_for_against,
                "skater/puckPossessions": puck_possessions,
            },
        )
        rows = by_player(instance.build_player_advanced_stats(), "players_advanced_stats")
        table = "players_advanced_stats"
        ovi, sourdif = rows[OVECHKIN], rows[SOURDIF]

        self.assertEqual(field(ovi, table, "season_id"), SEASON_ID)
        self.assertEqual(field(ovi, table, "sat_pct"), 50.8)
        self.assertEqual(field(ovi, table, "usat_pct"), 49.5)
        self.assertEqual(field(ovi, table, "goals_pct"), 55.5)
        self.assertEqual(field(ovi, table, "oz_start_pct"), 59.79)
        self.assertEqual(field(ovi, table, "dz_start_pct"), 9.92)
        self.assertEqual(field(ovi, table, "nz_start_pct"), 30.28)
        self.assertEqual(field(ovi, table, "on_ice_shooting_pct"), 11.1)
        self.assertEqual(field(ovi, table, "ev_goals_for"), 63)
        self.assertEqual(field(ovi, table, "ev_goals_against"), 57)
        self.assertEqual(field(ovi, table, "ev_goals_for_pct"), 52.5)
        # NHL spells this one powerPlayGoalFor; a wrong key would surface as NULL.
        self.assertEqual(field(ovi, table, "pp_goals_for"), 41)
        self.assertEqual(field(ovi, table, "sh_goals_against"), 11)
        # Real zeros.
        self.assertEqual(field(ovi, table, "pp_goals_against"), 0)
        self.assertEqual(field(ovi, table, "sh_goals_for"), 0)

        # §2.6: no goalsForAgainst row and an empty puckPossessions one.
        self.assertColumnsNone(sourdif, table, keep=("player_id", "season_id"))

    def test_shot_type_row_keeps_real_zeros_and_nulls_missing_fields(self):
        instance = make_loader()
        report = load_fixture("nhl_skater_shottype.json")
        report = [
            r if r["playerId"] != SOURDIF else without(r, *SHOT_TYPE_KEYS)
            for r in report
        ]
        stub_api(instance, paginated_routes={"skater/shottype": report})
        rows = by_player(instance.build_player_shot_types(), "players_shot_types")
        table = "players_shot_types"
        ovi, sourdif = rows[OVECHKIN], rows[SOURDIF]

        self.assertEqual(field(ovi, table, "season_id"), SEASON_ID)
        self.assertEqual(field(ovi, table, "goals_wrist"), 8)
        self.assertEqual(field(ovi, table, "shots_wrist"), 60)
        self.assertEqual(field(ovi, table, "goals_slap"), 3)
        self.assertEqual(field(ovi, table, "shots_slap"), 57)
        self.assertEqual(field(ovi, table, "goals_snap"), 17)
        self.assertEqual(field(ovi, table, "shots_snap"), 92)
        self.assertEqual(field(ovi, table, "goals_backhand"), 1)
        self.assertEqual(field(ovi, table, "shots_backhand"), 6)
        self.assertEqual(field(ovi, table, "goals_tip_in"), 3)
        self.assertEqual(field(ovi, table, "shots_tip_in"), 23)
        self.assertEqual(field(ovi, table, "shots_deflected"), 4)
        # Real zeros: no deflected or wrap-around goals all season.
        self.assertEqual(field(ovi, table, "goals_deflected"), 0)
        self.assertEqual(field(ovi, table, "goals_wrap_around"), 0)
        self.assertEqual(field(ovi, table, "shots_wrap_around"), 0)

        self.assertColumnsNone(sourdif, table, keep=("player_id", "season_id"))


if __name__ == "__main__":
    unittest.main()
