-- Откат миграции 0006: подписки на страны удаляются, схема возвращается к виду после 0005.

DELETE FROM bot_subscriptions WHERE kind = 'country_players';

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_digest_time_kind;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_digest_time_kind
    CHECK ((kind = 'morning_digest') = (digest_time IS NOT NULL));
ALTER TABLE bot_subscriptions DROP COLUMN country;
ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_kind_check;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_kind_check
    CHECK (kind IN ('morning_digest', 'team_scores'));
