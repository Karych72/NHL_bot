# Загрузка данных и запуск Telegram-бота

Пошаговая инструкция для локальной машины: окружение, PostgreSQL, NHL-пайплайн и бот.

## Требования

- **Python 3** (в проекте используется venv `.venv`).
- **PostgreSQL** с доступом по `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_DATABASE`.
- **Токен Telegram-бота** от [@BotFather](https://t.me/BotFather) для запуска бота.
- Сеть для запросов к API NHL и к Telegram.

## Быстрый старт (кратко)

```bash
make setup env-example          # venv + зависимости + .env из примера
# Отредактируйте .env: TELEGRAM_BOT_TOKEN, PG_*, SEASON_ID, CURRENT_SEASON
make db-reset-local             # DROP всех таблиц NHL_bot + CREATE из data_tables/t.*.sql + SQL-функции
make season-sync-month          # загрузка за последние ~30 дней (сеть + NHL API)
make test-db                    # проверка схемы в PostgreSQL
RUN_DB_DATA_TESTS=1 make test-db   # проверка целостности данных после загрузки
make bot                        # запуск бота
```

Ниже — детали и альтернативные команды.

---

## 1. Установка окружения

```bash
make setup
```

Создаёт или обновляет виртуальное окружение `.venv`, ставит зависимости из `requirements.txt`.

Создать файл `.env` по образцу (если ещё нет):

```bash
make env-example
```

Скопируется `.env.example` → `.env` (существующий `.env` не перезаписывается).

Каталоги для старого пайплайна (DataFrame):

```bash
make dirs
```

---

## 2. Переменные окружения (`.env`)

| Переменная | Назначение |
|------------|------------|
| `TELEGRAM_BOT_TOKEN` | Токен бота; **обязателен** для `make bot`. |
| `PG_HOST` | Хост PostgreSQL (по умолчанию `localhost`). |
| `PG_PORT` | Порт (по умолчанию `5432`). |
| `PG_USER` | Пользователь БД (в коде по умолчанию — текущий пользователь ОС). |
| `PG_DATABASE` | Имя базы. |
| `DATE_FROM` | Начало окна дат для **загрузки завершённых игр** (ISO `YYYY-MM-DD`). Используется лоадером, если не задано через `make season-sync` / CLI. |
| `DATE_TO` | Конец окна включительно. |
| `SEASON_ID` | Идентификатор сезона в NHL Stats API, например `20252026`. |
| `CURRENT_SEASON` | Строка для колонки `games.season`, например `25/26`. |

`Makefile` при наличии `.env` подключает его и экспортирует переменные в дочерние команды.

Модуль `telegram_bot/config.py` читает те же переменные для бота и для `pipeline/load_season_modern.py`.

---

## 3. База данных

Схема задаётся **только** файлами `data_tables/t.*.sql` (чистый `CREATE TABLE`, без `ALTER` и без цепочки миграций).

### 3.1. Полный сброс и создание (рекомендуется)

**Удаляет** все таблицы проекта и создаёт их заново, затем поднимает функции из `telegram_bot/queries/*.sql`:

```bash
make db-reset-local    # PG_USER = текущий логин ОС
make db-reset          # PG_* из .env
```

Скрипт дропа: `scripts/db_drop_all_tables.sql`. Порядок создания таблиц задан в `Makefile` (`DDL_TABLES`).

`make db-init` / `make db-init-local` — **синонимы** `db-reset` (то же поведение).

### 3.2. Только CREATE без DROP

```bash
make db-sync-local
```

Выполнит `CREATE TABLE` из `t.*.sql`. На **уже существующих** таблицах команда **упадёт** — сначала нужен `make db-reset-local`.

### 3.3. Проверки

```bash
make verify-skater-schema   # SQL: колонки и PK для skater reports
make test-skater-bot        # текст лидербордов, без БД
make test-db                # по умолчанию RUN_DB_SCHEMA_TESTS=1 — PK/колонки в PostgreSQL
RUN_DB_SCHEMA_TESTS=0 make test-db   # без проверок схемы (если в .env нет этой переменной)
make test-db RUN_DB_SCHEMA_TESTS=0     # то же; так надёжнее при значении RUN_DB_SCHEMA_TESTS в .env
RUN_DB_DATA_TESTS=1 make test-db     # + ссылочная целостность после загрузки
```

Тесты схемы/данных: `tests/test_db_nhl.py`.

---

## 4. Загрузка данных NHL (`load_season_modern.py`)

Скрипт:

- тянет справочник команд и агрегаты сезона;
- строит ростеры и сезонную статистику игроков/вратарей (в т.ч. advanced / shot types);
- по окну **`DATE_FROM` … `DATE_TO`** запрашивает **завершённые** игры (`gameStateId=7`) для `SEASON_ID`;
- для каждой игры запрашивает play-by-play и boxscore;
- пишет в PostgreSQL в **одной транзакции** (commit / rollback).

### 4.1. Стратегия записи в БД

- Таблицы **`teams`**, **`teams_stats`**, **`rosters`**, **`players_season_stats`**, **`goalies_season_stats`**, **`players_advanced_stats`**, **`players_shot_types`**: **UPSERT** по **`(team_id, season_id)`** или **`(player_id, season_id)`** — без `TRUNCATE`.
- В **`games`** пишется и строковая метка **`season`**, и числовой **`season_id`** (как в API).
- Таблицы **`games`**, **`all_goals`**, **`game_*_stats`**: для игр из текущего окна сначала **удаляются** строки с этими `game_id`, затем вставляются заново.

**Несколько сезонов в одной БД:** строки разных `SEASON_ID` не перетирают друг друга. Бот и дайджест фильтруют данные по **`SEASON_ID` из `.env` / `config`**.

### 4.2. Команды `make` (рекомендуемый способ)

Все цели ниже вызывают `setup` и `env-example` там, где указано; окно дат передаётся через переменные окружения в дочерний процесс.

| Команда | Описание |
|---------|----------|
| `make season-sync DATE_FROM=… DATE_TO=…` | Универсальная загрузка: агрегаты + завершённые игры в диапазоне дат. |
| `make season-load-full` | С `SEASON_START` (по умолчанию в Makefile `2025-10-01`) по **сегодняшнюю** дату. Переопределение: `make season-load-full SEASON_START=2024-10-01`. |
| `make season-reload-current` | То же, что `season-load-full`. |
| `make season-sync-week` | Последние 7 дней включительно до сегодня. |
| `make season-sync-month` | Последние **30 дней** до сегодня (`DATE_FROM` считается через Python). |
| `make season-sync-today` | Только игры за **сегодня** (календарная дата на машине). |
| `make season-load` | Алиас на `season-load-full`. |
| `make season-update` | Алиас на `season-sync-week`. |

**macOS:** `season-sync-week` использует `date -v-7d`. На **Linux** эта опция другая; задайте даты вручную:

```bash
make season-sync DATE_FROM=2026-03-22 DATE_TO=2026-03-29
```

### 4.3. Запуск лоадера вручную (CLI)

Из корня репозитория, с активированным venv или через `pipeline/../.venv/bin/python`:

```bash
cd pipeline && ../.venv/bin/python -u load_season_modern.py \
  --date-from 2025-10-01 \
  --date-to 2026-03-29 \
  --season-id 20252026 \
  --current-season "25/26"
```

Аргументы CLI переопределяют значения из env/`config.py`. Без аргументов используются `DATE_FROM`, `DATE_TO`, `SEASON_ID`, `CURRENT_SEASON` из окружения.

В логах в начале загрузки печатается окно дат и `season_id`.

---

## 5. Старый пайплайн (DataFrame / отдельные скрипты)

Отдельная цепочка **не заменяет** `load_season_modern` для текущей NHL-загрузки в те же таблицы бота, но оставлена в Makefile:

```bash
make pipeline          # pipeline/teams_and_players.py + pipeline/pipeline.py
make run-pipeline      # setup, dirs, db-init-local, pipeline
```

Используйте, только если вам нужен именно этот legacy-поток.

---

## 6. Запуск Telegram-бота

1. В `.env` задайте **`TELEGRAM_BOT_TOKEN`**.
2. Убедитесь, что PostgreSQL доступен с теми же `PG_*`, что и у лоадера, и что данные загружены.

Запуск:

```bash
make bot
```

`check-token` проверяет, что токен не пустой; затем выполняется `telegram_bot/bot.py` через интерпретатор из `.venv`.

Другие цели:

```bash
make run-bot    # setup + env-example + bot
make run-local  # то же, что run-bot
make run-full   # setup, dirs, db-init-local, старый pipeline, bot — тяжёлый сценарий «с нуля»
```

---

## 7. Типичные проблемы

| Симптом | Что проверить |
|---------|----------------|
| `ON CONFLICT` / отсутствует constraint | Выполнен ли **`make db-reset-local`** после смены DDL; нет ли дубликатов по составному ключу. |
| Пустой список игр | Окно `DATE_FROM`/`DATE_TO`, `SEASON_ID`, что игры уже **окончены** в API. |
| Бот не стартует | `TELEGRAM_BOT_TOKEN`, доступ к сети, параметры `PG_*`. |
| Ошибка подключения к БД | `pg_hba.conf`, пароль (`PGPASSWORD`), хост/порт. |

---

## 8. Сводка переменных для `make`

`make` подхватывает `.env` из корня проекта. Для разовых переопределений:

```bash
DATE_FROM=2026-01-01 DATE_TO=2026-01-31 make season-sync
SEASON_START=2024-10-01 make season-load-full
PG_USER=myuser make db-sync-local
```

Переменные `PG_*` в Makefile имеют значения по умолчанию; при пустом `PG_USER` для локальных целей часто используют `db-*-local`, где подставляется `$(id -un)`.
