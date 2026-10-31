-- Откат миграции 0017: удаляет триггер истории подписок, его функцию и оба журнала.

DROP TRIGGER bot_subscription_event ON bot_subscriptions;
DROP FUNCTION bot_subscription_event();
DROP TABLE bot_subscription_events;
DROP TABLE bot_actions;
