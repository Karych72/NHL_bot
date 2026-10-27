-- Откат миграции 0014: отметки чата возвращаются в bot_game_sent каждой его подписке на страну —
-- прежний код не повторит уже ушедшие матчи; ночи дайджеста хранит last_sent_night. Ограничение:
-- last_sent_night у team_scores новый код не пишет — откат посреди ночи повторит карточки команд.

INSERT INTO bot_game_sent (subscription_id, game_id)
SELECT s.id, c.game_id
FROM bot_chat_game_sent c
JOIN bot_subscriptions s ON s.chat_id = c.chat_id AND s.kind = 'country_players'
ON CONFLICT DO NOTHING;

DROP TABLE bot_chat_game_sent;
