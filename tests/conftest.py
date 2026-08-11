"""Shared pytest fixtures for modeling and bot tests.

Bot fixtures import ``telegram_bot/*.py`` modules by their bare in-repo name
(mirroring how the bot process itself runs, with ``telegram_bot/`` as cwd and
on sys.path) and fake the two boundaries its handlers touch: the psycopg2
connection behind ``database.py``, and the PTB ``Update``/``CallbackContext``
objects passed into every handler.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Optional

import pytest

from tests._modeling_fixtures import write_synthetic_train_dataset

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
# Fake Update / CallbackContext (PTB 21.x shape)
# ---------------------------------------------------------------------------
#
# Duck-typed stand-ins, not real `telegram.Update`/`telegram.CallbackQuery`
# instances: handlers under test only ever touch `.callback_query`/`.message`
# and a handful of their attributes and methods, and the real objects require
# constructing several unrelated nested objects (User, Chat, Message, Bot...)
# just to satisfy __init__.
#
# Every Bot API method here is `async def`, mirroring PTB 21.x: handlers await
# them, so a sync stand-in would blow up on `await`. Recording still happens
# eagerly inside the coroutine body, so a call that is created but never
# awaited records nothing — that is deliberate, it keeps a forgotten `await`
# in a test visible instead of silently passing.

class FakeBot:
    """Records outgoing calls instead of hitting api.telegram.org."""

    def __init__(self) -> None:
        self.sent_messages: List[dict] = []
        self.sent_videos: List[dict] = []

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_messages))

    async def send_video(self, **kwargs: Any) -> SimpleNamespace:
        self.sent_videos.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_videos))


class FakeCallbackQuery:
    """Fakes the subset of ``telegram.CallbackQuery`` the handlers use."""

    def __init__(self, data: Optional[str], chat_id: int = 100) -> None:
        self.data = data
        # PTB 21.x: CallbackQuery.message is a MaybeInaccessibleMessage, which
        # carries `.chat` but no `.chat_id` — handlers read `message.chat.id`.
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=1
        )
        self.answers: List[Optional[str]] = []
        self.edited_texts: List[dict] = []
        self.edited_markups: List[Any] = []

    async def answer(self, text: Optional[str] = None, **kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edited_texts.append({"text": text, **kwargs})

    async def edit_message_reply_markup(self, reply_markup: Any = None, **kwargs: Any) -> None:
        self.edited_markups.append(reply_markup)


class FakeMessage:
    """Fakes the subset of ``telegram.Message`` used by text-input handlers."""

    def __init__(self, text: Optional[str], chat_id: int = 100) -> None:
        self.text = text
        self.chat_id = chat_id
        self.replies: List[dict] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append({"text": text, **kwargs})


@pytest.fixture
def fake_context() -> SimpleNamespace:
    """A CallbackContext-shaped object exposing only ``.bot`` (a FakeBot)."""
    return SimpleNamespace(bot=FakeBot())


@pytest.fixture
def make_callback_update() -> Callable[..., SimpleNamespace]:
    """Factory: build a fake Update carrying a ``callback_query`` with *data*."""

    def _make(data: Optional[str], chat_id: int = 100) -> SimpleNamespace:
        return SimpleNamespace(callback_query=FakeCallbackQuery(data, chat_id=chat_id))

    return _make


@pytest.fixture
def make_message_update() -> Callable[..., SimpleNamespace]:
    """Factory: build a fake Update carrying a plain-text ``message``."""

    def _make(text: Optional[str], chat_id: int = 100) -> SimpleNamespace:
        return SimpleNamespace(message=FakeMessage(text, chat_id=chat_id))

    return _make
