-- Миграция 0004: таблица game_predictions (вероятности модели для бота, Задача 22B).
-- Определение зеркалирует data_tables/t.game_predictions.sql — db-reset/db-tables
-- пересоздают таблицу из DDL в обход миграций, поэтому обе копии должны совпадать.
-- IF NOT EXISTS: на уже поднятой из DDL схеме повторный db-migrate не должен падать.

CREATE TABLE IF NOT EXISTS game_predictions(
    game_id                 bigint NOT NULL,
    task                    text NOT NULL,
    model                   text NOT NULL,
    run_id                  text NOT NULL,
    probability             double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
    computed_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, task)
);
