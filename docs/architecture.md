# NHL Bot — Техническая архитектура

## Обзор проекта

NHL Bot — это Telegram-бот для просмотра статистики NHL. Проект состоит из двух основных подсистем:

1. **Pipeline** (`pipeline/`) — ETL-загрузчик, который забирает данные из официального NHL API и записывает их в PostgreSQL.
2. **Telegram Bot** (`telegram_bot/`) — интерактивный бот с многоуровневым inline-меню, который читает данные из PostgreSQL и отображает статистику через Jinja2-шаблоны.

Стек: Python 3, PostgreSQL, python-telegram-bot 13.15, psycopg2, requests, Jinja2.

---

## Дерево проекта

```
NHL_bot/
├── .env.example                        # Шаблон переменных окружения
├── .gitignore
├── Makefile                            # Сборка, запуск, инициализация БД
├── README.md                           # Quick start
├── requirements.txt                    # Зависимости Python
│
├── data_tables/                        # DDL — схемы всех таблиц PostgreSQL
│   ├── t.all_goals.sql
│   ├── t.game_goalie_stats.sql
│   ├── t.game_player_stats.sql
│   ├── t.game_team_stats.sql
│   ├── t.games.sql
│   ├── t.goalies_season_stats.sql
│   ├── t.players_season_stats.sql
│   ├── t.rosters.sql
│   ├── t.teams.sql
│   └── t.teams_stats.sql
│
├── docs/                               # Документация (архитектура, исследования API, гайды)
│   ├── architecture.md                 # ← этот файл
│   ├── api_data_research.md
│   ├── data_loading_and_bot.md
│   └── user_journey_stats.md
│
├── plan/                               # Планы (индекс: plan/README.md)
│   ├── README.md
│   ├── deprecated_plan/                # Выполненные планы (UX, skater reports)
│   ├── refactoring_plan.md
│   ├── refactoring_plan_2.md
│   └── …                               # тесты БД, tonight games и др.
│
├── pipeline/                           # ETL: NHL API → PostgreSQL
│   └── load_season_modern.py           # Класс ModernNhlLoader
│
├── telegram_bot/                       # Telegram-бот
│   ├── bot.py                          # Точка входа: Updater + ConversationHandler
│   ├── config.py                       # Чтение .env-переменных
│   ├── database.py                     # Пул соединений + fetch_all + whitelist
│   ├── dialog_states.py                # FSM-состояния и callback ID
│   ├── script_bot.py                   # Обработчики меню навигации
│   ├── stats_handlers.py              # Фабрика обработчиков статистики
│   ├── bot_messages.py                 # Формирование текстов ответов (SQL + шаблоны)
│   ├── template_funcs.py              # Обёртка Jinja2
│   ├── sql_query.py                    # Legacy-обёртка над database.fetch_all
│   ├── messages/                       # Jinja2-шаблоны сообщений
│   │   ├── game_message.txt
│   │   ├── league_table.txt
│   │   ├── season_leaders_players.txt
│   │   └── team_stats.txt
│   └── queries/                        # PL/pgSQL функции
│       ├── get_game_stats.sql
│       ├── get_goals_game.sql
│       └── get_goalies_game.sql
│
└── all_data/                           # Runtime-директория (gitignored)
    └── dataframes/
        ├── rosters/
        ├── players/
        ├── teams/
        └── myself_analyses/
```

---

## Конфигурация

### Переменные окружения (`.env`)

| Переменная | Описание | Значение по умолчанию |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота (обязательно) | — |
| `PG_HOST` | Хост PostgreSQL | `localhost` |
| `PG_PORT` | Порт PostgreSQL | `5432` |
| `PG_USER` | Пользователь PostgreSQL | текущий пользователь ОС |
| `PG_DATABASE` | Имя базы данных | `postgres` |
| `DATE_FROM` | Начало диапазона дат для загрузки игр | `2025-10-01` |
| `DATE_TO` | Конец диапазона дат для загрузки игр | текущая дата |
| `SEASON_ID` | Идентификатор сезона NHL API (напр. `20252026`) | `20252026` |
| `CURRENT_SEASON` | Человекочитаемая метка сезона | `25/26` |

Конфигурация считывается модулем `telegram_bot/config.py`. Pipeline повторно использует тот же модуль, добавляя его директорию в `sys.path`.

### Makefile-цели

