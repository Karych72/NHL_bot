-- Откат миграции 0017: удаляет журнал платежей Stars (премиум premium_access остаётся).

DROP TABLE premium_payments;
