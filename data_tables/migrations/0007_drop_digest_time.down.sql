-- Откат миграции 0007: возвращает digest_time (11:00 МСК у morning_digest и country_players)
-- и его CHECK в том виде, как после миграции 0006.

ALTER TABLE bot_subscriptions ADD COLUMN digest_time TIME;
UPDATE bot_subscriptions SET digest_time = '11:00'
    WHERE kind IN ('morning_digest', 'country_players');
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_digest_time_kind
    CHECK ((kind IN ('morning_digest', 'country_players')) = (digest_time IS NOT NULL));
