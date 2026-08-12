"""Database module: connection pooling, safe query execution, identifier validation.

Provides a single SimpleConnectionPool shared across the bot, context-managed
connections, and whitelist-based validation for dynamic table/column names so
that SQL composition via psycopg2.sql is safe even when identifiers come from
non-literal sources. Also provides ``cached_fetch_all`` — a TTL-memoized
``fetch_all`` for read-only reference/table/leaderboard queries that only
change once per data load (see ``ttl_cache``).
"""

import functools
import inspect
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import psycopg2
import psycopg2.pool
from psycopg2 import sql

import config

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None

# ---------------------------------------------------------------------------
# Whitelists — only identifiers listed here may be used in dynamic queries.
# Skater advanced / shot-type column names should match ADVANCED_STATS_COLUMNS /
# SHOT_TYPES_COLUMNS in bot_messages.py (used for JOIN / secondary sort).
# ---------------------------------------------------------------------------

ALLOWED_TABLES = frozenset({
    "players_season_stats",
    "players_advanced_stats",
    "players_shot_types",
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
    "season_id",
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
    "short_handed_time_on_ice_per_game",     "power_play_time_on_ice_per_game",
    "oz_faceoff_pct", "dz_faceoff_pct", "nz_faceoff_pct",
    "shootout_goals", "shootout_shots", "shootout_pct", "shootout_gd_goals",
    # players_advanced_stats
    "sat_pct", "usat_pct", "goals_pct", "oz_start_pct", "dz_start_pct",
    "nz_start_pct", "on_ice_shooting_pct", "ev_goals_for", "ev_goals_against",
    "ev_goals_for_pct", "pp_goals_for", "pp_goals_against", "sh_goals_for",
    "sh_goals_against",
    # players_shot_types
    "goals_wrist", "shots_wrist", "goals_slap", "shots_slap", "goals_snap",
    "shots_snap", "goals_backhand", "shots_backhand", "goals_tip_in",
    "shots_tip_in", "goals_deflected", "shots_deflected", "goals_wrap_around",
    "shots_wrap_around",
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


# ---------------------------------------------------------------------------
# TTL-кэш read-only справочников / таблиц / лидеров
# ---------------------------------------------------------------------------

# Данные обновляются не чаще, чем «раз в загрузку»: пайплайн грузится вручную
# (`make season-sync-*`) либо из cron-скрипта `push_digest_job.py`, который по
# его собственному докстрингу запускается «раз в день» после загрузки. TTL
# ниже на два порядка короче этого интервала — гасит повторные обращения в
# пределах одной пачки пользовательских запросов (навигация по меню/лидербордам),
# но не откладывает видимость новой загрузки на сколько-нибудь заметное время.
CACHED_FETCH_ALL_TTL_SECONDS = 300.0  # 5 минут


def ttl_cache(func):
    """Мемоизирует синхронную функцию на CACHED_FETCH_ALL_TTL_SECONDS секунд.

    Аргументы вызова нормализуются через ``inspect.signature(func).bind()``,
    так что один и тот же логический вызов даёт один и тот же ключ кэша
    независимо от того, передан ли аргумент позиционно или по имени. Сам ключ
    строится как ``имя_аргумента=тип:repr(значение)`` для каждого аргумента —
    ``repr`` у ``psycopg2.sql.Composable`` рекурсивно разворачивает обёрнутое
    содержимое (а не ``id()``/адрес объекта), поэтому два разных запроса дают
    разные ключи, а совпадающий запрос — тот же самый; префикс типа отделяет
    ``str``-запрос от ``sql.Composable`` с тем же текстом. ``params``/``columns``
    — списки и сами по себе нехешируемы, но их ``repr`` хешируем и отражает
    содержимое.

    Результат — всегда независимая копия сохранённого в кэше словаря (списки
    внутри копируются), поэтому вызывающий код может свободно использовать
    вернувшийся dict, не рискуя испортить запись в кэше для следующего вызова.

    Исключение из *func* не перехватывается и не кэшируется: падать громко
    важнее, чем сэкономить один поход в БД (Global Constraint 4).

    Только TTL: без LRU-вытеснения и без инвалидации по событию (YAGNI,
    Global Constraint 1) — это осознанно узкий кэш под конкретную задачу.
    """
    signature = inspect.signature(func)
    cache: Dict[Tuple[str, ...], Tuple[float, Dict[str, Any]]] = {}

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        key = tuple(
            f"{name}={type(value).__name__}:{value!r}"
            for name, value in bound.arguments.items()
        )

        now = time.monotonic()
        cached = cache.get(key)
        if cached is not None:
            expires_at, result = cached
            if now < expires_at:
                return _copy_fetch_result(result)

        result = func(*args, **kwargs)
        cache[key] = (now + CACHED_FETCH_ALL_TTL_SECONDS, result)
        return _copy_fetch_result(result)

    # Внутренний кэш-дикт доступен тестам напрямую (по аналогии с тем, как
    # тесты этого модуля уже сбрасывают database._pool между прогонами) —
    # без него состояние утекало бы между тестами через закэшированный
    # sys.modules["database"].
    wrapper._cache = cache  # type: ignore[attr-defined]
    return wrapper


def _copy_fetch_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Неглубокая защитная копия результата fetch_all для ttl_cache: копирует
    списки-значения, чтобы мутация возвращённого dict вызывающим кодом не
    портила запись, которая всё ещё лежит в кэше."""
    return {
        col: (list(values) if isinstance(values, list) else values)
        for col, values in result.items()
    }


cached_fetch_all = ttl_cache(fetch_all)
cached_fetch_all.__doc__ = (
    "TTL-кэшированная обёртка над fetch_all() — см. ttl_cache() и "
    "CACHED_FETCH_ALL_TTL_SECONDS. Использовать только для справочников/таблиц/"
    "лидеров, которые меняются раз в загрузку; для данных идущей игры "
    "оставаться на обычном fetch_all()."
)
