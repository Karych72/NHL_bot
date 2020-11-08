CREATE TABLE game_team_stats(
    goals                   int,
    pim                     int,
    shots                   int,
    powerPlayPercentage     double PRECISION,
    powerPlayGoals          double PRECISION,
    powerPlayOpportunities  double PRECISION,
    faceOffWinPercentage    double PRECISION,
    blocked                 int,
    takeaways               int,
    giveaways               int,
    hits                    int,
    game_id                 bigint,
    team_id                 int,
    fst_period_goals        int,
    snd_period_goals        int,
    trd_period_goals        int
);

ALTER TABLE ONLY game_team_stats
    ADD CONSTRAINT game_team_stats_key UNIQUE (game_id, team_id);