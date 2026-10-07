-- Миграция 0006: подписка на страну (Задача 61), kind = 'country_players'.
-- country — трёхбуквенный код страны (rosters.nationality), есть только у country_players.
-- У подписки на страну, как у дайджеста, есть время доставки (digest_time, МСК): общее
-- на все страны чата — его хранит приложение, а не схема. Одна строка на пару
-- (chat_id, country); last_sent_night отмечается по каждой стране отдельно.

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_kind_check;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_kind_check
    CHECK (kind IN ('morning_digest', 'team_scores', 'country_players'));

ALTER TABLE bot_subscriptions ADD COLUMN country TEXT;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_country_kind
    CHECK ((kind = 'country_players') = (country IS NOT NULL));

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_digest_time_kind;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_digest_time_kind
    CHECK ((kind IN ('morning_digest', 'country_players')) = (digest_time IS NOT NULL));

CREATE UNIQUE INDEX bot_subscriptions_country_unique
    ON bot_subscriptions (chat_id, country)
    WHERE kind = 'country_players';
