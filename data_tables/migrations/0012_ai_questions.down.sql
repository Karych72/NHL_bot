-- Откат миграции 0012: удаляет журнал вопросов к модели и список текущих пользователей.

DROP TABLE ai_existing_users;
DROP TABLE ai_questions;
