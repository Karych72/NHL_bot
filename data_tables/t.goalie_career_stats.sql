-- Счётчики суммируются, процент считается из сумм за сезоны с обоими счётчиками.
-- Сезонные проценты и коэффициенты надёжности усреднять нельзя.
CREATE OR REPLACE VIEW goalie_career_stats AS
WITH seasons AS (
    SELECT *, split_part(time_on_ice, ':', 1)::numeric * 60
              + split_part(time_on_ice, ':', 2)::numeric AS toi_seconds
    FROM goalies_season_stats
)
SELECT player_id, game_type,
       min(season_id) AS first_season_id, max(season_id) AS last_season_id,
       count(*) AS seasons,
       sum(games) AS games, sum(wins) AS wins, sum(losses) AS losses,
       sum(ties) AS ties, sum(ot) AS ot, sum(shutouts) AS shutouts,
       sum(goals_against) AS goals_against,
       sum(saves) AS saves, sum(shots_against) AS shots_against,
       100.0 * sum(saves) FILTER (WHERE shots_against IS NOT NULL)
           / nullif(sum(shots_against) FILTER (WHERE saves IS NOT NULL), 0)
           AS save_percentage,
       3600.0 * sum(goals_against) FILTER (WHERE toi_seconds IS NOT NULL)
           / nullif(sum(toi_seconds) FILTER (WHERE goals_against IS NOT NULL), 0)
           AS goal_against_average
FROM seasons
GROUP BY player_id, game_type;
