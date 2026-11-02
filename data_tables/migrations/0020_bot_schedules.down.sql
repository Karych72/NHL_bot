-- Откат миграции 0020: удаляет расписания и их строки журнала bot_actions.

DELETE FROM bot_actions WHERE kind IN ('schedule_sent', 'schedule_deleted');
ALTER TABLE bot_actions DROP CONSTRAINT bot_actions_kind_check;
ALTER TABLE bot_actions ADD CONSTRAINT bot_actions_kind_check CHECK (
    kind IN ('command', 'callback', 'ai_limit', 'blocked')
);
DROP TABLE bot_schedules;
