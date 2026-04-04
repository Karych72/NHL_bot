# Реализация отчётов по полевым игрокам (раздел 2.1) — v2

**Статус:** выполнено по требованиям через [`skater_reports_plan_realize.md`](./skater_reports_plan_realize.md). Детали — [`plan/README.md`](../README.md).

## Обзор

Цель: добавить в проект высокоприоритетные skater-отчёты из NHL Stats API так, чтобы они естественно встроились в текущую архитектуру `pipeline -> PostgreSQL -> telegram_bot`.

В этой версии плана scope разделён на 2 независимых направления:

1. `leaderboards` — новые топы в боте по продвинутым skater-метрикам.
2. `player detail` — отдельный отчёт по типам бросков для конкретного игрока.

Это разделение важно, потому что текущий бот хорошо поддерживает leaderboard-сценарии через callback-кнопки, но не поддерживает ввод имени игрока внутри текущего диалога без дополнительного UX.

---

## Что входит в реализацию

### 1. Новые данные в БД

- Новая таблица `players_advanced_stats`
- Новая таблица `players_shot_types`
- Расширение `players_season_stats` колонками:
  - `oz_faceoff_pct`
  - `dz_faceoff_pct`
  - `nz_faceoff_pct`
  - `shootout_goals`
  - `shootout_shots`
  - `shootout_pct`
  - `shootout_gd_goals`

### 2. Pipeline

- Новый метод `build_player_advanced_stats()`
- Новый метод `build_player_shot_types()`
- Расширение `build_player_season_stats()` данными из `faceoffpercentages` и `shootout`
- Обновление `run()`:
  - очистка новых таблиц
  - построение новых наборов данных
  - вставка новых таблиц
  - вставка расширенного `players_season_stats`

### 3. Бот

- Новые leaderboard-хендлеры для advanced-метрик
- Отдельный сценарий для просмотра shot types конкретного игрока
- Обновление whitelist в `telegram_bot/database.py`
- Новые шаблоны Jinja2
- Обновление callback state-ов, меню и регистрации хендлеров

---

## Что не входит в этот этап

Чтобы не раздувать scope, в этот этап не входят:

- `scoringRates`
- `penalties`
- `powerplay`
- `penaltykill`
- `summaryshooting`
- `bios`
- NHL EDGE

Причина: либо данные дублируют уже существующие таблицы, либо требуют отдельного UX, либо добавляют заметную сложность без сильного выигрыша в полезности.

---

## Архитектурное решение

## 1. Таблица `players_advanced_stats`

Используется для leaderboard-метрик, которые концептуально не относятся к базовой сезонной статистике:

- `sat_pct`
- `usat_pct`
- `goals_pct`
- `oz_start_pct`
- `dz_start_pct`
- `nz_start_pct`
- `on_ice_shooting_pct`
- `ev_goals_for`
- `ev_goals_against`
- `ev_goals_for_pct`
- `pp_goals_for`
- `pp_goals_against`
- `sh_goals_for`
- `sh_goals_against`

Источник данных:

- `goalsForAgainst`
- `puckPossessions`

Важно: PDO в этот этап не включать. В исходном плане он упоминался, но в схеме не был определён. Чтобы избежать расхождения между дизайном и реализацией, PDO либо добавляется отдельной задачей позже, либо исключается из scope полностью.

## 2. Таблица `players_shot_types`

Используется для детального профиля конкретного игрока, а не как универсальная leaderboard-таблица.

Колонки:

- `goals_wrist`, `shots_wrist`
- `goals_slap`, `shots_slap`
- `goals_snap`, `shots_snap`
- `goals_backhand`, `shots_backhand`
- `goals_tip_in`, `shots_tip_in`
- `goals_deflected`, `shots_deflected`
- `goals_wrap_around`, `shots_wrap_around`

Источник данных:

- `shottype`

## 3. Расширение `players_season_stats`

Туда остаётся логично положить:

- зональные проценты вбрасываний
- буллитную статистику

Обоснование:

- это естественное расширение уже существующей сезонной skater-таблицы
- эти поля удобно использовать и в будущих карточках игрока, и в leaderboard-режиме

---

## DDL

### 1. Новый файл `data_tables/t.players_advanced_stats.sql`

```sql
CREATE TABLE IF NOT EXISTS players_advanced_stats(
    player_id                   int,
    sat_pct                     double PRECISION,
    usat_pct                    double PRECISION,
    goals_pct                   double PRECISION,
    oz_start_pct                double PRECISION,
    dz_start_pct                double PRECISION,
    nz_start_pct                double PRECISION,
    on_ice_shooting_pct         double PRECISION,
    ev_goals_for                int,
    ev_goals_against            int,
    ev_goals_for_pct            double PRECISION,
    pp_goals_for                int,
    pp_goals_against            int,
    sh_goals_for                int,
    sh_goals_against            int
);
```

### 2. Новый файл `data_tables/t.players_shot_types.sql`

