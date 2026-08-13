CREATE TABLE game_player_stats(
    team_id                     bigint NOT NULL,
    game_id                     bigint NOT NULL,
    player_id                   bigint NOT NULL,
    time_on_ice                 varchar(10),
    assists                     int,
    goals                       int,
    shots                       int,
    hits                        int,
    power_play_goals            int,
    power_play_assists          int,
    penalty_minutes             int,
    face_off_wins               int,
    face_off_taken              int,
    takeaways                   int,
    giveaways                   int,
    short_handed_goals          int,
    short_handed_assists        int,
    blocked                     int,
    plus_minus                  int,
    face_off_pct                double precision,
    UNIQUE (game_id, player_id),
    -- Построчная статистика игрока по конкретной игре. Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

-- Отдельный индекс на game_id не нужен: UNIQUE (game_id, player_id) уже даёт btree с
-- ведущей колонкой game_id — достаточно для FK. Таблицу сейчас не читает ни один модуль.
