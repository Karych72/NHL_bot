-- Один итог на франшизу и тип игры, независимо от переездов и смены team_id.
-- Голы восстанавливаются из сезонного среднего: округление до сложения сезонов.
-- NULL franchise_id (в том числе клубы других лиг в ранних финалах) не объединяем.
CREATE OR REPLACE VIEW franchise_stats AS
WITH season_totals AS (
    SELECT t.franchise_id, s.season_id, s.game_type,
           sum(s.games_played) AS games, sum(s.wins) AS wins,
           sum(s.points) AS points,
           sum(round((s.goals_per_game * s.games_played)::numeric)) AS goals,
           sum(round((s.goals_against_per_game * s.games_played)::numeric)) AS goals_against
    FROM teams_stats s JOIN teams t USING (team_id, season_id)
    WHERE t.franchise_id IS NOT NULL
    GROUP BY t.franchise_id, s.season_id, s.game_type
), playoff_counts AS (
    SELECT franchise_id, count(*) AS playoff_seasons
    FROM season_totals WHERE game_type = 3 AND games > 0
    GROUP BY franchise_id
)
SELECT s.franchise_id, s.game_type,
       min(s.season_id) AS first_season_id, max(s.season_id) AS last_season_id,
       count(*) AS seasons, sum(s.games) AS games, sum(s.wins) AS wins,
       sum(s.points) AS points, sum(s.goals) AS goals, sum(s.goals_against) AS goals_against,
       coalesce(p.playoff_seasons, 0) AS playoff_seasons
FROM season_totals s LEFT JOIN playoff_counts p USING (franchise_id)
GROUP BY s.franchise_id, s.game_type, p.playoff_seasons;
