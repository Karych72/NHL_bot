"""Премиум-доступ чата (Задача 96): ИИ с суточным лимитом вместо недельного и прогноз модели.

Часть `telegram_bot/`: признак читают лимит ИИ (`ai_router.limit_refusal`), превью матча по
кнопке (`stats_handlers`) и рассылка превью (`push_digest_job`). Хранится в `premium_access`
(миграция 0016): чат и срок; выдаёт его пока человек SQL-ом (`DEVELOPMENT.md`).
"""

from database import fetch_all


def is_premium(chat_id: int) -> bool:
    """Действует ли у чата премиум: есть строка `premium_access` со сроком позже текущего момента.

    Зачем: одна проверка на всех потребителей. Сбой БД не превращается в «бесплатный» — ошибка
    уходит вызывающему, как у остальных запросов бота.

    Аргументы:
        chat_id: чат Telegram (личка или группа — премиум выдаётся на чат).
    """
    rows = fetch_all(
        "SELECT EXISTS (SELECT 1 FROM premium_access WHERE chat_id = %s AND expires_at > now()) "
        "AS premium",
        (chat_id,),
        columns=["premium"],
    )
    return bool(rows["premium"][0])
