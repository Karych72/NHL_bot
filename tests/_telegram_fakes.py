"""Граница Telegram для тестов бота: aiogram ``Bot`` на записывающей HTTP-сессии.

Без pytest (как ``_pipeline_fixtures``/``_modeling_fixtures``): модуль импортируют и фикстуры
``tests/conftest.py``, и ``unittest``-тесты, которые CI гоняет без dev-зависимостей
(``tests/test_db_nhl.py`` в job ``db-tests``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Tuple, Type

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    CreateInvoiceLink,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMediaGroup,
    SendMessage,
    SendRichMessage,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import Chat, Message, MessageId, RichBlockVideo, RichMessage, User, Video

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
    an uploaded video gets ``file_id`` ``fid-<n>``, a video sent by ``file_id`` keeps it —
    and so does every video in ``media`` of a rich message or its edit (answered as video blocks).
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[TelegramMethod[Any]] = []
        self._errors: List[Tuple[Type[TelegramMethod[Any]], Exception]] = []
        self.sent_rich_ids: Dict[int, int] = {}
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

    def _message(self, chat_id: Any, message_id: Optional[int] = None, **fields: Any) -> Message:
        """Сообщение ответа; новое — со следующим ``message_id``, правка — со своим."""
        if message_id is None:
            self._message_ids += 1
            message_id = self._message_ids
        return Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            **fields,
        )

    def _video(self, media: Any) -> Video:
        if isinstance(media, str):
            file_id = media
        else:
            self._uploads += 1
            file_id = f"fid-{self._uploads}"
        return Video(file_id=file_id, file_unique_id=file_id, width=1, height=1, duration=1)

    def _video_message(self, chat_id: Any, media: Any) -> Message:
        return self._message(chat_id, video=self._video(media))

    def _rich_message(self, chat_id: Any, rich: Any, message_id: Optional[int] = None) -> Message:
        """Ответ на rich-сообщение (отправку или правку): видео из ``media`` — блоками."""
        blocks = [RichBlockVideo(video=self._video(m.media.media)) for m in rich.media or []]
        return self._message(chat_id, message_id, rich_message=RichMessage(blocks=blocks))

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
            result = self._rich_message(method.chat_id, method.rich_message)
            self.sent_rich_ids[id(method)] = result.message_id
            return result
        if isinstance(method, CreateInvoiceLink):
            return "https://t.me/$fake-invoice"
        if isinstance(method, CopyMessage):
            self._message_ids += 1
            return MessageId(message_id=self._message_ids)
        if isinstance(method, SendVideo):
            return self._video_message(method.chat_id, method.video)
        if isinstance(method, SendMediaGroup):
            return [self._video_message(method.chat_id, m.media) for m in method.media]
        if isinstance(method, EditMessageText):
            if method.rich_message is not None:
                return self._rich_message(method.chat_id, method.rich_message, method.message_id)
            return self._message(method.chat_id, method.message_id, text=method.text)
        return True

    def remove_ai_progress(self, start: int, text: str, refusals: Sequence[str] = ()) -> None:
        """Исключает один отправленный статус и его удаление из сравнений экранов."""
        requests = self.requests[start:]
        statuses = [r for r in requests if isinstance(r, SendRichMessage) and body(r) == rich_html(text)]
        if (len(requests) == 1 and isinstance(requests[0], SendRichMessage)
                and body(requests[0]) in {rich_html(t) for t in refusals}):
            return  # Отказ по лимиту: запроса к ИИ и статуса нет.
        assert len(statuses) == 1, "статус должен отправляться ровно один раз"
        status = statuses[0]
        deletions = [
            r for r in requests if isinstance(r, DeleteMessage)
            and r.message_id == self.sent_rich_ids[id(status)] and r.chat_id == status.chat_id
        ]
        assert len(deletions) == 1, "статус должен удаляться ровно один раз"
        self.requests[start:] = [r for r in requests if r is not status and r is not deletions[0]]

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
    """HTML, в который ``rich_markup.rich_message`` превращает построчный *text* — ожидаемое
    тело экрана в тестах (блок строк — абзац, таблицы — блоками)."""
    import rich_markup

    return rich_markup.rich_message(text).html


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
# (1, 2, 3…): ответ, отправленный новым сообщением, отличим от правки сообщения с кнопкой.
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