| Цель | Описание |
|---|---|
| `make setup` | Создаёт `.venv` (с учётом архитектуры arm64/x86_64), устанавливает зависимости |
| `make env-example` | Копирует `.env.example` → `.env`, если файл не существует |
| `make dirs` | Создаёт директории `all_data/dataframes/*` |
| `make db-init` | Применяет DDL из `data_tables/*.sql` и PL/pgSQL из `telegram_bot/queries/*.sql` |
| `make db-init-local` | То же, что `db-init`, но с текущим пользователем ОС вместо `postgres` |
| `make season-load` | Запуск ETL-загрузчика `load_season_modern.py` |
| `make bot` | Запуск Telegram-бота (проверяет наличие токена) |
| `make run-bot` | `setup` + `env-example` + `bot` |
| `make run-pipeline` | `setup` + `env-example` + `dirs` + `db-init-local` + `pipeline` |
| `make run-full` | Полный цикл: venv + db-init + pipeline + bot |
| `make run-local` | Запуск бота без pipeline (для работы с уже загруженными данными) |

### Зависимости (`requirements.txt`)

| Пакет | Назначение |
|---|---|
| `python-telegram-bot==13.15` | Telegram Bot API, ConversationHandler, InlineKeyboard |
| `psycopg2-binary` | Драйвер PostgreSQL |
| `requests` | HTTP-запросы к NHL API |
| `jinja2` | Шаблонизатор для формирования текстов сообщений |
| `pandas` | Указан, но не используется в текущем коде |

---

## Архитектура Pipeline (ETL)

### Модуль: `pipeline/load_season_modern.py`

Класс `ModernNhlLoader` реализует полный цикл загрузки данных сезона NHL.

### Источники данных (NHL API)

| API | Базовый URL | Данные |
|---|---|---|
| Stats API | `api.nhle.com/stats/rest/en/` | Команды, статистика игроков/вратарей/команд за сезон, список игр |
| Web API | `api-web.nhle.com/v1/` | Турнирная таблица, составы, play-by-play, boxscore |

### Поток данных

```
NHL Stats API                          NHL Web API
     │                                      │
     ├─ /team ──────────────────────┐        ├─ /standings/now
     ├─ /team/summary               │        ├─ /roster/{tri}/{season}
     ├─ /skater/summary             │        ├─ /gamecenter/{id}/play-by-play
     ├─ /goalie/summary             │        └─ /gamecenter/{id}/boxscore
     └─ /game (finished)            │
                                    │
              ┌─────────────────────┘
              ▼
     ModernNhlLoader.run()
              │
              ├── 1. load_team_reference()
              │       → team_meta_by_id, team_standings_by_abbrev
              │
              ├── 2. build_teams_and_stats()
              │       → teams_rows, teams_stats_rows
              │
              ├── 3. build_rosters(teams_rows)
              │       → roster_rows (дедупликация по player_id)
              │
              ├── 4. build_player_season_stats()
              │       → skater_rows (сводная за сезон, пагинация)
              │
              ├── 5. build_goalie_season_stats()
              │       → goalie_rows (сводная за сезон, пагинация)
              │
              ├── 6. fetch_final_games()
              │       → games_meta (завершённые игры за DATE_FROM..DATE_TO)
              │
              ├── 7. build_game_rows(games_meta)
              │       Для каждой игры: play-by-play + boxscore
              │       → games_rows, all_goals_rows, game_team_rows,
              │         game_player_rows, game_goalie_rows
              │
              └── 8. PostgreSQL: TRUNCATE ALL → bulk INSERT (execute_values)
```

### Стратегия загрузки

- **Полная перезагрузка:** все 10 таблиц очищаются (`TRUNCATE`) перед вставкой. Идемпотентность гарантирована.
- **Пагинация:** API с ответами `{data, total}` выгружаются постранично (`fetch_paginated`, размер страницы 200–1000).
- **Retry-логика:** до 10 попыток, экспоненциальный backoff, обработка HTTP 429 (Rate Limit) через заголовок `Retry-After`.
- **Таймаут запросов:** 30 секунд.
- **Транзакционность:** все INSERT выполняются в одной транзакции; при ошибке — `ROLLBACK`.

### Агрегация play-by-play

Для каждой игры парсятся события из play-by-play и агрегируются в командную статистику:

