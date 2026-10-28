-- Миграция 0015: ответы ИИ на статистические вопросы (Задача 83) — роль только на чтение для SQL,
-- который пишет модель, и журнал вопросов. Только миграция, без data_tables/t.*.sql: журнал не
-- восстановить повторной загрузкой.

-- Роль кластерная, а базы nhl_dev и postgres живут в одном кластере: создаётся один раз.
-- Пароля нет: Postgres в compose на trust (порт на 127.0.0.1, Задача 82), права роли
-- ограничивают независимо от аутентификации; ни в какую роль она не входит, сменить пользователя
-- SQL модели не может. Таймаут и «только чтение» — и на роли, и в параметрах соединения бота.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nhl_ai_reader') THEN
        CREATE ROLE nhl_ai_reader LOGIN;
    END IF;
END $$;
ALTER ROLE nhl_ai_reader SET statement_timeout = '5s';
ALTER ROLE nhl_ai_reader SET default_transaction_read_only = on;

-- GRANT CONNECT и SELECT на таблицы данных (общий файл с `make db-tables`).
\ir ../ai_reader_grants.sql

-- Строка на вопрос, дошедший до ai_stats.answer. Суточный лимит (ai_router.questions_left)
-- считает и эту таблицу; история продолжения диалога читается отсюда же.
CREATE TABLE ai_stats_log(
    id                  bigserial PRIMARY KEY,
    chat_id             bigint NOT NULL,
    asked_at            timestamptz NOT NULL DEFAULT now(),
    question            text NOT NULL,
    -- Продолжение прошлого вопроса: отметка модели; ответ на уточнение ревьюера — принудительно.
    continuation        boolean NOT NULL,
    -- answer — ответ прошёл ревью; clarify — вместо черновика ушло уточнение ревьюера;
    -- no_data / off_topic — финал модели без ревью; ceiling — потолок попыток, вызовов или
    -- стоимости; error — сбой API или БД (лимит не списывает).
    outcome             text NOT NULL CHECK (outcome IN
                            ('answer', 'clarify', 'no_data', 'off_topic', 'ceiling', 'error')),
    -- Каждый вызов инструмента: имя, вход, число строк или ошибка SQL.
    steps               jsonb NOT NULL DEFAULT '[]',
    draft               text,
    verdict             text,
    verdict_reason      text,
    -- Что увидел пользователь.
    answer              text,
    -- Каждый вызов модели: роль (answer/review), токены, стоимость, request_id.
    calls               jsonb NOT NULL DEFAULT '[]',
    cost_usd            numeric(10, 6) NOT NULL DEFAULT 0,
    latency_ms          int NOT NULL,
    error               text
);
CREATE INDEX ai_stats_log_chat_asked ON ai_stats_log (chat_id, asked_at);
