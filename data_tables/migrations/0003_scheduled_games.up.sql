-- Миграция 0003: таблица scheduled_games (будущие игры для predict-датасета).
-- Определение зеркалирует data_tables/t.scheduled_games.sql — db-reset/db-tables
-- пересоздают таблицу из DDL в обход миграций, поэтому обе копии должны совпадать.
-- IF NOT EXISTS: на уже поднятой из DDL схеме повторный db-migrate не должен падать.

CREATE TABLE IF NOT EXISTS scheduled_games(
    game_id                 bigint NOT NULL,
    day                     date NOT NULL,
    home_team_id            bigint NOT NULL,
    away_team_id            bigint NOT NULL,
    season_id               bigint NOT NULL,
    PRIMARY KEY (game_id),
    FOREIGN KEY (home_team_id, season_id) REFERENCES teams (team_id, season_id),
    FOREIGN KEY (away_team_id, season_id) REFERENCES teams (team_id, season_id)
);
