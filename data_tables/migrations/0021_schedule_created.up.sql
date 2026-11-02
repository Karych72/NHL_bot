-- Миграция 0021: журнал действий (Задача 99) пишет создание расписания с карточки-черновика
-- (Задача 108): kind = 'schedule_created', data = '<id>:<экран>'.
ALTER TABLE bot_actions DROP CONSTRAINT bot_actions_kind_check;
ALTER TABLE bot_actions ADD CONSTRAINT bot_actions_kind_check CHECK (
    kind IN ('command', 'callback', 'ai_limit', 'blocked', 'schedule_sent', 'schedule_deleted',
             'schedule_created')
);
