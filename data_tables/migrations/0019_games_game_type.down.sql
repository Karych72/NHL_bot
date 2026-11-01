-- Откат миграции 0019: удаляет колонку games.game_type (вместе с её CHECK).

ALTER TABLE games DROP COLUMN game_type;
