-- One-shot teardown for NHL_bot tables (no ALTER / migration path).
-- Run before applying data_tables/t.*.sql from scratch.

DROP TABLE IF EXISTS all_goals CASCADE;
DROP TABLE IF EXISTS game_goalie_stats CASCADE;
DROP TABLE IF EXISTS game_player_stats CASCADE;
DROP TABLE IF EXISTS game_team_stats CASCADE;
DROP TABLE IF EXISTS games CASCADE;
DROP TABLE IF EXISTS goalies_season_stats CASCADE;
DROP TABLE IF EXISTS players_advanced_stats CASCADE;
DROP TABLE IF EXISTS players_season_stats CASCADE;
DROP TABLE IF EXISTS players_shot_types CASCADE;
DROP TABLE IF EXISTS rosters CASCADE;
DROP TABLE IF EXISTS teams_stats CASCADE;
DROP TABLE IF EXISTS teams CASCADE;