| Тип события | Агрегация |
|---|---|
| `goal` | Счёт по периодам, список голов с ассистентами |
| `penalty` | PIM по командам |
| `hit` | Хиты по командам |
| `giveaway` / `takeaway` | Потери / отборы |
| `blocked-shot` | Блокированные броски (инвертируются: автор — защищающаяся команда) |
| `faceoff` | Вбрасывания: выигранные / проведённые |

Broски (SOG) берутся из boxscore (`homeTeam.sog`, `awayTeam.sog`).

---

## Архитектура Telegram Bot

### Точка входа: `bot.py`

Использует `python-telegram-bot` v13.15 (callback-based, не async). Основной механизм — `ConversationHandler` с двумя состояниями FSM.

### FSM (Finite State Machine)

```
                    /stats
                      │
                      ▼
               ┌─────────────┐
               │    FIRST     │  ← Навигация по меню
               │              │
               │  Главное меню│
               │  ├─ Дайджест дня ──────────────────┐
               │  ├─ Статистика игроков              │
               │  │   ├─ Полевые игроки              │
               │  │   │   ├─ Очки                    │
               │  │   │   ├─ Голы                     │
               │  │   │   ├─ Ассисты                  │
               │  │   │   ├─ Хиты                     │
               │  │   │   ├─ +/-                      │
               │  │   │   ├─ Игровое время            │
               │  │   │   ├─ Штрафные минуты          │
               │  │   │   └─ Блоки ──────────────┐    │
               │  │   └─ Вратари                  │    │
               │  │       ├─ Победы               │    │
               │  │       ├─ % отр. бросков        │    │
               │  │       └─ Сухари ─────────┐    │    │
               │  └─ Статистика команд        │    │    │
               │      ├─ % очков              │    │    │
               │      ├─ Большинство          │    │    │
               │      └─ Меньшинство ────┐    │    │    │
               └─────────────────────────┘────┘────┘────┘
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │   SECOND     │  ← Показ результата
                                  │              │
                                  │  [В меню]    │──→ FIRST
                                  │  [Выход]     │──→ END
                                  └──────────────┘
```

### Состояния и callback ID (`dialog_states.py`)

| ID | Константа | Назначение |
|---|---|---|
| 0 | `CHOOSE_STATS` | Возврат в главное меню |
| 1 | `TEAM_STATS` | Подменю статистики команд |
| 2 | `PLAYER_STATS` | Подменю статистики игроков |
| 3 | `DAY_DIGEST` | Дайджест дня |
| 4 | `PLAYER_FIELD` | Подменю полевых игроков |
| 5 | `PLAYER_GOALIE` | Подменю вратарей |
| 6–8 | `TEAM_PROCENT_WINS`, `TEAM_POWER_PLAY`, `TEAM_POWER_KILL` | Показатели команд |
| 9–16 | `PLAYER_POINTS` ... `PLAYER_ICE_TIME` | Статистики полевых игроков |
| 17–19 | `GOALIE_WINS`, `GOALIE_PERCENTAGE`, `GOALIE_SHOOTOUTS` | Статистики вратарей |
| 20 | `END_CONVERSATION` | Завершение диалога |

### Модульная структура бота

```
bot.py
  │
  ├── config.py              Чтение переменных окружения
  │
  ├── dialog_states.py       FSM-состояния, callback ID, build_menu()
  │
  ├── script_bot.py          Навигация: stats(), stats_over(),
  │                          bot_team_stats(), bot_player_stats(),
  │                          bot_player_field(), bot_player_goalie(), end()
  │
  ├── stats_handlers.py      Фабрика _make_stats_handler():
  │   │                      создаёт обработчики, вызывающие data_func()
  │   │                      и показывающие кнопки «Назад» / «Выход»
  │   │
  │   └── bot_messages.py    Бизнес-логика формирования текстов:
  │       │                  day_digest(), player_stats(),
  │       │                  team_table(), team_stats(), game_message()
  │       │
  │       ├── database.py    Пул (SimpleConnectionPool 1–5 conn),
  │       │                  get_connection(), fetch_all(),
  │       │                  whitelist-валидация (ALLOWED_TABLES, ALLOWED_COLUMNS)
  │       │
  │       └── template_funcs.py   read_template() + output_text()
  │                               → Jinja2 Template.render()
  │
  └── sql_query.py           Legacy-обёртка → database.fetch_all
```

