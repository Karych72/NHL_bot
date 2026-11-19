-- Откат миграции 0031: колонки и индекс удаляются вместе с загруженными в них значениями.

DROP INDEX rosters_season_lastname_idx;
ALTER TABLE teams_stats DROP COLUMN ties;
ALTER TABLE teams DROP COLUMN franchise_id;
