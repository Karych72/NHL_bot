"""Shared pytest fixtures for modeling and bot tests.

Bot fixtures import ``telegram_bot/*.py`` modules by their bare in-repo name
(mirroring how the bot process itself runs, with ``telegram_bot/`` as cwd and
on sys.path) and fake the two boundaries its handlers touch: the psycopg2
connection behind ``database.py``, and the HTTP session behind aiogram's ``Bot``
(``FakeSession``: every Bot API request is recorded and answered like Telegram
would). Handlers receive real aiogram ``Message``/``CallbackQuery`` objects bound
to that bot. The DB boundary comes in two flavours: ``fake_db_connection`` answers
every query with one fixed row set, while ``fake_db_router`` routes per query for
whole-scenario runs and additionally swaps ``bot_messages.cached_fetch_all`` for a
fresh, cache-empty wrapper.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from tests._modeling_fixtures import write_synthetic_train_dataset
from tests._telegram_fakes import CHAT_ID, FAKE_TOKEN, USER_ID, FakeSession, _callback_data, _message_data

REPO_ROOT = Path(__file__).resolve().parent.parent
TELEGRAM_BOT_DIR = REPO_ROOT / "telegram_bot"


@pytest.fixture(scope="session")
def matplotlib_agg_backend() -> None:
    """Pay matplotlib import cost once for PNG-related tests."""
    import matplotlib

    matplotlib.use("Agg", force=True)


@pytest.fixture
def synthetic_train_dataset(tmp_path: Path) -> tuple[Path, Path]:
    """Mini ``dataset_train.csv`` + ``metadata_train.json`` in *tmp_path*."""
    return write_synthetic_train_dataset(tmp_path)


# ---------------------------------------------------------------------------
# telegram_bot module loading
# ---------------------------------------------------------------------------

@pytest.fixture
def bot_module(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Any]:
    """Import a ``telegram_bot/*.py`` module by its bare name.

    telegram_bot modules use bare imports (``import config``, ``from database
    import fetch_all``) and some render templates via paths relative to
    ``telegram_bot/`` (see ``template_funcs.read_template``). This mirrors the
    layout the bot process runs under: ``telegram_bot/`` on sys.path and as
    cwd. monkeypatch restores both automatically when the test ends, so this
    never leaks into other test files regardless of collection order.
    """
    monkeypatch.chdir(TELEGRAM_BOT_DIR)
    monkeypatch.syspath_prepend(str(TELEGRAM_BOT_DIR))

    def _import(name: str) -> Any:
        return importlib.import_module(name)

    return _import


# config.validate_env() checks the raw environment (os.environ), not the
# already-defaulted config.PG_*, so any test that drives bot.main() /
# push_digest_job.main() end-to-end must set all six required variables
# itself — it cannot rely on whatever `make` happened to supply.
_REQUIRED_BOT_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456789:TEST-TOKEN-NOT-A-REAL-SECRET",
    "PG_HOST": "localhost",
    "PG_PORT": "5432",
    "PG_USER": "nhl_bot_test",
    "PG_DATABASE": "nhl_bot_test",
    "SEASON_ID": "20262027",
    # Только бот (`validate_env("ANTHROPIC_API_KEY")`, Задача 81); рассылке не нужен.
    "ANTHROPIC_API_KEY": "sk-ant-TEST-NOT-A-REAL-KEY",
}


@pytest.fixture
def set_required_bot_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Factory: set every variable ``config.validate_env()`` requires.

    ``**overrides`` swaps individual values (e.g. to an empty string) for
    tests targeting one specific missing/blank variable.
    """

    def _set(**overrides: str) -> None:
        for name, value in {**_REQUIRED_BOT_ENV, **overrides}.items():
            monkeypatch.setenv(name, value)

    return _set


# ---------------------------------------------------------------------------
# Fake DB connection — boundary mock for database.get_connection/fetch_all
# ---------------------------------------------------------------------------

class FakeCursor:
    """Records every ``execute()`` call and answers ``fetchall()`` from *rows*."""

    def __init__(self, rows: Optional[List[tuple]] = None) -> None:
        self.rows: List[tuple] = rows if rows is not None else []
        self.executed: List[tuple] = []  # (query_text, params)

    def execute(self, query: Any, params: Any = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> List[tuple]:
        return self.rows

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeConnection:
    """Stand-in for a psycopg2 connection: holds the one cursor fetch_all() gets
    via ``.cursor()``. get_connection()'s own borrow/rollback/return contract
    around the pool is a separate concern, tested directly in
    tests/test_bot_database.py against a fake pool — this class only needs to
    satisfy fetch_all()'s ``with get_connection() as conn: conn.cursor()``."""

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        """Запись (журнал `ai_questions`) коммитит транзакцию; фейку фиксировать нечего."""


@pytest.fixture
def fake_db_connection(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeCursor]:
    """Patch a bot module's ``get_connection`` with a scriptable fake.

    Usage: ``cursor = fake_db_connection(database, rows=[(1, "a")])`` patches
    ``database.get_connection`` to yield a fake connection wrapping a single
    fake cursor, and returns that cursor so the test can assert on
    ``cursor.executed`` (the SQL/params handed to ``cur.execute``).
    """

    def _patch(module: Any, rows: Optional[List[tuple]] = None) -> FakeCursor:
        cursor = FakeCursor(rows)
        connection = FakeConnection(cursor)

        @contextmanager
        def _fake_get_connection():
            yield connection

        monkeypatch.setattr(module, "get_connection", _fake_get_connection)
        return cursor

    return _patch


# ---------------------------------------------------------------------------
# Routed fake DB — boundary mock for whole-scenario runs
# ---------------------------------------------------------------------------

def _query_text(query: Any) -> str:
    """Текст запроса, по которому фикстура узнаёт его среди маршрутов.

    ``psycopg2.sql.Composable`` не приводится к строке без подключения
    (``as_string()`` требует connection), но его ``repr`` рекурсивно
    разворачивает вложенные ``SQL``/``Identifier`` и содержит все текстовые
    куски запроса — этого хватает, чтобы опознать запрос по подстроке. Тот же
    приём уже используется для ключей кэша в ``database.ttl_cache``.
    """
    return query if isinstance(query, str) else repr(query)


class RoutedCursor(FakeCursor):
    """Курсор, отвечающий на каждый запрос сценария своими строками.

    Один сценарий бота делает несколько разных запросов (карточка матча — шесть),
    поэтому единственного набора строк, как у ``FakeCursor``, ему мало. Маршрут —
    пара «подстрока запроса → строки»; побеждает первый подошедший, так что более
    узкую подстроку надо ставить раньше. Вместо списка строк маршрут принимает и
    функцию от ``params`` — так отвечают запросы, у которых ответ зависит от
    аргумента (страница лидеров по ``LIMIT``/``OFFSET``, карточка по ``game_id``).

    Запрос, не подошедший ни к одному маршруту, роняет тест: молчаливый пустой
    ответ дал бы зелёный тест на пустых данных (Global Constraint 4).
    """

    def __init__(self, routes: Sequence[Tuple[str, Any]]) -> None:
        super().__init__()
        self.routes = list(routes)

    def execute(self, query: Any, params: Any = None) -> None:
        text = _query_text(query)
        self.executed.append((text, params))
        for fragment, rows in self.routes:
            if fragment in text:
                self.rows = list(rows(params) if callable(rows) else rows)
                return
        raise AssertionError(f"Запрос не описан ни одним маршрутом фикстуры: {text}")


@pytest.fixture
def fake_db_router(
    bot_module: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> Callable[[Sequence[Tuple[str, Any]]], RoutedCursor]:
    """Подменить БД на маршрутизирующий фейк для всего стека бота.

    Подменяется только граница БД, зато обе её половины: ``database.get_connection``
    и ``bot_messages.cached_fetch_all``. Вторая — свежей обёрткой
    ``ttl_cache(fetch_all)``, потому что боевая собирается один раз на импорте
    модуля и её TTL-кэш иначе протекал бы между тестами (тот же приём, что
    ``cleared_cache`` в tests/test_bot_database.py). Выше этой границы всё
    работает настоящее: композиция SQL, маппинг строк, шаблоны, клавиатуры.

    Возвращает курсор, чтобы тест мог утверждать по ``cursor.executed``
    (текст запроса и параметры, дошедшие до БД).
    """

    def _patch(routes: Sequence[Tuple[str, Any]]) -> RoutedCursor:
        database = bot_module("database")
        bot_messages = bot_module("bot_messages")
        cursor = RoutedCursor(routes)
        connection = FakeConnection(cursor)

        @contextmanager
        def _fake_get_connection():
            yield connection

        monkeypatch.setattr(database, "get_connection", _fake_get_connection)
        monkeypatch.setattr(
            bot_messages, "cached_fetch_all", database.ttl_cache(database.fetch_all)
        )
        return cursor

    return _patch


# ---------------------------------------------------------------------------
# Telegram boundary: aiogram Bot on a recording HTTP session (tests/_telegram_fakes.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_bot() -> Bot:
    """aiogram ``Bot`` on a ``FakeSession``: ``fake_bot.session`` holds the requests."""
    return Bot(FAKE_TOKEN, session=FakeSession())


@pytest.fixture
def api(fake_bot: Bot) -> FakeSession:
    """The recording session of ``fake_bot`` — what the handlers asked Telegram to do."""
    session = fake_bot.session
    assert isinstance(session, FakeSession)
    return session


@pytest.fixture
def fsm_state(fake_bot: Bot) -> FSMContext:
    """State of the `/stats` dialog for (CHAT_ID, USER_ID), as aiogram hands it to handlers."""
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=fake_bot.id, chat_id=CHAT_ID, user_id=USER_ID),
    )


@pytest.fixture
def make_message(fake_bot: Bot) -> Callable[..., Message]:
    """Factory: a user's text message bound to ``fake_bot`` (``message.answer()`` is recorded)."""

    def _make(text: Optional[str], chat_id: int = CHAT_ID, chat_type: str = "private") -> Message:
        return Message.model_validate(_message_data(text, chat_id, chat_type), context={"bot": fake_bot})

    return _make


@pytest.fixture
def make_callback(fake_bot: Bot) -> Callable[..., CallbackQuery]:
    """Factory: a button press with *data* on a bot message, bound to ``fake_bot``."""

    def _make(data: Optional[str], chat_id: int = CHAT_ID, user_id: int = USER_ID) -> CallbackQuery:
        return CallbackQuery.model_validate(_callback_data(data, chat_id, user_id), context={"bot": fake_bot})

    return _make


@pytest.fixture
def make_update() -> Callable[..., Any]:
    """Factory: a raw ``Update`` for ``Dispatcher.feed_update`` — ``text`` (a message) or
    ``data`` (a button press); *update_id* grows with every call."""
    from aiogram.types import Update

    counter = {"id": 0}

    def _make(
        text: Optional[str] = None, data: Optional[str] = None, chat_id: int = CHAT_ID,
        chat_type: str = "private", user_id: int = USER_ID,
    ) -> Update:
        counter["id"] += 1
        if data is not None:
            return Update.model_validate(
                {"update_id": counter["id"], "callback_query": _callback_data(data, chat_id, user_id)}
            )
        return Update.model_validate(
            {"update_id": counter["id"], "message": _message_data(text, chat_id, chat_type, user_id)}
        )

    return _make
