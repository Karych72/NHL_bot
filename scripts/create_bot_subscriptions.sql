-- Подписки бота на push (утренний дайджест, счёт команды).
-- Применение: psql с теми же PG_* что у бота, см. docs/telegram_bot.md § «Подписки».

CREATE TABLE IF NOT EXISTS bot_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('morning_digest', 'team_scores')),
    team_id BIGINT,
    timezone TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS bot_subscriptions_digest_unique
    ON bot_subscriptions (chat_id)
    WHERE kind = 'morning_digest';

CREATE UNIQUE INDEX IF NOT EXISTS bot_subscriptions_team_unique
    ON bot_subscriptions (chat_id, team_id)
    WHERE kind = 'team_scores';
