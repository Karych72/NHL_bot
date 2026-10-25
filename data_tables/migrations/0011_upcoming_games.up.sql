-- Миграция 0011: подписка «Предстоящие матчи» (Задача 78), kind = 'upcoming_games' — превью
-- матчей дня, с 15:30 МСК по одному раз в полчаса. Одна строка на чат, как у дайджеста.
-- Превью уходит по матчу, поэтому отметка «матч отправлен» — та же, что у страны (миграция 0009): таблица
-- становится общей для рассылок по матчу и теряет «country» в имени.

ALTER TABLE bot_subscriptions DROP CONSTRAINT bot_subscriptions_kind_check;
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_kind_check
    CHECK (kind IN ('morning_digest', 'team_scores', 'country_players', 'upcoming_games'));

CREATE UNIQUE INDEX bot_subscriptions_upcoming_unique
    ON bot_subscriptions (chat_id)
    WHERE kind = 'upcoming_games';

ALTER TABLE bot_country_game_sent RENAME TO bot_game_sent;
ALTER TABLE bot_game_sent RENAME CONSTRAINT bot_country_game_sent_pkey TO bot_game_sent_pkey;
ALTER TABLE bot_game_sent RENAME CONSTRAINT bot_country_game_sent_subscription_id_fkey
    TO bot_game_sent_subscription_id_fkey;
