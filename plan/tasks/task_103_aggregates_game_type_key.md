# Задача 103. Плей-офф: `game_type` в ключе сезонных агрегатов

**Блок:** релиз+ · **Надобность:** 🟠
**Область:** `data_tables/migrations/`, `data_tables/t.{teams_stats,players_season_stats,goalies_season_stats,players_advanced_stats,players_shot_types}.sql`,
`pipeline/load_season_modern.py`, `telegram_bot/bot_messages.py`, `telegram_bot/ai_stats.py`,
`scripts/verify_skater_reports_schema.sql`, тесты.
**Зависит от:** 102.
**Источник:** решение человека 2026-10-31 (итоги плей-офф — в те же сезонные таблицы, вариант B);
дизайн — [`spec`](../../docs/superpowers/specs/2026-10-31-playoffs-data-design.md), разделы
«Сезонные агрегаты», «Загрузка → Ключи upsert», «Потребители»; реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

Пять сезонных таблиц получают колонку `game_type` и PK `(…, season_id, game_type)`; загрузчик
пишет `2` с новым ключом upsert; каждый экран бота и предпроверки ИИ читают только регулярку.
Данные не меняются — задача готовит схему и потребителей к строкам плей-офф (104).

## Зачем и на что влияет

- Итоги плей-офф игроков, вратарей и команд (официальные цифры stats API, включая Corsi и зоны)
  хранятся рядом с регуляркой и отличаются только `game_type`.
- Пропущенный фильтр после 104 молча сложит регулярку с плей-офф или задублирует строки —
  поэтому фильтры ставятся здесь, до данных, и проверяются фикстурами с типом 3.

## Степень важности

🟠: обязательна до загрузки итогов плей-офф (104).

## Подробно

Строки кода — снимок `origin/develop` = `15483fb`, сверять.

- **Миграция** (номер — следующий свободный по `origin/develop`): в `teams_stats`,
  `players_season_stats`, `goalies_season_stats`, `players_advanced_stats`, `players_shot_types` —
  `game_type smallint`, бэкфилл `2`, `NOT NULL`, `CHECK (game_type IN (2, 3))`; PK →
  `(team_id | player_id, season_id, game_type)`; индексы `(season_id)` → `(season_id, game_type)`.
  FK на `rosters` / `teams` остаются `(id, season_id)`. Парные `t.*.sql` — синхронно.
- **Загрузчик:** строки агрегатов несут `game_type = 2`; пять вызовов `execute_upsert` агрегатов
  (`:1627` `teams_stats`, `:1674`, `:1718`, `:1742`, `:1766`) — `conflict_columns`
  `[…, "season_id", "game_type"]`. У `teams` (`:1608`) и `rosters` (`:1653`) ключ прежний.
- **Потребители — фильтр `<alias>.game_type = 2`:** `bot_messages.py` — `_pss_join_sql`
  (`:105`, `:107`, `:112`), лидерборд (`:1162`), `teams_stats` (`:1035`, `:1426`, `:1677`,
  `:2157`), профиль игрока и вратаря (`:1579`, `:1600`), `:1787`, `:1900`; `ai_stats.py` —
  `_OVERVIEW_SKATER_SEASONS_SQL` (`:85`) и `_OVERVIEW_GOALIE_SEASONS_SQL` (`:95`).
  `database.py:35–83` и `leaderboard_specs.py` — белые списки, `game_type` туда не добавляется.
- `scripts/verify_skater_reports_schema.sql` — проверки PK под новый ключ.
- `modeling/` агрегаты не читает.

### Критерии приёмки

- В фикстурах — строка `game_type = 3` в каждой агрегатной таблице; каждый экран и
  `player_overview` показывают регулярку без дублей.
- На `nhl_dev` после миграции у агрегатов одна группа `game_type = 2`, числа строк прежние.
- `make all-tests` зелёный, `test_db_nhl` прогнан на БД.

### Подводные камни

- Номер миграции — сверять с `origin/develop` перед PR.
- Бюджет 300 строк: 12 мест фильтра и тесты к каждому экрану — при перерасходе разделить на
  «схема + загрузчик» и «потребители», но обе части — до 104.
