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
    PRIMARY KEY (player_id, season_id),
    -- current_team_id — команда игрока в этом сезоне (teams.PK = team_id+season_id);
    -- nullable — свободный агент/без текущей команды. Проверено на живой БД: 0 сирот (2026-08-12).
    FOREIGN KEY (current_team_id, season_id) REFERENCES teams (team_id, season_id)
);

-- Индекс на season_id не добавлен: единственный фактический паттерн запроса к rosters —
-- JOIN по (player_id, season_id) вместе (telegram_bot/bot_messages.py:602-603,
-- telegram_bot/queries/get_goals_game.sql:36-41, get_goalies_game.sql:16-17) — его уже
-- обслуживает PRIMARY KEY (player_id, season_id). Отдельного WHERE season_id = %s
-- по rosters в коде нет.
