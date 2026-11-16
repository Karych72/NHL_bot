-- Откат миграции 0027: удаляет подписки на игроков и их историю.

CREATE OR REPLACE FUNCTION bot_subscription_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR OLD.active IS DISTINCT FROM NEW.active THEN
        INSERT INTO bot_subscription_events (subscription_id, chat_id, kind, team_id, country, active)
        VALUES (NEW.id, NEW.chat_id, NEW.kind, NEW.team_id, NEW.country, NEW.active);
    END IF;
    RETURN NULL;
END
$$;

DELETE FROM bot_subscription_events WHERE kind = 'player_scores';
ALTER TABLE bot_subscription_events DROP COLUMN player_id;

DELETE FROM bot_subscriptions WHERE kind = 'player_scores';
DROP INDEX bot_subscriptions_player_unique;
ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_player_kind;
ALTER TABLE bot_subscriptions DROP COLUMN player_id;

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_kind_check;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_kind_check
    CHECK (kind IN ('morning_digest', 'team_scores', 'country_players', 'upcoming_games'));
