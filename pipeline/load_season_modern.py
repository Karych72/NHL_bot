import os
import getpass
import time
from datetime import date, datetime
from typing import Dict, List, Tuple

import psycopg2
import requests
from psycopg2 import sql as psql
from psycopg2.extras import execute_values


def to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def pct_from_ratio(value, default=0.0):
    if value is None:
        return default
    val = to_float(value, None)
    if val is None:
        return default
    if val <= 1.0:
        return round(val * 100, 2)
    return round(val, 2)


def split_sv(value: str) -> Tuple[int, int]:
    if not value or "/" not in str(value):
        return 0, 0
    left, right = str(value).split("/", 1)
    return to_int(left, 0), to_int(right, 0)


def age_from_birthdate(birth_date: str) -> int:
    if not birth_date:
        return 0
    try:
        born = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except ValueError:
        return 0
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


class ModernNhlLoader:
    def __init__(self):
        self.pg_host = os.getenv("PG_HOST", "localhost")
        self.pg_port = os.getenv("PG_PORT", "5432")
        self.pg_user = os.getenv("PG_USER", "") or getpass.getuser()
        self.pg_database = os.getenv("PG_DATABASE", "postgres")

        self.season_id = int(os.getenv("SEASON_ID", "20252026"))
        self.current_season_label = os.getenv("CURRENT_SEASON", "25/26")
        self.start_date = os.getenv("DATE_FROM", "2025-10-01")
        self.end_date = os.getenv("DATE_TO", date.today().isoformat())

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "NHL-bot-loader/1.0",
                "Accept": "application/json",
            }
        )
        self.timeout = 30

        self.team_meta_by_id: Dict[int, dict] = {}
        self.team_standings_by_abbrev: Dict[str, dict] = {}
        self.team_abbrev_by_id: Dict[int, str] = {}

    def get_json(self, url: str) -> dict:
        last_error = None
        backoff = 2
        for _ in range(10):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait_seconds = to_int(retry_after, backoff)
                    wait_seconds = max(wait_seconds, backoff)
                    time.sleep(wait_seconds)
                    backoff = min(backoff * 2, 60)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"Request failed: {url}; error: {last_error}")

    def fetch_paginated(self, url: str, page_size: int = 500) -> List[dict]:
        out: List[dict] = []
        start = 0
        while True:
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}start={start}&limit={page_size}"
            payload = self.get_json(page_url)
            data = payload.get("data", [])
            total = payload.get("total", len(data))
            out.extend(data)
            start += len(data)
            if len(data) == 0 or start >= total:
                break
        return out

    def load_team_reference(self):
        teams = self.fetch_paginated("https://api.nhle.com/stats/rest/en/team", page_size=200)
        self.team_meta_by_id = {to_int(t.get("id")): t for t in teams}
        for team_id, row in self.team_meta_by_id.items():
            tri = row.get("triCode") or row.get("rawTricode")
            if tri:
                self.team_abbrev_by_id[team_id] = tri

        standings = self.get_json("https://api-web.nhle.com/v1/standings/now").get("standings", [])
        self.team_standings_by_abbrev = {}
        for s in standings:
            tri = (s.get("teamAbbrev") or {}).get("default")
            if tri:
                self.team_standings_by_abbrev[tri] = s

    def build_teams_and_stats(self) -> Tuple[List[tuple], List[tuple]]:
        summary_url = (
            "https://api.nhle.com/stats/rest/en/team/summary"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        summary_rows = self.fetch_paginated(summary_url, page_size=200)

        teams_rows: List[tuple] = []
        teams_stats_rows: List[tuple] = []

        for row in summary_rows:
            team_id = to_int(row.get("teamId"))
            meta = self.team_meta_by_id.get(team_id, {})
            tri = meta.get("triCode") or meta.get("rawTricode") or ""
            standing = self.team_standings_by_abbrev.get(tri, {})

            full_name = meta.get("fullName") or row.get("teamFullName") or tri
            common_name = (standing.get("teamCommonName") or {}).get("default")
            short_name = common_name or full_name

            division = standing.get("divisionName")
            conference = standing.get("conferenceName")
            city = full_name.split(" ")[0] if full_name else None

            teams_rows.append(
                (
                    team_id,
                    full_name,
                    division,
                    None,  # arena
                    conference,
                    tri,
                    None,  # first_year_of_play
                    city,
                    True,
                    short_name,
                )
            )

            teams_stats_rows.append(
                (
                    team_id,
                    to_int(row.get("gamesPlayed")),
                    to_int(row.get("wins")),
                    to_int(row.get("losses")),
                    to_int(row.get("otLosses")),
                    to_int(row.get("points")),
                    pct_from_ratio(row.get("pointPct")),
                    to_float(row.get("goalsForPerGame")),
                    to_float(row.get("goalsAgainstPerGame")),
                    pct_from_ratio(row.get("powerPlayPct")),
                    to_int(row.get("powerPlayGoals")),
                    to_int(row.get("powerPlayGoalsAgainst")),
                    to_int(row.get("powerPlayOpportunities")),
                    pct_from_ratio(row.get("penaltyKillPct")),
                    to_float(row.get("shotsForPerGame")),
                    to_float(row.get("shotsAgainstPerGame")),
                    pct_from_ratio(row.get("faceoffWinPct")),
                )
            )
        return teams_rows, teams_stats_rows

    def build_rosters(self, team_rows: List[tuple]) -> List[tuple]:
        rows_by_player: Dict[int, tuple] = {}
        season_str = str(self.season_id)

        for team in team_rows:
            team_id = to_int(team[0])
            tri = team[5]
            if not tri:
                continue
            url = f"https://api-web.nhle.com/v1/roster/{tri}/{season_str}"
            payload = self.get_json(url)
            for group in ("forwards", "defensemen", "goalies"):
                for p in payload.get(group, []):
                    player_id = to_int(p.get("id"))
                    first_name = (p.get("firstName") or {}).get("default", "")
                    last_name = (p.get("lastName") or {}).get("default", "")
                    full_name = f"{first_name} {last_name}".strip()
                    position = p.get("positionCode")
                    row = (
                        player_id,
                        full_name,
                        position,
                        to_int(p.get("sweaterNumber"), 0),
                        age_from_birthdate(p.get("birthDate")),
                        last_name,
                        p.get("birthCountry"),
                        False,  # captain
                        False,  # alternate_captain
                        False,  # rookie
                        position,  # abbreviation
                        team_id,
                    )
                    rows_by_player[player_id] = row
        return list(rows_by_player.values())

    def build_player_season_stats(self) -> List[tuple]:
        url = (
            "https://api.nhle.com/stats/rest/en/skater/summary"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        rows = self.fetch_paginated(url, page_size=1000)
        out = []
        for r in rows:
            out.append(
                (
                    "00:00",  # time_on_ice
                    to_int(r.get("assists")),
                    to_int(r.get("goals")),
                    to_int(r.get("penaltyMinutes")),
                    to_int(r.get("shots")),
                    to_int(r.get("gamesPlayed")),
                    to_int(r.get("hits")),
                    to_int(r.get("ppGoals")),
                    to_int(r.get("ppPoints")),
                    "00:00",  # power_play_time_on_ice
                    "00:00",  # even_time_on_ice
                    to_int(r.get("penaltyMinutes")),
                    pct_from_ratio(r.get("faceoffWinPct")),
                    pct_from_ratio(r.get("shootingPct")),
                    to_int(r.get("gameWinningGoals")),
                    to_int(r.get("otGoals")),
                    to_int(r.get("shGoals")),
                    to_int(r.get("shPoints")),
                    "00:00",  # short_handed_time_on_ice
                    to_int(r.get("blockedShots")),
                    to_int(r.get("plusMinus")),
                    to_int(r.get("points")),
                    to_int(r.get("shifts")),
                    "00:00",  # time_on_ice_per_game
                    "00:00",  # even_time_on_ice_per_game
                    "00:00",  # short_handed_time_on_ice_per_game
                    "00:00",  # power_play_time_on_ice_per_game
                    to_int(r.get("playerId")),
                )
            )
        return out

    def build_goalie_season_stats(self) -> List[tuple]:
        url = (
            "https://api.nhle.com/stats/rest/en/goalie/summary"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        rows = self.fetch_paginated(url, page_size=1000)
        out = []
        for r in rows:
            out.append(
                (
                    "00:00",  # time_on_ice
                    to_int(r.get("otLosses")),
                    to_int(r.get("shutouts")),
                    0,  # ties
                    to_int(r.get("wins")),
                    to_int(r.get("losses")),
                    to_int(r.get("saves")),
                    0,  # power_play_saves
                    0,  # short_handed_saves
                    0,  # even_saves
                    0,  # short_handed_shots
                    0,  # even_shots
                    0,  # power_play_shots
                    pct_from_ratio(r.get("savePct")),
                    to_float(r.get("goalsAgainstAverage")),
                    to_int(r.get("gamesPlayed")),
                    to_int(r.get("gamesStarted")),
                    to_int(r.get("shotsAgainst")),
                    to_int(r.get("goalsAgainst")),
                    "00:00",  # time_on_ice_per_game
                    0.0,  # power_play_save_percentage
                    0.0,  # short_handed_save_percentage
                    0.0,  # even_strength_save_percentage
                    to_int(r.get("playerId")),
                )
            )
        return out

    def fetch_final_games(self) -> List[dict]:
        cayenne = (
            f"season={self.season_id} and gameType=2 and gameStateId=7 and "
            f'gameDate>="{self.start_date}" and gameDate<="{self.end_date}"'
        )
        url = f"https://api.nhle.com/stats/rest/en/game?cayenneExp={cayenne}"
        return self.fetch_paginated(url, page_size=1000)

    def build_game_rows(self, games_meta: List[dict]) -> Tuple[List[tuple], List[tuple], List[tuple], List[tuple], List[tuple]]:
        games_rows: List[tuple] = []
        all_goals_rows: List[tuple] = []
        game_team_rows: List[tuple] = []
        game_player_rows: List[tuple] = []
        game_goalie_rows: List[tuple] = []

        total = len(games_meta)
        for idx, game in enumerate(games_meta, start=1):
            game_id = to_int(game.get("id"))
            home_id = to_int(game.get("homeTeamId"))
            away_id = to_int(game.get("visitingTeamId"))
            home_score = to_int(game.get("homeScore"))
            away_score = to_int(game.get("visitingScore"))
            winner = home_id if home_score > away_score else away_id

            pbp = self.get_json(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play")
            box = self.get_json(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore")

            period_desc = pbp.get("periodDescriptor") or {}
            is_ot = str(period_desc.get("periodType")) == "OT" or to_int(period_desc.get("number")) > 3
            is_shootout = bool(pbp.get("shootoutInUse"))

            games_rows.append(
                (
                    game_id,
                    game.get("gameDate"),
                    home_id,
                    away_id,
                    winner,
                    is_ot,
                    is_shootout,
                    self.current_season_label,
                )
            )

            plays = pbp.get("plays", [])
            goals = [p for p in plays if p.get("typeDescKey") == "goal"]
            penalties = [p for p in plays if p.get("typeDescKey") == "penalty"]
            hits = [p for p in plays if p.get("typeDescKey") == "hit"]
            giveaways = [p for p in plays if p.get("typeDescKey") == "giveaway"]
            takeaways = [p for p in plays if p.get("typeDescKey") == "takeaway"]
            blocked = [p for p in plays if p.get("typeDescKey") == "blocked-shot"]
            faceoffs = [p for p in plays if p.get("typeDescKey") == "faceoff"]

            team_period_goals = {
                home_id: {1: 0, 2: 0, 3: 0},
                away_id: {1: 0, 2: 0, 3: 0},
            }

            for g in goals:
                d = g.get("details", {})
                period = to_int((g.get("periodDescriptor") or {}).get("number"), 0)
                team_id = to_int(d.get("eventOwnerTeamId"))
                if period in (1, 2, 3) and team_id in team_period_goals:
                    team_period_goals[team_id][period] += 1
                all_goals_rows.append(
                    (
                        to_int(d.get("scoringPlayerId"), -9999),
                        to_int(d.get("scoringPlayerTotal"), 0),
                        to_int(d.get("assist1PlayerId"), -9999),
                        to_int(d.get("assist1PlayerTotal"), 0),
                        to_int(d.get("assist2PlayerId"), -9999),
                        to_int(d.get("assist2PlayerTotal"), 0),
                        False,  # empty_net unavailable in this endpoint
                        False,  # winner_goal unavailable in this endpoint
                        False,  # is_ppg unavailable in this endpoint
                        False,  # is_shg unavailable in this endpoint
                        team_id,
                        game_id,
                        period,
                        g.get("timeInPeriod"),
                        to_int(d.get("awayScore")),
                        to_int(d.get("homeScore")),
                    )
                )

            pim_by_team = {home_id: 0, away_id: 0}
            for p in penalties:
                d = p.get("details", {})
                team_id = to_int(d.get("eventOwnerTeamId"))
                pim_by_team[team_id] = pim_by_team.get(team_id, 0) + to_int(d.get("duration"), 2)

            hits_by_team = {home_id: 0, away_id: 0}
            for h in hits:
                team_id = to_int((h.get("details") or {}).get("eventOwnerTeamId"))
                hits_by_team[team_id] = hits_by_team.get(team_id, 0) + 1

            giveaways_by_team = {home_id: 0, away_id: 0}
            for ga in giveaways:
                team_id = to_int((ga.get("details") or {}).get("eventOwnerTeamId"))
                giveaways_by_team[team_id] = giveaways_by_team.get(team_id, 0) + 1

            takeaways_by_team = {home_id: 0, away_id: 0}
            for ta in takeaways:
                team_id = to_int((ta.get("details") or {}).get("eventOwnerTeamId"))
                takeaways_by_team[team_id] = takeaways_by_team.get(team_id, 0) + 1

            blocked_by_team = {home_id: 0, away_id: 0}
            for b in blocked:
                shooting_team = to_int((b.get("details") or {}).get("eventOwnerTeamId"))
                defending_team = home_id if shooting_team == away_id else away_id
                blocked_by_team[defending_team] = blocked_by_team.get(defending_team, 0) + 1

            faceoff_wins = {home_id: 0, away_id: 0}
            faceoff_taken = {home_id: 0, away_id: 0}
            for f in faceoffs:
                d = f.get("details", {})
                winner_team = to_int(d.get("eventOwnerTeamId"))
                if winner_team in faceoff_wins:
                    faceoff_wins[winner_team] += 1
                if winner_team == home_id:
                    loser_team = away_id
                else:
                    loser_team = home_id
                if winner_team in faceoff_taken:
                    faceoff_taken[winner_team] += 1
                if loser_team in faceoff_taken:
                    faceoff_taken[loser_team] += 1

            home_sog = to_int((box.get("homeTeam") or {}).get("sog"))
            away_sog = to_int((box.get("awayTeam") or {}).get("sog"))

            game_team_rows.append(
                (
                    home_score,
                    "home",
                    pim_by_team.get(home_id, 0),
                    home_sog,
                    0.0,  # power_play_percentage
                    0.0,  # power_play_goals
                    0.0,  # power_play_opportunities
                    round((faceoff_wins.get(home_id, 0) / faceoff_taken.get(home_id, 1)) * 100, 2)
                    if faceoff_taken.get(home_id, 0) > 0 else 0.0,
                    blocked_by_team.get(home_id, 0),
                    takeaways_by_team.get(home_id, 0),
                    giveaways_by_team.get(home_id, 0),
                    hits_by_team.get(home_id, 0),
                    game_id,
                    home_id,
                    team_period_goals.get(home_id, {}).get(1, 0),
                    team_period_goals.get(home_id, {}).get(2, 0),
                    team_period_goals.get(home_id, {}).get(3, 0),
                )
            )
            game_team_rows.append(
                (
                    away_score,
                    "away",
                    pim_by_team.get(away_id, 0),
                    away_sog,
                    0.0,
                    0.0,
                    0.0,
                    round((faceoff_wins.get(away_id, 0) / faceoff_taken.get(away_id, 1)) * 100, 2)
                    if faceoff_taken.get(away_id, 0) > 0 else 0.0,
                    blocked_by_team.get(away_id, 0),
                    takeaways_by_team.get(away_id, 0),
                    giveaways_by_team.get(away_id, 0),
                    hits_by_team.get(away_id, 0),
                    game_id,
                    away_id,
                    team_period_goals.get(away_id, {}).get(1, 0),
                    team_period_goals.get(away_id, {}).get(2, 0),
                    team_period_goals.get(away_id, {}).get(3, 0),
                )
            )

            for side_key, team_id in (("homeTeam", home_id), ("awayTeam", away_id)):
                side_stats = (box.get("playerByGameStats") or {}).get(side_key, {})
                skaters = side_stats.get("forwards", []) + side_stats.get("defense", [])
                for p in skaters:
                    game_player_rows.append(
                        (
                            team_id,
                            game_id,
                            to_int(p.get("playerId")),
                            p.get("toi") or "00:00",
                            to_int(p.get("assists")),
                            to_int(p.get("goals")),
                            to_int(p.get("sog")),
                            to_int(p.get("hits")),
                            to_int(p.get("powerPlayGoals")),
                            0,  # power_play_assists
                            to_int(p.get("pim")),
                            0,  # face_off_wins
                            0,  # face_off_taken
                            to_int(p.get("takeaways")),
                            to_int(p.get("giveaways")),
                            0,  # short_handed_goals
                            0,  # short_handed_assists
                            to_int(p.get("blockedShots")),
                            to_int(p.get("plusMinus")),
                            "00:00",  # even_time_on_ice
                            "00:00",  # power_play_time_on_ice
                            "00:00",  # short_handed_time_on_ice
                            to_float(p.get("faceoffWinningPctg")),
                        )
                    )

                for gk in side_stats.get("goalies", []):
                    pp_saves, pp_shots = split_sv(gk.get("powerPlayShotsAgainst"))
                    sh_saves, sh_shots = split_sv(gk.get("shorthandedShotsAgainst"))
                    ev_saves, ev_shots = split_sv(gk.get("evenStrengthShotsAgainst"))
                    shots_against = to_int(gk.get("shotsAgainst"))
                    saves = to_int(gk.get("saves"))
                    game_goalie_rows.append(
                        (
                            team_id,
                            game_id,
                            to_int(gk.get("playerId")),
                            gk.get("toi") or "00:00",
                            to_int(gk.get("assists")),
                            to_int(gk.get("goals")),
                            to_int(gk.get("pim")),
                            shots_against,
                            saves,
                            pp_saves,
                            sh_saves,
                            ev_saves,
                            sh_shots,
                            ev_shots,
                            pp_shots,
                            str(gk.get("decision")) == "W",
                            pct_from_ratio(gk.get("savePctg")),
                            round((pp_saves / pp_shots) * 100, 2) if pp_shots else 0.0,
                            round((sh_saves / sh_shots) * 100, 2) if sh_shots else 0.0,
                            round((ev_saves / ev_shots) * 100, 2) if ev_shots else 0.0,
                        )
                    )

            if idx % 50 == 0 or idx == total:
                print(f"Processed games: {idx}/{total}")

        return games_rows, all_goals_rows, game_team_rows, game_player_rows, game_goalie_rows

    def execute_insert(self, conn, table: str, columns: List[str], rows: List[tuple], page_size: int = 1000):
        if not rows:
            return
        insert_query = psql.SQL("INSERT INTO {table} ({cols}) VALUES %s").format(
            table=psql.Identifier(table),
            cols=psql.SQL(", ").join(psql.Identifier(c) for c in columns),
        )
        with conn.cursor() as cur:
            execute_values(cur, insert_query.as_string(conn), rows, page_size=page_size)

    def run(self):
        print("Loading team reference...")
        self.load_team_reference()

        print("Building teams and season stats...")
        teams_rows, teams_stats_rows = self.build_teams_and_stats()
        roster_rows = self.build_rosters(teams_rows)
        skater_rows = self.build_player_season_stats()
        goalie_rows = self.build_goalie_season_stats()

        print("Fetching finished games...")
        games_meta = self.fetch_final_games()
        print(f"Finished games to load: {len(games_meta)}")
        games_rows, all_goals_rows, game_team_rows, game_player_rows, game_goalie_rows = self.build_game_rows(games_meta)

        print("Writing to PostgreSQL...")
        conn = psycopg2.connect(
            host=self.pg_host,
            port=self.pg_port,
            user=self.pg_user,
            database=self.pg_database,
        )
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    TRUNCATE TABLE
                        all_goals,
                        game_player_stats,
                        game_team_stats,
                        game_goalie_stats,
                        games,
                        players_season_stats,
                        goalies_season_stats,
                        rosters,
                        teams_stats,
                        teams
                    """
                )

            self.execute_insert(
                conn,
                "teams",
                [
                    "team_id",
                    "name",
                    "division_name",
                    "arena",
                    "conference_name",
                    "abbreviation",
                    "first_year_of_play",
                    "city",
                    "active",
                    "short_name",
                ],
                teams_rows,
            )
            self.execute_insert(
                conn,
                "teams_stats",
                [
                    "team_id",
                    "games_played",
                    "wins",
                    "losses",
                    "ot",
                    "points",
                    "procent_points",
                    "goals_per_game",
                    "goals_against_per_game",
                    "power_play_percentage",
                    "power_play_goals",
                    "power_play_goals_against",
                    "power_play_opportunities",
                    "penalty_kill_percentage",
                    "shots_per_game",
                    "shots_allowed",
                    "face_off_win_percentage",
                ],
                teams_stats_rows,
            )
            self.execute_insert(
                conn,
                "rosters",
                [
                    "player_id",
                    "name",
                    "position",
                    "jersey_number",
                    "currentAge",
                    "lastName",
                    "nationality",
                    "captain",
                    "alternate_captain",
                    "rookie",
                    "abbreviation",
                    "current_team_id",
                ],
                roster_rows,
            )
            self.execute_insert(
                conn,
                "players_season_stats",
                [
                    "time_on_ice",
                    "assists",
                    "goals",
                    "pim",
                    "shots",
                    "games",
                    "hits",
                    "power_play_goals",
                    "power_play_points",
                    "power_play_time_on_ice",
                    "even_time_on_ice",
                    "penalty_minutes",
                    "face_off_pct",
                    "shot_pct",
                    "game_winning_goals",
                    "over_time_goals",
                    "short_handed_goals",
                    "short_handed_points",
                    "short_handed_time_on_ice",
                    "blocked",
                    "plus_minus",
                    "points",
                    "shifts",
                    "time_on_ice_per_game",
                    "even_time_on_ice_per_game",
                    "short_handed_time_on_ice_per_game",
                    "power_play_time_on_ice_per_game",
                    "player_id",
                ],
                skater_rows,
            )
            self.execute_insert(
                conn,
                "goalies_season_stats",
                [
                    "time_on_ice",
                    "ot",
                    "shutouts",
                    "ties",
                    "wins",
                    "losses",
                    "saves",
                    "power_play_saves",
                    "short_handed_saves",
                    "even_saves",
                    "short_handed_shots",
                    "even_shots",
                    "power_play_shots",
                    "save_percentage",
                    "goal_against_average",
                    "games",
                    "games_started",
                    "shots_against",
                    "goals_against",
                    "time_on_ice_per_game",
                    "power_play_save_percentage",
                    "short_handed_save_percentage",
                    "even_strength_save_percentage",
                    "player_id",
                ],
                goalie_rows,
            )
            self.execute_insert(
                conn,
                "games",
                [
                    "game_id",
                    "day",
                    "home_team_id",
                    "away_team_id",
                    "winner_id",
                    "is_overtime",
                    "is_shootouts",
                    "season",
                ],
                games_rows,
                page_size=2000,
            )
            self.execute_insert(
                conn,
                "all_goals",
                [
                    "goal_player_id",
                    "total_goals",
                    "assist_player1_id",
                    "assist_total_1",
                    "assist_player2_id",
                    "assist_total_2",
                    "empty_net",
                    "winner_goal",
                    "is_ppg",
                    "is_shg",
                    "team_id",
                    "game_id",
                    "period",
                    "time",
                    "goals_away",
                    "goals_home",
                ],
                all_goals_rows,
                page_size=5000,
            )
            self.execute_insert(
                conn,
                "game_team_stats",
                [
                    "goals",
                    "field",
                    "pim",
                    "shots",
                    "power_play_percentage",
                    "power_play_goals",
                    "power_play_opportunities",
                    "face_off_win_percentage",
                    "blocked",
                    "takeaways",
                    "giveaways",
                    "hits",
                    "game_id",
                    "team_id",
                    "fst_period_goals",
                    "snd_period_goals",
                    "trd_period_goals",
                ],
                game_team_rows,
                page_size=5000,
            )
            self.execute_insert(
                conn,
                "game_player_stats",
                [
                    "team_id",
                    "game_id",
                    "player_id",
                    "time_on_ice",
                    "assists",
                    "goals",
                    "shots",
                    "hits",
                    "power_play_goals",
                    "power_play_assists",
                    "penalty_minutes",
                    "face_off_wins",
                    "face_off_taken",
                    "takeaways",
                    "giveaways",
                    "short_handed_goals",
                    "short_handed_assists",
                    "blocked",
                    "plus_minus",
                    "even_time_on_ice",
                    "power_play_time_on_ice",
                    "short_handed_time_on_ice",
                    "face_off_pct",
                ],
                game_player_rows,
                page_size=5000,
            )
            self.execute_insert(
                conn,
                "game_goalie_stats",
                [
                    "team_id",
                    "game_id",
                    "player_id",
                    "timeOnIce",
                    "assists",
                    "goals",
                    "pim",
                    "shots",
                    "saves",
                    "power_play_saves",
                    "short_handed_saves",
                    "even_saves",
                    "short_handed_shots_against",
                    "even_shots_against",
                    "power_play_shots_against",
                    "decision",
                    "save_percentage",
                    "power_play_save_percentage",
                    "short_handed_save_percentage",
                    "even_strength_save_percentage",
                ],
                game_goalie_rows,
                page_size=5000,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        print("Done.")
        print(
            f"Loaded: teams={len(teams_rows)}, rosters={len(roster_rows)}, "
            f"skaters={len(skater_rows)}, goalies={len(goalie_rows)}, games={len(games_rows)}"
        )


if __name__ == "__main__":
    loader = ModernNhlLoader()
    loader.run()
