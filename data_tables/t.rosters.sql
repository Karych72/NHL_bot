create table rosters(
    player_id               bigint,
    name                    varchar(50),
    position                varchar(5),
    jersey_number           int,
    currentAge              int,
    lastName                varchar(50),
    nationality             varchar(10),
    captain                 boolean,
    alternate_captain       boolean,
    rookie                  boolean,
    abbreviation            varchar(10),
    current_team_id         int
);