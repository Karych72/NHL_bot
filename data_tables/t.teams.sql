CREATE TABLE teams(
    team_id             bigint NOT NULL,
    season_id           bigint NOT NULL,
    name                varchar(30),
    division_name       varchar(30),
    arena               varchar(30),
    conference_name     varchar(30),
    abbreviation        varchar(10),
    first_year_of_play  int,
    city                varchar(30),
    active              boolean,
    short_name          varchar(30),
    PRIMARY KEY (team_id, season_id)
);
