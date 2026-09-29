-- Вероятности модели по ещё не сыгранным играм (Задача 22B): бот читает только PG.
-- Одна вероятность на игру и задачу; строки задачи целиком заменяет
-- `modeling.cli publish-predictions`. FK на games нет — игра ещё не сыграна.
CREATE TABLE game_predictions(
    game_id                 bigint NOT NULL,
    task                    text NOT NULL,
    model                   text NOT NULL,
    run_id                  text NOT NULL,
    probability             double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
    computed_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, task)
);
