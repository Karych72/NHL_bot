CREATE TABLE game_goalie_stats(
    team_id                             bigint NOT NULL,
    game_id                             bigint NOT NULL,
    player_id                           bigint NOT NULL,
    timeonice                           varchar(20),
    assists                             int,
    goals                               int,
    pim                                 int,
    shots                               int,
    saves                               int,
    power_play_saves                    int,
    short_handed_saves                  int,
    even_saves                          int,
    short_handed_shots_against          int,
    even_shots_against                  int,
    power_play_shots_against            int,
    decision                            boolean,
    save_percentage                     double precision,
    power_play_save_percentage          double precision,
    short_handed_save_percentage        double precision,
    even_strength_save_percentage       double precision,
    UNIQUE (game_id, player_id),
    -- Построчная статистика вратаря по конкретной игре. Проверено на живой БД: 0 сирот.
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

-- Отдельный индекс на game_id не нужен: UNIQUE (game_id, player_id) уже даёт btree с
-- ведущей колонкой game_id, чего достаточно для WHERE game_id = %s
-- (telegram_bot/queries/get_goalies_game.sql:13-18) и для этого FK.
