-- Миграция 0013: кэш клипов голов (Задача 87). Клип грузится в Telegram один раз — при первой
-- отправке (рассылка или кнопка видео в карточке), дальше уходит по file_id без скачивания с
-- NHL и ffmpeg. file_id свой у каждого бота: у dev- и прод-бота разные базы (Задача 80).
-- Только миграция, без data_tables/t.*.sql: кэш не восстановить повторной загрузкой.
CREATE TABLE bot_goal_clips (
    game_id     bigint NOT NULL,
    event_id    bigint NOT NULL,
    file_id     text NOT NULL,
    -- HTML-подпись к видео (video_replay.goal_caption): кто забил, с чьих передач, когда.
    caption     text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, event_id)
);
