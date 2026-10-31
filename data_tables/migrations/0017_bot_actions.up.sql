-- Миграция 0017: журнал действий пользователей и история подписок (Задача 99). Только
-- миграция, без data_tables/t.*.sql: журнал не восстановить повторной загрузкой (как
-- ai_questions). Не чистится — ни срока хранения, ни задания очистки (решение человека).

-- Строка на действие: команда (data — имя без «/» и «@бот»), нажатие кнопки (callback_data),
-- отказ ИИ по лимиту (day / week / total), блокировка бота (рассылка поймала Forbidden).
-- Свободный текст к ИИ сюда не пишется — он в ai_questions и ai_stats_log. user_id — кто
-- действовал (в группе чат общий); у blocked его нет: это событие чата, а не человека.
CREATE TABLE bot_actions(
    id                  bigserial PRIMARY KEY,
    chat_id             bigint NOT NULL,
    user_id             bigint,
    at                  timestamptz NOT NULL DEFAULT now(),
    kind                text NOT NULL CHECK (kind IN ('command', 'callback', 'ai_limit', 'blocked')),
    data                text
);
CREATE INDEX bot_actions_chat_at ON bot_actions (chat_id, at);

-- История включения и выключения подписок: строка bot_subscriptions перезаписывается на
-- месте, а updated_at двигает и отметка рассылки. Пишет триггер — покрыты все пути записи.
CREATE TABLE bot_subscription_events(
    id                  bigserial PRIMARY KEY,
    subscription_id     bigint NOT NULL,
    chat_id             bigint NOT NULL,
    kind                text NOT NULL,
    team_id             bigint,
    country             text,
    active              boolean NOT NULL,
    at                  timestamptz NOT NULL DEFAULT now()
);

-- Событие — вставка подписки и фактическая смена active; повторная запись того же active
-- и отметка last_sent_night событий не дают.
CREATE FUNCTION bot_subscription_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR OLD.active IS DISTINCT FROM NEW.active THEN
        INSERT INTO bot_subscription_events (subscription_id, chat_id, kind, team_id, country, active)
        VALUES (NEW.id, NEW.chat_id, NEW.kind, NEW.team_id, NEW.country, NEW.active);
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER bot_subscription_event AFTER INSERT OR UPDATE OF active ON bot_subscriptions
    FOR EACH ROW EXECUTE FUNCTION bot_subscription_event();
