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

-- Основной паттерн — JOIN по (player_id, season_id), его обслуживает PRIMARY KEY. Состав клуба,
-- поиск игрока для ИИ и player_display_name читают rosters по сезону без игрока: после загрузки
-- истории таблица втрое больше, и этот запрос идёт по индексу (миграция 0031, Задача 158).
CREATE INDEX rosters_season_lastname_idx ON rosters (season_id, lastname);
