-- Откат миграции 0026: удаляет строки журнала о конце пробника и признаки пробника.

DELETE FROM bot_actions WHERE kind = 'trial_ended';
ALTER TABLE bot_actions DROP CONSTRAINT bot_actions_kind_check;
ALTER TABLE bot_actions ADD CONSTRAINT bot_actions_kind_check CHECK (
    kind IN ('command', 'callback', 'ai_limit', 'blocked', 'schedule_sent', 'schedule_deleted',
             'schedule_created')
);

ALTER TABLE premium_access
    DROP COLUMN trial_notice_at,
    DROP COLUMN is_trial;
