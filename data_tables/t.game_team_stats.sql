CREATE TABLE game_team_stats(
    goals                       int,
    field                       varchar(20),
    pim                         int,
    shots                       int,
    power_play_percentage       double precision,
    power_play_goals            double precision,
    power_play_opportunities    double precision,
    face_off_win_percentage     double precision,
    blocked                     int,
    takeaways                   int,
    giveaways                   int,
    hits                        int,
    game_id                     bigint NOT NULL,
    team_id                     int NOT NULL,
    fst_period_goals            int,
    snd_period_goals            int,
    trd_period_goals            int,
    UNIQUE (game_id, team_id),
    -- Построчная статистика команды по конкретной игре. Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

-- Отдельный индекс на game_id не нужен: UNIQUE (game_id, team_id) уже даёт btree с
-- ведущей колонкой game_id, чего достаточно для WHERE game_id = %s
-- (telegram_bot/queries/get_game_stats.sql:16-21,
--  modeling/dataset_builder/base.py:88-93,129-130) и для этого FK.
