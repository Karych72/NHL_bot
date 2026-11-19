-- Серии плей-офф из games (Задача 105, миграция 0023; старые форматы — Задача 157, миграция 0033):
-- без своей загрузки, текущий плей-офф обновляется вместе с games ежедневным sync. Раунд и номер
-- серии — из game_id (2025030123: сезон 2025, тип 03, раунд 01, серия 2, игра 3). Пара
-- упорядочена: team_a_id — меньший id, team_b_id — больший. goals_a / goals_b — сумма голов
-- каждой в серии.
--
-- winner_id. В идущем сезоне (самый новый сезон в games) — команда с 4 победами, NULL, пока серия
-- идёт. В завершённых сезонах серия закрыта целиком, форматы разные (до 7 побед, до 1-й
-- половины 1980-х — 3 и 5 побед, 1917–1930-е — «по сумме голов»), поэтому победитель — тот, у кого
-- больше побед, а при равенстве побед — больше голов; NULL, если и голы равны (финал 1919 года
-- не доигран). Обладатель Кубка завершённого сезона — winner_id серии самого большого round
-- (финал — 4-й раунд только с 1980-х).
--
-- Файл идемпотентен и нужен в двух местах: его подключает миграция (\ir) и применяет
-- `make db-tables` — db-drop уносит представление CASCADE-ом от games, а применённую миграцию
-- повторно никто не запускает. Комментарии — только над CREATE: ai_stats._schema() отдаёт
-- модели текст представления до первой «;».
CREATE OR REPLACE VIEW playoff_series AS
SELECT
    season_id, round, series_number, team_a_id, team_b_id, wins_a, wins_b, games_played,
    CASE
        WHEN season_id < (SELECT max(season_id) FROM games) THEN
            CASE
                WHEN wins_a > wins_b OR (wins_a = wins_b AND goals_a > goals_b) THEN team_a_id
                WHEN wins_b > wins_a OR (wins_a = wins_b AND goals_b > goals_a) THEN team_b_id
            END
        WHEN wins_a = 4 THEN team_a_id
        WHEN wins_b = 4 THEN team_b_id
    END AS winner_id,
    first_day, last_day, goals_a, goals_b
FROM (
    SELECT
        g.season_id,
        (g.game_id / 100 % 100)::int AS round,
        (g.game_id / 10 % 10)::int AS series_number,
        LEAST(g.home_team_id, g.away_team_id) AS team_a_id,
        GREATEST(g.home_team_id, g.away_team_id) AS team_b_id,
        count(*) FILTER (WHERE g.winner_id = LEAST(g.home_team_id, g.away_team_id))::int AS wins_a,
        count(*) FILTER (WHERE g.winner_id = GREATEST(g.home_team_id, g.away_team_id))::int AS wins_b,
        count(*)::int AS games_played,
        min(g.day) AS first_day,
        max(g.day) AS last_day,
        coalesce(sum(CASE WHEN g.home_team_id < g.away_team_id THEN h.goals ELSE a.goals END), 0)::int AS goals_a,
        coalesce(sum(CASE WHEN g.home_team_id < g.away_team_id THEN a.goals ELSE h.goals END), 0)::int AS goals_b
    FROM games g
    LEFT JOIN game_team_stats h ON h.game_id = g.game_id AND h.team_id = g.home_team_id
    LEFT JOIN game_team_stats a ON a.game_id = g.game_id AND a.team_id = g.away_team_id
    WHERE g.game_type = 3
    GROUP BY 1, 2, 3, 4, 5
) AS s;
