-- Будущие (ещё не сыгранные) игры регулярки: цели для predict-датасета (Задача 22A).
-- Отдельно от games, чтобы инвариант games «только сыгранные, с победителем» остался
-- нетронутым. Содержимое сезона целиком заменяется при каждом прогоне загрузчика.
CREATE TABLE scheduled_games(
    game_id                 bigint NOT NULL,
    day                     date NOT NULL,
    home_team_id            bigint NOT NULL,
    away_team_id            bigint NOT NULL,
    season_id               bigint NOT NULL,
    PRIMARY KEY (game_id),
    FOREIGN KEY (home_team_id, season_id) REFERENCES teams (team_id, season_id),
    FOREIGN KEY (away_team_id, season_id) REFERENCES teams (team_id, season_id)
);
