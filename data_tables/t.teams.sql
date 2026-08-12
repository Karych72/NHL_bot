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

-- Обслуживает выборку "команда сезона по аббревиатуре" (WHERE season_id = %s):
--   telegram_bot/bot_messages.py:327-329   (_team_id_for_abbrev)
--   telegram_bot/bot_messages.py:946-948   (season_team_abbrev_help_text)
--   telegram_bot/subscription_repo.py:44-46 (resolve_team_id_by_abbrev)
CREATE INDEX idx_teams_season ON teams (season_id);
