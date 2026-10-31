# Задача 101. Плей-офф: колонка `games.game_type`

**Блок:** релиз+ · **Надобность:** 🟠
**Область:** `data_tables/migrations/`, `data_tables/t.games.sql`, `pipeline/load_season_modern.py`,
`tests/test_db_nhl.py`, `tests/test_pipeline_game_rows.py`.
**Зависит от:** нет. Первая из цепочки 101 → 102 → 103 → 104 → 105 → 106.
**Источник:** бывшая подзадача 37A (Задача 37 закрыта 2026-10-31, полная загрузка плей-офф
вынесена в 101–106); дизайн — [`spec`](../../docs/superpowers/specs/2026-10-31-playoffs-data-design.md),
раздел «Схема → `games`»; реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

В `games` нет типа игры, поэтому игры плей-офф нельзя отличить от регулярки. Добавить колонку
`game_type` (2 — регулярка, 3 — плей-офф), заполнить `2` у всех существующих строк, загрузчик
пишет тип из мета-записи игры. Данные не меняются: загрузчик по-прежнему берёт только `gameType=2`.

## Зачем и на что влияет

- Через `games.game_type` отличаются и все поматчевые данные плей-офф (`game_player_stats`,
  `game_goalie_stats`, `game_team_stats`, `all_goals`, `shot_attempts`, `game_three_stars`):
  они ключуются по `game_id`.
- Без колонки нельзя ни отфильтровать регулярку (102), ни загрузить плей-офф (104).

## Степень важности

🟠: фундамент цепочки плей-офф. Плей-офф 2026/27 начнётся в апреле.

## Подробно

Строки кода — снимок `origin/develop` = `15483fb`, сверять.

- Миграция `data_tables/migrations/NNNN_games_game_type.{up,down}.sql` (номер — следующий
  свободный по `origin/develop` на момент PR; на 2026-10-31 последняя — `0018`) по образцу 0002:
  `ALTER TABLE games ADD COLUMN IF NOT EXISTS game_type smallint`, бэкфилл `2`, затем
  `NOT NULL` и `CHECK (game_type IN (2, 3))`. Без BEGIN/COMMIT (миграции идут `--single-transaction`).
- Парный DDL `data_tables/t.games.sql` — то же определение колонки (правило синхрона,
  `DEVELOPMENT.md`); индекс не нужен, пока нет запроса, который им пользуется.
- Загрузчик пишет `game_type` из `gameType` мета-записи игры (`build_game_rows`, `:965`);
  фильтр `fetch_final_games` (`:839`) остаётся `gameType=2`.
- Тесты: схема в `tests/test_db_nhl.py` (колонка, NOT NULL, CHECK); в
  `test_pipeline_game_rows.py` — строка игры несёт `game_type = 2`.

### Критерии приёмки

- `make db-migrate` на `nhl_dev`; `SELECT game_type, count(*) FROM games GROUP BY 1` → одна
  группа `2` на все строки.
- `make all-tests` зелёный, `test_db_nhl` прогнан на БД.

### Подводные камни

- Номер миграции: параллельные задачи берут один номер, и `db-migrate` молча пропускает вторую —
  сверять с `origin/develop` перед PR.
