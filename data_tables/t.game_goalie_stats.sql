CREATE TABLE game_goalie_stats(
    team_id                             bigint,
    game_id                             bigint,
    player_id                           bigint,
    timeOnIce                           varchar(20),
    assists                             int,
    goals                               int,
    pim                                 int,
    shots                               int,
    saves                               int,
    powerPlaySaves                      int,
    shortHandedSaves                    int,
    evenSaves                           int,
    shortHandedShotsAgainst             int,
    evenShotsAgainst                    int,
    powerPlayShotsAgainst               int,
    decision                            boolean,
    savePercentage                      double PRECISION,
    powerPlaySavePercentage             double PRECISION,
    shortHandedSavePercentage           double PRECISION,
    evenStrengthSavePercentage          double PRECISION
);

ALTER TABLE ONLY game_goalie_stats
    ADD CONSTRAINT game_goalie_stats_key UNIQUE (game_id, player_id);