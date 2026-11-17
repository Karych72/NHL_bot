-- Миграция 0028: индексы по времени вопроса на журналах ИИ (Задача 141). Общий суточный
-- бюджет суммирует cost_usd за сутки по всем чатам на каждый вопрос; индексы (chat_id, asked_at)
-- для такого запроса не годятся.

CREATE INDEX ai_questions_asked ON ai_questions (asked_at);
CREATE INDEX ai_stats_log_asked ON ai_stats_log (asked_at);