```sql
CREATE TABLE IF NOT EXISTS players_shot_types(
    player_id                   int,
    goals_wrist                 int,
    shots_wrist                 int,
    goals_slap                  int,
    shots_slap                  int,
    goals_snap                  int,
    shots_snap                  int,
    goals_backhand              int,
    shots_backhand              int,
    goals_tip_in                int,
    shots_tip_in                int,
    goals_deflected             int,
    shots_deflected             int,
    goals_wrap_around           int,
    shots_wrap_around           int
);
```

### 3. Обновление `data_tables/t.players_season_stats.sql`

Добавить 7 новых колонок:

```sql
oz_faceoff_pct    double PRECISION,
dz_faceoff_pct    double PRECISION,
nz_faceoff_pct    double PRECISION,
shootout_goals    int,
shootout_shots    int,
shootout_pct      double PRECISION,
shootout_gd_goals int
```

### 4. Отдельная миграция для существующей БД

Важно: одного обновления DDL-файла недостаточно, потому что `CREATE TABLE IF NOT EXISTS` не меняет уже созданную таблицу.

Нужно отдельно выполнить migration step:

```sql
ALTER TABLE players_season_stats
    ADD COLUMN IF NOT EXISTS oz_faceoff_pct double precision,
    ADD COLUMN IF NOT EXISTS dz_faceoff_pct double precision,
    ADD COLUMN IF NOT EXISTS nz_faceoff_pct double precision,
    ADD COLUMN IF NOT EXISTS shootout_goals int,
    ADD COLUMN IF NOT EXISTS shootout_shots int,
    ADD COLUMN IF NOT EXISTS shootout_pct double precision,
    ADD COLUMN IF NOT EXISTS shootout_gd_goals int;
```

Эту миграцию лучше оформить отдельным SQL-файлом и запускать явно.

---

## Pipeline

Файл: `pipeline/load_season_modern.py`

## 1. `build_player_advanced_stats()`

Логика:

1. Загрузить `goalsForAgainst`
2. Загрузить `puckPossessions`
3. Построить два словаря по `playerId`
4. Мержить по объединению ключей, а не только по одному источнику
5. Заполнить tuple в порядке колонок таблицы

Почему именно по объединению ключей:

- если итерироваться только по `puckPossessions`, можно потерять игроков, которые есть только в `goalsForAgainst`
- pipeline должен быть устойчивым к неполному перекрытию отчётов API

## 2. `build_player_shot_types()`

Логика:

1. Запросить `shottype`
2. Преобразовать строки API в tuple
3. Вернуть список для вставки в `players_shot_types`

## 3. Расширение `build_player_season_stats()`

Добавить:

- `faceoffpercentages`
- `shootout`

И затем:

- собрать словари по `playerId`
- расширить tuple на 7 новых полей
- синхронно обновить порядок колонок в `execute_insert()`

## 4. Обновление `run()`

Необходимо:

- добавить `players_advanced_stats` и `players_shot_types` в блок очистки
- вызвать `build_player_advanced_stats()`
- вызвать `build_player_shot_types()`
- вставить данные в новые таблицы
- обновить insert-список для `players_season_stats`

---

## Бот: leaderboard vs player detail

## 1. Leaderboards

Первый этап стоит сделать только для leaderboard-метрик:

- Corsi (`sat_pct`)
- Fenwick (`usat_pct`)
- Goals For % (`goals_pct` или `ev_goals_for_pct`)
- OZ start %
- Shootout %

Это хорошо ложится на текущий паттерн:

- кнопка в меню
- callback state
- handler через `_make_stats_handler`
- текст через Jinja2

## 2. Shot types для игрока

Это отдельный UX-сценарий.

Текущая архитектура бота не поддерживает его из коробки, потому что сейчас пользователь выбирает только готовые callback-пункты. Для `players_shot_types` нужно отдельно решить один из вариантов:

1. Кнопка "Типы бросков игрока" -> затем ввод имени игрока текстом
2. Кнопка "Типы бросков лидеров" -> сначала простой leaderboard без выбора игрока
3. Переиспользование будущего сценария поиска игрока, если он уже планируется в refactoring

Для этого этапа рекомендуется выбрать вариант 2, а полноценный per-player сценарий вынести в следующий шаг.

---

## Изменения в `telegram_bot/database.py`

### 1. Добавить в `ALLOWED_TABLES`

- `players_advanced_stats`
- `players_shot_types`

### 2. Добавить в `ALLOWED_COLUMNS`

Advanced:

- `sat_pct`
- `usat_pct`
- `goals_pct`
- `oz_start_pct`
- `dz_start_pct`
- `nz_start_pct`
- `on_ice_shooting_pct`
- `ev_goals_for`
- `ev_goals_against`
- `ev_goals_for_pct`
- `pp_goals_for`
- `pp_goals_against`
- `sh_goals_for`
- `sh_goals_against`

Shot types:

