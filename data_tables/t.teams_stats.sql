CREATE TABLE teams_stats(
     team_id                        int,
     games_played                   int,
     wins                           int,
     losses                         int,
     ot                             int,
     points                         int,
     procent_points                 DOUBLE PRECISION,
     goals_per_game                 DOUBLE PRECISION,
     goals_against_per_game         DOUBLE PRECISION,
     power_play_percentage          DOUBLE PRECISION,
     power_play_goals               int,
     power_play_goals_against       int,
     power_play_opportunities       int,
     penalty_kill_percentage        DOUBLE PRECISION,
     shots_per_game                 DOUBLE PRECISION,
     shots_allowed                  DOUBLE PRECISION,
     face_off_win_percentage        DOUBLE PRECISION
);