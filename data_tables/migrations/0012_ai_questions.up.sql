-- Миграция 0012: свободный текст в боте (Задача 81) — журнал вопросов к модели и список
-- «текущих» пользователей для суточного лимита. Только миграция, без data_tables/t.*.sql:
-- журнал не восстановить повторной загрузкой (как bot_subscriptions и prediction_log).

-- Строка на каждый вызов модели: по ним считается суточный лимит, по ним же видно, какие
-- вопросы бот не покрывает. Отказ по лимиту модель не вызывает и строки не пишет.
CREATE TABLE ai_questions(
    id                  bigserial PRIMARY KEY,
    chat_id             bigint NOT NULL,
    asked_at            timestamptz NOT NULL DEFAULT now(),
    question            text NOT NULL,
    -- screen — открыт экран (tool, tool_input); text — модель ответила текстом (отказ или
    -- уточнение, answer); error — сбой API (error, request_id).
    outcome             text NOT NULL CHECK (outcome IN ('screen', 'text', 'error')),
    tool                text,
    tool_input          jsonb,
    answer              text,
    input_tokens        int,
    output_tokens       int,
    cache_read_tokens   int,
    cache_write_tokens  int,
    cost_usd            numeric(10, 6),
    latency_ms          int NOT NULL,
    request_id          text,
    error               text
);
CREATE INDEX ai_questions_chat_asked ON ai_questions (chat_id, asked_at);

-- Пользователи с повышенным лимитом: у кого на дату выкатки была хоть одна запись в
-- bot_subscriptions (другого списка пользователей в БД нет). Фиксируется один раз этой
-- миграцией: подписка, оформленная позже, лимит не поднимает.
CREATE TABLE ai_existing_users(
    chat_id             bigint PRIMARY KEY
);
INSERT INTO ai_existing_users (chat_id) SELECT DISTINCT chat_id FROM bot_subscriptions;
