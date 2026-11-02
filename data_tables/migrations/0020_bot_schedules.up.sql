-- Миграция 0020: расписания экранов бота (Задача 107). Только миграция, без
-- data_tables/t.*.sql: расписания не восстановить повторной загрузкой (как bot_subscriptions).
-- Пишет и читает бот: тикер в процессе бота шлёт экраны, /subscriptions их удаляет.

-- Расписание — экран бота с параметрами и время отправки. screen — ключ bot.SCHEDULE_SCREENS
-- (экраны bot.AI_SCREENS, кроме дайджеста за дату), params — параметры экрана, как у
-- инструмента модели. weekday — ISO-день недели (1 = понедельник) у freq = 'week'; month_day — число
-- месяца у freq = 'month', 0 = последний день. local_time — время в поясе timezone (IANA)
-- с шагом 30 минут; next_run_at — ближайшая отправка, её считает schedule_repo.next_run_at.
-- active = FALSE — чат заблокировал бота (deactivate_chat); удалённое расписание стирается.
CREATE TABLE bot_schedules(
    id              bigserial PRIMARY KEY,
    chat_id         bigint NOT NULL,
    screen          text NOT NULL,
    params          jsonb NOT NULL DEFAULT '{}',
    freq            text NOT NULL CHECK (freq IN ('day', 'week', 'month')),
    weekday         smallint CHECK (weekday BETWEEN 1 AND 7),
    month_day       smallint CHECK (month_day BETWEEN 0 AND 28),
    local_time      time NOT NULL
        CHECK (extract(minute FROM local_time) IN (0, 30) AND extract(second FROM local_time) = 0),
    timezone        text NOT NULL,
    next_run_at     timestamptz NOT NULL,
    active          boolean NOT NULL DEFAULT TRUE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK ((weekday IS NOT NULL) = (freq = 'week')),
    CHECK ((month_day IS NOT NULL) = (freq = 'month'))
);
CREATE INDEX bot_schedules_due ON bot_schedules (next_run_at) WHERE active;

-- Журнал действий (Задача 99): отправка расписания и его удаление пользователем.
ALTER TABLE bot_actions DROP CONSTRAINT bot_actions_kind_check;
ALTER TABLE bot_actions ADD CONSTRAINT bot_actions_kind_check CHECK (
    kind IN ('command', 'callback', 'ai_limit', 'blocked', 'schedule_sent', 'schedule_deleted')
);
