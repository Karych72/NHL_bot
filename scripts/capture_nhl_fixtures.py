"""Re-capture the trimmed NHL API fixtures under ``tests/fixtures/nhl_*.json``.

Manual, network-bound tool for the loader tests (``tests/test_pipeline_*.py``):
never imported by a test, never called from ``make``. Every file it writes is a
real response of the URL printed next to it below, trimmed only by *dropping
records* — server-side ``cayenneExp`` filters where the endpoint supports them,
an id whitelist otherwise. Keys and value types are stored exactly as the API
returned them (a ``null`` stays ``null``, a string stays a string).

Fixtures for ``https://api.nhle.com/stats/rest/en/*`` hold the ``data`` list of
the paginated response — i.e. what ``ModernNhlLoader.fetch_paginated`` returns;
fixtures for ``https://api-web.nhle.com/*`` hold the whole JSON object — what
``ModernNhlLoader.get_json`` returns.

Run (needs network): ``.venv/bin/python scripts/capture_nhl_fixtures.py``.
Last capture: 2026-08-13, season 20252026 (finished).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "pipeline"))

from load_season_modern import ModernNhlLoader  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

STATS_API = "https://api.nhle.com/stats/rest/en/"
WEB_API = "https://api-web.nhle.com/v1/"

SEASON_ID = 20252026
# WSH 4 : 1 OTT on 2026-03-18 — a finished regular-season game whose play-by-play
# carries every event type the loader aggregates (goal, penalty, faceoff, hit,
# blocked shot, giveaway, takeaway) plus an empty-net power-play goal.
GAME_ID = 2025021079
HOME_TEAM_ID, HOME_ABBREV = 15, "WSH"
AWAY_TEAM_ID, AWAY_ABBREV = 9, "OTT"

# Ovechkin (real zeros in shootout / short-handed columns, and a `null`
# defensiveZoneFaceoffPct) and Sourdif (centre — real faceoff counters).
SKATER_IDS = (8471214, 8482088)
GOALIE_ID = 8480313  # L. Thompson, WSH starter in GAME_ID
# Kept out of the roster and summary fixtures on purpose: exercises the player
# landing branch of ``supplement_rosters_from_reports``.
LANDING_PLAYER_ID = 8481580
# One forward / one defenceman / one goalie out of the 23-man roster response.
ROSTER_PLAYER_IDS = (8471214, 8480873, 8480313)
# Two forwards + one defenceman + the goalie per side of the boxscore.
BOXSCORE_PLAYER_IDS = (8471214, 8482088, 8480873, 8480313, 8481596, 8482245, 8476999)
# Plays kept per ``typeDescKey``: all five goals (they add up to the 4:1 final
# score) and the first events of every other type the loader counts.
PLAYS_PER_TYPE = {
    "goal": 5,
    "penalty": 2,
    "faceoff": 2,
    "hit": 1,
    "blocked-shot": 1,
    "giveaway": 1,
    "takeaway": 1,
}


def write_fixture(name: str, payload: Any, source_url: str) -> None:
    """Store *payload* as ``tests/fixtures/<name>`` and echo its source URL."""
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{name:<40} <- {source_url}")


def season_report_url(path: str, *filters: str) -> str:
    """Stats API URL for a season report, e.g. ``skater/summary``, with filters."""
    expr = "%20and%20".join((f"seasonId={SEASON_ID}", "gameTypeId=2", *filters))
    return f"{STATS_API}{path}?cayenneExp={expr}"


def any_of(field: str, values: Iterable[int]) -> str:
    """cayenneExp disjunction, e.g. ``(playerId=1%20or%20playerId=2)``."""
    return "(" + "%20or%20".join(f"{field}={v}" for v in values) + ")"


def trim_lists(value: Any, keep: int) -> Any:
    """Recursively cap every list at *keep* items; dict keys stay untouched."""
    if isinstance(value, dict):
        return {k: trim_lists(v, keep) for k, v in value.items()}
    if isinstance(value, list):
        return [trim_lists(v, keep) for v in value[:keep]]
    return value


def capture_report(loader: ModernNhlLoader, name: str, url: str) -> None:
    """Capture one paginated stats report (fixture = its ``data`` records)."""
    write_fixture(name, loader.fetch_paginated(url, page_size=200), url)


def capture_stats_reports(loader: ModernNhlLoader) -> None:
    skaters = any_of("playerId", SKATER_IDS)
    goalie = any_of("playerId", [GOALIE_ID])
    teams = any_of("teamId", (HOME_TEAM_ID, AWAY_TEAM_ID))

    capture_report(
        loader,
        "nhl_team_reference.json",
        f"{STATS_API}team?cayenneExp={any_of('id', (HOME_TEAM_ID, AWAY_TEAM_ID))}",
    )
    capture_report(loader, "nhl_team_summary.json", season_report_url("team/summary", teams))
    capture_report(
        loader,
        "nhl_games_meta.json",
        f"{STATS_API}game?cayenneExp=id={GAME_ID}",
    )
    capture_report(loader, "nhl_skater_summary.json", season_report_url("skater/summary", skaters))
    capture_report(loader, "nhl_skater_timeonice.json", season_report_url("skater/timeonice", skaters))
    capture_report(
        loader,
        "nhl_skater_faceoff_percentages.json",
        season_report_url("skater/faceoffpercentages", skaters),
    )
    capture_report(loader, "nhl_skater_shootout.json", season_report_url("skater/shootout", skaters))
    capture_report(loader, "nhl_skater_realtime.json", season_report_url("skater/realtime", skaters))
    capture_report(
        loader,
        "nhl_skater_goals_for_against.json",
        season_report_url("skater/goalsForAgainst", skaters),
    )
    capture_report(
        loader,
        "nhl_skater_puck_possessions.json",
        season_report_url("skater/puckPossessions", skaters),
    )
    capture_report(loader, "nhl_skater_shottype.json", season_report_url("skater/shottype", skaters))
    capture_report(loader, "nhl_goalie_summary.json", season_report_url("goalie/summary", goalie))
    capture_report(
        loader,
        "nhl_goalie_saves_by_strength.json",
        season_report_url("goalie/savesByStrength", goalie),
    )


def capture_web_payloads(loader: ModernNhlLoader) -> None:
    standings_url = f"{WEB_API}standings/now"
    standings = loader.get_json(standings_url)
    standings["standings"] = [
        s
        for s in standings["standings"]
        if (s.get("teamAbbrev") or {}).get("default") in (HOME_ABBREV, AWAY_ABBREV)
    ]
    write_fixture("nhl_standings_now.json", standings, standings_url)

    roster_url = f"{WEB_API}roster/{HOME_ABBREV}/{SEASON_ID}"
    roster = loader.get_json(roster_url)
    write_fixture(
        "nhl_roster_wsh.json",
        {
            group: [p for p in players if p.get("id") in ROSTER_PLAYER_IDS]
            for group, players in roster.items()
        },
        roster_url,
    )

    landing_url = f"{WEB_API}player/{LANDING_PLAYER_ID}/landing"
    # Landing carries season-by-season and last-5-games tables the loader never
    # reads; one record each is enough to keep the response shape.
    write_fixture("nhl_player_landing.json", trim_lists(loader.get_json(landing_url), keep=1), landing_url)

    pbp_url = f"{WEB_API}gamecenter/{GAME_ID}/play-by-play"
    pbp = loader.get_json(pbp_url)
    kept_plays = []
    for type_key, limit in PLAYS_PER_TYPE.items():
        kept_plays.extend([p for p in pbp["plays"] if p.get("typeDescKey") == type_key][:limit])
    kept_plays.sort(key=lambda p: p["sortOrder"])
    pbp["plays"] = kept_plays
    pbp["rosterSpots"] = pbp["rosterSpots"][:2]
    write_fixture("nhl_game_play_by_play.json", pbp, pbp_url)

    box_url = f"{WEB_API}gamecenter/{GAME_ID}/boxscore"
    box = loader.get_json(box_url)
    for side in ("homeTeam", "awayTeam"):
        box["playerByGameStats"][side] = {
            group: [p for p in players if p.get("playerId") in BOXSCORE_PLAYER_IDS]
            for group, players in box["playerByGameStats"][side].items()
        }
    write_fixture("nhl_game_boxscore.json", box, box_url)


def main() -> None:
    loader = ModernNhlLoader()
    loader.season_id = SEASON_ID
    capture_stats_reports(loader)
    capture_web_payloads(loader)


if __name__ == "__main__":
    main()
