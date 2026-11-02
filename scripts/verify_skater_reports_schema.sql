-- Verifies skater reports schema (columns + composite PKs with season_id).
-- Usage: psql ... -v ON_ERROR_STOP=1 -f scripts/verify_skater_reports_schema.sql

DO $$
BEGIN
    IF to_regclass('public.players_advanced_stats') IS NULL THEN
        RAISE EXCEPTION 'table public.players_advanced_stats is missing';
    END IF;
    IF to_regclass('public.players_shot_types') IS NULL THEN
        RAISE EXCEPTION 'table public.players_shot_types is missing';
    END IF;
END$$;

DO $$
DECLARE
    need text[] := ARRAY[
        'oz_faceoff_pct', 'dz_faceoff_pct', 'nz_faceoff_pct',
        'shootout_goals', 'shootout_shots', 'shootout_pct', 'shootout_gd_goals'
    ];
    col text;
BEGIN
    FOREACH col IN ARRAY need
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'players_season_stats'
              AND column_name = col
        ) THEN
            RAISE EXCEPTION 'missing players_season_stats column: %', col;
        END IF;
    END LOOP;
END$$;

DO $$
DECLARE
    need text[] := ARRAY[
        'sat_pct', 'usat_pct', 'goals_pct', 'oz_start_pct', 'dz_start_pct',
        'nz_start_pct', 'on_ice_shooting_pct', 'ev_goals_for', 'ev_goals_against',
        'ev_goals_for_pct', 'pp_goals_for', 'pp_goals_against',
        'sh_goals_for', 'sh_goals_against', 'season_id'
    ];
    col text;
BEGIN
    FOREACH col IN ARRAY need
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'players_advanced_stats'
              AND column_name = col
        ) THEN
            RAISE EXCEPTION 'missing players_advanced_stats column: %', col;
        END IF;
    END LOOP;
END$$;

DO $$
DECLARE
    need text[] := ARRAY[
        'goals_wrist', 'shots_wrist', 'goals_slap', 'shots_slap', 'goals_snap',
        'shots_snap', 'goals_backhand', 'shots_backhand', 'goals_tip_in',
        'shots_tip_in', 'goals_deflected', 'shots_deflected', 'goals_wrap_around',
        'shots_wrap_around', 'season_id'
    ];
    col text;
BEGIN
    FOREACH col IN ARRAY need
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'players_shot_types'
              AND column_name = col
        ) THEN
            RAISE EXCEPTION 'missing players_shot_types column: %', col;
        END IF;
    END LOOP;
END$$;

-- PK — (player_id, season_id, game_type): строки регулярки и плей-офф (миграция 0022, Задача 103).
DO $$
DECLARE
    tbl text;
    pk text[];
BEGIN
    FOREACH tbl IN ARRAY ARRAY['players_season_stats', 'players_advanced_stats', 'players_shot_types']
    LOOP
        SELECT array_agg(a.attname::text ORDER BY k.ord) INTO pk
        FROM pg_index i
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE i.indrelid = ('public.' || tbl)::regclass AND i.indisprimary;
        IF pk IS DISTINCT FROM ARRAY['player_id', 'season_id', 'game_type'] THEN
            RAISE EXCEPTION '% PRIMARY KEY is %, expected (player_id, season_id, game_type)', tbl, pk;
        END IF;
    END LOOP;
END$$;

SELECT 'skater reports schema OK' AS verify_skater_reports_schema;
