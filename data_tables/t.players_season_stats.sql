CREATE TABLE players_season_stats(
    time_on_ice                             varchar(20),
    assists                                 int,
    goals                                   int,
    pim                                     int,
    shots                                   int,
    games                                   int,
    hits                                    int,
    power_play_goals                        int,
    power_play_points                       int,
    power_play_time_on_ice                  varchar(20),
    even_time_on_ice                        varchar(20),
    penalty_minutes                         int,
    face_off_pct                            double precision,
    shot_pct                                double precision,
    game_winning_goals                      int,
    over_time_goals                         int,
    short_handed_goals                      int,
    short_handed_points                     int,
    short_handed_time_on_ice                varchar(20),
    blocked                                 int,
    plus_minus                              int,
    points                                  int,
    shifts                                  int,
    time_on_ice_per_game                    varchar(20),
    even_time_on_ice_per_game               varchar(20),
    short_handed_time_on_ice_per_game       varchar(20),
    power_play_time_on_ice_per_game         varchar(20),
    oz_faceoff_pct                          double precision,
    dz_faceoff_pct                          double precision,
    nz_faceoff_pct                          double precision,
    shootout_goals                          int,
    shootout_shots                          int,
    shootout_gd_goals                       int,
    shootout_pct                            double precision,
    player_id                               int NOT NULL,
    season_id                               bigint NOT NULL,
    PRIMARY KEY (player_id, season_id),
    -- Сезонная статистика существует только для игрока, известного в этом сезоне
    -- (rosters.PK = player_id+season_id). Проверено на живой БД: 0 сирот.
    FOREIGN KEY (player_id, season_id) REFERENCES rosters (player_id, season_id)
);

-- Обслуживает лидерборды WHERE pl.season_id = %s ORDER BY <col> DESC LIMIT/OFFSET
-- (table_name="players_season_stats" — см. telegram_bot/leaderboard_specs.py:7-18,48
-- и telegram_bot/stats_handlers.py:271-292,308):
--   telegram_bot/bot_messages.py:598-608 (player_stats_with_count)
-- PRIMARY KEY(player_id, season_id) начинается с player_id и этот фильтр не обслуживает.
CREATE INDEX idx_players_season_stats_season ON players_season_stats (season_id);
