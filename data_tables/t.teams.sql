CREATE TABLE teams(
    team_id           bigint,
    name              varchar(30),
    tricode           varchar(5),
    division_id       int,
    division_name     varchar(30),
    conference_id     int,
    conference_name   varchar(30),
    short_name        varchar(30),
    active            boolean
);