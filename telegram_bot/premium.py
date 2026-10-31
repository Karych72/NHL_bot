"""Премиум-доступ чата (Задачи 96, 97): ИИ с суточным лимитом вместо недельного и прогноз модели.

Часть `telegram_bot/`: признак читают лимит ИИ (`ai_router.limit_refusal`), превью матча по
кнопке (`stats_handlers`) и рассылка превью (`push_digest_job`). Хранится в `premium_access`
(миграция 0016): чат и срок. Покупка — подписка Telegram Stars (`bot.py`: `/premium`, оплата);
каждый платёж пишется в журнал `premium_payments` (миграция 0017) и продлевает срок на 30 дней
(`extend_premium`). Возврат делает человек на VM: `python premium.py refund <charge_id>`.
"""

import argparse
import asyncio
from datetime import datetime
from typing import Optional

from aiogram import Bot

import config
from database import fetch_all, get_connection

# Один тариф: подписка Stars с автопродлением. 2592000 с (30 дней) — единственный период,
# который Telegram принимает для `subscription_period`.
PRICE_STARS = 100
PERIOD_SECONDS = 2592000
PERIOD_DAYS = 30


def is_premium(chat_id: int) -> bool:
    """Действует ли у чата премиум: есть строка `premium_access` со сроком позже текущего момента.

    Зачем: одна проверка на всех потребителей. Сбой БД не превращается в «бесплатный» — ошибка
    уходит вызывающему, как у остальных запросов бота.

    Аргументы:
        chat_id: чат Telegram (личка или группа — премиум выдаётся на чат).
    """
    return premium_until(chat_id) is not None


def premium_until(chat_id: int) -> Optional[datetime]:
    """Срок действующего премиума чата или None, если премиума нет (или он истёк).

    Зачем: `/premium` показывает дату окончания; `is_premium` её не отдаёт.

    Аргументы:
        chat_id: чат Telegram.
    """
    rows = fetch_all(
        "SELECT expires_at FROM premium_access WHERE chat_id = %s AND expires_at > now()",
        (chat_id,),
        columns=["expires_at"],
    )
    return rows["expires_at"][0] if rows["expires_at"] else None


def has_paid_subscription(chat_id: int) -> bool:
    """Есть ли у чата действующая оплаченная подписка Stars (не ручная выдача, Задача 96).

    Зачем: `/premium` не должен предлагать вторую подписку тому, у кого первая ещё идёт, —
    иначе получатся две параллельные с автопродлением. Признак — невозвращённый платёж журнала
    со сроком `paid_until` позже текущего момента.

    Аргументы:
        chat_id: чат Telegram (личка).
    """
    rows = fetch_all(
        "SELECT EXISTS (SELECT 1 FROM premium_payments WHERE chat_id = %s "
        "AND paid_until > now() AND refunded_at IS NULL) AS paid",
        (chat_id,),
        columns=["paid"],
    )
    return bool(rows["paid"][0])