- `goals_wrist`
- `shots_wrist`
- `goals_slap`
- `shots_slap`
- `goals_snap`
- `shots_snap`
- `goals_backhand`
- `shots_backhand`
- `goals_tip_in`
- `shots_tip_in`
- `goals_deflected`
- `shots_deflected`
- `goals_wrap_around`
- `shots_wrap_around`

Season stats extension:

- `oz_faceoff_pct`
- `dz_faceoff_pct`
- `nz_faceoff_pct`
- `shootout_goals`
- `shootout_shots`
- `shootout_pct`
- `shootout_gd_goals`

---

## Изменения в `player_stats()`

Текущий `player_stats()` нельзя просто переиспользовать для новых skater-таблиц без доработки.

Нужно изменить интерфейс функции так, чтобы она принимала:

- `table_name`
- `column_name`
- `secondary_sort`
- при необходимости `template_name`

Пример целевого интерфейса:

```python
player_stats(
    name_stats='Лидеры по Corsi%',
    table_name='players_advanced_stats',
    column_name='sat_pct',
    secondary_sort='ev_goals_for'
)
```

Почему это нужно:

- сейчас функция жёстко завязана на `goals` для skater и `save_percentage` для goalie
- для `players_advanced_stats` и `players_shot_types` это неверно

---

## Изменения в Telegram UI

Нужно обновить не только `bot.py`, но весь стек:

### 1. `telegram_bot/dialog_states.py`

- добавить новые state-константы
- увеличить `range(...)`, чтобы количество соответствовало числу констант

### 2. `telegram_bot/script_bot.py`

- добавить новые кнопки в меню полевых игроков

### 3. `telegram_bot/stats_handlers.py`

- зарегистрировать новые leaderboard-хендлеры
- при необходимости добавить отдельный handler для shot types

### 4. `telegram_bot/bot.py`

- подключить новые импорты
- зарегистрировать новые `CallbackQueryHandler`

---

## Шаблоны

Добавить:

- `telegram_bot/messages/season_leaders_advanced.txt`
- `telegram_bot/messages/player_shot_types.txt`

Замечание:

- шаблоны рендерятся через относительный путь `messages/...`
- поэтому нужно сохранять ту же структуру запуска бота через `make bot`

---

## Продуктовые ограничения для удобства

Чтобы отчёты были полезными, а не шумными, для advanced leaderboard стоит добавить порог квалификации.

Минимальный вариант:

- `games >= 20`

Лучший вариант:

- фильтр по `games` из `players_season_stats`
- либо join с `players_season_stats` и порог по матчам

Без такого порога топы по Corsi/Fenwick могут быть заполнены игроками с маленькой выборкой, что ухудшит UX.

---

## Порядок реализации

### Этап 1. Данные

1. Создать `t.players_advanced_stats.sql`
2. Создать `t.players_shot_types.sql`
3. Обновить `t.players_season_stats.sql`
4. Добавить отдельную SQL-миграцию для `ALTER TABLE`

### Этап 2. Pipeline

1. Реализовать `build_player_advanced_stats()`
2. Реализовать `build_player_shot_types()`
3. Расширить `build_player_season_stats()`
4. Обновить `run()` и `execute_insert()` вызовы

### Этап 3. Доступ к данным

1. Обновить `ALLOWED_TABLES`
2. Обновить `ALLOWED_COLUMNS`
3. Обновить `player_stats()` под настраиваемый `secondary_sort`

### Этап 4. Бот

1. Добавить новые callback states
2. Добавить кнопки в `script_bot.py`
3. Добавить leaderboard-хендлеры
4. Зарегистрировать их в `bot.py`
5. Добавить шаблоны

### Этап 5. Опционально

1. Добавить отдельный сценарий поиска игрока для `players_shot_types`

---

## Тестирование

### 1. Схема

- применить новые DDL
- отдельно выполнить migration SQL с `ALTER TABLE`
- убедиться, что новые таблицы и колонки реально появились в БД

### 2. Pipeline

- запустить `make season-load`
- проверить, что:
  - `players_advanced_stats` заполнена
  - `players_shot_types` заполнена
  - `players_season_stats` заполнена новыми колонками

### 3. Бот

- запустить `make bot`
- проверить новые пункты меню
- проверить leaderboard по новым метрикам
- проверить, что dynamic SQL не падает на whitelist

### 4. Качество данных

- вручную сравнить 2-3 игроков с API
- проверить, что проценты корректно конвертируются
- проверить, что игроки не теряются при merge между отчётами

---

## Рекомендуемый scope первого PR

Чтобы снизить риск, первый PR лучше ограничить следующим:

- `players_advanced_stats`
- расширение `players_season_stats`
- новые leaderboard-кнопки для advanced-метрик
- без per-player shot types UX

`players_shot_types` можно либо:

- включить только как таблицу + pipeline без UI,
- либо вынести во второй PR вместе с отдельным сценарием выбора игрока.

Такой разрез лучше подходит под текущую архитектуру проекта и уменьшает вероятность сломать диалоговую модель бота.
