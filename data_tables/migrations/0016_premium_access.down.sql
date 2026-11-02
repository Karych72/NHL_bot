-- Откат миграции 0016: удаляет премиум и возвращает список «текущих» пользователей так же,
-- как его заполнила миграция 0012, — по подпискам на момент отката.

DROP TABLE premium_access;
CREATE TABLE ai_existing_users(
    chat_id             bigint PRIMARY KEY
);
INSERT INTO ai_existing_users (chat_id) SELECT DISTINCT chat_id FROM bot_subscriptions;
