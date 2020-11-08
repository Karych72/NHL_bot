CREATE TABLE games(
    game_id                 bigint,
    day                     date,
    home_team_id            bigint,
    away_team_id            bigint,
    winner_id               bigint,
    is_overtime             boolean,
    is_shootouts            boolean,
    season                  varchar(10)
);

ALTER TABLE ONLY games
    ADD CONSTRAINT games_key UNIQUE (game_id);