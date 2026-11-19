-- Миграция 0031: итоги сезонов 1917/18–2008/09 (Задача 158).
-- teams.franchise_id — клуб, переехавший в другой город, меняет team_id, а franchise_id остаётся.
-- teams_stats.ties — ничьи: API отдаёт их до 2003/04 (otLosses там null), с 2005/06 — NULL.
-- Индекс rosters (season_id, lastname): состав клуба, поиск игрока для ИИ и player_display_name
-- читают rosters по сезону без игрока; таблица вырастет втрое, PK (player_id, season_id) тут не помогает.

ALTER TABLE teams ADD COLUMN franchise_id bigint;
ALTER TABLE teams_stats ADD COLUMN ties int;
CREATE INDEX rosters_season_lastname_idx ON rosters (season_id, lastname);
