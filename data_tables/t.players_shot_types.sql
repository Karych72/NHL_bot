CREATE TABLE players_shot_types(
    player_id                   int NOT NULL,
    season_id                   bigint NOT NULL,
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
    PRIMARY KEY (player_id, season_id)
);