### Фабрика обработчиков (`stats_handlers.py`)

Все конечные обработчики статистики создаются единообразно через `_make_stats_handler(data_func, back_label)`:

```python
handler = _make_stats_handler(
    partial(player_stats, 'Лучшие бомбардиры', 'players_season_stats', 'points')
)
```

Фабрика:
1. Отвечает на callback query.
2. Вызывает `data_func()` для получения текста.
3. Редактирует сообщение с `parse_mode='MARKDOWN'`.
4. Добавляет кнопки «В главное меню» и «Выход».
5. Переводит FSM в состояние `SECOND`.

Это устраняет дублирование: 17 обработчиков создаются в одну строку каждый.

---

## Слой данных

### Connection Pool (`database.py`)

- `SimpleConnectionPool` из psycopg2 (1–5 соединений).
- Контекстный менеджер `get_connection()`: auto-rollback при ошибке, возврат в пул при выходе.
- Функция `close_pool()` для корректного завершения.

### Whitelist-валидация

Для защиты от SQL-инъекций при динамическом построении запросов используется двойная защита:
1. **Whitelist:** `ALLOWED_TABLES` (10 таблиц) и `ALLOWED_COLUMNS` (80+ колонок) — проверка перед использованием.
2. **psycopg2.sql:** идентификаторы оборачиваются в `sql.Identifier()`, параметры подставляются через `%s`.

### Функция `fetch_all()`

Универсальный метод выполнения SELECT-запросов:

```python
fetch_all(query_text, params, columns) → {col1: [...], col2: [...], 'count_rows': N}
```

Возвращает словарь, где ключи — имена колонок, значения — списки значений. Добавляется ключ `count_rows` с количеством строк.

---

## Схема базы данных

### ER-диаграмма (логические связи)

```
┌───────────┐       ┌─────────────┐       ┌──────────────────────┐
│   teams   │◄──┐   │   rosters   │       │  players_season_stats │
│           │   │   │             │       │                      │
│ team_id   │   ├───┤ current_    │   ┌──►│ player_id            │
│ name      │   │   │ team_id     │   │   │ goals, assists, ...  │
│ division  │   │   │ player_id ──┼───┤   └──────────────────────┘
│ conference│   │   │ name        │   │
│ abbrevia. │   │   │ position    │   │   ┌──────────────────────┐
│ short_name│   │   │ lastName    │   │   │ goalies_season_stats │
└───────────┘   │   └─────────────┘   │   │                      │
                │                     └──►│ player_id            │
┌───────────┐   │                         │ wins, save_pct, ...  │
│teams_stats│   │                         └──────────────────────┘
│           │   │
│ team_id ──┼───┘   ┌─────────────┐       ┌──────────────────────┐
│ wins      │       │    games    │       │   game_team_stats    │
│ points    │       │             │       │                      │
│ pp%       │       │ game_id ────┼──┬───►│ game_id              │
│ pk%       │       │ day         │  │    │ team_id              │
└───────────┘       │ home_team_id│  │    │ goals, shots, hits.. │
                    │ away_team_id│  │    └──────────────────────┘
                    │ winner_id   │  │
                    └─────────────┘  │    ┌──────────────────────┐
                                     │    │  game_player_stats   │
                                     ├───►│                      │
                                     │    │ game_id, player_id   │
                                     │    │ goals, assists, ...  │
                                     │    └──────────────────────┘
                                     │
                                     │    ┌──────────────────────┐
                                     ├───►│  game_goalie_stats   │
                                     │    │                      │
                                     │    │ game_id, player_id   │
                                     │    │ saves, shots, ...    │
                                     │    └──────────────────────┘
                                     │
                                     │    ┌──────────────────────┐
                                     └───►│     all_goals        │
                                          │                      │
                                          │ game_id              │
                                          │ goal_player_id       │
                                          │ assist_player1_id    │
                                          │ period, time         │
                                          └──────────────────────┘
```

### Таблицы

#### `teams` — Справочник команд

| Колонка | Тип | Описание |
|---|---|---|
| `team_id` | bigint | NHL team ID |
| `name` | varchar(30) | Полное название |
| `division_name` | varchar(30) | Дивизион (Atlantic, Metropolitan, Central, Pacific) |
| `arena` | varchar(30) | Арена (не заполняется pipeline) |
| `conference_name` | varchar(30) | Конференция (Eastern, Western) |
| `abbreviation` | varchar(10) | Трёхбуквенный код (TOR, NYR, ...) |
| `first_year_of_play` | int | Год основания (не заполняется pipeline) |
| `city` | varchar(30) | Город |
| `active` | boolean | Активная франшиза |
| `short_name` | varchar(30) | Краткое название для отображения |

