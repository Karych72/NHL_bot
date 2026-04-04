# Реализация skater reports (v2) — пошаговый план

**Статус:** выполнено (схема БД, pipeline, UI advanced / типы бросков). Детали — [`plan/README.md`](../README.md).

Источник требований: [skater_reports_plan_v2.md](./skater_reports_plan_v2.md).

---

## Пошаговый план реализации

### Этап 1. Схема БД (4 шага)

**Шаг 1.1.** Создать файл `data_tables/t.players_advanced_stats.sql` с DDL новой таблицы: **15 колонок** (14 метрик + `player_id`) — `player_id` с `PRIMARY KEY`, далее `sat_pct`, `usat_pct`, `goals_pct`, `oz_start_pct`, `dz_start_pct`, `nz_start_pct`, `on_ice_shooting_pct`, `ev_goals_for`, `ev_goals_against`, `ev_goals_for_pct`, `pp_goals_for`, `pp_goals_against`, `sh_goals_for`, `sh_goals_against`.

**Шаг 1.2.** Создать файл `data_tables/t.players_shot_types.sql` с DDL: **15 колонок** (`player_id` с `PRIMARY KEY` + 7 пар `goals_*/shots_*` для каждого типа броска).

**Шаг 1.3.** Обновить `data_tables/t.players_season_stats.sql` — добавить 7 новых колонок перед `player_id`:

- `oz_faceoff_pct`, `dz_faceoff_pct`, `nz_faceoff_pct` (`double precision`)
- `shootout_goals`, `shootout_shots`, `shootout_gd_goals` (`int`)
- `shootout_pct` (`double precision`)

**Шаг 1.4.** Создать файл миграции (например `data_tables/migrations/001_add_skater_advanced_columns.sql`) с `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` для 7 новых колонок `players_season_stats`. Без этого `CREATE TABLE IF NOT EXISTS` не изменит уже существующую таблицу.

**Шаг 1.5.** Для `players_advanced_stats` и `players_shot_types`: в DDL — `PRIMARY KEY (player_id)`; для уже существующих БД без ключа — миграция `data_tables/migrations/002_players_skater_reports_primary_keys.sql` (идемпотентное `ALTER TABLE ... ADD PRIMARY KEY`, только если ключа ещё нет). Порядок колонок буллитов в **1.3** и порядок в **1.4** могут отличаться: для приложений важны **имена и типы**, не физический порядок в `CREATE`/`ALTER`.

---

### Этап 2. Pipeline (4 шага)

Все изменения в файле `pipeline/load_season_modern.py` (класс `ModernNhlLoader`).

**Шаг 2.1.** Добавить метод `build_player_advanced_stats()`:

- Запросить `goalsForAgainst` через `fetch_paginated()` по URL вида  
  `https://api.nhle.com/stats/rest/en/skater/goalsForAgainst?cayenneExp=seasonId=...%20and%20gameTypeId=2`
- Запросить `puckPossessions` аналогично
- Построить два словаря по `playerId` (при дубликатах `playerId` в ответе API остаётся **последняя** строка — как при обычном dict comprehension)
- Итерироваться по **объединению** ключей обоих словарей (`set(dict1) | set(dict2)`)
- В JSON `goalsForAgainst` поле голов в большинстве вбросах — `powerPlayGoalFor` (без «s» в `Goal`), как в официальном API
- Для каждого игрока собрать tuple из значений в порядке колонок таблицы, используя `to_float()` / `to_int()` / `pct_from_ratio()`
- Вернуть `List[tuple]`

**Шаг 2.2.** Добавить метод `build_player_shot_types()`:

- Запросить `shottype` через `fetch_paginated()`  
  `https://api.nhle.com/stats/rest/en/skater/shottype?cayenneExp=seasonId=...%20and%20gameTypeId=2`
- Преобразовать каждую строку API в tuple из 15 значений (`player_id` + 7 пар)
- Вернуть `List[tuple]`

**Шаг 2.3.** Расширить `build_player_season_stats()`:

- Добавить запросы к API: `faceoffpercentages`, `shootout`
- Построить словари по `playerId`
- В tuple каждой строки добавить 7 новых полей перед `player_id` (зональные вбрасывания + буллиты)

**Шаг 2.4.** Обновить `run()`:

- В `TRUNCATE` добавить `players_advanced_stats`, `players_shot_types`
- Вызвать `build_player_advanced_stats()` и `build_player_shot_types()`
- Добавить `execute_insert()` для обеих новых таблиц с полными списками колонок
- В `execute_insert()` для `players_season_stats` добавить 7 новых имён колонок и расширить tuples

---

### Этап 3. Слой доступа к данным (3 шага)

**Шаг 3.1.** В `telegram_bot/database.py` добавить в `ALLOWED_TABLES`: `players_advanced_stats`, `players_shot_types`.

