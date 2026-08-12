CREATE TABLE all_goals(
    goal_player_id        bigint,
    total_goals           int,
    assist_player1_id     bigint,
    assist_total_1        int,
    assist_player2_id     bigint,
    assist_total_2        int,
    empty_net             boolean,
    winner_goal           boolean,
    is_ppg                boolean,
    is_shg                boolean,
    team_id               int,
    game_id               bigint NOT NULL,
    period                int,
    time                  varchar(20),
    goals_away            int,
    goals_home            int,
    event_id              int,
    -- Голы конкретной игры. Проверено на живой БД: 0 сирот.
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

-- В отличие от game_team_stats/game_player_stats/game_goalie_stats у all_goals нет
-- UNIQUE(game_id, ...), поэтому явный индекс на game_id нужен отдельно. Обслуживает:
--   telegram_bot/queries/get_goals_game.sql:34-42 (WHERE g.game_id = now_game_id)
-- и служит опорным индексом для FK выше.
CREATE INDEX idx_all_goals_game_id ON all_goals (game_id);
