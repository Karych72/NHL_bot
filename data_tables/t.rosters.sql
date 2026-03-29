CREATE TABLE rosters(
    player_id               bigint NOT NULL,
    season_id               bigint NOT NULL,
    name                    varchar(50),
    position                varchar(5),
    jersey_number           int,
    currentage              int,
    lastname                varchar(50),
    nationality             varchar(10),
    captain                 boolean,
    alternate_captain       boolean,
    rookie                  boolean,
    abbreviation            varchar(10),
    current_team_id         int,
    PRIMARY KEY (player_id, season_id)
);
