-- Миграция 0022: тип игры в ключе сезонных агрегатов (Задача 103) — 2 регулярка, 3 плей-офф.
-- Итоги плей-офф (Задача 104) лягут в те же таблицы строкой game_type = 3 рядом с регуляркой.
-- Определения зеркалируют data_tables/t.{teams_stats,players_season_stats,goalies_season_stats,
-- players_advanced_stats,players_shot_types}.sql — db-reset/db-tables пересоздают таблицы из DDL
-- в обход миграций. До Задачи 104 загрузчик пишет только регулярку — бэкфилл 2 у всех строк.
-- Идемпотентна: на схеме, уже поднятой из DDL, повторный db-migrate не падает.
-- FK на teams / rosters остаются по (id, season_id): клуб и игрок плей-офф — те же, что в сезоне.

DO $$
DECLARE
    t record;
BEGIN
    FOR t IN
        SELECT * FROM (VALUES
            ('teams_stats', 'team_id', NULL),
            ('players_season_stats', 'player_id', 'idx_players_season_stats_season'),
            ('goalies_season_stats', 'player_id', 'idx_goalies_season_stats_season'),
            ('players_advanced_stats', 'player_id', 'idx_players_advanced_stats_season'),
            ('players_shot_types', 'player_id', 'idx_players_shot_types_season')
        ) AS v(tbl, id_col, season_idx)
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS game_type smallint', t.tbl);
        EXECUTE format('UPDATE %I SET game_type = 2 WHERE game_type IS NULL', t.tbl);
        EXECUTE format('ALTER TABLE %I ALTER COLUMN game_type SET NOT NULL', t.tbl);
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', t.tbl, t.tbl || '_game_type_check');
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK (game_type IN (2, 3))',
                       t.tbl, t.tbl || '_game_type_check');
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', t.tbl, t.tbl || '_pkey');
        EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%I, season_id, game_type)', t.tbl, t.id_col);
        IF t.season_idx IS NOT NULL THEN
            EXECUTE format('DROP INDEX IF EXISTS %I', t.season_idx);
            EXECUTE format('CREATE INDEX %I ON %I (season_id, game_type)', t.season_idx, t.tbl);
        END IF;
    END LOOP;
END
$$;
