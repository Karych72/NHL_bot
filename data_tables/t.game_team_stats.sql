CREATE TABLE game_team_stats(
    goals                       int,
    field                       varchar(20),
    pim                         int,
    shots                       int,
    power_play_percentage       double PRECISION,
    power_play_goals            double PRECISION,
    power_play_opportunities    double PRECISION,
    face_off_win_percentage     double PRECISION,
    blocked                     int,
    takeaways                   int,
    giveaways                   int,
    hits                        int,
    game_id                     bigint,
    team_id                     int,
    fst_period_goals            int,
    snd_period_goals            int,
    trd_period_goals            int
);

ALTER TABLE ONLY game_team_stats
    ADD CONSTRAINT game_team_stats_key UNIQUE (game_id, team_id);