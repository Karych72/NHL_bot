-- Откат миграции 0022: удаляет game_type у пяти сезонных агрегатов (вместе с CHECK, PK и
-- индексом по (season_id, game_type)) и возвращает PK (id, season_id) и индексы (season_id).
-- После загрузки плей-офф (Задача 104) старый PK не встанет на дубли игрок+сезон — откат
-- упадёт; строки game_type = 3 удалить вручную, если откат действительно нужен.

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
        EXECUTE format('ALTER TABLE %I DROP COLUMN game_type', t.tbl);
        EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%I, season_id)', t.tbl, t.id_col);
        IF t.season_idx IS NOT NULL THEN
            EXECUTE format('CREATE INDEX %I ON %I (season_id)', t.season_idx, t.tbl);
        END IF;
    END LOOP;
END
$$;
