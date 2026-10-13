CREATE TABLE shot_attempts(
    game_id                     bigint NOT NULL,
    event_id                    int NOT NULL,
    sort_order                  int,
    period                      int,
    period_type                 varchar(4),
    time_in_period              varchar(20),
    team_id                     int,
    event_type                  varchar(20),
    x_coord                     int,
    y_coord                     int,
    zone_code                   varchar(2),
    shot_type                   varchar(20),
    situation_code              varchar(4),
    home_team_defending_side    varchar(8),
    shooter_id                  bigint,
    goalie_in_net_id            bigint,
    PRIMARY KEY (game_id, event_id),
    -- Каждая попытка броска игры из play-by-play (shot-on-goal / missed-shot / blocked-shot / goal);
    -- буллитная серия (periodType SO) не пишется. team_id — всегда БРОСАВШАЯ команда
    -- (у blocked-shot eventOwnerTeamId тоже бросавшая — проверено на реальном payload).
    -- Отсутствующее поле API — NULL (у blocked-shot нет shot_type; координаты бывают пропущены).
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

-- Отдельный индекс на game_id не нужен: PRIMARY KEY (game_id, event_id) уже даёт btree
-- с ведущей колонкой game_id (и для WHERE game_id = ..., и для этого FK).
-- FK на team_id/shooter_id/goalie_in_net_id не ставится: пер-игровые таблицы проекта
-- их не ставят (нет season_id; игрок может отсутствовать в rosters сезона).
