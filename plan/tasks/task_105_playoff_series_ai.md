# Задача 105. Плей-офф: серии и ответы ИИ

**Блок:** релиз+ · **Надобность:** 🟠
**Область:** `data_tables/t.playoff_series.sql` (новый), `data_tables/migrations/`,
`data_tables/ai_reader_grants.sql`, `Makefile`, `telegram_bot/ai_stats.py`,
`telegram_bot/ai_router.py`, `telegram_bot/prompts/`, `scripts/ai_stats_eval.jsonl`, тесты.
**Зависит от:** 104.
**Источник:** решение человека 2026-10-31 (ИИ отвечает про плей-офф, экраны — позже); дизайн —
[`spec`](../../docs/superpowers/specs/2026-10-31-playoffs-data-design.md), разделы «Серии» и
«ИИ»; реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

Представление `playoff_series` из `games` (пара, счёт серии, победитель, обладатель Кубка),
доступ к нему у ИИ, промпты перестают отказывать на плей-офф, eval-кейсы.

## Зачем и на что влияет

- «Кто выиграл Кубок 2024», «как закончилась серия», «лучший бомбардир плей-офф» — ИИ отвечает
  по нашей БД. Новых экранов бота нет.
- Серии строятся из уже загруженных игр — без нового источника и загрузки; текущий плей-офф
  обновляется ежедневным sync.

## Степень важности

🟠: без неё данные плей-офф есть, а ИИ на них отказывает.

## Подробно

Строки кода — снимок `origin/develop` = `15483fb`, сверять.

- **Представление** `playoff_series` по `games WHERE game_type = 3`: `round = (game_id / 100) % 100`,
  `series_number = (game_id / 10) % 10`; колонки `season_id, round, series_number, team_a_id,
  team_b_id, wins_a, wins_b, games_played, winner_id, first_day, last_day`. Пара:
  `team_a_id = LEAST(home_team_id, away_team_id)`, `team_b_id = GREATEST(…)`. `winner_id` —
  команда с 4 победами, `NULL`, пока серия идёт; Кубок — `winner_id` серии `round = 4`.
- **Где DDL.** `data_tables/t.playoff_series.sql` с `CREATE OR REPLACE VIEW … ;` — в `DDL_TABLES`
  сразу после `t.games.sql` (`Makefile:26`): `db_drop_all_tables.sql` уносит представление
  `CASCADE`-ом от `games`, а применённая миграция после `db-reset` не повторяется — пересоздаёт
  только `db-tables`. Миграция подключает тот же файл (`\ir`, как 0015).
- **Доступ.** `'playoff_series'` — в массив `ai_reader_grants.sql` и в `ai_stats.AI_TABLES`
  (их сверяет `test_grants_are_exactly_select_on_ai_tables`). `ai_stats._schema()` (`:187–196`)
  ищет только `CREATE TABLE` и иначе бросает `RuntimeError` — научить читать
  `CREATE OR REPLACE VIEW`.
- **Промпты.** Аннотация `playoff_series` и обладателя Кубка в `stats_schema_notes.md`; из
  `ai_router.py` (`:55`, `:69`, `:80`), `stats_answer.md:19`, `stats_reviewer.md:8` уходит
  «плей-офф нет / отказ».
- **Eval.** Кейсы в `scripts/ai_stats_eval.jsonl`: обладатель Кубка, счёт серии, бомбардир
  плей-офф сезона, «голы в сезоне» по умолчанию без плей-офф.

### Критерии приёмки

- `make db-reset` сохраняет представление и GRANT; `test_db_nhl` зелёный на БД.
- Тест: схема для промпта содержит `playoff_series`.
- ИИ на dev-боте верно называет обладателя Кубка 2024 и счёт серии; `make ai-stats-eval` —
  новые кейсы проходят.

### Подводные камни

- Номер миграции — сверять с `origin/develop` перед PR.
