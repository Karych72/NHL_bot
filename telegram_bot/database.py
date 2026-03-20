"""Database module: connection pooling, safe query execution, identifier validation.

Provides a single SimpleConnectionPool shared across the bot, context-managed
connections, and whitelist-based validation for dynamic table/column names so
that SQL composition via psycopg2.sql is safe even when identifiers come from
non-literal sources.
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence, Union

import psycopg2
import psycopg2.pool
from psycopg2 import sql

import config

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None

# ---------------------------------------------------------------------------
# Whitelists — only identifiers listed here may be used in dynamic queries.
# ---------------------------------------------------------------------------

ALLOWED_TABLES = frozenset({
    "players_season_stats",
    "goalies_season_stats",
    "teams_stats",
    "teams",
    "rosters",
    "games",
    "all_goals",
    "game_team_stats",
    "game_player_stats",
    "game_goalie_stats",
})

ALLOWED_COLUMNS = frozenset({
    # teams
    "team_id", "name", "division_name", "arena", "conference_name",
    "abbreviation", "first_year_of_play", "city", "active", "short_name",
    # teams_stats
    "games_played", "wins", "losses", "ot", "points", "procent_points",
    "goals_per_game", "goals_against_per_game", "power_play_percentage",
    "power_play_goals", "power_play_goals_against", "power_play_opportunities",
    "penalty_kill_percentage", "shots_per_game", "shots_allowed",
    "face_off_win_percentage",
    # rosters
    "player_id", "position", "jersey_number", "currentAge", "lastName",
    "nationality", "captain", "alternate_captain", "rookie", "current_team_id",
    # players_season_stats
    "time_on_ice", "assists", "goals", "pim", "shots", "games", "hits",
    "power_play_points", "power_play_time_on_ice", "even_time_on_ice",
    "penalty_minutes", "face_off_pct", "shot_pct", "game_winning_goals",
    "over_time_goals", "short_handed_goals", "short_handed_points",
    "short_handed_time_on_ice", "blocked", "plus_minus", "shifts",
    "time_on_ice_per_game", "even_time_on_ice_per_game",
    "short_handed_time_on_ice_per_game", "power_play_time_on_ice_per_game",
    # goalies_season_stats
    "shutouts", "ties", "saves", "power_play_saves", "short_handed_saves",
    "even_saves", "short_handed_shots", "even_shots", "power_play_shots",
    "save_percentage", "goal_against_average", "games_started",
    "shots_against", "goals_against", "power_play_save_percentage",
    "short_handed_save_percentage", "even_strength_save_percentage",
    # games
    "game_id", "day", "home_team_id", "away_team_id", "winner_id",
    "is_overtime", "is_shootouts", "season",
    # all_goals
    "goal_player_id", "total_goals", "assist_player1_id", "assist_total_1",
    "assist_player2_id", "assist_total_2", "empty_net", "winner_goal",
    "is_ppg", "is_shg", "period", "time", "goals_away", "goals_home",
    # game_team_stats
    "field", "fst_period_goals", "snd_period_goals", "trd_period_goals",
    "takeaways", "giveaways",
    # game_player_stats
    "power_play_assists", "face_off_wins", "face_off_taken",
    "short_handed_assists",
    # game_goalie_stats
    "timeOnIce", "decision", "short_handed_shots_against",
    "even_shots_against", "power_play_shots_against",
})


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

def get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            host=config.PG_HOST,
            port=config.PG_PORT,
            user=config.PG_USER,
            database=config.PG_DATABASE,
        )
        logger.info("Created DB connection pool (%s:%s/%s)",
                     config.PG_HOST, config.PG_PORT, config.PG_DATABASE)
    return _pool


@contextmanager
def get_connection():
    """Borrow a connection from the pool; auto-rollback on error, auto-return on exit."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.closeall()
        _pool = None
        logger.info("Closed DB connection pool")


# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------

def validate_table(name: str) -> str:
    if name not in ALLOWED_TABLES:
        raise ValueError(f"Table not allowed: {name!r}")
    return name


def validate_column(name: str) -> str:
    if name not in ALLOWED_COLUMNS:
        raise ValueError(f"Column not allowed: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def fetch_all(
    query_text: Union[str, sql.Composable],
    params: Optional[Sequence] = None,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute a SELECT and return ``{column: [values…], 'count_rows': N}``.

    *query_text* can be a plain string or a ``psycopg2.sql.Composable``.
    *params* are passed to ``cursor.execute()`` for safe value substitution.
    *columns* maps positional result columns to dict keys.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query_text, params)
            rows = cur.fetchall()

    if columns is None:
        columns = []

    result: Dict[str, Any] = {col: [] for col in columns}
    result["count_rows"] = len(rows)
    for row in rows:
        for i, col in enumerate(columns):
            result[col].append(row[i])
    return result
