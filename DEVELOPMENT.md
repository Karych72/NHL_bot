# Разработка NHL_bot

Краткие правила, на которые ориентируемся в команде и в CI.

## Окружение

- **Python 3.11** — целевая версия (как в GitHub Actions).
- Рабочее окружение: `make setup` → каталог `.venv`, зависимости из `requirements.txt`. Не полагаться на глобальный `pip` на macOS без venv (см. комментарий в `requirements.txt` про архитектуру и NumPy).
- Линтер и mypy: `make setup-dev` (дополнительно ставит `requirements-dev.txt`).
- Секреты только в `.env`; в репозитории — шаблон `.env.example`. Токены бота и доступы к БД не коммитить.

## Docker

- Сборка образа бота из корня репозитория: `docker build -t nhl-bot .`
- Локальный стек PostgreSQL + бот: скопировать `.env.example` в `.env`, задать `TELEGRAM_BOT_TOKEN`, затем `docker compose up`. Имя compose-проекта закреплено полем `name: nhl_bot` в `docker-compose.yml`, поэтому named volume `nhl_bot_pgdata` под PGDATA не зависит от каталога, из которого запущена команда (основной чекаут или таск-воркtree). Сервис `db` пробрасывает порт `5432`; в compose для бота выставлены `PG_HOST=db` и `PG_USER=postgres` (см. `docker-compose.yml`). Перед первым запуском бота примените DDL/SQL-функции к этой БД с хоста: `make PG_HOST=localhost PG_USER=postgres db-init` (когда контейнер `db` уже слушает порт). Переменные нужно передавать именно аргументами `make`, а не переменными окружения перед командой — Makefile делает `include .env` и `export`, и если в `.env` уже задан свой `PG_USER` (например, от локального нативного PostgreSQL), значение из `.env` перекрывает переменную окружения, но не аргумент командной строки `make`.
- В образ не копируется `.env`; при `docker compose up` используется `env_file: .env`.

## Тесты и качество

- **Минимум перед PR / слиянием:** `make ci-local` (ruff, mypy, `compileall`, `pytest` без `test_db_nhl`) — совпадает с GitHub Actions. Цель сама ставит `requirements-dev.txt` и `requirements-modeling.txt` (через `setup-dev`/`modeling-dev`), поэтому собирает и выполняет и `tests/test_modeling_*.py`.
- Только быстрые тесты без линтера: `make test-fast`.
- **Полный контур локально:** при изменениях DDL, SQL-функций или загрузчика — поднять БД, `make db-init` / `db-init-local`, затем `make all-tests` (схема и при необходимости данные — см. `README.md`).
- Проверки на уже загруженных данных: `make test-db-data` (`RUN_DB_DATA_TESTS=1`); нужна БД с данными (например, после `make season-sync-month`). В CI не запускается — там БД пустая (только схема). `test_games_config_season_id_present` и `test_season_stats_season_id_not_orphaned_and_config_season_present` рассчитаны на мультисезонную БД: они не требуют, чтобы вся таблица была одним сезоном, а проверяют, что сезон из `config.SEASON_ID` в таблице представлен, и (для четырёх stats-таблиц) что ни у одной строки `season_id` не ссылается на сезон, отсутствующий в `teams`.
- Ломающие изменения в `data_tables/*.sql` или `telegram_bot/queries/*.sql` сопровождаем понятным порядком применения (как в `Makefile`: `DDL_TABLES`, затем функции).

## Миграции схемы БД

Точечные изменения схемы (не пересоздание таблицы целиком через `data_tables/t.*.sql`)
идут через `data_tables/migrations/`: пары файлов `NNNN_slug.up.sql` / `NNNN_slug.down.sql`,
`NNNN` — версия с ведущими нулями (`0001`, `0002`, …). Ведущие нули обязательны: порядок
применения и отката — обычная строковая сортировка (`$(sort ...)` в Makefile, `ORDER BY
version` в SQL), а не численная, так что версии разной ширины (`1` и `0010`) отсортируются
неверно. Применённые версии учитываются в таблице `schema_migrations`, которую создаёт сам
раннер.

- **Добавить миграцию:** взять следующий номер по порядку, создать пару
  `data_tables/migrations/NNNN_slug.up.sql` (само изменение) и `NNNN_slug.down.sql`
  (обратное действие), обе с шапкой-комментарием. Применяется `make db-migrate` —
  уже применённые версии пропускаются, up и вставка строки в `schema_migrations`
  коммитятся одной транзакцией. `*.up.sql` должен быть совместим с `--single-transaction`
  (`psql` применяет его и вставку в `schema_migrations` одной транзакцией): без операторов,
  требующих быть вне транзакции (`CREATE INDEX CONCURRENTLY`, `VACUUM`), и без собственных
  `BEGIN;`/`COMMIT;` внутри файла — иначе `INSERT` в `schema_migrations` уедет отдельной
  транзакцией, и учёт разойдётся с реальным состоянием схемы.
