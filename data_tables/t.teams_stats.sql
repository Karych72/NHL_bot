CREATE TABLE teams_stats(
    team_id                        int NOT NULL,
    season_id                      bigint NOT NULL,
    games_played                   int,
    wins                           int,
    losses                         int,
    ot                             int,
    points                         int,
    procent_points                 double precision,
    goals_per_game                 double precision,
    goals_against_per_game         double precision,
    power_play_percentage          double precision,
    power_play_goals               int,
    power_play_goals_against       int,
    power_play_opportunities       int,
    penalty_kill_percentage        double precision,
    shots_per_game                 double precision,
    shots_allowed                  double precision,
    face_off_win_percentage        double precision,
    PRIMARY KEY (team_id, season_id),
    -- Агрегат команды за сезон существует только для команды, известной в этом
    -- сезоне (teams.PK = team_id+season_id). Проверено на живой БД: 0 сирот.
    FOREIGN KEY (team_id, season_id) REFERENCES teams (team_id, season_id)
);

-- Обслуживает WHERE ts.season_id = %s (турнирная таблица / сравнение команд / лидерборд):
--   telegram_bot/bot_messages.py:424-431  (matchup_season_preview, INNER JOIN teams)
--   telegram_bot/bot_messages.py:909-916  (team_table)
--   telegram_bot/bot_messages.py:990-997  (team_stats_with_count)
CREATE INDEX idx_teams_stats_season ON teams_stats (season_id);
