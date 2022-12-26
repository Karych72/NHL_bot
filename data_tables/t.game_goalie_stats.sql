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
    power_play_saves                    int,
    short_handed_saves                  int,
    even_saves                          int,
    short_handed_shots_against          int,
    even_shots_against                  int,
    power_play_shots_against            int,
    decision                            boolean,
    save_percentage                     double PRECISION,
    power_play_save_percentage          double PRECISION,
    short_handed_save_percentage        double PRECISION,
    even_strength_save_percentage       double PRECISION
);

ALTER TABLE ONLY game_goalie_stats
    ADD CONSTRAINT game_goalie_stats_key UNIQUE (game_id, player_id);