-- Миграция 0023: представление серий плей-офф playoff_series и его чтение ролью nhl_ai_reader
-- (Задача 105). Определение — data_tables/t.playoff_series.sql, общее с `make db-tables`;
-- GRANT — общий файл ai_reader_grants.sql (как в 0015). Оба идемпотентны.

\ir ../t.playoff_series.sql
\ir ../ai_reader_grants.sql