**Шаг 3.2.** В `ALLOWED_COLUMNS` добавить (и **синхронизировать** с наборами `ADVANCED_STATS_COLUMNS` / `SHOT_TYPES_COLUMNS` в `bot_messages.py`, иначе `validate_column` и логика JOIN разъедутся):

- advanced: `sat_pct`, `usat_pct`, `goals_pct`, `oz_start_pct`, `dz_start_pct`, `nz_start_pct`, `on_ice_shooting_pct`, `ev_goals_for`, `ev_goals_against`, `ev_goals_for_pct`, `pp_goals_for`, `pp_goals_against`, `sh_goals_for`, `sh_goals_against`
- shot types: все `goals_*` / `shots_*` из DDL `players_shot_types`
- season extension: `oz_faceoff_pct`, `dz_faceoff_pct`, `nz_faceoff_pct`, `shootout_goals`, `shootout_shots`, `shootout_pct`, `shootout_gd_goals`
- для лидеров по `players_advanced_stats` вторичная сортировка по умолчанию — колонка `goals` из **joined** `players_season_stats`; поле `goals` уже должно быть в whitelist (`players_season_stats`)

**Шаг 3.3.** Обновить `player_stats()` в `telegram_bot/bot_messages.py`:

- Сделать вторичную сортировку параметром (`secondary_sort`), с дефолтом как сейчас: `goals` для `players_season_stats`, `save_percentage` для вратарей
- Для **advanced** leaderboard — квалификационный порог (`games >= 20`) через `INNER JOIN` с `players_season_stats`
- Для **shot types** тот же порог **не** обязателен: при вторичной сортировке через `players_season_stats` используется `LEFT JOIN` без фильтра по играм (игроки с малым GP остаются в выдаче); при необходимости порог можно добавить отдельным продуктовым решением

---

### Этап 4. Telegram-бот (5 шагов)

**Шаг 4.1.** `telegram_bot/dialog_states.py`: новые константы для Corsi, Fenwick, Goals For %, OZ Start %, Shootout %; увеличить `range(...)` под общее число констант.

**Шаг 4.2.** `telegram_bot/script_bot.py` — в `bot_player_field()` добавить кнопки для этих пяти метрик.

**Шаг 4.3.** `telegram_bot/stats_handlers.py` — зарегистрировать хендлеры через `_make_stats_handler` и `partial(player_stats, ..., table_name, column_name, secondary_sort=...)`.

**Шаг 4.4.** `telegram_bot/bot.py` — импорты констант и хендлеров, `CallbackQueryHandler` в `states[FIRST]`.

**Шаг 4.5.** Шаблоны: при необходимости `telegram_bot/messages/season_leaders_advanced.txt` или переиспользование `season_leaders_players.txt`.

---

### Этап 5. Тестирование (4 шага)

**Шаг 5.1.** Схема: применить DDL, выполнить миграции `001_add_skater_advanced_columns.sql` и при необходимости `002_players_skater_reports_primary_keys.sql`, проверить таблицы, колонки и первичные ключи.

**Шаг 5.2.** Pipeline: `make season-load`, проверить заполнение новых таблиц и колонок.

**Шаг 5.3.** Бот: `make bot`, новые пункты меню, отсутствие ошибок whitelist.

**Шаг 5.4.** Качество: выборочное сравнение с NHL API, проверка процентов и merge ключей в advanced stats.

---

## Рекомендуемый scope первого PR

1. `players_advanced_stats` (DDL + pipeline + whitelist + leaderboard-кнопки)
2. Расширение `players_season_stats` (7 колонок + миграция + pipeline + кнопка буллитов)

`players_shot_types` — только таблица + pipeline **без UI**, либо второй PR вместе с UX выбора игрока.

---

## Граф зависимостей шагов

```
1.1, 1.2, 1.3, 1.4  (параллельно); 1.5 — миграция PK для БД, созданных до появления PRIMARY KEY в DDL
        │
        ▼
2.1, 2.2  (параллельно)   2.3  (зависит от 1.3)
        │                   │
        └───────┬───────────┘
                ▼
              2.4
                │
                ▼
         3.1, 3.2  (параллельно)
                │
                ▼
              3.3
                │
                ▼
     4.1 → 4.2 → 4.3 → 4.4 → 4.5
                                │
                                ▼
                    5.1 → 5.2 → 5.3 → 5.4
```

---

## Ссылки на код (на момент составления плана)

- Миграции: `data_tables/migrations/001_add_skater_advanced_columns.sql`, `002_players_skater_reports_primary_keys.sql`
- Pipeline: `pipeline/load_season_modern.py` — `build_player_season_stats()`, `run()`, `TRUNCATE`, `execute_insert`
- Сообщения и запросы лидеров: `telegram_bot/bot_messages.py` — `player_stats()`
- Whitelist: `telegram_bot/database.py`
- Состояния и меню: `telegram_bot/dialog_states.py`, `telegram_bot/script_bot.py`, `telegram_bot/bot.py`, `telegram_bot/stats_handlers.py`
