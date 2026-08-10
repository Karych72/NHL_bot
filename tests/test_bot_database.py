"""Unit tests for telegram_bot/database.py: pooling, identifier whitelists, and
fetch_all() query/param formation and empty-result handling. No real Postgres
connection is ever made — psycopg2.pool.SimpleConnectionPool and
get_connection() are both replaced with fakes at their respective boundaries.
"""
from __future__ import annotations

from typing import Any

import pytest
from psycopg2 import sql


# ---------------------------------------------------------------------------
# Identifier whitelists — validate_table / validate_column
# ---------------------------------------------------------------------------

def test_validate_table_accepts_whitelisted_name(bot_module):
    database = bot_module("database")
    assert database.validate_table("teams") == "teams"


@pytest.mark.parametrize(
    "bad_name",
    [
        "not_a_real_table",
        "teams; DROP TABLE teams;--",
        "teams -- comment",
        "teams' OR '1'='1",
        "teams\"",
        "",
    ],
)
def test_validate_table_rejects_unknown_or_malicious_name(bot_module, bad_name):
    database = bot_module("database")
    with pytest.raises(ValueError, match="Table not allowed"):
        database.validate_table(bad_name)


def test_validate_column_accepts_whitelisted_name(bot_module):
    database = bot_module("database")
    assert database.validate_column("points") == "points"


@pytest.mark.parametrize(
    "bad_name",
    [
        "not_a_real_column",
        "points; DROP TABLE teams;--",
        "points\" = 1 OR \"1\"=\"1",
        "points, (SELECT 1)",
    ],
)
def test_validate_column_rejects_unknown_or_malicious_name(bot_module, bad_name):
    database = bot_module("database")
    with pytest.raises(ValueError, match="Column not allowed"):
        database.validate_column(bad_name)


# ---------------------------------------------------------------------------
# fetch_all — query/param formation and result shaping
# ---------------------------------------------------------------------------

def test_fetch_all_passes_query_text_and_params_to_cursor_unchanged(bot_module, fake_db_connection):
    database = bot_module("database")
    cursor = fake_db_connection(database, rows=[])

    database.fetch_all("SELECT 1 FROM teams WHERE team_id = %s", (42,), columns=["x"])

    assert cursor.executed == [("SELECT 1 FROM teams WHERE team_id = %s", (42,))]


def test_fetch_all_passes_composable_sql_object_through_untouched(bot_module, fake_db_connection):
    """fetch_all's type is Union[str, sql.Composable]; a Composable must reach
    cursor.execute() as-is (not stringified), since psycopg2 needs the object
    itself to bind Identifier/Literal parts safely."""
    database = bot_module("database")
    cursor = fake_db_connection(database, rows=[])
    query = sql.SQL("SELECT {col} FROM teams").format(col=sql.Identifier("team_id"))

    database.fetch_all(query, (), columns=["team_id"])

    [(executed_query, executed_params)] = cursor.executed
    assert executed_query is query
    assert executed_params == ()


def test_fetch_all_defaults_params_to_none_when_omitted(bot_module, fake_db_connection):
    database = bot_module("database")
    cursor = fake_db_connection(database, rows=[])

    database.fetch_all("SELECT 1", columns=["x"])

    assert cursor.executed == [("SELECT 1", None)]


def test_fetch_all_maps_rows_onto_named_columns_in_order(bot_module, fake_db_connection):
    database = bot_module("database")
    fake_db_connection(
        database,
        rows=[(1, "Ovechkin"), (2, "Crosby")],
    )

    result = database.fetch_all("SELECT id, name FROM x", columns=["id", "name"])

    assert result == {
        "id": [1, 2],
        "name": ["Ovechkin", "Crosby"],
        "count_rows": 2,
    }


def test_fetch_all_empty_result_returns_empty_lists_not_a_crash(bot_module, fake_db_connection):
    database = bot_module("database")
    fake_db_connection(database, rows=[])

    result = database.fetch_all("SELECT id, name FROM x", columns=["id", "name"])

    assert result == {"id": [], "name": [], "count_rows": 0}


def test_fetch_all_without_columns_returns_only_count_rows(bot_module, fake_db_connection):
    database = bot_module("database")
    fake_db_connection(database, rows=[(1,), (2,), (3,)])

    result = database.fetch_all("SELECT 1")

    assert result == {"count_rows": 3}


