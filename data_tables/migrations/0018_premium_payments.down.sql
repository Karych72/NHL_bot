-- Откат миграции 0018: удаляет журнал платежей Stars (премиум premium_access остаётся).

DROP TABLE premium_payments;
