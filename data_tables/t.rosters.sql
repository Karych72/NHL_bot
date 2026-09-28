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

-- Индекс на season_id не добавлен: основной паттерн запроса к rosters — JOIN по
-- (player_id, season_id) вместе (telegram_bot/bot_messages.py, telegram_bot/queries/
-- get_goals_game.sql, get_goalies_game.sql) — его уже обслуживает
-- PRIMARY KEY (player_id, season_id). WHERE (current_team_id, season_id) = (%s, %s)
-- без player_id тоже есть (team_profile(), Задача 41, Фаза D) — отдельного индекса под
-- него не заводили: ростер команды на сезон — не больше ~30 строк, полный скан таблицы
-- rosters (несколько сезонов × 32 команды) на этот объём укладывается в доли миллисекунды
-- без индекса (Global Constraint 8 — масштабировать под фактический объём, не гипотетический).
