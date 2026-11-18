CREATE TABLE bot_upcoming_ai (
    game_id bigint PRIMARY KEY,
    body text,
    created_at timestamptz NOT NULL DEFAULT now()
);