- **Откатить последнюю миграцию:** `make db-migrate-down` — выполняет её `*.down.sql`
  и удаляет строку из `schema_migrations`, тоже одной транзакцией. Без применённых
  миграций — не ошибка, просто сообщение. Откатывается только одна, последняя,
  миграция за вызов.
- `db-sync` и `db-reset` (и их `-local` варианты) уже включают `db-migrate` после
  `db-functions` — отдельно звать его нужно только вне этих целей.
- `db-reset` не сбрасывает мигрированные объекты: `db-drop` (`scripts/db_drop_all_tables.sql`)
  удаляет только таблицы из `DDL_TABLES`, не трогая `bot_subscriptions` и
  `schema_migrations`. Поэтому правка уже применённой миграции `db-reset`-ом заново не
  накатится, пока её версия не уйдёт из `schema_migrations` (например, через
  `db-migrate-down`) — так и задумано, ради живых подписок.
- **Миграция, трогающая таблицу из `DDL_TABLES`** (например, `ALTER TABLE games …`),
  требует двух условий сразу, иначе учёт в `schema_migrations` разойдётся со схемой:
  (а) то же изменение тем же коммитом переносится в соответствующий `data_tables/t.*.sql`
  — `db-reset`/`db-tables` пересоздают эти таблицы из DDL-файлов, минуя миграции, так что
  без этого пересозданная таблица останется на старой схеме, а `schema_migrations` будет
  врать, что миграция применена; (б) сама `*.up.sql` пишется идемпотентно (`ADD COLUMN IF
  NOT EXISTS`, `DROP … IF EXISTS` и т.п.), чтобы не упасть на «already exists» при
  применении к уже актуальной (из DDL) таблице — сценарий чистой БД (CI `db-tests`,
  `make db-sync`), где DDL уже содержит новое состояние, а `db-migrate` всё равно
  прогоняет все файлы по порядку.

## Структура проекта

| Область | Назначение |
|--------|------------|
| `pipeline/` | Загрузка NHL API → PostgreSQL |
| `telegram_bot/` | Telegram-бот; запросы к БД — в том числе `telegram_bot/queries/*.sql` |
| `modeling/` | Сборка датасетов, CLI |
| `data_tables/` | DDL таблиц; `data_tables/migrations/` — точечные изменения схемы (`make db-migrate`) |
| `docs/` | Описание пайплайнов и архитектуры |
| `plan/` | Черновики планов (не дублируем договорённости из этого файла без обновления) |

## Зависимости и изменения кода

- Пакеты приложения — в `requirements.txt`; инструменты CI/линтера — в `requirements-dev.txt`, по необходимости с коротким комментарием «зачем».
- **Зависимости моделирования** — отдельный файл `requirements-modeling.txt`: семь ML-пакетов этапа 3 (LightGBM, scikit-learn, matplotlib и др.) плюс `pydantic` v2 для `modeling/config.py` (этап 2). Установка: `make modeling-dev`. Они **не** входят в `requirements.txt`, **не** ставятся через `make setup` / `make run-bot` и **не** попадают в Docker-образ бота. CatBoost в v1 не используется (опционально — этап 15 UPDATE-плана). Подробнее: [`docs/modeling_training.md`](docs/modeling_training.md#dependencies).
- **Async-тесты бота.** Хендлеры `telegram_bot/` — корутины (python-telegram-bot 21.x), поэтому тест, который вызывает хендлер напрямую, объявляется `async def` и помечается `@pytest.mark.asyncio` (`pytest-asyncio` в `requirements-dev.txt`, строгий режим по умолчанию — конфиг-файла pytest в репозитории нет). В `unittest.TestCase` этот маркер не работает: такие классы наследуем от `unittest.IsolatedAsyncioTestCase` (stdlib). Забытый маркер строгий режим не пропускает молча — тест будет пропущен с предупреждением.
- Следуем стилю существующих модулей (импорты, имена, обработка ошибок) вместо разнобоя.
- Большие бинарные артефакты и выгрузки данных не коммитить без явной договорённости (см. `.gitignore`).

## CI

Файл `.github/workflows/ci.yml` (runner `ubuntu-24.04`, Python 3.11), два job:

- **`quality`** — установка `requirements.txt`, `requirements-dev.txt` и `requirements-modeling.txt` (иначе `tests/test_modeling_*.py` не собираются — нет `sklearn`/`lightgbm`/…), затем **Ruff** (`telegram_bot`, `modeling`, `pipeline`), **mypy** (те же каталоги, настройка в `mypy.ini`), `compileall`, **pytest** без `tests/test_db_nhl.py`. Без БД, гоняется на каждый PR быстро.
- **`db-tests`** — поднимает service-контейнер `postgres:16.6-alpine` (trust-аутентификация, без пароля, как в `docker-compose.yml`), затем `make setup`, `make db-sync` (применяет `DDL_TABLES`, потом SQL-функции, потом `data_tables/migrations/*.up.sql` — тот же порядок, что `make db-init`) и `make test-db` (схемные проверки `tests/test_db_nhl.py`). Данные в этой БД не загружаются, поэтому `test-db-data` тут не вызывается.
