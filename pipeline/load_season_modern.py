import argparse
import logging
import time
from datetime import date, datetime
from pathlib import Path
import sys
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
import requests
from psycopg2 import sql as psql
from psycopg2.extras import execute_values

# Reuse a single shared project config module from telegram_bot/.
TELEGRAM_BOT_DIR = Path(__file__).resolve().parents[1] / "telegram_bot"
if str(TELEGRAM_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(TELEGRAM_BOT_DIR))
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coercion helpers.
#
# One family for each null semantics:
#   * ``to_int`` — eager default (0 on missing/invalid). Use ONLY for required
#     (PK / NOT NULL) columns or where business logic mandates a concrete int.
#   * ``optional_int`` / ``optional_float`` / ``optional_str`` /
#     ``optional_pct_from_ratio`` / ``optional_split_sv`` /
#     ``optional_seconds_to_mmss`` / ``optional_mmss`` /
#     ``optional_age_from_birthdate`` — return ``None`` when the source is
#     missing / empty / unparseable, so PostgreSQL stores ``NULL`` instead of
#     a synthetic 0 / "" / "00:00".
#   * ``safe_pct(numerator, denominator)`` — derived percentage with ``NULL``
#     when the denominator is missing / zero (no attempts).
#
# See ``docs/pipeline_nulls_and_explicit_null_tz.md`` for the contract.
# ---------------------------------------------------------------------------