#### `teams_stats` — Статистика команд за сезон

| Колонка | Тип | Описание |
|---|---|---|
| `team_id` | int | FK → teams |
| `games_played` | int | Сыгранные матчи |
| `wins`, `losses`, `ot` | int | Победы, поражения, OT-поражения |
| `points` | int | Очки |
| `procent_points` | double | Процент набранных очков |
| `goals_per_game` | double | Голов за игру |
| `goals_against_per_game` | double | Пропущено голов за игру |
| `power_play_percentage` | double | Реализация большинства (%) |
| `power_play_goals` | int | Голов в большинстве |
| `power_play_goals_against` | int | Пропущено в большинстве |
| `power_play_opportunities` | int | Возможностей большинства |
| `penalty_kill_percentage` | double | Игра в меньшинстве (%) |
| `shots_per_game` | double | Бросков за игру |
| `shots_allowed` | double | Бросков пропущено за игру |
| `face_off_win_percentage` | double | Процент выигранных вбрасываний |

#### `rosters` — Составы команд

| Колонка | Тип | Описание |
|---|---|---|
| `player_id` | bigint | NHL player ID |
| `name` | varchar(50) | Полное имя |
| `position` | varchar(5) | Позиция (C, LW, RW, D, G) |
| `jersey_number` | int | Игровой номер |
| `currentAge` | int | Возраст |
| `lastName` | varchar(50) | Фамилия (для отображения) |
| `nationality` | varchar(10) | Код страны |
| `captain` | boolean | Капитан |
| `alternate_captain` | boolean | Ассистент капитана |
| `rookie` | boolean | Новичок |
| `abbreviation` | varchar(10) | Код позиции (дублирует position) |
| `current_team_id` | int | FK → teams |

#### `players_season_stats` — Сезонная статистика полевых игроков

28 колонок, основные: `player_id`, `goals`, `assists`, `points`, `pim`, `shots`, `games`, `hits`, `blocked`, `plus_minus`, `time_on_ice_per_game`, `power_play_goals`, `power_play_points`, `face_off_pct`, `shot_pct`, `game_winning_goals`.

#### `goalies_season_stats` — Сезонная статистика вратарей

24 колонки, основные: `player_id`, `wins`, `losses`, `shutouts`, `save_percentage`, `goal_against_average`, `games`, `saves`, `shots_against`, `goals_against`.

#### `games` — Матчи

| Колонка | Тип | Описание |
|---|---|---|
| `game_id` | bigint | UNIQUE. NHL game ID |
| `day` | date | Дата матча |
| `home_team_id` | bigint | FK → teams |
| `away_team_id` | bigint | FK → teams |
| `winner_id` | bigint | FK → teams |
| `is_overtime` | boolean | Овертайм |
| `is_shootouts` | boolean | Буллиты |
| `season` | varchar(10) | Метка сезона (25/26) |

#### `all_goals` — Все голы

| Колонка | Тип | Описание |
|---|---|---|
| `goal_player_id` | bigint | Автор гола |
| `total_goals` | int | Номер гола автора в сезоне |
| `assist_player1_id` | bigint | Первый ассистент |
| `assist_total_1` | int | Номер передачи в сезоне |
| `assist_player2_id` | bigint | Второй ассистент |
| `assist_total_2` | int | Номер передачи в сезоне |
| `empty_net` | boolean | Гол в пустые ворота |
| `winner_goal` | boolean | Победный гол |
| `is_ppg` / `is_shg` | boolean | Гол в большинстве / меньшинстве |
| `team_id` | int | FK → teams |
| `game_id` | bigint | FK → games |
| `period` | int | Период |
| `time` | varchar(20) | Время гола в периоде |
| `goals_away` / `goals_home` | int | Счёт на момент гола |

#### `game_team_stats` — Командная статистика за матч

UNIQUE(`game_id`, `team_id`). Содержит: `goals`, `field` (home/away), `pim`, `shots`, `face_off_win_percentage`, `blocked`, `takeaways`, `giveaways`, `hits`, `fst/snd/trd_period_goals`.

