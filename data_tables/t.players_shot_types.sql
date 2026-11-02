CREATE TABLE players_shot_types(
    player_id                   int NOT NULL,
    season_id                   bigint NOT NULL,
    -- См. players_season_stats.sql — тот же тип игры (миграция 0022, Задача 103).
    game_type                   smallint NOT NULL
        CONSTRAINT players_shot_types_game_type_check CHECK (game_type IN (2, 3)),
    goals_wrist                 int,
    shots_wrist                 int,
    goals_slap                  int,
    shots_slap                  int,
    goals_snap                  int,
    shots_snap                  int,
    goals_backhand              int,
    shots_backhand              int,
    goals_tip_in                int,
    shots_tip_in                int,
    goals_deflected             int,
    shots_deflected             int,
    goals_wrap_around           int,
    shots_wrap_around           int,
    PRIMARY KEY (player_id, season_id, game_type),
    -- См. players_season_stats.sql — тот же контракт (rosters.PK = player_id+season_id).
    -- Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (player_id, season_id) REFERENCES rosters (player_id, season_id)
);

-- Обслуживает лидерборды WHERE pl.season_id = %s (table_name="players_shot_types" —
-- см. telegram_bot/leaderboard_specs.py:23-29,53-59 и
-- telegram_bot/stats_handlers.py:332-350):
--   telegram_bot/bot_messages.py:598-608 (player_stats_with_count)
CREATE INDEX idx_players_shot_types_season ON players_shot_types (season_id, game_type);
