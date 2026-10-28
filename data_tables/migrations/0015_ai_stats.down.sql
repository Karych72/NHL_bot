-- Откат миграции 0015: журнал вопросов и права роли nhl_ai_reader в этой базе. Саму роль (кластерную)
-- удаляем только если права на неё не остались в другой базе кластера (nhl_dev и postgres
-- делят кластер): иначе DROP ROLE упал бы, а откат на одной базе сломал бы другую.

DROP TABLE ai_stats_log;
DROP OWNED BY nhl_ai_reader;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid
        WHERE r.rolname = 'nhl_ai_reader' AND d.deptype = 'a'
    ) THEN
        DROP ROLE nhl_ai_reader;
    END IF;
END $$;
