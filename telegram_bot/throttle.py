"""Троттлинг: входящих нажатий callback-кнопок и исходящих отправок рассылки.

Нажатия — outer-middleware перед всеми хендлерами.

Гасит N+1 нагрузку на PostgreSQL от спама кнопками пагинации (Задача 10,
после Задачи 9 — кэш TTL уже снял часть нагрузки, лимит калибруется по
остатку). Per-user скользящее окно; состояние — только в памяти процесса.
`bot.py` лишь импортирует `enforce_callback_rate_limit` и вешает его на
`Dispatcher.callback_query.outer_middleware`.
Отправки — `throttled`: пауза между сообщениями и повтор после 429 (`push_digest_job`, `bot.py`).
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, TypeVar, Union

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, TelegramObject

import config

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

# 8 нажатий за 3 секунды: ручная пагинация (человек тапает кнопку не чаще
# нескольких раз в секунду) укладывается с запасом, а автоматический спам
# кнопками — ради которого Задача 10 и заведена — режется.
CALLBACK_RATE_LIMIT = 8  # максимум нажатий в окне
CALLBACK_RATE_WINDOW_SEC = 3.0  # длина скользящего окна, секунд

RATE_LIMIT_ANSWER_TEXT = "Слишком много нажатий подряд, подождите пару секунд."

# Per-user список меток времени (time.monotonic()) нажатий внутри окна.
# Единственное состояние модуля: Redis и внешние хранилища не вводятся.
_hits: Dict[int, List[float]] = {}


def _prune_expired(cutoff: float) -> None:
    """Убирает из `_hits` пользователей, у которых все нажатия истекли.

    Проход по всему словарю — здесь же, при каждом вызове перехватчика,
    отдельного планировщика/джобы не заводится (Задача 10, YAGNI): апдейтов
    с callback_query на порядки меньше, чем апдейтов вообще, так что цена
    прохода пренебрежимо мала. Список меток времени внутри каждого
    пользователя — append-only и по возрастанию, поэтому последняя метка
    (`hits[-1]`) — самая свежая: если и она истекла, весь список устарел.
    """
    expired_users = [user_id for user_id, hits in _hits.items() if hits[-1] <= cutoff]
    for user_id in expired_users:
        del _hits[user_id]


async def enforce_callback_rate_limit(
    handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: Dict[str, Any],
) -> Any:
    """Outer-middleware aiogram: троттлит нажатия inline-кнопок per-user.

    Зачем: outer-middleware видит нажатие раньше фильтров и хендлеров кнопок
    и может остановить его до того, как
    оно дойдёт до адресата и до БД. Команды и текст сюда не попадают: middleware
    висит только на `callback_query` (Ruling 4 задачи 10).

    При превышении лимита в текущем окне пользователю уходит короткий
    `answer()`, а `handler` не вызывается: нажатие не доходит ни до одного
    хендлера.

    Аргументы: `handler` — следующий шаг цепочки aiogram, `event` — нажатие,
    `data` — контекст апдейта (передаётся дальше без изменений).
    """
    assert isinstance(event, CallbackQuery)
    now = time.monotonic()
    cutoff = now - CALLBACK_RATE_WINDOW_SEC
    _prune_expired(cutoff)

    user_id = event.from_user.id
    hits = [hit for hit in _hits.get(user_id, []) if hit > cutoff]

    if len(hits) >= CALLBACK_RATE_LIMIT:
        _hits[user_id] = hits
        await event.answer(text=RATE_LIMIT_ANSWER_TEXT)
        return None

    hits.append(now)
    _hits[user_id] = hits
    return await handler(event, data)


async def throttled(call: Callable[[], Awaitable[_T]], chat_id: Union[int, str]) -> _T:
    """Выполняет одну отправку и выдерживает паузу до следующей.

    Зачем: ``TelegramRetryAfter`` означает, что отправка не доставлена, — повторяем
    её один раз после указанной API задержки. Второй ``TelegramRetryAfter``, как и
    любая другая ошибка Telegram, улетает вызывающему.

    Аргументы: ``call`` — отправка без аргументов (её можно вызвать повторно);
    ``chat_id`` — получатель, для лога.
    """
    try:
        result = await call()
    except TelegramRetryAfter as exc:
        wait = float(exc.retry_after)
        logger.warning("429 RetryAfter %ss for chat_id=%s", wait, chat_id)
        await asyncio.sleep(wait)
        result = await call()
    # Telegram лимитирует бота ~30 сообщениями в секунду.
    await asyncio.sleep(max(config.PUSH_SEND_INTERVAL_SEC, 0.02))
    return result
