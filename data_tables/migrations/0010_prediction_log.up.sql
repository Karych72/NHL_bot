-- Миграция 0010: таблица prediction_log (журнал прогнозов и исходов, Задача 77).
-- Только миграция, без data_tables/t.*.sql: журнал не восстановить повторной загрузкой,
-- поэтому db-drop/db-reset его не трогают (как bot_subscriptions).

CREATE TABLE prediction_log(
    game_id                 bigint NOT NULL,
    task                    text NOT NULL,
    model                   text NOT NULL,
    run_id                  text NOT NULL,
    probability             double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
    published_on            date NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
    published_at            timestamptz NOT NULL DEFAULT now(),
    home_goals              int,
    away_goals              int,
    settled_at              timestamptz,
    PRIMARY KEY (game_id, task, run_id, published_on)
);
