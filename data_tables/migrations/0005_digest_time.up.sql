-- Миграция 0005: время доставки дайджеста и отметка об отправке (Задача 60).
-- digest_time — выбранное подписчиком время по МСК (UTC+3 без летнего времени), есть только
-- у morning_digest; прежнее расписание (11:00 МСК) становится значением существующих подписок.
-- last_sent_night — игровая дата ночи, за которую чату уже ушла рассылка: защита от дубля,
-- рассылка проверяется после каждого получасового прогона sync.
-- timezone удаляется: колонкой никто не пользовался, время везде по МСК.

ALTER TABLE bot_subscriptions DROP COLUMN timezone;
ALTER TABLE bot_subscriptions ADD COLUMN digest_time TIME;
UPDATE bot_subscriptions SET digest_time = '11:00' WHERE kind = 'morning_digest';
ALTER TABLE bot_subscriptions ADD CONSTRAINT bot_subscriptions_digest_time_kind
    CHECK ((kind = 'morning_digest') = (digest_time IS NOT NULL));
ALTER TABLE bot_subscriptions ADD COLUMN last_sent_night DATE;
