-- Серии плей-офф из games (Задача 105, миграция 0023): без своей загрузки, текущий плей-офф
-- обновляется вместе с games ежедневным sync. Раунд и номер серии — из game_id
-- (2025030123: сезон 2025, тип 03, раунд 01, серия 2, игра 3). Пара упорядочена:
-- team_a_id — меньший id, team_b_id — больший. winner_id — команда с 4 победами, NULL, пока
-- серия идёт; обладатель Кубка — winner_id серии round = 4.
--
-- Файл идемпотентен и нужен в двух местах: его подключает миграция 0023 (\ir) и применяет
-- `make db-tables` — db-drop уносит представление CASCADE-ом от games, а применённую миграцию
-- повторно никто не запускает. Комментарии — только над CREATE: ai_stats._schema() отдаёт
-- модели текст представления до первой «;».
CREATE OR REPLACE VIEW playoff_series AS
SELECT
    season_id, round, series_number, team_a_id, team_b_id, wins_a, wins_b, games_played,
    CASE WHEN wins_a = 4 THEN team_a_id WHEN wins_b = 4 THEN team_b_id END AS winner_id,
    first_day, last_day
FROM (
    SELECT
        season_id,
        (game_id / 100 % 100)::int AS round,
        (game_id / 10 % 10)::int AS series_number,
        LEAST(home_team_id, away_team_id) AS team_a_id,
        GREATEST(home_team_id, away_team_id) AS team_b_id,
        count(*) FILTER (WHERE winner_id = LEAST(home_team_id, away_team_id))::int AS wins_a,
        count(*) FILTER (WHERE winner_id = GREATEST(home_team_id, away_team_id))::int AS wins_b,
        count(*)::int AS games_played,
        min(day) AS first_day,
        max(day) AS last_day
    FROM games
    WHERE game_type = 3
    GROUP BY 1, 2, 3, 4, 5
) AS s;
