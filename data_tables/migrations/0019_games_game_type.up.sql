-- Миграция 0019: тип игры в games (Задача 101) — 2 регулярка, 3 плей-офф.
-- Определение зеркалирует data_tables/t.games.sql — db-reset/db-tables пересоздают таблицу
-- из DDL в обход миграций, поэтому обе копии должны совпадать.
-- До Задачи 104 загрузчик берёт только регулярку, поэтому бэкфилл — 2 у всех строк.
-- IF NOT EXISTS: на уже поднятой из DDL схеме повторный db-migrate не должен падать.

ALTER TABLE games ADD COLUMN IF NOT EXISTS game_type smallint;
UPDATE games SET game_type = 2 WHERE game_type IS NULL;
ALTER TABLE games ALTER COLUMN game_type SET NOT NULL;
ALTER TABLE games DROP CONSTRAINT IF EXISTS games_game_type_check;
ALTER TABLE games ADD CONSTRAINT games_game_type_check CHECK (game_type IN (2, 3));
