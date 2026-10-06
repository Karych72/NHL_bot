-- Откат миграции 0005: убирает время доставки и отметку об отправке, возвращает timezone.

ALTER TABLE bot_subscriptions DROP COLUMN last_sent_night;
ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_digest_time_kind;
ALTER TABLE bot_subscriptions DROP COLUMN digest_time;
ALTER TABLE bot_subscriptions ADD COLUMN timezone TEXT;
