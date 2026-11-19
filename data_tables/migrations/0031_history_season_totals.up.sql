-- Миграция 0031: итоги сезонов 1917/18–2008/09 (Задача 158).
-- teams.franchise_id — клуб, переехавший в другой город, меняет team_id, а franchise_id остаётся.
-- teams_stats.ties — ничьи: API отдаёт их до 2003/04, с 2005/06 — NULL.
-- Определение зеркалирует data_tables/t.*.sql — db-tables создаёт таблицы из DDL в обход миграций,
-- поэтому IF NOT EXISTS: на такой схеме db-migrate не должен падать.
-- Индекс rosters (season_id, lastname): состав клуба, поиск игрока для ИИ и player_display_name
-- читают rosters по сезону без игрока; таблица вырастет втрое, PK (player_id, season_id) тут не помогает.

ALTER TABLE teams ADD COLUMN IF NOT EXISTS franchise_id bigint;
ALTER TABLE teams_stats ADD COLUMN IF NOT EXISTS ties int;
CREATE INDEX IF NOT EXISTS rosters_season_lastname_idx ON rosters (season_id, lastname);
