-- Миграция 0002: таблица game_three_stars (три звезды матча).
-- Определение зеркалирует data_tables/t.game_three_stars.sql — db-reset/db-tables
-- пересоздают таблицу из DDL в обход миграций, поэтому обе копии должны совпадать.
-- IF NOT EXISTS: на уже поднятой из DDL схеме повторный db-migrate не должен падать.

CREATE TABLE IF NOT EXISTS game_three_stars(
    game_id     bigint NOT NULL,
    star        int NOT NULL,
    player_id   bigint NOT NULL,
    team_id     bigint NOT NULL,
    UNIQUE (game_id, star),
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);
