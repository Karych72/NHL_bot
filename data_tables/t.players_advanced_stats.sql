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
    PRIMARY KEY (player_id, season_id)
);
