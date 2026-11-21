-- Откат миграции 0032: колонка удаляется вместе с загруженными в неё значениями.

ALTER TABLE all_goals DROP COLUMN goalie_id;
