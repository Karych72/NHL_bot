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
    game_id               bigint
);