def extend_premium(chat_id: int, charge_id: str, amount_stars: int, is_recurring: bool) -> datetime:
    """Записывает платёж Stars в журнал и продлевает премиум чата на 30 дней — одной транзакцией.

    Срок считается от большего из «текущий `expires_at`» и «сейчас». Повтор того же
    `charge_id` (Telegram может прислать событие дважды) в журнал не попадает и срок не меняет.
    `subscription_expiration_date` из события не используется: срок ведёт наша таблица.

    Аргументы:
        chat_id: чат (личка), которому выдаётся премиум.
        charge_id: `telegram_payment_charge_id` платежа — ключ идемпотентности.
        amount_stars: списанная сумма в Stars.
        is_recurring: платёж — автопродление, а не первая оплата.

    Возвращает: срок премиума чата после вызова.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT GREATEST(COALESCE((SELECT expires_at FROM premium_access WHERE chat_id = %s), "
                "now()), now()) + make_interval(days => %s)",
                (chat_id, PERIOD_DAYS),
            )
            new_until = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO premium_payments "
                "(chat_id, telegram_payment_charge_id, amount_stars, is_recurring, paid_until) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (telegram_payment_charge_id) DO NOTHING RETURNING id",
                (chat_id, charge_id, amount_stars, is_recurring, new_until),
            )
            if cur.fetchone() is not None:
                cur.execute(
                    "INSERT INTO premium_access (chat_id, expires_at) VALUES (%s, %s) "
                    "ON CONFLICT (chat_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
                    (chat_id, new_until),
                )
            cur.execute("SELECT expires_at FROM premium_access WHERE chat_id = %s", (chat_id,))
            expires_at = cur.fetchone()[0]
        conn.commit()
    return expires_at


def find_refundable_payment(charge_id: str) -> int:
    """`chat_id` платежа из журнала; громкая ошибка, если платежа нет или он уже возвращён.

    Аргументы:
        charge_id: `telegram_payment_charge_id` платежа.
    """
    rows = fetch_all(
        "SELECT chat_id, refunded_at FROM premium_payments WHERE telegram_payment_charge_id = %s",
        (charge_id,),
        columns=["chat_id", "refunded_at"],
    )
    if not rows["chat_id"]:
        raise LookupError(f"платежа {charge_id} нет в premium_payments")
    if rows["refunded_at"][0] is not None:
        raise LookupError(f"платёж {charge_id} уже возвращён {rows['refunded_at'][0]}")
    return int(rows["chat_id"][0])


def mark_refunded(charge_id: str) -> None:
    """Отмечает платёж возвращённым и откатывает срок премиума на 30 дней — одной транзакцией.

    Срок не уходит в прошлое: `GREATEST(now(), expires_at - 30 дней)`. Вызывается после того,
    как Telegram вернул Stars (`refund`).

    Аргументы:
        charge_id: `telegram_payment_charge_id` платежа.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE premium_payments SET refunded_at = now() "
                "WHERE telegram_payment_charge_id = %s AND refunded_at IS NULL RETURNING chat_id",
                (charge_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"платёж {charge_id} не найден или уже возвращён")
            cur.execute(
                "UPDATE premium_access "
                "SET expires_at = GREATEST(now(), expires_at - make_interval(days => %s)) "
                "WHERE chat_id = %s",
                (PERIOD_DAYS, row[0]),
            )
        conn.commit()


async def refund(bot: Bot, charge_id: str) -> None:
    """Возврат платежа: возврат Stars, запись в БД, затем отмена автопродления.

    Зачем: человек возвращает деньги по обращению в `/paysupport`. Отмена — последней: у уже
    отменённой или истёкшей подписки она может упасть, и это не должно блокировать возврат;
    деньги и БД остаются согласованными, а упавшая отмена — громкая ошибка для человека.

    Аргументы:
        bot: бот с токеном (Bot API).
        charge_id: `telegram_payment_charge_id` платежа.
    """
    chat_id = find_refundable_payment(charge_id)
    await bot.refund_star_payment(user_id=chat_id, telegram_payment_charge_id=charge_id)
    mark_refunded(charge_id)
    await bot.edit_user_star_subscription(
        user_id=chat_id, telegram_payment_charge_id=charge_id, is_canceled=True
    )


async def _run_refund(charge_id: str) -> None:
    """Создаёт бота из `config.TOKEN`, делает возврат и закрывает сессию."""
    bot = Bot(config.TOKEN)
    try:
        await refund(bot, charge_id)
    finally:
        await bot.session.close()


def main() -> None:
    """Точка входа `python premium.py refund <telegram_payment_charge_id>` (контейнер бота на VM)."""
    parser = argparse.ArgumentParser(description="Операции с премиумом")
    parser.add_argument("command", choices=["refund"])
    parser.add_argument("charge_id", help="telegram_payment_charge_id платежа из premium_payments")
    args = parser.parse_args()
    config.validate_env()
    asyncio.run(_run_refund(args.charge_id))
    print(f"Платёж {args.charge_id} возвращён")


if __name__ == "__main__":
    main()
