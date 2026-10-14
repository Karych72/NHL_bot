-- Миграция 0008: таблица shot_attempts (попытки бросков из play-by-play, Задача 69A).
-- Определение зеркалирует data_tables/t.shot_attempts.sql — db-reset/db-tables
-- пересоздают таблицу из DDL в обход миграций, поэтому обе копии должны совпадать.
-- IF NOT EXISTS: на уже поднятой из DDL схеме повторный db-migrate не должен падать.

CREATE TABLE IF NOT EXISTS shot_attempts(
    game_id                     bigint NOT NULL,
    event_id                    int NOT NULL,
    sort_order                  int,
    period                      int,
    period_type                 varchar(4),
    time_in_period              varchar(20),
    team_id                     int,
    event_type                  varchar(20),
    x_coord                     int,
    y_coord                     int,
    zone_code                   varchar(2),
    shot_type                   varchar(20),
    situation_code              varchar(4),
    home_team_defending_side    varchar(8),
    shooter_id                  bigint,
    goalie_in_net_id            bigint,
    PRIMARY KEY (game_id, event_id),
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);
