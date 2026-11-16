-- Миграция 0026: сообщение о конце пробника (Задача 125). premium_access отличает пробник
-- (is_trial, ставит premium.grant_trial) от оплаты и ручной выдачи и помнит, когда ушло
-- сообщение о его конце (trial_notice_at). Строки до миграции — false: пробникам, выданным до
-- выкатки, сообщение не уходит. Журнал действий пишет отправку: kind = 'trial_ended'.

ALTER TABLE premium_access
    ADD COLUMN is_trial boolean NOT NULL DEFAULT false,
    ADD COLUMN trial_notice_at timestamptz;

ALTER TABLE bot_actions DROP CONSTRAINT bot_actions_kind_check;
ALTER TABLE bot_actions ADD CONSTRAINT bot_actions_kind_check CHECK (
    kind IN ('command', 'callback', 'ai_limit', 'blocked', 'schedule_sent', 'schedule_deleted',
             'schedule_created', 'trial_ended')
);
