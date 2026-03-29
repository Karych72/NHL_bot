CREATE TABLE games(
    game_id                 bigint NOT NULL,
    day                     date,
    home_team_id            bigint,
    away_team_id            bigint,
    winner_id               bigint,
    is_overtime             boolean,
    is_shootouts            boolean,
    season                  varchar(10),
    season_id               bigint NOT NULL,
    PRIMARY KEY (game_id)
);
