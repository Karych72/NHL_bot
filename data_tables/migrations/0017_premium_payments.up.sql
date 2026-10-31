-- Миграция 0017: журнал платежей Telegram Stars за премиум (Задача 97).
-- Только миграция, без data_tables/t.*.sql: платежи не восстановить повторной загрузкой (как
-- premium_access). Строку пишет бот при successful_payment, возврат — CLI `premium.py refund`.

-- Один платёж Stars (первая оплата или автопродление). telegram_payment_charge_id уникален:
-- повтор того же события не продлевает премиум дважды. paid_until — срок premium_access
-- сразу после платежа; refunded_at — когда платёж возвращён.
CREATE TABLE premium_payments(
    id                          bigserial PRIMARY KEY,
    chat_id                     bigint NOT NULL,
    telegram_payment_charge_id  text NOT NULL UNIQUE,
    amount_stars                integer NOT NULL,
    is_recurring                boolean NOT NULL,
    paid_until                  timestamptz NOT NULL,
    paid_at                     timestamptz NOT NULL DEFAULT now(),
    refunded_at                 timestamptz
);