#### `game_player_stats` — Статистика полевого игрока за матч

UNIQUE(`game_id`, `player_id`). Содержит: `time_on_ice`, `goals`, `assists`, `shots`, `hits`, `blocked`, `plus_minus`, `power_play_goals`, `penalty_minutes`, и др.

#### `game_goalie_stats` — Статистика вратаря за матч

UNIQUE(`game_id`, `player_id`). Содержит: `timeOnIce`, `shots`, `saves`, `save_percentage`, `decision` (boolean: победа), детализация по ситуациям (PP/SH/EV).

### PL/pgSQL функции

#### `get_game_stats(game_id)` → game_team_stats + games + teams

Возвращает: `goals`, `pim`, `blocked`, `hits`, `shots`, `is_overtime`, `is_shootouts`, `field`, `team_name`. Сортировка: `field DESC` (home первым).

#### `get_goals_game(game_id)` → all_goals + rosters (JOIN по scorer, assist1, assist2)

Возвращает: `scorer`, `assist_1`, `assist_2` (фамилии), `period`, `goal_time`, `home_score`, `away_score`. Сортировка: `period, time`.

#### `get_goalies_game(game_id)` → game_goalie_stats + rosters + games

Возвращает: `shots`, `saves`, `timeonice`, `lastname`, `save_percentage`, `is_home`. Сортировка: `is_home DESC` (домашний вратарь первым).

---

## Jinja2-шаблоны

Все шаблоны расположены в `telegram_bot/messages/` и рендерятся через `template_funcs.output_text()`.

### `game_message.txt` — Карточка матча

```
*Rangers Lightning 3:2 (OT)*
1:0 Panarin(Fox, Zibanejad) 5:23
1:1 Kucherov(Point) 12:45
...
*Броски:* 32 - 28
*Штрафное время:* 6 - 8
*Вратари:* Shesterkin (26/28, 92.86%, 65:00) - Vasilevskiy (29/32, 90.63%, 65:00)
```

### `season_leaders_players.txt` — Лидеры сезона

```
*Лучшие бомбардиры*

McDavid          120  EDM
Kucherov         110  TBL
...
```

### `team_stats.txt` — Статистика команд

```
*Статистика большинства*
Команда/Статистика/Кол-во матчей

Panthers        28.5  70
...
```

### `league_table.txt` — Турнирная таблица

Группировка по конференциям и дивизионам: Atlantic, Metropolitan, Central, Pacific. Колонки: Команда / Очки / Игры / % очков.

---

## Полная диаграмма потока данных

