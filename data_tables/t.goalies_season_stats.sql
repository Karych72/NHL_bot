CREATE TABLE goalies_season_stats(
    time_on_ice                         varchar(20),
    ot                                  int,
    shutouts                            int,
    ties                                int,
    wins                                int,
    losses                              int,
    saves                               int,
    power_play_saves                    int,
    short_handed_saves                  int,
    even_saves                          int,
    short_handed_shots                  int,
    even_shots                          int,
    power_play_shots                    int,
    save_percentage                     double precision,
    goal_against_average                double precision,
    games                               int,
    games_started                       int,
    shots_against                       int,
    goals_against                       int,
    time_on_ice_per_game                varchar(10),
    power_play_save_percentage          double precision,
    short_handed_save_percentage        double precision,
    even_strength_save_percentage       double precision,
    player_id                           int NOT NULL,
    season_id                           bigint NOT NULL,
    -- См. players_season_stats.sql — тот же тип игры (миграция 0022, Задача 103).
    game_type                           smallint NOT NULL
        CONSTRAINT goalies_season_stats_game_type_check CHECK (game_type IN (2, 3)),
    PRIMARY KEY (player_id, season_id, game_type),
    -- См. players_season_stats.sql — тот же контракт (rosters.PK = player_id+season_id).
    -- Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (player_id, season_id) REFERENCES rosters (player_id, season_id)
);

-- Обслуживает лидерборды вратарей WHERE pl.season_id = %s
-- (table_name="goalies_season_stats" — см. telegram_bot/leaderboard_specs.py:30-32 и
-- telegram_bot/stats_handlers.py:314-320):
--   telegram_bot/bot_messages.py:598-608 (player_stats_with_count)
CREATE INDEX idx_goalies_season_stats_season ON goalies_season_stats (season_id, game_type);
