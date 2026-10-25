-- Откат миграции 0011: подписки «Предстоящие матчи» удаляются (их отметки — каскадом),
-- таблица отметок возвращает имя bot_country_game_sent.

DELETE FROM bot_subscriptions WHERE kind = 'upcoming_games';

ALTER TABLE bot_game_sent RENAME CONSTRAINT bot_game_sent_subscription_id_fkey
    TO bot_country_game_sent_subscription_id_fkey;
ALTER TABLE bot_game_sent RENAME CONSTRAINT bot_game_sent_pkey TO bot_country_game_sent_pkey;
ALTER TABLE bot_game_sent RENAME TO bot_country_game_sent;

DROP INDEX bot_subscriptions_upcoming_unique;

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_kind_check;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_kind_check
    CHECK (kind IN ('morning_digest', 'team_scores', 'country_players'));