```
┌─────────────────────────────────────────────────────────────────────┐
│                          NHL API                                    │
│                                                                     │
│  api.nhle.com/stats/rest/en/         api-web.nhle.com/v1/          │
│  ├── /team                           ├── /standings/now            │
│  ├── /team/summary                   ├── /roster/{tri}/{season}    │
│  ├── /skater/summary                 ├── /gamecenter/{id}/play-by-play
│  ├── /goalie/summary                 └── /gamecenter/{id}/boxscore │
│  └── /game                                                         │
└─────────────┬───────────────────────────────────────────────────────┘
              │  HTTP GET (requests.Session, retry ×10, backoff)
              ▼
┌─────────────────────────────┐
│    ModernNhlLoader.run()    │
│    pipeline/                │
│    load_season_modern.py    │
│                             │
│  Парсинг JSON → tuple-ы    │
│  TRUNCATE → bulk INSERT     │
└─────────────┬───────────────┘
              │  psycopg2 (execute_values)
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         PostgreSQL                                   │
│                                                                      │
│  10 таблиц:                     3 PL/pgSQL функции:                 │
│  teams, teams_stats,            get_game_stats()                     │
│  rosters,                       get_goals_game()                     │
│  players_season_stats,          get_goalies_game()                   │
│  goalies_season_stats,                                               │
│  games, all_goals,                                                   │
│  game_team_stats,                                                    │
│  game_player_stats,                                                  │
│  game_goalie_stats                                                   │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │  psycopg2 (SimpleConnectionPool)
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                                  │
│                                                                      │
│  bot.py → ConversationHandler (FSM: FIRST / SECOND)                 │
│     │                                                                │
│     ├── script_bot.py      Inline-меню навигации                    │
│     ├── stats_handlers.py  Фабрика: _make_stats_handler()           │
│     └── bot_messages.py    SQL-запросы → Jinja2-шаблоны             │
│            │                                                         │
│            ├── database.py     fetch_all() + whitelist               │
│            └── template_funcs.py  Jinja2 рендеринг                  │
│                  └── messages/*.txt                                   │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │  Telegram Bot API (polling)
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          Telegram                                    │
│                                                                      │
│  Пользователь:                                                       │
│  /stats → Inline-меню → Выбор статистики → Markdown-ответ           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Доступные статистики в боте

### Статистика полевых игроков (Top-10)

| Кнопка меню | Таблица | Колонка сортировки |
|---|---|---|
| Лидеры по очкам | `players_season_stats` | `points` |
| Лидеры по голам | `players_season_stats` | `goals` |
| Лидеры по ассистам | `players_season_stats` | `assists` |
| Лидеры по хитам | `players_season_stats` | `hits` |
| Лидеры по +/- | `players_season_stats` | `plus_minus` |
| Лидеры по игровому времени | `players_season_stats` | `time_on_ice_per_game` |
| Лидеры по штрафу | `players_season_stats` | `pim` |
| Лидеры по блокам | `players_season_stats` | `blocked` |

### Статистика вратарей (Top-10)

| Кнопка меню | Таблица | Колонка сортировки |
|---|---|---|
| Лидеры по победам | `goalies_season_stats` | `wins` |
| Лидеры по % отр. бросков | `goalies_season_stats` | `save_percentage` |
| Лидеры по сухарям | `goalies_season_stats` | `shutouts` |

### Статистика команд (все 32 команды)

| Кнопка меню | Колонка сортировки |
|---|---|
| % набранных очков | `procent_points` |
| Большинство | `power_play_percentage` |
| Меньшинство | `penalty_kill_percentage` |

### Дайджест дня

Автоматически определяет последнюю дату с завершёнными матчами и выводит карточку каждого матча (счёт, голы, броски, штрафы, вратари).

---

## Безопасность

| Аспект | Реализация |
|---|---|
| SQL-инъекции | Whitelist таблиц/колонок + `psycopg2.sql.Identifier` + параметризованные запросы |
| Токен бота | Хранится в `.env`, файл в `.gitignore` |
| Пароль БД | Не используется (локальный peer/trust), при необходимости добавляется через env |
| Rate limiting NHL API | Retry с exponential backoff, обработка HTTP 429 |

---

## Запуск проекта

### Первоначальная настройка

```bash
make setup          # venv + зависимости
make env-example    # создать .env из шаблона
# отредактировать .env: TELEGRAM_BOT_TOKEN=...
make db-init-local  # создать таблицы и PL/pgSQL функции
make season-load    # загрузить данные из NHL API
make bot            # запустить бота
```

### Повседневный запуск

```bash
make run-local      # бот без перезагрузки данных
make season-load    # обновить данные (с новым DATE_FROM/DATE_TO)
```

### Полный цикл с нуля

```bash
make run-full       # venv + db-init + pipeline + bot
```

---

## Известные особенности и ограничения

1. **Полная перезагрузка:** Pipeline делает TRUNCATE всех таблиц — нет инкрементальной загрузки.
2. **Makefile `pipeline`:** Цель `make pipeline` ссылается на несуществующие файлы (`teams_and_players.py`, `pipeline.py`); актуальная цель — `make season-load`.
3. **Отсутствие PRIMARY KEY:** Таблицы `teams`, `rosters`, `players_season_stats`, `goalies_season_stats`, `all_goals` не имеют PRIMARY KEY (только `games`, `game_team_stats`, `game_player_stats`, `game_goalie_stats` имеют UNIQUE constraint).
4. **Нет FOREIGN KEY:** Связи между таблицами существуют логически, но не объявлены на уровне DDL.
5. **Working directory:** Шаблоны загружаются по относительным путям (`messages/game_message.txt`) — бот должен запускаться из директории `telegram_bot/`.
6. **python-telegram-bot 13.15:** Callback-based API (не async). Версия зафиксирована; миграция на v20+ потребует перехода на asyncio.
7. **pandas:** Указан в зависимостях, но не используется в текущем коде.
8. **Rosters `abbreviation`:** Pipeline записывает код позиции в колонку `abbreviation`, хотя по смыслу это поле предназначено для аббревиатуры команды.
