-- Откат миграции 0033: представление возвращается к определению миграции 0023 (победитель —
-- команда с 4 победами, без колонок goals_a / goals_b). DROP + CREATE, потому что
-- CREATE OR REPLACE VIEW не убирает колонки; GRANT роли nhl_ai_reader возвращает ai_reader_grants.sql.

DROP VIEW playoff_series;
CREATE VIEW playoff_series AS
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

\ir ../ai_reader_grants.sql
