CREATE TABLE game_three_stars(
    game_id     bigint NOT NULL,
    star        int NOT NULL,
    player_id   bigint NOT NULL,
    team_id     bigint NOT NULL,
    UNIQUE (game_id, star),
    -- Три звезды конкретного матча (star = 1, 2 или 3 — первая, вторая, третья звезда).
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

-- Отдельный индекс на game_id не нужен: UNIQUE (game_id, star) уже даёт btree с
-- ведущей колонкой game_id, чего достаточно и для WHERE game_id = %s, и для этого FK.
-- FK на player_id/team_id не ставится: ни одна пер-игровая таблица проекта их не ставит
-- (t.game_player_stats.sql, t.all_goals.sql), а звезда может не оказаться в rosters
-- того сезона — жёсткий FK обвалил бы транзакцию прогона.
