create table goalies_season_stats(
    time_on_ice                         varchar(20),
    ot                                  int,
    shutouts                            int,
    ties                                int,
    wins                                int,
    losses                              int,
    saves                               int,
    power_play_saves                    int,
    short_handed_saves                  int,
    even_saves                          int,
    short_handed_shots                  int,
    even_shots                          int,
    power_play_shots                    int,
    save_percentage                     double PRECISION,
    goal_against_average                double PRECISION,
    games                               int,
    games_started                       int,
    shots_against                       int,
    goals_against                       int,
    time_on_ice_per_game                varchar(10),
    power_play_save_percentage          double PRECISION,
    short_handed_save_percentage        double PRECISION,
    even_strength_save_percentage       double PRECISION,
    player_id                           int
);