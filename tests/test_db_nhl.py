"""PostgreSQL schema and loader data integrity (optional).

* Schema: set RUN_DB_SCHEMA_TESTS=1 (``make test-db`` does this) and reachable PG from config.
* Data: set RUN_DB_DATA_TESTS=1 after loading (e.g. ``make season-sync-month``).

Once RUN_DB_SCHEMA_TESTS is on, TestNhlSchema fails loudly (error/failure, not
skip) on an unreachable server or a missing table — the caller asked for these
checks, so "couldn't check" must not read as "passed" (see CLAUDE.md Global
Constraint 4, падать громко). Only TestNhlLoadedData (opt-in via
RUN_DB_DATA_TESTS, never run in CI) still skips on missing prerequisites, since
that flag is a developer choosing to check loaded data, not a CI gate.

Requires: psycopg2, .env or env vars PG_* matching the loader.
"""

from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TELEGRAM_BOT = os.path.join(REPO_ROOT, "telegram_bot")
if TELEGRAM_BOT not in sys.path:
    sys.path.insert(0, TELEGRAM_BOT)

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

import config  # noqa: E402


def _connect():
    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        user=config.PG_USER,
        database=config.PG_DATABASE,
    )


def _base_table_exists(cur, relname: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = %s
        """,
        (relname,),
    )
    return cur.fetchone() is not None


def _pk_columns(cur, table: str):
    """Primary-key column names in index order."""
    cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
            AND NOT a.attisdropped
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND i.indisprimary
        ORDER BY k.ord
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestNhlSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # RUN_DB_SCHEMA_TESTS was explicitly requested (see class decorator above),
        # so an unreachable server is a hard error, not a skip.
        cls.conn = _connect()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn") and cls.conn:
            cls.conn.close()

    def test_primary_keys(self):
        with self.conn.cursor() as cur:
            if not _base_table_exists(cur, "teams"):
                self.fail("public.teams missing — run: make db-reset-local (or check DDL)")
            pk = _pk_columns(cur, "teams")
            if not pk:
                self.fail(
                    "teams has no PRIMARY KEY — run `make db-reset-local` "
                    "to apply data_tables/t.*.sql"
                )
            self.assertEqual(pk, ["team_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "teams_stats"), ["team_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "rosters"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "players_season_stats"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "goalies_season_stats"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "players_advanced_stats"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "players_shot_types"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "games"), ["game_id"])

    def test_games_has_season_id(self):
        with self.conn.cursor() as cur:
            if not _base_table_exists(cur, "games"):
                self.fail("public.games missing — run: make db-reset-local (or check DDL)")
            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = 'games'
                  AND a.attname = 'season_id' AND NOT a.attisdropped
                """
            )
            row = cur.fetchone()
            self.assertIsNotNone(
                row,
                "games.season_id missing — run: make db-reset-local",
            )
            self.assertTrue(
                str(row[0]).startswith("bigint"),
                f"expected bigint season_id, got {row[0]!r}",
            )


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_DATA_TESTS", "").strip() in ("1", "true", "yes"),
    "set RUN_DB_DATA_TESTS=1 after loading data (e.g. make season-sync-month)",
)
class TestNhlLoadedData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.conn = _connect()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"PostgreSQL unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn") and cls.conn:
            cls.conn.close()

    def test_has_games(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM games")
            n = cur.fetchone()[0]
        self.assertGreater(n, 0, "No games in DB; run the loader for your date window")

    def test_games_season_id_matches_config(self):
        sid = config.SEASON_ID
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM games WHERE season_id IS DISTINCT FROM %s",
                (sid,),
            )
            bad = cur.fetchone()[0]
        self.assertEqual(bad, 0, "games.season_id must match config.SEASON_ID for loaded rows")

    def test_home_away_teams_exist_for_season(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM games g
                WHERE NOT EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = g.home_team_id AND t.season_id = g.season_id
                )
                """
            )
            nh = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM games g
                WHERE NOT EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = g.away_team_id AND t.season_id = g.season_id
                )
                """
            )
            na = cur.fetchone()[0]
        self.assertEqual(nh, 0, "orphan home_team_id vs teams")
        self.assertEqual(na, 0, "orphan away_team_id vs teams")

    def test_game_fact_tables_reference_games(self):
        with self.conn.cursor() as cur:
            for tbl in (
                "game_team_stats",
                "game_player_stats",
                "game_goalie_stats",
                "all_goals",
            ):
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM {tbl} x
                    WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.game_id = x.game_id)
                    """
                )
                n = cur.fetchone()[0]
                self.assertEqual(n, 0, f"orphan game_id in {tbl}")

    def test_rosters_team_ids_exist(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM rosters r
                WHERE NOT EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = r.current_team_id AND t.season_id = r.season_id
                )
                """
            )
            n = cur.fetchone()[0]
        self.assertEqual(n, 0, "rosters.current_team_id must exist in teams for same season_id")

    def test_season_stats_align_with_config_season(self):
        sid = config.SEASON_ID
        with self.conn.cursor() as cur:
            for tbl in (
                "players_season_stats",
                "goalies_season_stats",
                "players_advanced_stats",
                "players_shot_types",
            ):
                cur.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE season_id IS DISTINCT FROM %s",
                    (sid,),
                )
                n = cur.fetchone()[0]
                self.assertEqual(n, 0, f"{tbl}.season_id must match SEASON_ID")

    def test_teams_and_stats_row_counts_match(self):
        sid = config.SEASON_ID
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM teams WHERE season_id = %s", (sid,))
            nt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM teams_stats WHERE season_id = %s", (sid,))
            ns = cur.fetchone()[0]
        self.assertEqual(nt, ns, "teams and teams_stats should have same team count per season_id")
        self.assertGreaterEqual(nt, 1)


if __name__ == "__main__":
    unittest.main()
