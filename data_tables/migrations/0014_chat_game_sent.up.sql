-- Миграция 0014: итог матча приходит чату ровно один раз, сколько бы подписок на него ни вело
-- (Задача 85). Отметка «итог матча ушёл» — по паре (чат, матч), общая для сообщения по матчу
-- (команда, страна) и дайджеста; bot_game_sent остаётся только у превью (Задача 78).
-- Только миграция, без data_tables/t.*.sql: отметки не восстановить повторной загрузкой.

CREATE TABLE bot_chat_game_sent (
    chat_id BIGINT NOT NULL,
    game_id BIGINT NOT NULL,
    PRIMARY KEY (chat_id, game_id)
);

-- Уже разосланное прежним кодом — отметить, иначе первый прогон после деплоя повторил бы ночь:
-- матчи страны (bot_game_sent), ночи команды и дайджеста (last_sent_night).
INSERT INTO bot_chat_game_sent (chat_id, game_id)
SELECT s.chat_id, gs.game_id
FROM bot_game_sent gs
JOIN bot_subscriptions s ON s.id = gs.subscription_id
WHERE s.kind = 'country_players'
UNION
SELECT s.chat_id, g.game_id
FROM bot_subscriptions s
JOIN games g ON g.day = s.last_sent_night AND s.team_id IN (g.home_team_id, g.away_team_id)
WHERE s.kind = 'team_scores'
UNION
SELECT s.chat_id, g.game_id
FROM bot_subscriptions s
JOIN games g ON g.day = s.last_sent_night
WHERE s.kind = 'morning_digest';

DELETE FROM bot_game_sent gs
USING bot_subscriptions s
WHERE s.id = gs.subscription_id AND s.kind = 'country_players';
