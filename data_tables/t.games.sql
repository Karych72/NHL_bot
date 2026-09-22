CREATE TABLE games(
    game_id                 bigint NOT NULL,
    day                     date,
    home_team_id            bigint,
    away_team_id            bigint,
    winner_id               bigint,
    is_overtime             boolean,
    is_shootouts            boolean,
    season                  varchar(10),
    season_id               bigint NOT NULL,
    PRIMARY KEY (game_id),
    -- Команда конкретного сезона: teams.PRIMARY KEY = (team_id, season_id).
    -- Колонки nullable — значение проставляется по мере загрузки счёта/победителя,
    -- проверено на живой БД (6559 игр, 5 сезонов): 0 сирот на 2026-08-12.
    FOREIGN KEY (home_team_id, season_id) REFERENCES teams (team_id, season_id),
    FOREIGN KEY (away_team_id, season_id) REFERENCES teams (team_id, season_id),
    FOREIGN KEY (winner_id, season_id) REFERENCES teams (team_id, season_id)
);

-- Обслуживает фактические паттерны запросов бота/пайплайна, все фильтруют по season_id
-- (первая колонка индекса), часть — ещё и по day:
--   telegram_bot/bot_messages.py:506-512  (_recent_team_outcomes: ORDER BY day DESC LIMIT
--                                          n/100 — общий запрос _last_n_form_record()
--                                          и _current_streak(), Задача 24)
--   telegram_bot/bot_messages.py:591-594  (_h2h_season_wins: WHERE season_id = %s)
--   telegram_bot/bot_messages.py:1020 (_standings_as_of_day: max(day)::text AS d WHERE season_id = %s)
--   telegram_bot/bot_messages.py:1382 (day_digest: max(day) AS day WHERE season_id = %s)
--   telegram_bot/bot_messages.py:1395 (day_digest: WHERE day = %s AND season_id = %s)
--   telegram_bot/push_digest_job.py:50-51 (WHERE season_id = %s AND day = %s::date AND ...)
--   modeling/dataset_builder/base.py:87-101 (load_target_games: FROM games g ... WHERE
--                                            g.day IS NOT NULL ... season_id IN (...) AND
--                                            day BETWEEN ... ORDER BY g.day, g.game_id)
CREATE INDEX idx_games_season_day ON games (season_id, day);
