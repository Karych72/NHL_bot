-- Откат миграции 0001: удаляет таблицу подписок bot_subscriptions
-- (оба частичных UNIQUE-индекса уходят вместе с таблицей).

DROP TABLE bot_subscriptions;
