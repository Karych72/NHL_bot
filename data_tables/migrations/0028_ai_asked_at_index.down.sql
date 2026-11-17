-- Откат миграции 0028: удаляет индексы по asked_at.

DROP INDEX ai_stats_log_asked;
DROP INDEX ai_questions_asked;
