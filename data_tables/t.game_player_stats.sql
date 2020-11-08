CREATE TABLE game_player_stats(
    team_id               bigint,
    game_id               bigint,
    player_id             bigint,
    timeOnIce             varchar(10),
    assists               int,
    goals                 int,
    shots                 int,
    hits                  int,
    powerPlayGoals        int,
    powerPlayAssists      int,
    penaltyMinutes        int,
    faceOffWins           int,
    faceoffTaken          int,
    takeaways             int,
    giveaways             int,
    shortHandedGoals      int,
    shortHandedAssists    int,
    blocked               int,
    plusMinus             int,
    evenTimeOnIce         varchar(10),
    powerPlayTimeOnIce    varchar(10),
    shortHandedTimeOnIce  varchar(10),
    faceOffPct            DOUBLE PRECISION
);

ALTER TABLE ONLY game_player_stats
    ADD CONSTRAINT game_player_stats_key UNIQUE (game_id, player_id);