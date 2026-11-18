-- Откат миграции 0030: строки 'limited' удаляются, исход снова 'screen' / 'text' / 'error'.

DELETE FROM ai_questions WHERE outcome = 'limited';
ALTER TABLE ai_questions DROP CONSTRAINT ai_questions_outcome_check;
ALTER TABLE ai_questions ADD CONSTRAINT ai_questions_outcome_check
    CHECK (outcome IN ('screen', 'text', 'error'));
