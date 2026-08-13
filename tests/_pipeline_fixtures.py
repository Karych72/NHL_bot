"""Shared plumbing for the loader row-assembly tests (``test_pipeline_*_rows.py``).

Holds the three things both modules need: the trimmed NHL API payloads captured
by ``scripts/capture_nhl_fixtures.py`` (see its docstring for URL + capture
date), the DB column order each ``build_*`` tuple must line up with, and a
``ModernNhlLoader`` stand-in whose only network exits (``get_json`` /
``fetch_paginated``) answer from those fixtures while real HTTP raises.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict, Iterable, Optional
from unittest import mock

import requests

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
PIPELINE_DIR = os.path.join(REPO_ROOT, "pipeline")
TELEGRAM_BOT = os.path.join(REPO_ROOT, "telegram_bot")
for path in (TELEGRAM_BOT, PIPELINE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import load_season_modern as loader  # noqa: E402

# Season and game the fixtures were captured for (WSH 4:1 OTT, 2026-03-18).
SEASON_ID = 20252026
SEASON_LABEL = "25/26"
GAME_ID = 2025021079
WSH, OTT = 15, 9
OVECHKIN, SOURDIF, SANDIN = 8471214, 8482088, 8480873
THOMPSON, ULLMARK, PINTO = 8480313, 8476999, 8481596
MCMICHAEL = 8481580  # only reachable through the player landing endpoint

# Column order of every INSERT/UPSERT in ``ModernNhlLoader.run()``: the build_*
# tuples are positional, so these lists are the contract the tests assert on.
# Copied by hand from run() — keep in sync manually; these tests pin build_*
# tuple order, not run()'s.
TABLE_COLUMNS: Dict[str, tuple] = {
    "teams": (
        "team_id", "season_id", "name", "division_name", "arena", "conference_name",
        "abbreviation", "first_year_of_play", "city", "active", "short_name",
    ),
    "teams_stats": (
        "team_id", "season_id", "games_played", "wins", "losses", "ot", "points",
        "procent_points", "goals_per_game", "goals_against_per_game",
        "power_play_percentage", "power_play_goals", "power_play_goals_against",
        "power_play_opportunities", "penalty_kill_percentage", "shots_per_game",
        "shots_allowed", "face_off_win_percentage",
    ),
    "rosters": (
        "player_id", "season_id", "name", "position", "jersey_number", "currentage",
        "lastname", "nationality", "captain", "alternate_captain", "rookie",
        "abbreviation", "current_team_id",
    ),
    "players_season_stats": (
        "time_on_ice", "assists", "goals", "pim", "shots", "games", "hits",
        "power_play_goals", "power_play_points", "power_play_time_on_ice",
        "even_time_on_ice", "penalty_minutes", "face_off_pct", "shot_pct",
        "game_winning_goals", "over_time_goals", "short_handed_goals",
        "short_handed_points", "short_handed_time_on_ice", "blocked", "plus_minus",
        "points", "shifts", "time_on_ice_per_game", "even_time_on_ice_per_game",
        "short_handed_time_on_ice_per_game", "power_play_time_on_ice_per_game",
        "oz_faceoff_pct", "dz_faceoff_pct", "nz_faceoff_pct", "shootout_goals",
        "shootout_shots", "shootout_gd_goals", "shootout_pct", "player_id", "season_id",
    ),
    "players_advanced_stats": (
        "player_id", "season_id", "sat_pct", "usat_pct", "goals_pct", "oz_start_pct",
        "dz_start_pct", "nz_start_pct", "on_ice_shooting_pct", "ev_goals_for",
        "ev_goals_against", "ev_goals_for_pct", "pp_goals_for", "pp_goals_against",
        "sh_goals_for", "sh_goals_against",
    ),
    "players_shot_types": (
        "player_id", "season_id", "goals_wrist", "shots_wrist", "goals_slap",
        "shots_slap", "goals_snap", "shots_snap", "goals_backhand", "shots_backhand",
        "goals_tip_in", "shots_tip_in", "goals_deflected", "shots_deflected",
        "goals_wrap_around", "shots_wrap_around",
    ),
    "goalies_season_stats": (
        "time_on_ice", "ot", "shutouts", "ties", "wins", "losses", "saves",
        "power_play_saves", "short_handed_saves", "even_saves", "short_handed_shots",
        "even_shots", "power_play_shots", "save_percentage", "goal_against_average",
        "games", "games_started", "shots_against", "goals_against",
        "time_on_ice_per_game", "power_play_save_percentage",
        "short_handed_save_percentage", "even_strength_save_percentage", "player_id",
        "season_id",
    ),
    "games": (
        "game_id", "day", "home_team_id", "away_team_id", "winner_id", "is_overtime",
        "is_shootouts", "season", "season_id",
    ),
    "all_goals": (
        "goal_player_id", "total_goals", "assist_player1_id", "assist_total_1",
        "assist_player2_id", "assist_total_2", "empty_net", "winner_goal", "is_ppg",
        "is_shg", "team_id", "game_id", "period", "time", "goals_away", "goals_home",
        "event_id",
    ),
    "game_team_stats": (
        "goals", "field", "pim", "shots", "power_play_percentage", "power_play_goals",
        "power_play_opportunities", "face_off_win_percentage", "blocked", "takeaways",
        "giveaways", "hits", "game_id", "team_id", "fst_period_goals",
        "snd_period_goals", "trd_period_goals",
    ),
    "game_player_stats": (
        "team_id", "game_id", "player_id", "time_on_ice", "assists", "goals", "shots",
        "hits", "power_play_goals", "power_play_assists", "penalty_minutes",
        "face_off_wins", "face_off_taken", "takeaways", "giveaways",
        "short_handed_goals", "short_handed_assists", "blocked", "plus_minus",
        "face_off_pct",
    ),
    "game_goalie_stats": (
        "team_id", "game_id", "player_id", "timeonice", "assists", "goals", "pim",
        "shots", "saves", "power_play_saves", "short_handed_saves", "even_saves",
        "short_handed_shots_against", "even_shots_against", "power_play_shots_against",
        "decision", "save_percentage", "power_play_save_percentage",
        "short_handed_save_percentage", "even_strength_save_percentage",
    ),
}


def field(row: tuple, table: str, column: str) -> Any:
    """Read *column* of a build_* tuple by its DB column name, not by index."""
    return row[TABLE_COLUMNS[table].index(column)]


def by_player(rows, table: str) -> Dict[int, tuple]:
    """Index build_* output by its ``player_id`` column (report order varies)."""
    return {field(row, table, "player_id"): row for row in rows}


def load_fixture(name: str) -> Any:
    """Load ``tests/fixtures/<name>`` — a real NHL API payload, trimmed."""
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def without(record: dict, *keys: str) -> dict:
    """Copy of *record* without *keys* — models "the API omitted this field"."""
    return {k: v for k, v in record.items() if k not in keys}


class NetworkBlockedError(AssertionError):
    """Raised when a loader test tries to open a real HTTP connection."""


def _blocked_request(self, method, url, *args, **kwargs):
    raise NetworkBlockedError(f"loader tests must not use the network: {method} {url}")


def make_loader() -> Any:
    """A ``ModernNhlLoader`` built without ``__init__``: no config, no session, no DB.

    ``__init__`` only reads ``telegram_bot/config.py`` and opens a
    ``requests.Session``; the ``build_*`` methods under test need neither, just
    the season attributes and the team lookups filled by ``load_team_reference``.
    """
    instance = loader.ModernNhlLoader.__new__(loader.ModernNhlLoader)
    instance.season_id = SEASON_ID
    instance.current_season_label = SEASON_LABEL
    instance.team_meta_by_id = {}
    instance.team_standings_by_abbrev = {}
    instance.team_abbrev_by_id = {}
    return instance


def stub_api(
    instance: Any,
    json_routes: Optional[Dict[str, Any]] = None,
    paginated_routes: Optional[Dict[str, Any]] = None,
) -> None:
    """Answer the loader's two network exits from fixtures.

    Both mappings go from a distinctive URL fragment to the payload the endpoint
    returns (``get_json``: the whole object; ``fetch_paginated``: the ``data``
    records). The longest matching fragment wins; a URL nothing matches raises,
    so an unstubbed endpoint fails the test instead of reaching the internet.
    """
    json_routes = dict(json_routes or {})
    paginated_routes = dict(paginated_routes or {})

    def _match(url: str, routes: Dict[str, Any], exit_name: str) -> Any:
        matched = sorted((frag for frag in routes if frag in url), key=len, reverse=True)
        if not matched:
            raise AssertionError(f"{exit_name}: no fixture routed for URL {url}")
        return routes[matched[0]]

    instance.get_json = lambda url: _match(url, json_routes, "get_json")
    instance.fetch_paginated = lambda url, page_size=500: _match(
        url, paginated_routes, "fetch_paginated"
    )


class LoaderApiTestCase(unittest.TestCase):
    """Base case for loader tests: every real HTTP call raises instead of leaving."""

    def setUp(self) -> None:
        patcher = mock.patch.object(requests.Session, "request", _blocked_request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertColumnsNone(self, row: tuple, table: str, keep: Iterable[str] = ()) -> None:
        """Assert every column of *table* except *keep* is ``None`` in *row*.

        Used for the "the source said nothing" half of the NULL contract: it
        pins down the whole table at once, so a column quietly switched back to
        an eager 0 / "" default fails here.
        """
        for column in TABLE_COLUMNS[table]:
            if column not in keep:
                self.assertIsNone(field(row, table, column), f"{table}.{column}")
