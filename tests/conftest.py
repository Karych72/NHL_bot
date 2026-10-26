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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Sequence, Tuple, Type

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMediaGroup,
    SendMessage,
    SendRichMessage,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, MessageId, User, Video

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
# Telegram boundary: aiogram Bot on a recording HTTP session
# ---------------------------------------------------------------------------
#
# Handlers under test get a real `aiogram.Bot` and real `Message`/`CallbackQuery`
# objects bound to it; only the network layer is replaced. Every Bot API call a
# handler makes — `message.answer()`, `callback.answer()`, `bot.send_video()` — goes
# through aiogram's own serialization into `FakeSession.make_request`, which records
# the method object (the exact request that would reach api.telegram.org) and answers
# with what Telegram would return. Tests assert on those recorded requests.

FAKE_TOKEN = "123456789:TEST-TOKEN-NOT-A-REAL-SECRET"
BOT_USERNAME = "nhl_test_bot"
CHAT_ID = 100
USER_ID = 200


class FakeSession(BaseSession):
    """aiogram session that records Bot API requests instead of sending them.

    ``requests`` — every successful request in order. ``fail_next(method_type, error)``
    makes the next request of that type raise *error* (Telegram errors like «Message to
    be replied not found»); a failed request is not recorded. Message-producing methods
    answer with a ``Message`` whose ``message_id`` counts 1, 2, 3… across the session;
    an uploaded video gets ``file_id`` ``fid-<n>``, a video sent by ``file_id`` keeps it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[TelegramMethod[Any]] = []
        self._errors: List[Tuple[Type[TelegramMethod[Any]], Exception]] = []
        self._message_ids = 0
        self._uploads = 0

    def fail_next(self, method_type: Type[TelegramMethod[Any]], error: Exception) -> None:
        self._errors.append((method_type, error))

    def sent(self, method_type: Type[TelegramMethod[Any]]) -> List[Any]:
        """Recorded requests of one Bot API method, in order."""
        return [r for r in self.requests if isinstance(r, method_type)]

    @property
    def sent_messages(self) -> List[SendMessage]:
        """Обычные текстовые сообщения (служебные ответы, подсказки)."""
        return self.sent(SendMessage)

    @property
    def sent_rich(self) -> List[SendRichMessage]:
        """Rich-сообщения — экраны бота: таблицы, карточки, меню."""
        return self.sent(SendRichMessage)

    @property
    def outgoing(self) -> List[Any]:
        """Все новые сообщения бота по порядку — и обычные, и rich (тело — ``body()``)."""
        return [r for r in self.requests if isinstance(r, (SendMessage, SendRichMessage))]

    @property
    def answers(self) -> List[Optional[str]]:
        """Texts of `answerCallbackQuery` (None — answer without a toast)."""
        return [r.text for r in self.sent(AnswerCallbackQuery)]

    @property
    def edited_texts(self) -> List[EditMessageText]:
        return self.sent(EditMessageText)

    @property
    def edited_markups(self) -> List[EditMessageReplyMarkup]:
        return self.sent(EditMessageReplyMarkup)

    def _message(self, chat_id: Any, **fields: Any) -> Message:
        self._message_ids += 1
        return Message(
            message_id=self._message_ids,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            **fields,
        )

    def _video_message(self, chat_id: Any, media: Any) -> Message:
        if isinstance(media, str):
            file_id = media
        else:
            self._uploads += 1
            file_id = f"fid-{self._uploads}"
        video = Video(file_id=file_id, file_unique_id=file_id, width=1, height=1, duration=1)
        return self._message(chat_id, video=video)

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: Optional[int] = None) -> Any:
        for i, (method_type, error) in enumerate(self._errors):
            if isinstance(method, method_type):
                del self._errors[i]
                raise error
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="NHL", username=BOT_USERNAME)
        if isinstance(method, SendMessage):
            return self._message(method.chat_id, text=method.text)
        if isinstance(method, SendRichMessage):
            return self._message(method.chat_id)
        if isinstance(method, CopyMessage):
            self._message_ids += 1
            return MessageId(message_id=self._message_ids)
        if isinstance(method, SendVideo):
            return self._video_message(method.chat_id, method.video)
        if isinstance(method, SendMediaGroup):
            return [self._video_message(method.chat_id, m.media) for m in method.media]
        if isinstance(method, EditMessageText):
            return self._message(method.chat_id, text=method.text)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise AssertionError("тест не скачивает файлы из Telegram")
        yield b""

    async def close(self) -> None:
        return None


def body(request: Any) -> str:
    """Тело исходящего сообщения или правки: HTML rich-сообщения (``rich_message.html``)
    или текст обычного сообщения."""
    rich = getattr(request, "rich_message", None)
    return rich.html if rich is not None else request.text


def rich_html(text: str) -> str:
    """HTML, в который ``bot_messages.rich_message`` превращает построчный *text* — ожидаемое
    тело экрана в тестах (абзац на строку, таблицы — блоками)."""
    import bot_messages

    return bot_messages.rich_message(text).html


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


def _message_data(text: Optional[str], chat_id: int, chat_type: str = "private") -> Dict[str, Any]:
    """A user's message as Telegram sends it; a leading `/word` carries the bot_command
    entity, like a real client sets."""
    data: Dict[str, Any] = {
        "message_id": 1,
        "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
        "text": text,
    }
    if text and text.startswith("/"):
        data["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return data


@pytest.fixture
def make_message(fake_bot: Bot) -> Callable[..., Message]:
    """Factory: a user's text message bound to ``fake_bot`` (``message.answer()`` is recorded)."""

    def _make(text: Optional[str], chat_id: int = CHAT_ID, chat_type: str = "private") -> Message:
        return Message.model_validate(_message_data(text, chat_id, chat_type), context={"bot": fake_bot})

    return _make


# id сообщения с нажатой кнопкой заведомо не пересекается с нумерацией FakeSession
# (1, 2, 3…): хендлеры, записывающие id меню в данные диалога, должны класть туда id
# ОТПРАВЛЕННОГО сообщения, а не того, по кнопке которого кликнули.
MENU_MESSAGE_ID = 900


def _callback_data(data: Optional[str], chat_id: int, user_id: int) -> Dict[str, Any]:
    return {
        "id": "1",
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "chat_instance": "ci",
        "data": data,
        "message": {
            "message_id": MENU_MESSAGE_ID,
            "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "chat": {"id": chat_id, "type": "private"},
            "text": "меню",
        },
    }


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
            {"update_id": counter["id"], "message": _message_data(text, chat_id, chat_type)}
        )

    return _make
