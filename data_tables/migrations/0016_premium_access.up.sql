-- Миграция 0016: премиум-доступ (Задача 96) — ИИ без недельного лимита и прогноз модели.
-- Только миграция, без data_tables/t.*.sql: выдачу не восстановить повторной загрузкой (как
-- bot_subscriptions). Строку вставляет человек SQL-ом (DEVELOPMENT.md), продлевает оплата (97).

-- Премиум у чата до expires_at; бессрочного нет. Ключ — chat_id, как у подписок: группа с
-- премиумом видит прогноз всем участникам.
CREATE TABLE premium_access(
    chat_id             bigint PRIMARY KEY,
    expires_at          timestamptz NOT NULL,
    granted_at          timestamptz NOT NULL DEFAULT now()
);

-- Повышенный лимит «текущим» пользователям (миграция 0012) заменён премиумом.
DROP TABLE ai_existing_users;
