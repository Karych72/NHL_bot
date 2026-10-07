-- Миграция 0007: подписки без времени доставки (Задача 63).
-- Все подписки приходят одной рассылкой, как только загружены все матчи ночи (не позже
-- 11:00 МСК), поэтому digest_time (миграции 0005 и 0006) и его CHECK больше не нужны.

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_digest_time_kind;
ALTER TABLE bot_subscriptions DROP COLUMN digest_time;
