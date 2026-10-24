-- Откат миграции 0010: удаляет таблицу prediction_log (журнал прогнозов теряется).

DROP TABLE prediction_log;
