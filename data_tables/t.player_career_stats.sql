-- Карьера из официальных сезонных итогов; регулярка и плей-офф раздельно.
-- SUM сохраняет NULL, если метрика не учитывалась всю карьеру.
CREATE OR REPLACE VIEW player_career_stats AS
SELECT player_id, game_type,
       min(season_id) AS first_season_id, max(season_id) AS last_season_id,
       count(*) AS seasons,
       sum(games) AS games, sum(goals) AS goals, sum(assists) AS assists,
       sum(points) AS points, sum(pim) AS pim,
       sum(shots) AS shots, sum(plus_minus) AS plus_minus,
       sum(power_play_goals) AS power_play_goals,
       sum(short_handed_goals) AS short_handed_goals,
       sum(game_winning_goals) AS game_winning_goals,
       sum(hits) AS hits, sum(blocked) AS blocked
FROM players_season_stats
GROUP BY player_id, game_type;
