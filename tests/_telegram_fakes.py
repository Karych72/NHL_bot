"""Граница Telegram для тестов бота: aiogram ``Bot`` на записывающей HTTP-сессии.

Без pytest (как ``_pipeline_fixtures``/``_modeling_fixtures``): модуль импортируют и фикстуры
``tests/conftest.py``, и ``unittest``-тесты, которые CI гоняет без dev-зависимостей
(``tests/test_db_nhl.py`` в job ``db-tests``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Type

from aiogram import Bot
from aiogram.client.session.base import BaseSession
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
from aiogram.types import Chat, Message, MessageId, User, Video

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


def _message_data(
    text: Optional[str], chat_id: int, chat_type: str = "private", user_id: int = USER_ID
) -> Dict[str, Any]:
    """A user's message as Telegram sends it; a leading `/word` carries the bot_command
    entity, like a real client sets."""
    data: Dict[str, Any] = {
        "message_id": 1,
        "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "text": text,
    }
    if text and text.startswith("/"):
        data["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return data


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
