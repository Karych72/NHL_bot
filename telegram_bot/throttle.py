"""Троттлинг callback-кнопок: один перехватчик апдейтов ниже STANDALONE_GROUP.

Гасит N+1 нагрузку на PostgreSQL от спама кнопками пагинации (Задача 10,
после Задачи 9 — кэш TTL уже снял часть нагрузки, лимит калибруется по
остатку). Per-user скользящее окно; состояние — только в памяти процесса.
`bot.py` лишь импортирует `enforce_callback_rate_limit` и регистрирует его
как `TypeHandler(Update, ...)`.
"""

import time
from typing import Dict, List

from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackContext

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


async def enforce_callback_rate_limit(update: Update, context: CallbackContext) -> None:
    """Перехватчик апдейтов PTB: троттлит callback-кнопки per-user.

    Зачем: регистрируется как `TypeHandler(Update, ...)` в группе строго
    раньше `bot.STANDALONE_GROUP`, поэтому видит апдейт раньше
    `ConversationHandler` и standalone-хендлеров и может остановить его до
    того, как тот дойдёт до адресата.

    Апдейты без `callback_query` (команды, текст) или без `effective_user`
    пропускаются без изменений — троттлятся только нажатия инлайн-кнопок
    (Ruling 4 задачи 10). При превышении лимита в текущем окне пользователю
    уходит короткий `callback_query.answer()`, и поднимается
    `ApplicationHandlerStop`: апдейт не доходит ни до `ConversationHandler`,
    ни до standalone-хендлеров, состояние диалога не меняется.
    """
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    now = time.monotonic()
    cutoff = now - CALLBACK_RATE_WINDOW_SEC
    _prune_expired(cutoff)

    user_id = update.effective_user.id
    hits = [hit for hit in _hits.get(user_id, []) if hit > cutoff]

    if len(hits) >= CALLBACK_RATE_LIMIT:
        _hits[user_id] = hits
        await query.answer(text=RATE_LIMIT_ANSWER_TEXT)
        raise ApplicationHandlerStop

    hits.append(now)
    _hits[user_id] = hits
