# Задача 160 — что проверить человеку

Код, миграция 0032, промпт и тесты в ветке `task-160-goal-goalie`. Остаётся то, что требует БД.

1. Миграция на `nhl_dev` (после мержа в `develop` деплой применит сам), затем бэкфилл с ноутбука,
   где лежит `all_data/raw`: `PG_DATABASE=nhl_dev … .venv/bin/python scripts/backfill_goal_goalie.py`
   (на VM кэша нет — либо туннель к `nhl_dev`, либо перезалив сезонов).
2. `SELECT season_id, round(100.0*count(*) FILTER (WHERE goalie_id IS NULL)/count(*),1) FROM all_goals g JOIN games USING (game_id) GROUP BY 1` — доля NULL 3–6% с 2009/10.
3. `goalie_id IS NULL` только при `empty_net`; для голов из `shot_attempts` — `goalie_id = goalie_in_net_id`, расхождений 0.
4. Пара вопросов в `scripts/ai_stats_eval.jsonl` («кому чаще всего забивал игрок X») — ожидаемые значения
   снять запросом после бэкфилла; затем `make ai-stats-eval`, `make all-tests`.
