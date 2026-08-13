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
    -- сезоне (teams.PK = team_id+season_id). Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (team_id, season_id) REFERENCES teams (team_id, season_id)
);
