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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints tc
        WHERE tc.table_schema = 'public'
          AND tc.table_name = 'players_advanced_stats'
          AND tc.constraint_type = 'PRIMARY KEY'
    ) THEN
        RAISE EXCEPTION 'players_advanced_stats has no PRIMARY KEY';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints tc
        WHERE tc.table_schema = 'public'
          AND tc.table_name = 'players_shot_types'
          AND tc.constraint_type = 'PRIMARY KEY'
    ) THEN
        RAISE EXCEPTION 'players_shot_types has no PRIMARY KEY';
    END IF;
END$$;

SELECT 'skater reports schema OK' AS verify_skater_reports_schema;