def to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def optional_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def optional_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def optional_str(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def optional_pct_from_ratio(value) -> Optional[float]:
    val = optional_float(value)
    if val is None:
        return None
    if val <= 1.0:
        return round(val * 100, 2)
    return round(val, 2)


def optional_split_sv(value) -> Tuple[Optional[int], Optional[int]]:
    if not value or "/" not in str(value):
        return None, None
    left, right = str(value).split("/", 1)
    return optional_int(left), optional_int(right)


def optional_seconds_to_mmss(value) -> Optional[str]:
    """``mm:ss`` from API seconds, ``None`` if unknown.

    The NHL stats API encodes "did not play / not applicable" as either a missing
    field or a literal ``0`` (e.g. shTimeOnIce for a player with no PK shifts).
    Both cases collapse to ``None`` here so the DB sees ``NULL`` instead of
    ``00:00``, which used to be indistinguishable from "played for 0:00".
    """
    if value is None or value == "":
        return None
    try:
        s = float(value)
    except (ValueError, TypeError):
        return None
    if s <= 0:
        return None
    total_seconds = int(round(s))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def optional_mmss(value) -> Optional[str]:
    """Pass-through ``mm:ss`` string with NULL semantics for missing input."""
    s = optional_str(value)
    if s is None:
        return None
    if s == "00:00":
        return None
    return s


def optional_age_from_birthdate(birth_date) -> Optional[int]:
    if not birth_date:
        return None
    try:
        born = datetime.strptime(str(birth_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def safe_pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Return rounded percentage or ``None`` when inputs are missing/zero.

    Used for derived save% / PP% / face-off% values: when there were no
    attempts, the percentage is undefined and we want ``NULL`` rather than 0.0.
    """
    if numerator is None or denominator is None:
        return None
    try:
        denom = float(denominator)
    except (ValueError, TypeError):
        return None
    if denom <= 0:
        return None
    try:
        num = float(numerator)
    except (ValueError, TypeError):
        return None
    return round((num / denom) * 100, 2)


class ModernNhlLoader:
    def __init__(self):
        self.pg_host = config.PG_HOST
        self.pg_port = config.PG_PORT
        self.pg_user = config.PG_USER
        self.pg_database = config.PG_DATABASE

        self.season_id = config.SEASON_ID
        self.current_season_label = config.CURRENT_SEASON
        self.start_date = config.DATE_FROM
        self.end_date = config.DATE_TO

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
            tri_lookup = meta.get("triCode") or meta.get("rawTricode") or ""
            standing = self.team_standings_by_abbrev.get(tri_lookup, {})

            full_name = optional_str(
                meta.get("fullName") or row.get("teamFullName") or tri_lookup
            )
            common_name = optional_str((standing.get("teamCommonName") or {}).get("default"))
            short_name = common_name or full_name

            division = optional_str(standing.get("divisionName"))
            conference = optional_str(standing.get("conferenceName"))
            city = full_name.split(" ")[0] if full_name else None

            teams_rows.append(
                (
                    team_id,
                    self.season_id,
                    full_name,
                    division,
                    None,  # arena — not provided by the endpoints used here
                    conference,
                    optional_str(tri_lookup),
                    None,  # first_year_of_play — not provided by these endpoints
                    city,
                    True,
                    short_name,
                )
            )

            teams_stats_rows.append(
                (
                    team_id,
                    self.season_id,
                    optional_int(row.get("gamesPlayed")),
                    optional_int(row.get("wins")),
                    optional_int(row.get("losses")),
                    optional_int(row.get("otLosses")),
                    optional_int(row.get("points")),
                    optional_pct_from_ratio(row.get("pointPct")),
                    optional_float(row.get("goalsForPerGame")),
                    optional_float(row.get("goalsAgainstPerGame")),
                    optional_pct_from_ratio(row.get("powerPlayPct")),
                    optional_int(row.get("powerPlayGoals")),
                    optional_int(row.get("powerPlayGoalsAgainst")),
                    optional_int(row.get("powerPlayOpportunities")),
                    optional_pct_from_ratio(row.get("penaltyKillPct")),
                    optional_float(row.get("shotsForPerGame")),
                    optional_float(row.get("shotsAgainstPerGame")),
                    optional_pct_from_ratio(row.get("faceoffWinPct")),
                )
            )
        return teams_rows, teams_stats_rows

    def build_rosters(self, team_rows: List[tuple]) -> List[tuple]:
        rows_by_player: Dict[int, tuple] = {}
        season_str = str(self.season_id)

        for team in team_rows:
            team_id = to_int(team[0])
            tri = team[6]
            if not tri:
                continue
            url = f"https://api-web.nhle.com/v1/roster/{tri}/{season_str}"
            payload = self.get_json(url)
            for group in ("forwards", "defensemen", "goalies"):
                for p in payload.get(group, []):
                    player_id = to_int(p.get("id"))
                    first_name = optional_str((p.get("firstName") or {}).get("default")) or ""
                    last_name = optional_str((p.get("lastName") or {}).get("default")) or ""
                    full_name = f"{first_name} {last_name}".strip() or None
                    position = optional_str(p.get("positionCode"))
                    # captain / alternate_captain / rookie are not exposed by
                    # /v1/roster — leave as NULL ("unknown") rather than False.
                    row = (
                        player_id,
                        self.season_id,
                        full_name,
                        position,
                        optional_int(p.get("sweaterNumber")),
                        optional_age_from_birthdate(p.get("birthDate")),
                        optional_str(last_name) or full_name,
                        optional_str(p.get("birthCountry")),
                        None,  # captain
                        None,  # alternate_captain
                        None,  # rookie
                        position,  # abbreviation
                        team_id,
                    )
                    rows_by_player[player_id] = row
        return list(rows_by_player.values())

    def _abbrev_to_team_id(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for tid, meta in self.team_meta_by_id.items():
            tri = meta.get("triCode") or meta.get("rawTricode")
            if tri and tid:
                out[str(tri).upper()] = tid
        return out

    def _team_id_from_stats_team_abbrevs(
        self, team_abbrevs, abbrev_map: Dict[str, int]
    ) -> Optional[int]:
        """Map Stats API ``teamAbbrevs`` (e.g. ``SEA`` or ``TOR, BOS``) to ``team_id``."""
        if not team_abbrevs:
            return None
        parts = [p.strip().upper() for p in str(team_abbrevs).split(",") if p.strip()]
        if not parts:
            return None
        abbrev = parts[-1]
        return abbrev_map.get(abbrev)

    def _roster_tuple_from_landing(self, payload: dict) -> Optional[tuple]:
        player_id = to_int(payload.get("playerId"))
        if not player_id:
            return None
        first_name = optional_str((payload.get("firstName") or {}).get("default")) or ""
        last_name = optional_str((payload.get("lastName") or {}).get("default")) or ""
        full_name = f"{first_name} {last_name}".strip() or None
        last_only = optional_str(last_name) or full_name
        tid = to_int(payload.get("currentTeamId"), 0)
        team_id = tid if tid > 0 else None
        pos = optional_str(payload.get("position"))
        return (
            player_id,
            self.season_id,
            full_name,
            pos,
            optional_int(payload.get("sweaterNumber")),
            optional_age_from_birthdate(payload.get("birthDate")),
            last_only,
            optional_str(payload.get("birthCountry")),
            None,  # captain — not exposed by landing
            None,  # alternate_captain — not exposed by landing
            None,  # rookie — not exposed by landing
            pos,
            team_id,
        )

    def supplement_rosters_from_reports(
        self,
        roster_rows: List[tuple],
        skater_summary_rows: List[dict],
        goalie_summary_rows: List[dict],
        extra_player_ids: Optional[Set[int]] = None,
    ) -> List[tuple]:
        """Add roster rows for anyone in season stats but missing from team roster endpoints.

        Official ``/v1/roster/{tri}/{season}`` lists the current active roster only; skater and
        goalie summary reports list everyone with games in the season (call-ups, short stints).
        Remaining gaps (rare) are filled via the player landing endpoint.
        """
        abbrev_map = self._abbrev_to_team_id()
        rows_by_player: Dict[int, tuple] = {}
        for row in roster_rows:
            pid = to_int(row[0])
            if pid:
                rows_by_player[pid] = row

        def add_skater(r: dict) -> None:
            pid = to_int(r.get("playerId"))
            if not pid or pid in rows_by_player:
                return
            team_id = self._team_id_from_stats_team_abbrevs(r.get("teamAbbrevs"), abbrev_map)
            full_name = optional_str(r.get("skaterFullName") or r.get("lastName"))
            last_name = optional_str(r.get("lastName"))
            pos = optional_str(r.get("positionCode"))
            display_name = full_name or last_name
            # Stats summary lacks jersey/age/captain flags → store NULL
            # ("unknown") rather than fabricating zeros / False.
            rows_by_player[pid] = (
                pid,
                self.season_id,
                display_name,
                pos,
                None,  # jersey_number
                None,  # currentage
                last_name or display_name,
                None,
                None,  # captain
                None,  # alternate_captain
                None,  # rookie
                pos,
                team_id,
            )

        def add_goalie(r: dict) -> None:
            pid = to_int(r.get("playerId"))
            if not pid or pid in rows_by_player:
                return
            team_id = self._team_id_from_stats_team_abbrevs(r.get("teamAbbrevs"), abbrev_map)
            full_name = optional_str(r.get("goalieFullName") or r.get("lastName"))
            last_name = optional_str(r.get("lastName"))
            display_name = full_name or last_name
            rows_by_player[pid] = (
                pid,
                self.season_id,
                display_name,
                "G",
                None,  # jersey_number
                None,  # currentage
                last_name or display_name,
                None,
                None,  # captain
                None,  # alternate_captain
                None,  # rookie
                "G",
                team_id,
            )

        for r in skater_summary_rows:
            add_skater(r)
        for r in goalie_summary_rows:
            add_goalie(r)

        if extra_player_ids:
            missing = sorted(p for p in extra_player_ids if p > 0 and p not in rows_by_player)
            if missing:
                logger.info(
                    "Supplementing %d roster row(s) from player landing (not on team roster or summary)",
                    len(missing),
                )
            for pid in missing:
                url = f"https://api-web.nhle.com/v1/player/{pid}/landing"
                try:
                    payload = self.get_json(url)
                except Exception as exc:
                    logger.warning("Roster supplement: landing failed for player_id=%s: %s", pid, exc)
                    continue
                landing_row = self._roster_tuple_from_landing(payload)
                if landing_row:
                    rows_by_player[pid] = landing_row

        return list(rows_by_player.values())

    def build_player_advanced_stats(self) -> List[tuple]:
        gfa_url = (
            "https://api.nhle.com/stats/rest/en/skater/goalsForAgainst"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater goalsForAgainst report...")
        gfa_rows = self.fetch_paginated(gfa_url, page_size=1000)
        # Duplicate playerId in one page: last row wins (same for other dict-by-player merges).
        gfa_by_player = {to_int(r.get("playerId")): r for r in gfa_rows}

        pp_url = (
            "https://api.nhle.com/stats/rest/en/skater/puckPossessions"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater puckPossessions report...")
        pp_rows = self.fetch_paginated(pp_url, page_size=1000)
        pp_by_player = {to_int(r.get("playerId")): r for r in pp_rows}

        all_pids = {p for p in (set(gfa_by_player) | set(pp_by_player)) if p > 0}
        out: List[tuple] = []
        for pid in sorted(all_pids):
            gfa = gfa_by_player.get(pid, {})
            pp = pp_by_player.get(pid, {})
            out.append(
                (
                    pid,
                    self.season_id,
                    optional_pct_from_ratio(pp.get("satPct")),
                    optional_pct_from_ratio(pp.get("usatPct")),
                    optional_pct_from_ratio(pp.get("goalsPct")),
                    optional_pct_from_ratio(pp.get("offensiveZoneStartPct")),
                    optional_pct_from_ratio(pp.get("defensiveZoneStartPct")),
                    optional_pct_from_ratio(pp.get("neutralZoneStartPct")),
                    optional_pct_from_ratio(pp.get("onIceShootingPct")),
                    optional_int(gfa.get("evenStrengthGoalsFor")),
                    optional_int(gfa.get("evenStrengthGoalsAgainst")),
                    optional_pct_from_ratio(gfa.get("evenStrengthGoalsForPct")),
                    # NHL API field name (typo: Goal not Goals).
                    optional_int(gfa.get("powerPlayGoalFor")),
                    optional_int(gfa.get("powerPlayGoalsAgainst")),
                    optional_int(gfa.get("shortHandedGoalsFor")),
                    optional_int(gfa.get("shortHandedGoalsAgainst")),
                )
            )
        return out

    def build_player_shot_types(self) -> List[tuple]:
        url = (
            "https://api.nhle.com/stats/rest/en/skater/shottype"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater shottype report...")
        rows = self.fetch_paginated(url, page_size=1000)
        out: List[tuple] = []
        for r in rows:
            pid = to_int(r.get("playerId"))
            if not pid:
                continue
            out.append(
                (
                    pid,
                    self.season_id,
                    optional_int(r.get("goalsWrist")),
                    optional_int(r.get("shotsOnNetWrist")),
                    optional_int(r.get("goalsSlap")),
                    optional_int(r.get("shotsOnNetSlap")),
                    optional_int(r.get("goalsSnap")),
                    optional_int(r.get("shotsOnNetSnap")),
                    optional_int(r.get("goalsBackhand")),
                    optional_int(r.get("shotsOnNetBackhand")),
                    optional_int(r.get("goalsTipIn")),
                    optional_int(r.get("shotsOnNetTipIn")),
                    optional_int(r.get("goalsDeflected")),
                    optional_int(r.get("shotsOnNetDeflected")),
                    optional_int(r.get("goalsWrapAround")),
                    optional_int(r.get("shotsOnNetWrapAround")),
                )
            )
        return out

    def build_player_season_stats(self) -> Tuple[List[tuple], List[dict]]:
        url = (
            "https://api.nhle.com/stats/rest/en/skater/summary"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        rows = self.fetch_paginated(url, page_size=1000)

        toi_url = (
            "https://api.nhle.com/stats/rest/en/skater/timeonice"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater time-on-ice report...")
        toi_rows = self.fetch_paginated(toi_url, page_size=1000)
        toi_by_player = {to_int(t.get("playerId")): t for t in toi_rows}
        if toi_rows:
            logger.debug("timeonice sample keys: %s", list(toi_rows[0].keys()))

        fop_url = (
            "https://api.nhle.com/stats/rest/en/skater/faceoffpercentages"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater faceoffpercentages report...")
        fop_rows = self.fetch_paginated(fop_url, page_size=1000)
        fop_by_player = {to_int(t.get("playerId")): t for t in fop_rows}

        so_url = (
            "https://api.nhle.com/stats/rest/en/skater/shootout"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater shootout report...")
        so_rows = self.fetch_paginated(so_url, page_size=1000)
        so_by_player = {to_int(t.get("playerId")): t for t in so_rows}

        # summary no longer includes hits / blockedShots (as of 2025–26 API); realtime does
        rt_url = (
            "https://api.nhle.com/stats/rest/en/skater/realtime"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching skater realtime report (hits, blocked shots)...")
        rt_rows = self.fetch_paginated(rt_url, page_size=1000)
        rt_by_player = {to_int(t.get("playerId")): t for t in rt_rows}
        if rt_rows:
            logger.debug("realtime sample keys: %s", list(rt_rows[0].keys()))

        out = []
        for r in rows:
            player_id = to_int(r.get("playerId"))
            toi = toi_by_player.get(player_id, {})
            fop = fop_by_player.get(player_id, {})
            so = so_by_player.get(player_id, {})
            rt = rt_by_player.get(player_id, {})
            # rt.get("hits") may be a real 0 (no hits this season). Only fall back
            # to the summary value when the realtime row is missing the key.
            hits_value = rt["hits"] if "hits" in rt else r.get("hits")
            blocked_value = (
                rt["blockedShots"] if "blockedShots" in rt else r.get("blockedShots")
            )
            out.append(
                (
                    optional_seconds_to_mmss(toi.get("timeOnIce")),
                    optional_int(r.get("assists")),
                    optional_int(r.get("goals")),
                    optional_int(r.get("penaltyMinutes")),
                    optional_int(r.get("shots")),
                    optional_int(r.get("gamesPlayed")),
                    optional_int(hits_value),
                    optional_int(r.get("ppGoals")),
                    optional_int(r.get("ppPoints")),
                    optional_seconds_to_mmss(toi.get("ppTimeOnIce")),
                    optional_seconds_to_mmss(toi.get("evTimeOnIce")),
                    optional_int(r.get("penaltyMinutes")),
                    optional_pct_from_ratio(r.get("faceoffWinPct")),
                    optional_pct_from_ratio(r.get("shootingPct")),
                    optional_int(r.get("gameWinningGoals")),
                    optional_int(r.get("otGoals")),
                    optional_int(r.get("shGoals")),
                    optional_int(r.get("shPoints")),
                    optional_seconds_to_mmss(toi.get("shTimeOnIce")),
                    optional_int(blocked_value),
                    optional_int(r.get("plusMinus")),
                    optional_int(r.get("points")),
                    optional_int(r.get("shifts")),
                    optional_seconds_to_mmss(toi.get("timeOnIcePerGame")),
                    optional_seconds_to_mmss(toi.get("evTimeOnIcePerGame")),
                    optional_seconds_to_mmss(toi.get("shTimeOnIcePerGame")),
                    optional_seconds_to_mmss(toi.get("ppTimeOnIcePerGame")),
                    optional_pct_from_ratio(fop.get("offensiveZoneFaceoffPct")),
                    optional_pct_from_ratio(fop.get("defensiveZoneFaceoffPct")),
                    optional_pct_from_ratio(fop.get("neutralZoneFaceoffPct")),
                    optional_int(so.get("shootoutGoals")),
                    optional_int(so.get("shootoutShots")),
                    optional_int(so.get("shootoutGameDecidingGoals")),
                    optional_pct_from_ratio(so.get("shootoutShootingPct")),
                    player_id,
                    self.season_id,
                )
            )
        return out, rows

    def build_goalie_season_stats(self) -> Tuple[List[tuple], List[dict]]:
        url = (
            "https://api.nhle.com/stats/rest/en/goalie/summary"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        rows = self.fetch_paginated(url, page_size=1000)

        sbs_url = (
            "https://api.nhle.com/stats/rest/en/goalie/savesByStrength"
            f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
        )
        logger.info("Fetching goalie saves-by-strength report...")
        sbs_rows = self.fetch_paginated(sbs_url, page_size=1000)
        sbs_by_player = {to_int(s.get("playerId")): s for s in sbs_rows}
        if sbs_rows:
            logger.debug("savesByStrength sample keys: %s", list(sbs_rows[0].keys()))

        out = []
        for r in rows:
            player_id = to_int(r.get("playerId"))
            sbs = sbs_by_player.get(player_id, {})
            pp_saves = optional_int(sbs.get("ppSaves"))
            sh_saves = optional_int(sbs.get("shSaves"))
            ev_saves = optional_int(sbs.get("evSaves"))
            pp_shots = optional_int(sbs.get("ppShotsAgainst"))
            sh_shots = optional_int(sbs.get("shShotsAgainst"))
            ev_shots = optional_int(sbs.get("evShotsAgainst"))
            out.append(
                (
                    None,  # time_on_ice — not available in summary/savesByStrength
                    optional_int(r.get("otLosses")),
                    optional_int(r.get("shutouts")),
                    # The NHL has not had regular-season ties since 2005; the API
                    # does not expose a "ties" stat anymore. We persist 0 as a
                    # known-true business value (not "data missing").
                    0,
                    optional_int(r.get("wins")),
                    optional_int(r.get("losses")),
                    optional_int(r.get("saves")),
                    pp_saves,
                    sh_saves,
                    ev_saves,
                    sh_shots,
                    ev_shots,
                    pp_shots,
                    optional_pct_from_ratio(r.get("savePct")),
                    optional_float(r.get("goalsAgainstAverage")),
                    optional_int(r.get("gamesPlayed")),
                    optional_int(r.get("gamesStarted")),
                    optional_int(r.get("shotsAgainst")),
                    optional_int(r.get("goalsAgainst")),
                    None,  # time_on_ice_per_game — not in summary/savesByStrength
                    safe_pct(pp_saves, pp_shots),
                    safe_pct(sh_saves, sh_shots),
                    safe_pct(ev_saves, ev_shots),
                    player_id,
                    self.season_id,
                )
            )
        return out, rows

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
                    self.season_id,
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

            game_goals = []
            for g in goals:
                d = g.get("details", {})
                period = optional_int((g.get("periodDescriptor") or {}).get("number"))
                team_id = to_int(d.get("eventOwnerTeamId"))
                if period in (1, 2, 3) and team_id in team_period_goals:
                    team_period_goals[team_id][period] += 1

                situation = g.get("situationCode") or ""
                modifier = (d.get("goalModifier") or "").lower()

                empty_net = "empty-net" in modifier
                if not empty_net and len(situation) == 4:
                    if team_id == home_id:
                        empty_net = situation[0] == '0'
                    else:
                        empty_net = situation[3] == '0'

                is_ppg = False
                is_shg = False
                if len(situation) == 4:
                    try:
                        away_goalie = int(situation[0])
                        away_skaters = int(situation[1])
                        home_skaters = int(situation[2])
                        home_goalie = int(situation[3])
                        eff_away = away_skaters - (1 - away_goalie)
                        eff_home = home_skaters - (1 - home_goalie)
                        if team_id == home_id:
                            is_ppg = eff_home > eff_away
                            is_shg = eff_home < eff_away
                        else:
                            is_ppg = eff_away > eff_home
                            is_shg = eff_away < eff_home
                    except (ValueError, IndexError):
                        pass

                # Player IDs / totals: when a goal play omits the field (rare,
                # e.g. an unattributed empty-netter or unsigned shootout flush),
                # store NULL instead of the legacy -9999 sentinel; aggregations
                # below filter on ``is not None``.
                game_goals.append(
                    [
                        optional_int(d.get("scoringPlayerId")),
                        optional_int(d.get("scoringPlayerTotal")),
                        optional_int(d.get("assist1PlayerId")),
                        optional_int(d.get("assist1PlayerTotal")),
                        optional_int(d.get("assist2PlayerId")),
                        optional_int(d.get("assist2PlayerTotal")),
                        empty_net,
                        False,
                        is_ppg,
                        is_shg,
                        team_id,
                        game_id,
                        period,
                        optional_str(g.get("timeInPeriod")),
                        optional_int(d.get("awayScore")),
                        optional_int(d.get("homeScore")),
                        optional_int(g.get("eventId")),
                    ]
                )

            losing_score = min(home_score, away_score)
            winner_goal_count = 0
            for row in game_goals:
                if row[10] == winner:
                    winner_goal_count += 1
                    if winner_goal_count == losing_score + 1:
                        row[7] = True
                        break

            all_goals_rows.extend(tuple(r) for r in game_goals)

            pim_by_team = {home_id: 0, away_id: 0}
            for p in penalties:
                d = p.get("details", {})
                team_id = to_int(d.get("eventOwnerTeamId"))
                # Skip penalties whose duration the API omits — better to
                # under-count than to invent a 2-minute default. Per-game team
                # PIM stays an aggregate count (real 0 means "no penalties").
                duration = optional_int(d.get("duration"))
                if duration is None:
                    continue
                pim_by_team[team_id] = pim_by_team.get(team_id, 0) + duration

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

            home_sog = optional_int((box.get("homeTeam") or {}).get("sog"))
            away_sog = optional_int((box.get("awayTeam") or {}).get("sog"))

            pp_opps = {home_id: 0, away_id: 0}
            for p in penalties:
                penalized = to_int((p.get("details") or {}).get("eventOwnerTeamId"))
                if penalized == home_id:
                    pp_opps[away_id] += 1
                elif penalized == away_id:
                    pp_opps[home_id] += 1

            pp_goals_team = {home_id: 0, away_id: 0}
            for row in game_goals:
                if row[8]:
                    pp_goals_team[row[10]] = pp_goals_team.get(row[10], 0) + 1

            game_team_rows.append(
                (
                    home_score,
                    "home",
                    pim_by_team.get(home_id, 0),
                    home_sog,
                    safe_pct(pp_goals_team[home_id], pp_opps[home_id]),
                    float(pp_goals_team[home_id]),
                    float(pp_opps[home_id]),
                    safe_pct(faceoff_wins.get(home_id, 0), faceoff_taken.get(home_id, 0)),
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
                    safe_pct(pp_goals_team[away_id], pp_opps[away_id]),
                    float(pp_goals_team[away_id]),
                    float(pp_opps[away_id]),
                    safe_pct(faceoff_wins.get(away_id, 0), faceoff_taken.get(away_id, 0)),
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

            player_fo_wins: Dict[int, int] = {}
            player_fo_taken: Dict[int, int] = {}
            for f in faceoffs:
                d = f.get("details", {})
                # ``winningPlayerId``/``losingPlayerId`` may be missing for
                # malformed faceoff plays; skip rather than counting a fake "0"
                # player.
                winner_pid = optional_int(d.get("winningPlayerId"))
                loser_pid = optional_int(d.get("losingPlayerId"))
                if winner_pid is not None:
                    player_fo_wins[winner_pid] = player_fo_wins.get(winner_pid, 0) + 1
                    player_fo_taken[winner_pid] = player_fo_taken.get(winner_pid, 0) + 1
                if loser_pid is not None:
                    player_fo_taken[loser_pid] = player_fo_taken.get(loser_pid, 0) + 1

            player_pp_assists: Dict[int, int] = {}
            player_sh_goals: Dict[int, int] = {}
            player_sh_assists: Dict[int, int] = {}
            for row in game_goals:
                scorer_id = row[0]
                assist1_id = row[2]
                assist2_id = row[4]
                if row[8]:  # is_ppg
                    if assist1_id is not None:
                        player_pp_assists[assist1_id] = player_pp_assists.get(assist1_id, 0) + 1
                    if assist2_id is not None:
                        player_pp_assists[assist2_id] = player_pp_assists.get(assist2_id, 0) + 1
                if row[9]:  # is_shg
                    if scorer_id is not None:
                        player_sh_goals[scorer_id] = player_sh_goals.get(scorer_id, 0) + 1
                    if assist1_id is not None:
                        player_sh_assists[assist1_id] = player_sh_assists.get(assist1_id, 0) + 1
                    if assist2_id is not None:
                        player_sh_assists[assist2_id] = player_sh_assists.get(assist2_id, 0) + 1

            for side_key, team_id in (("homeTeam", home_id), ("awayTeam", away_id)):
                side_stats = (box.get("playerByGameStats") or {}).get(side_key, {})
                skaters = side_stats.get("forwards", []) + side_stats.get("defense", [])
                for p in skaters:
                    pid = to_int(p.get("playerId"))
                    # PP assists / SH goals / SH assists / face-off counts are
                    # derived from PBP plays; a real 0 here means "the skater
                    # had none in this game", not "data missing", so we keep 0.
                    game_player_rows.append(
                        (
                            team_id,
                            game_id,
                            pid,
                            optional_mmss(p.get("toi")),
                            optional_int(p.get("assists")),
                            optional_int(p.get("goals")),
                            optional_int(p.get("sog")),
                            optional_int(p.get("hits")),
                            optional_int(p.get("powerPlayGoals")),
                            player_pp_assists.get(pid, 0),
                            optional_int(p.get("pim")),
                            player_fo_wins.get(pid, 0),
                            player_fo_taken.get(pid, 0),
                            optional_int(p.get("takeaways")),
                            optional_int(p.get("giveaways")),
                            player_sh_goals.get(pid, 0),
                            player_sh_assists.get(pid, 0),
                            optional_int(p.get("blockedShots")),
                            optional_int(p.get("plusMinus")),
                            optional_float(p.get("faceoffWinningPctg")),
                        )
                    )

                for gk in side_stats.get("goalies", []):
                    pp_saves, pp_shots = optional_split_sv(gk.get("powerPlayShotsAgainst"))
                    sh_saves, sh_shots = optional_split_sv(gk.get("shorthandedShotsAgainst"))
                    ev_saves, ev_shots = optional_split_sv(gk.get("evenStrengthShotsAgainst"))
                    shots_against = optional_int(gk.get("shotsAgainst"))
                    saves = optional_int(gk.get("saves"))
                    decision_raw = gk.get("decision")
                    decision = None if decision_raw is None else (str(decision_raw) == "W")
                    game_goalie_rows.append(
                        (
                            team_id,
                            game_id,
                            to_int(gk.get("playerId")),
                            optional_mmss(gk.get("toi")),
                            optional_int(gk.get("assists")),
                            optional_int(gk.get("goals")),
                            optional_int(gk.get("pim")),
                            shots_against,
                            saves,
                            pp_saves,
                            sh_saves,
                            ev_saves,
                            sh_shots,
                            ev_shots,
                            pp_shots,
                            decision,
                            optional_pct_from_ratio(gk.get("savePctg")),
                            safe_pct(pp_saves, pp_shots),
                            safe_pct(sh_saves, sh_shots),
                            safe_pct(ev_saves, ev_shots),
                        )
                    )

            if idx % 50 == 0 or idx == total:
                logger.info("Processed games: %d/%d", idx, total)

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

    def execute_upsert(
        self,
        conn,
        table: str,
        columns: List[str],
        conflict_columns: List[str],
        rows: List[tuple],
        page_size: int = 1000,
    ):
        if not rows:
            return
        # Paginated NHL stats APIs may repeat the same entity across pages; one INSERT
        # must not propose duplicate ON CONFLICT keys (Postgres CardinalityViolation).
        key_idxs = [columns.index(c) for c in conflict_columns]
        dedup: Dict[tuple, tuple] = {}
        for row in rows:
            dedup[tuple(row[i] for i in key_idxs)] = row
        rows = list(dedup.values())
        update_cols = [c for c in columns if c not in conflict_columns]
        insert_query = psql.SQL("INSERT INTO {table} ({cols}) VALUES %s").format(
            table=psql.Identifier(table),
            cols=psql.SQL(", ").join(psql.Identifier(c) for c in columns),
        )
        conflict_sql = psql.SQL(", ").join(psql.Identifier(c) for c in conflict_columns)
        if update_cols:
            set_clause = psql.SQL(", ").join(
                psql.SQL("{} = EXCLUDED.{}").format(psql.Identifier(c), psql.Identifier(c))
                for c in update_cols
            )
            suffix = psql.SQL(" ON CONFLICT ({conf}) DO UPDATE SET {sets}").format(
                conf=conflict_sql,
                sets=set_clause,
            )
        else:
            suffix = psql.SQL(" ON CONFLICT ({conf}) DO NOTHING").format(conf=conflict_sql)
        full = insert_query + suffix
        with conn.cursor() as cur:
            execute_values(cur, full.as_string(conn), rows, page_size=page_size)

    def run(self):
        logger.info("Loading team reference...")
        self.load_team_reference()

        logger.info("Building teams and season stats...")
        teams_rows, teams_stats_rows = self.build_teams_and_stats()
        roster_rows = self.build_rosters(teams_rows)
        skater_rows, skater_summary_rows = self.build_player_season_stats()
        goalie_rows, goalie_summary_rows = self.build_goalie_season_stats()
        advanced_rows = self.build_player_advanced_stats()
        shot_type_rows = self.build_player_shot_types()
        roster_rows = self.supplement_rosters_from_reports(
            roster_rows,
            skater_summary_rows,
            goalie_summary_rows,
            {to_int(r[0]) for r in advanced_rows},
        )

        logger.info(
            "Date window %s .. %s (season_id=%s, games.season=%s)",
            self.start_date,
            self.end_date,
            self.season_id,
            self.current_season_label,
        )
        logger.info("Fetching finished games...")
        games_meta = self.fetch_final_games()
        logger.info("Finished games to load: %d", len(games_meta))
        games_rows, all_goals_rows, game_team_rows, game_player_rows, game_goalie_rows = self.build_game_rows(games_meta)

        logger.info("Writing to PostgreSQL...")
        conn = psycopg2.connect(
            host=self.pg_host,
            port=self.pg_port,
            user=self.pg_user,
            database=self.pg_database,
        )
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                game_ids = tuple(r[0] for r in games_rows)
                if game_ids:
                    for tbl in (
                        "all_goals",
                        "game_player_stats",
                        "game_team_stats",
                        "game_goalie_stats",
                        "games",
                    ):
                        cur.execute(
                            psql.SQL("DELETE FROM {} WHERE game_id = ANY(%s)").format(
                                psql.Identifier(tbl)
                            ),
                            (list(game_ids),),
                        )
                    logger.info("Removed prior rows for %d game(s) in window", len(game_ids))
                else:
                    logger.info("No finished games in date window; skipping game table deletes")

            self.execute_upsert(
                conn,
                "teams",
                [
                    "team_id",
                    "season_id",
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
                ["team_id", "season_id"],
                teams_rows,
            )
            self.execute_upsert(
                conn,
                "teams_stats",
                [
                    "team_id",
                    "season_id",
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
                ["team_id", "season_id"],
                teams_stats_rows,
            )
            self.execute_upsert(
                conn,
                "rosters",
                [
                    "player_id",
                    "season_id",
                    "name",
                    "position",
                    "jersey_number",
                    "currentage",
                    "lastname",
                    "nationality",
                    "captain",
                    "alternate_captain",
                    "rookie",
                    "abbreviation",
                    "current_team_id",
                ],
                ["player_id", "season_id"],
                roster_rows,
            )
            self.execute_upsert(
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
                    "oz_faceoff_pct",
                    "dz_faceoff_pct",
                    "nz_faceoff_pct",
                    "shootout_goals",
                    "shootout_shots",
                    "shootout_gd_goals",
                    "shootout_pct",
                    "player_id",
                    "season_id",
                ],
                ["player_id", "season_id"],
                skater_rows,
            )
            self.execute_upsert(
                conn,
                "players_advanced_stats",
                [
                    "player_id",
                    "season_id",
                    "sat_pct",
                    "usat_pct",
                    "goals_pct",
                    "oz_start_pct",
                    "dz_start_pct",
                    "nz_start_pct",
                    "on_ice_shooting_pct",
                    "ev_goals_for",
                    "ev_goals_against",
                    "ev_goals_for_pct",
                    "pp_goals_for",
                    "pp_goals_against",
                    "sh_goals_for",
                    "sh_goals_against",
                ],
                ["player_id", "season_id"],
                advanced_rows,
            )
            self.execute_upsert(
                conn,
                "players_shot_types",
                [
                    "player_id",
                    "season_id",
                    "goals_wrist",
                    "shots_wrist",
                    "goals_slap",
                    "shots_slap",
                    "goals_snap",
                    "shots_snap",
                    "goals_backhand",
                    "shots_backhand",
                    "goals_tip_in",
                    "shots_tip_in",
                    "goals_deflected",
                    "shots_deflected",
                    "goals_wrap_around",
                    "shots_wrap_around",
                ],
                ["player_id", "season_id"],
                shot_type_rows,
            )
            self.execute_upsert(
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
                    "season_id",
                ],
                ["player_id", "season_id"],
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
                    "season_id",
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
                    "event_id",
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
                    "timeonice",
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

        logger.info("Done.")
        logger.info(
            "Loaded: teams=%d, rosters=%d, skaters=%d, advanced=%d, shot_types=%d, goalies=%d, games=%d",
            len(teams_rows), len(roster_rows), len(skater_rows), len(advanced_rows),
            len(shot_type_rows), len(goalie_rows), len(games_rows),
        )


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    parser = argparse.ArgumentParser(
        description="Load NHL teams, season aggregates, and finished games into PostgreSQL.",
    )
    parser.add_argument(
        "--date-from",
        dest="date_from",
        metavar="YYYY-MM-DD",
        help="Game window start (overrides env DATE_FROM / config).",
    )
    parser.add_argument(
        "--date-to",
        dest="date_to",
        metavar="YYYY-MM-DD",
        help="Game window end inclusive (overrides env DATE_TO / config).",
    )
    parser.add_argument(
        "--season-id",
        dest="season_id",
        type=int,
        metavar="ID",
        help="NHL stats seasonId, e.g. 20252026 (overrides SEASON_ID).",
    )
    parser.add_argument(
        "--current-season",
        dest="current_season",
        metavar="LABEL",
        help='Season label stored on games.season, e.g. "25/26" (overrides CURRENT_SEASON).',
    )
    args = parser.parse_args()
    loader = ModernNhlLoader()
    if args.date_from:
        loader.start_date = args.date_from
    if args.date_to:
        loader.end_date = args.date_to
    if args.season_id is not None:
        loader.season_id = args.season_id
    if args.current_season:
        loader.current_season_label = args.current_season
    loader.run()
