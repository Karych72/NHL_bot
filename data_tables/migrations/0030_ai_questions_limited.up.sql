-- Миграция 0030: исход 'limited' в журнале маршрутизатора (Задача 114). Сверх лимита чата модель
-- спрашивается — вдруг вопрос о самом боте; её ответ не о боте пользователь не получает (отказ по
-- лимиту), и такой вызов пишется 'limited': не экран, не текст, не уточнение для следующего вопроса.

ALTER TABLE ai_questions DROP CONSTRAINT ai_questions_outcome_check;
ALTER TABLE ai_questions ADD CONSTRAINT ai_questions_outcome_check
    CHECK (outcome IN ('screen', 'text', 'error', 'limited'));
