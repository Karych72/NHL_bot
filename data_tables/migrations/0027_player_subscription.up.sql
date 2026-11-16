-- Миграция 0027: подписка на игрока (Задача 122), kind = 'player_scores'.
-- player_id — игрок (rosters.player_id), есть только у player_scores; подписка на игрока, а не
-- на клуб, поэтому переход в другой клуб её не ломает. Одна строка на пару (chat_id, player_id).
-- Журнал bot_subscription_events (миграция 0017) пишет и игрока.

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_kind_check;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_kind_check
    CHECK (kind IN ('morning_digest', 'team_scores', 'country_players', 'upcoming_games', 'player_scores'));

ALTER TABLE bot_subscriptions ADD COLUMN player_id BIGINT;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_player_kind
    CHECK ((kind = 'player_scores') = (player_id IS NOT NULL));

CREATE UNIQUE INDEX bot_subscriptions_player_unique
    ON bot_subscriptions (chat_id, player_id)
    WHERE kind = 'player_scores';

ALTER TABLE bot_subscription_events ADD COLUMN player_id BIGINT;

CREATE OR REPLACE FUNCTION bot_subscription_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR OLD.active IS DISTINCT FROM NEW.active THEN
        INSERT INTO bot_subscription_events (subscription_id, chat_id, kind, team_id, country, player_id, active)
        VALUES (NEW.id, NEW.chat_id, NEW.kind, NEW.team_id, NEW.country, NEW.player_id, NEW.active);
    END IF;
    RETURN NULL;
END
$$;
