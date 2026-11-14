-- Миграция 0024: отметки публикаций в Telegram-канал (Задача 118) — матч × вид поста
-- (превью до матча, итог после). Отдельно от отметок чатов: канал с подписками не пересекается.
-- Только миграция, без data_tables/t.*.sql: отметки не восстановить повторной загрузкой.

CREATE TABLE bot_channel_posts (
    game_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('preview', 'result')),
    PRIMARY KEY (game_id, kind)
);
