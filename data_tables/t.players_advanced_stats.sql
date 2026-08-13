CREATE TABLE players_advanced_stats(
    player_id                   int NOT NULL,
    season_id                   bigint NOT NULL,
    sat_pct                     double precision,
    usat_pct                    double precision,
    goals_pct                   double precision,
    oz_start_pct                double precision,
    dz_start_pct                double precision,
    nz_start_pct                double precision,
    on_ice_shooting_pct         double precision,
    ev_goals_for                int,
    ev_goals_against            int,
    ev_goals_for_pct            double precision,
    pp_goals_for                int,
    pp_goals_against            int,
    sh_goals_for                int,
    sh_goals_against            int,
    PRIMARY KEY (player_id, season_id),
    -- См. players_season_stats.sql — тот же контракт (rosters.PK = player_id+season_id).
    -- Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (player_id, season_id) REFERENCES rosters (player_id, season_id)
);

-- Обслуживает лидерборды WHERE pl.season_id = %s (table_name="players_advanced_stats" —
-- см. telegram_bot/leaderboard_specs.py:19-22,44-47 и
-- telegram_bot/stats_handlers.py:296-305):
--   telegram_bot/bot_messages.py:598-608 (player_stats_with_count)
CREATE INDEX idx_players_advanced_stats_season ON players_advanced_stats (season_id);
