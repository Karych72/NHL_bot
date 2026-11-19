-- Миграция 0033: playoff_series для старых форматов плей-офф (Задача 157): победитель серии в
-- завершённых сезонах — по победам и сумме голов, плюс колонки goals_a / goals_b. Определение —
-- data_tables/t.playoff_series.sql, общее с `make db-tables`; идемпотентно.

\ir ../t.playoff_series.sql
