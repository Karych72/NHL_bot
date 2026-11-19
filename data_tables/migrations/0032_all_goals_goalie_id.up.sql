-- Миграция 0032: вратарь, пропустивший гол (Задача 160).
-- IF NOT EXISTS: db-tables/db-sync создают колонки из DDL в обход миграций.
-- Значения для уже загруженных голов — scripts/backfill_goal_goalie.py (кэш all_data/raw).

ALTER TABLE all_goals ADD COLUMN IF NOT EXISTS goalie_id bigint;
