-- Права роли nhl_ai_reader (Задача 83): SELECT только на таблицы данных NHL. Список — единственный
-- источник; его же сверяет tests/test_db_nhl.py с ai_stats.AI_TABLES. Таблиц бота (bot_*, ai_*,
-- prediction_log, schema_migrations) здесь нет и быть не должно: роль — граница безопасности для SQL,
-- который пишет модель. game_predictions не выдана: бот не даёт прогнозов (plan/ai_questions).
--
-- Файл идемпотентен и нужен в двух местах: его подключает миграция 0015 (\ir) и применяет
-- `make db-tables` — db-drop сносит таблицы вместе с GRANT, а применённую миграцию повторно
-- никто не запускает. Роли ещё нет (db-tables на пустой базе до миграции) — ничего не делает.
DO $$
DECLARE
    t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nhl_ai_reader') THEN
        RETURN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO nhl_ai_reader', current_database());
    GRANT USAGE ON SCHEMA public TO nhl_ai_reader;
    FOREACH t IN ARRAY ARRAY[
        'games', 'teams', 'teams_stats', 'rosters', 'players_season_stats',
        'players_advanced_stats', 'players_shot_types', 'goalies_season_stats',
        'game_team_stats', 'game_player_stats', 'game_goalie_stats', 'all_goals',
        'game_three_stars', 'shot_attempts', 'scheduled_games'
    ] LOOP
        EXECUTE format('GRANT SELECT ON %I TO nhl_ai_reader', t);
    END LOOP;
END $$;
