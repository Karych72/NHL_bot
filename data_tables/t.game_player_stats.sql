CREATE TABLE game_player_stats(
    team_id                     bigint,
    game_id                     bigint,
    player_id                   bigint,
    time_on_ice                 varchar(10),
    assists                     int,
    goals                       int,
    shots                       int,
    hits                        int,
    power_play_goals            int,
    power_play_assists          int,
    penalty_minutes             int,
    face_off_wins               int,
    face_off_taken              int,
    takeaways                   int,
    giveaways                   int,
    short_handed_goals          int,
    short_handed_assists        int,
    blocked                     int,
    plus_minus                  int,
    even_time_on_ice            varchar(10),
    power_play_time_on_ice      varchar(10),
    short_handed_time_on_ice    varchar(10),
    face_off_pct                DOUBLE PRECISION
);

ALTER TABLE ONLY game_player_stats
    ADD CONSTRAINT game_player_stats_key UNIQUE (game_id, player_id);