# ---------------------------------------------------------------------------
# get_connection — borrow/return/rollback contract around the pool
# ---------------------------------------------------------------------------

class _FakePool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.getconn_calls = 0
        self.putconn_calls: list = []

    def getconn(self) -> Any:
        self.getconn_calls += 1
        return self._conn

    def putconn(self, conn: Any) -> None:
        self.putconn_calls.append(conn)


class _FakeConn:
    def __init__(self) -> None:
        self.rollback_called = False

    def rollback(self) -> None:
        self.rollback_called = True


def test_get_connection_returns_conn_to_pool_on_success(bot_module, monkeypatch):
    database = bot_module("database")
    conn = _FakeConn()
    pool = _FakePool(conn)
    monkeypatch.setattr(database, "get_pool", lambda: pool)

    with database.get_connection() as borrowed:
        assert borrowed is conn

    assert pool.getconn_calls == 1
    assert pool.putconn_calls == [conn]
    assert conn.rollback_called is False


def test_get_connection_rolls_back_and_still_returns_conn_on_exception(bot_module, monkeypatch):
    database = bot_module("database")
    conn = _FakeConn()
    pool = _FakePool(conn)
    monkeypatch.setattr(database, "get_pool", lambda: pool)

    with pytest.raises(RuntimeError, match="boom"):
        with database.get_connection():
            raise RuntimeError("boom")

    assert conn.rollback_called is True
    assert pool.putconn_calls == [conn], "connection must be returned to the pool even on error"


# ---------------------------------------------------------------------------
# get_pool / close_pool — construction args and reuse
# ---------------------------------------------------------------------------

class _FakeSimpleConnectionPool:
    """Stands in for psycopg2.pool.SimpleConnectionPool; never touches a socket."""

    instances: list = []

    def __init__(self, *, minconn: int, maxconn: int, host: str, port: str, user: str, database: str) -> None:
        self.kwargs = dict(minconn=minconn, maxconn=maxconn, host=host, port=port, user=user, database=database)
        self.closed = False
        self.closeall_called = False
        _FakeSimpleConnectionPool.instances.append(self)

    def closeall(self) -> None:
        self.closeall_called = True
        self.closed = True


@pytest.fixture
def fake_pool_class(bot_module, monkeypatch):
    database = bot_module("database")
    _FakeSimpleConnectionPool.instances = []
    monkeypatch.setattr(database.psycopg2.pool, "SimpleConnectionPool", _FakeSimpleConnectionPool)
    monkeypatch.setattr(database, "_pool", None)
    yield database
    monkeypatch.setattr(database, "_pool", None)


def test_get_pool_creates_pool_with_config_values(fake_pool_class, monkeypatch):
    database = fake_pool_class
    monkeypatch.setattr(database.config, "PG_HOST", "test-host")
    monkeypatch.setattr(database.config, "PG_PORT", "6543")
    monkeypatch.setattr(database.config, "PG_USER", "test-user")
    monkeypatch.setattr(database.config, "PG_DATABASE", "test-db")

    pool = database.get_pool()

    assert isinstance(pool, _FakeSimpleConnectionPool)
    assert pool.kwargs == {
        "minconn": 1,
        "maxconn": 5,
        "host": "test-host",
        "port": "6543",
        "user": "test-user",
        "database": "test-db",
    }


def test_get_pool_reuses_existing_open_pool(fake_pool_class):
    database = fake_pool_class

    first = database.get_pool()
    second = database.get_pool()

    assert first is second
    assert len(_FakeSimpleConnectionPool.instances) == 1


def test_get_pool_creates_new_pool_after_close(fake_pool_class):
    database = fake_pool_class

    first = database.get_pool()
    database.close_pool()
    second = database.get_pool()

    assert first is not second
    assert len(_FakeSimpleConnectionPool.instances) == 2


def test_close_pool_closes_and_clears_global_pool(fake_pool_class):
    database = fake_pool_class
    pool = database.get_pool()

    database.close_pool()

    assert pool.closeall_called is True
    assert database._pool is None


def test_close_pool_is_a_no_op_when_no_pool_exists(fake_pool_class):
    """fake_pool_class already guarantees _pool is None; the only thing this
    test asserts is that close_pool() tolerates that (no AttributeError from
    calling closeall() on None)."""
    database = fake_pool_class

    database.close_pool()  # must not raise

    assert database._pool is None
