-- Откат миграции 0021: удаляет строки журнала о создании расписаний.

DELETE FROM bot_actions WHERE kind = 'schedule_created';
ALTER TABLE bot_actions DROP CONSTRAINT bot_actions_kind_check;
ALTER TABLE bot_actions ADD CONSTRAINT bot_actions_kind_check CHECK (
    kind IN ('command', 'callback', 'ai_limit', 'blocked', 'schedule_sent', 'schedule_deleted')
);
