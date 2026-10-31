# NHL Bot — Техническая архитектура

## Обзор проекта

NHL Bot — это Telegram-бот для просмотра статистики NHL. Проект состоит из двух основных подсистем:

1. **Pipeline** (`pipeline/`) — ETL-загрузчик, который забирает данные из официального NHL API и записывает их в PostgreSQL.
2. **Telegram Bot** (`telegram_bot/`) — интерактивный бот с многоуровневым inline-меню, который читает данные из PostgreSQL и отображает статистику через Jinja2-шаблоны.

Стек: Python 3, PostgreSQL, aiogram 3.31 (asyncio, Bot API 10.3), psycopg2, requests, Jinja2.

---

## Дерево проекта

```
NHL_bot/
├── .env.example                        # Шаблон переменных окружения
├── .gitignore
├── Makefile                            # Сборка, запуск, инициализация БД
├── README.md                           # Quick start
├── requirements.in / requirements-dev.in / requirements-modeling.in  # Источник зависимостей (правим руками)
├── requirements.txt / requirements-dev.txt / requirements-modeling.txt  # Lock-файлы, генерируются `make lock` (не редактировать)
│
├── data_tables/                        # DDL — схемы всех таблиц PostgreSQL
│   ├── ai_reader_grants.sql            # GRANT SELECT роли nhl_ai_reader на таблицы данных: миграция 0015 (\ir) и `make db-tables` (Задача 83)
│   ├── t.all_goals.sql
│   ├── t.game_goalie_stats.sql
│   ├── t.game_player_stats.sql
│   ├── t.game_predictions.sql
│   ├── t.game_team_stats.sql
│   ├── t.game_three_stars.sql
│   ├── t.games.sql
│   ├── t.goalies_season_stats.sql
│   ├── t.players_season_stats.sql
│   ├── t.rosters.sql
│   ├── t.scheduled_games.sql
│   ├── t.shot_attempts.sql
│   ├── t.teams.sql
│   ├── t.teams_stats.sql
│   └── migrations/                     # Точечные изменения схемы: NNNN_slug.{up,down}.sql (make db-migrate)
│       ├── 0001_bot_subscriptions.up.sql
│       ├── 0001_bot_subscriptions.down.sql
│       ├── 0002_game_three_stars.up.sql
│       ├── 0002_game_three_stars.down.sql
│       ├── 0003_scheduled_games.up.sql
│       ├── 0003_scheduled_games.down.sql
│       ├── 0004_game_predictions.up.sql
│       ├── 0004_game_predictions.down.sql
│       ├── 0005_digest_time.up.sql     # Время дайджеста и отметка отправки ночи (Задача 60)
│       ├── 0005_digest_time.down.sql
│       ├── 0006_country_subscription.up.sql   # Подписка на страну: kind country_players (Задача 61)
│       ├── 0006_country_subscription.down.sql
│       ├── 0007_drop_digest_time.up.sql   # Подписки без времени доставки (Задача 63)
│       ├── 0007_drop_digest_time.down.sql
│       ├── 0008_shot_attempts.up.sql   # Попытки бросков из play-by-play (Задача 69A)
│       ├── 0008_shot_attempts.down.sql
│       ├── 0009_country_game_sent.up.sql   # Подписка на страну по матчу: отметка «матч ушёл» (Задача 76)
│       ├── 0009_country_game_sent.down.sql
│       ├── 0010_prediction_log.up.sql   # Журнал прогнозов и исходов (Задача 77)
│       ├── 0010_prediction_log.down.sql
│       ├── 0011_upcoming_games.up.sql   # Подписка «Предстоящие матчи»; отметка матча — общая bot_game_sent (Задача 78)
│       ├── 0011_upcoming_games.down.sql
│       ├── 0012_ai_questions.up.sql     # Журнал вопросов к модели и список текущих пользователей для лимита (Задача 81)
│       ├── 0012_ai_questions.down.sql
│       ├── 0013_goal_clips.up.sql       # Кэш клипов голов: file_id Telegram и подпись по голу (Задача 87)
│       ├── 0013_goal_clips.down.sql
│       ├── 0014_chat_game_sent.up.sql   # Отметка «итог матча ушёл в чат» по паре (чат, матч) (Задача 85)
│       ├── 0014_chat_game_sent.down.sql
│       ├── 0015_ai_stats.up.sql         # Роль nhl_ai_reader (только чтение) и журнал ai_stats_log (Задача 83)
│       ├── 0015_ai_stats.down.sql
│       ├── 0016_premium_access.up.sql   # Премиум чата до даты; удаляет список текущих пользователей 0012 (Задача 96)
│       ├── 0016_premium_access.down.sql
│       ├── 0017_bot_actions.up.sql      # Журнал действий bot_actions и история подписок bot_subscription_events (триггер) (Задача 99)
│       └── 0017_bot_actions.down.sql
│
├── deploy/                             # Только VM: токены ботов и ключ Claude API из Secret Manager (Задачи 65, 81), dev-стек (Задача 80), DEVELOPMENT.md §«Прод-деплой»
│   ├── compose.prod.yml                # Оверрайд: bot и sync получают токен, bot — ещё ключ Claude API файлами-секретами (COMPOSE_FILE в .env на VM)
│   ├── compose.dev.yml                 # Dev-стек ветки develop (Задача 80): без db/backup, база nhl_dev в Postgres прода, токен dev-бота
│   ├── fetch-secrets.sh                # Secret Manager → /run/<стек>/telegram_bot_token и /run/<стек>/anthropic/anthropic_api_key (tmpfs)
│   ├── models/                         # Задача 73: модели прода в образе — home_win/logreg и over_5_5/goals (<run_id>/final + latest)
│   └── systemd/nhl-bot-secrets.service # Запускает fetch-secrets.sh при загрузке VM, до docker.service
│
├── docs/                               # Документация (архитектура, исследования API, гайды)
│   ├── architecture.md                 # ← этот файл
│   ├── api_data_research.md
│   ├── data_loading.md                 # установка окружения, БД, загрузка NHL API
│   ├── pipeline_nulls_and_explicit_null_tz.md   # контракт NULL-семантики лоадера
│   └── telegram_bot.md                 # запуск и команды бота
│
├── plan/                               # Планы (индекс: plan/README.md)
│   ├── README.md
│   ├── tasks/                          # карточки открытых задач (по файлу на задачу)
│   │   └── task_model/                 # трек B (модель): карточки 66–71, 73 + исследование
│   ├── archive/                        # закрытые задачи: closed_tasks.md + их карточки
│   ├── stats/                          # продуктовые планы (статистика)
│   ├── dataset_agents/                 # ТЗ и шаблоны агентов для датасета
│   ├── classifier/                     # ML / прематч-классификаторы
│   ├── engineering/                    # рефакторинг, тесты БД, планы работ
│   └── deprecated_plan/                # выполненные и архивные планы
│
├── pipeline/                           # ETL: NHL API → PostgreSQL
│   ├── load_season_modern.py           # Класс ModernNhlLoader
│   ├── scheduled_sync.py               # Задачи 34, 56: ночной планировщик (сервис `sync`)
│   ├── scheduled_retrain.py            # Задача 26: еженедельный retrain (сервис `retrain`)
│   └── scheduled_publish.py            # Задача 73: ежедневная публикация прогноза (сервис `predict`)
│
├── scripts/                            # Утилиты вне пайплайна (запускаются вручную)
│   ├── ai_router_eval.py               # `make ai-eval`: контрольный набор свободного текста против Claude API (Задача 81)
│   ├── ai_router_eval.jsonl            # Фразы набора с ожидаемым экраном или текстовым ответом
│   ├── ai_stats_eval.py                # `make ai-stats-eval`: контрольный набор статистических вопросов против Claude API и БД (Задача 83)
│   ├── ai_stats_eval.jsonl             # Вопросы с ожидаемыми числами (точечные, обзоры игроков, нет данных, уточнения, цепочки)
│   ├── capture_nhl_fixtures.py         # Захват реальных ответов NHL API → tests/fixtures/nhl_*.json
│   ├── db_drop_all_tables.sql
│   ├── dev_premium.sh                  # Премиум чата на dev-боте: выдать на 2 часа / снять (Задача 96, DEVELOPMENT.md)
│   └── verify_skater_reports_schema.sql
│
├── tests/                              # pytest + unittest (`make test-fast`, `make ci-local`)
│   ├── conftest.py                     # Общие pytest-фикстуры бота и modeling
│   ├── _modeling_fixtures.py           # Синтетические датасеты для тестов modeling
│   ├── _pipeline_fixtures.py           # Обвязка тестов загрузчика: фикстуры, порядок колонок, запрет сети
│   ├── test_pipeline_optional_helpers.py  # §1 контракта NULL: to_int / optional_* / safe_pct
│   ├── test_pipeline_season_rows.py    # Сборка строк сезонных таблиц (teams … goalies_season_stats)
│   ├── test_pipeline_game_rows.py      # Сборка строк пер-игровых таблиц (games, all_goals, …)
│   ├── test_bot_integration.py         # Сквозные сценарии бота: /standings, /stats, /team_stats, карточка матча, /today
│   ├── test_bot_*.py, test_modeling_*.py, test_db_nhl.py, …
│   └── fixtures/                       # Урезанные реальные payload'ы NHL API (nhl_*.json)
│
├── telegram_bot/                       # Telegram-бот
│   ├── bot.py                          # Точка входа: Dispatcher, роутеры, команды, журнал действий bot_actions (Задача 99)
│   ├── throttle.py                     # Троттлинг callback-кнопок (per-user, in-memory)
│   ├── ai_router.py                    # Свободный текст: Claude выбирает экран бота, лимит вопросов, журнал ai_questions (Задачи 81, 96)
│   ├── ai_stats.py                     # Ответы ИИ на статистические вопросы: SQL модели под read-only ролью, обзор игрока, ревью, журнал ai_stats_log (Задача 83)
│   ├── premium.py                      # Премиум чата (premium_access): ИИ без недельного лимита и прогноз модели (Задача 96)
│   ├── prompts/                        # Промпты ai_stats: stats_answer.md, stats_reviewer.md, stats_schema_notes.md (аннотация схемы)
│   ├── config.py                       # Чтение .env-переменных
│   ├── database.py                     # Пул соединений + fetch_all + whitelist
│   ├── keyboards.py                    # build_menu(), callback_message() — общие помощники кнопок
│   ├── leaderboard_specs.py            # Таблицы лидеров: заголовки, колонки, раскладка меню
│   ├── stats_handlers.py              # Меню /stats, /team_stats, листание таблиц, дайджест
│   ├── bot_messages.py                 # Формирование текстов ответов (SQL + шаблоны)
│   ├── template_funcs.py              # Обёртка Jinja2
│   ├── messages/                       # Jinja2-шаблоны сообщений
│   │   ├── game_message.txt
│   │   ├── league_table.txt
│   │   └── team_profile.txt
│   └── queries/                        # PL/pgSQL функции
│       ├── get_game_stats.sql
│       ├── get_goals_game.sql
│       ├── get_goalies_game.sql
│       └── get_three_stars_game.sql
│
├── modeling/                            # ML-пайплайн: датасет-билдер + обучение + инференс (см. docs/modeling_dataset_builder.md, docs/modeling_training.md)
│   ├── cli.py                           # `python -m modeling.cli build-dataset|train|evaluate-goals|evaluate-xg|promote|predict|publish-predictions`
│   ├── dataset_builder/                 # base.py, team_game_facts.py, features.py, assemble.py, schema.py, validate.py
│   ├── elo.py                           # Задача 66: ядро Elo (признак diff_elo) + эталон «Elo с подобранной кривой» для гейта; читает games_train.csv
│   ├── goal_model.py                    # Задача 67: Poisson GLM голов основного времени → сетка счёта → P(тотал > L) по правилу букмекеров; проверка по сезонам против константы (`evaluate-goals`, читает games_train.csv)
│   ├── xg_model.py                      # Задача 69: Fenwick-xG — логрег P(гол) по расстоянию, углу, типу броска, составу, подбору; нормировка сторон по home_team_defending_side; fit_xg/predict_xg, проверка по сезонам (`evaluate-xg`, читает shots_train.csv)
│   ├── predict_runner.py                # Задача 15: грузит latest-модель, скорит dataset_predict.csv, пишет CSV с probability
│   ├── publish_predictions.py           # Задачи 22B, 73, 77: CSV predict → game_predictions (гейт по status latest); прогноз тотала голевой моделью; журнал prediction_log
│   └── …                                # train_runner.py, train_logreg.py, train_lgbm.py, splits.py (прогоны по сезонам), config.py, artifacts.py, и др.
│
└── artifacts/                           # datasets/, models/, predictions/, reports/*/ — в .gitignore (см. .gitignore)
    ├── datasets/                        # dataset_{train,predict}.csv + metadata + games_train.csv (все сыгранные игры с исходом REG/OT/SO и голами основного времени — для Elo-эталона и голевой модели) + shots_train.csv (строки shot_attempts сыгранных игр с season_id/day/командами — для xG-модели) — пересобираются из БД
    ├── models/                          # <task>/<model>/<run_id>/final/ + symlink latest (см. docs/modeling_training.md); в .gitignore с Задачи 15
    ├── predictions/                     # CLI predict пишет сюда по умолчанию: <task>_<model>_predictions.csv; в .gitignore с Задачи 15
    └── reports/                         # top-level скрипты и CSV-выгрузки отчётов коммитятся (к датасету отношения не имеют); reports/<run_id>/ (train-прогоны) — в .gitignore с Задачи 15
```

---

## Конфигурация

### Переменные окружения (`.env`)

| Переменная | Описание | Значение по умолчанию |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота (обязательно) | — |
| `PG_HOST` | Хост PostgreSQL | `localhost` |
| `PG_PORT` | Порт PostgreSQL | `5432` |
| `PG_USER` | Пользователь PostgreSQL | `postgres` |
| `PG_DATABASE` | Имя базы данных | `postgres` |
| `SEASON_ID` | Идентификатор сезона NHL API (напр. `20262027`); единственный источник — `CURRENT_SEASON` (в `config.py`) и стартовая дата окна (в лоадере) выводятся из него через `config.derive_season()` (Задача 33) | — (обязательно) |
| `DATE_FROM` | Начало диапазона дат для загрузки игр (только лоадер; бот эту переменную не читает) | нет дефолта в `config.py`; если пусто/не задано, лоадер (`load_season_modern.py::main()`) сам подставляет 1 сентября первого года *итогового* `season_id` (после `--season-id`) |
| `DATE_TO` | Конец диапазона дат для загрузки игр | текущая дата |

Конфигурация считывается модулем `telegram_bot/config.py`. Pipeline повторно использует тот же модуль, добавляя его директорию в `sys.path`.

Модульные дефолты `PG_*` рассчитаны на лоадер и `make db-*` — сам импорт `config.py` на отсутствии переменных не падает; `SEASON_ID` тоже не роняет импорт, но дефолта у него нет (`config.SEASON_ID is None`, тогда и `CURRENT_SEASON` — `None`). `DATE_FROM` в `config.py` дефолта не имеет вовсе — это либо сырое значение переменной окружения, либо `None`; дату старта от `SEASON_ID` подставляет только лоадер (`pipeline/load_season_modern.py::main()`), причём от **итогового** `season_id` (после `--season-id`), не от того, что было в `config.SEASON_ID` на момент импорта — это и держит вывод единственным (fix round 1, Задача 33: старая версия вычисляла дефолт `DATE_FROM` в `config.py` на импорте и могла разойтись с `--season-id`). Для бота действует отдельная явная проверка `config.validate_env()`, вызываемая из точек входа (`bot.py`, `push_digest_job.py`): отсутствие или пустое значение любой из `TELEGRAM_BOT_TOKEN`/`PG_HOST`/`PG_PORT`/`PG_USER`/`PG_DATABASE`/`SEASON_ID` — падение на старте с сообщением, называющим переменную (без её значения). У лоадера своя явная проверка на том же условии (`pipeline/load_season_modern.py::main()`), потому что ему не нужен `TELEGRAM_BOT_TOKEN` — вызывать весь `validate_env()` ему не подходит.

### Makefile-цели

| Цель | Описание |
|---|---|
| `make setup` | Создаёт `.venv` (с учётом архитектуры arm64/x86_64), устанавливает зависимости |
| `make env-example` | Копирует `.env.example` → `.env`, если файл не существует |
| `make db-init` / `db-reset` | DROP → CREATE: DDL из `data_tables/*.sql`, затем PL/pgSQL из `telegram_bot/queries/*.sql`, затем `data_tables/migrations/*.up.sql` |
| `make db-init-local` / `db-reset-local` | То же, но с текущим пользователем ОС вместо `postgres` |
| `make db-sync` | Без DROP: DDL → функции → миграции (порядок как выше) |
| `make db-migrate` | Применяет непримененные `data_tables/migrations/*.up.sql` по возрастанию версии, фиксируя каждую в `schema_migrations` |
| `make db-migrate-down` | Откатывает последнюю применённую миграцию (`*.down.sql` + удаление строки из `schema_migrations`) |
| `make season-sync DATE_FROM=… DATE_TO=…` | Запуск ETL-загрузчика `load_season_modern.py` для произвольного окна дат |
| `make season-load-full` | Полная перезагрузка текущего сезона: запускает лоадер напрямую с очищенным `DATE_FROM`, чтобы он сам вывел дату старта из `SEASON_ID` (см. §Конфигурация); окно — до сегодня |
| `make season-sync-week` / `-month` / `-today` | Синхронизация за последние 7 / 30 / 0 дней |
| `make bot` | Запуск Telegram-бота (`bot.py::main()` проверяет обязательные переменные окружения) |
| `make run-bot` | `setup` + `env-example` + `bot` |
| `make run-local` | Запуск бота без pipeline (для работы с уже загруженными данными) |
| `make lock` | Перегенерирует `requirements*.txt` из `requirements*.in` (pip-tools, hash-режим), в порядке рантайм → modeling → dev |

### Зависимости (`requirements.in` → `requirements.txt`)

Источник истины — `requirements.in` / `requirements-dev.in` / `requirements-modeling.in`;
`requirements*.txt` — lock-файлы с хешами, генерируются `make lock` и руками не
редактируются (подробнее — `DEVELOPMENT.md`).

| Пакет | Назначение |
|---|---|
| `aiogram==3.31.0` | Telegram Bot API 10.3 (asyncio): роутеры, InlineKeyboard, Rich Messages |
| `psycopg2-binary` | Драйвер PostgreSQL |
| `requests` | HTTP-запросы к NHL API |
| `jinja2` | Шаблонизатор для формирования текстов сообщений |
| `pandas==2.2.3` | Используется в `modeling/dataset_builder/*` (сборка фичей для моделирования); пин совпадает с `requirements-modeling.in`, иначе совместный резолв слоёв не сходится |

`pytest` — dev-зависимость (`requirements-dev.in`), в рантайм-образ не попадает.
Системный пакет `ffmpeg` ставится в образ через `apt-get` в `Dockerfile` (faststart-ремукс и превью
видео голов, `telegram_bot/video_replay.py`); без него `download_goal_video` поднимает `RuntimeError`.

---

## Архитектура Pipeline (ETL)

### Модуль: `pipeline/load_season_modern.py`

Класс `ModernNhlLoader` реализует полный цикл загрузки данных сезона NHL.

Тесты сборки строк — `tests/test_pipeline_season_rows.py` и `tests/test_pipeline_game_rows.py`:
подменяют единственные сетевые выходы `build_*` (`get_json` / `fetch_paginated`) фикстурами
`tests/fixtures/nhl_*.json` — урезанными реальными ответами API, снятыми
`scripts/capture_nhl_fixtures.py`. Сети в тестах нет; порядок колонок каждого
`INSERT`/`UPSERT` из `run()` зафиксирован в `tests/_pipeline_fixtures.py`.

### Источники данных (NHL API)

| API | Базовый URL | Данные |
|---|---|---|
| Stats API | `api.nhle.com/stats/rest/en/` | Команды, статистика игроков/вратарей/команд за сезон, список игр |
| Web API | `api-web.nhle.com/v1/` | Турнирная таблица, составы, play-by-play, boxscore, landing (звёзды матча) |

### Поток данных

```
NHL Stats API                          NHL Web API
     │                                      │
     ├─ /team ──────────────────────┐        ├─ /standings/now
     ├─ /team/summary               │        ├─ /roster/{tri}/{season}
     ├─ /skater/summary             │        ├─ /gamecenter/{id}/play-by-play
     ├─ /goalie/summary             │        ├─ /gamecenter/{id}/boxscore
     └─ /game (finished)            │        └─ /gamecenter/{id}/landing
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
              │       Для каждой игры: play-by-play + boxscore + landing
              │       через fetch_game_json() (диск-кэш, см. ниже)
              │       → games_rows, all_goals_rows, game_team_rows,
              │         game_player_rows, game_goalie_rows,
              │         game_three_stars_rows, shot_attempt_rows
              │         (попытки бросков из того же прохода по plays)
              │
              └── 8. PostgreSQL (одна транзакция):
                      DELETE per-game для game_id в окне
                      UPSERT в сезонные таблицы (ON CONFLICT DO UPDATE)
                      INSERT в per-game таблицы
                      COMMIT (или ROLLBACK при любой ошибке)
```

### Стратегия загрузки

- **Сезонные таблицы:** UPSERT по PK `(team_id, season_id)` или `(player_id, season_id)`.
- **Per-game таблицы (`games`, `all_goals`, `shot_attempts`, `game_*_stats`):** `DELETE … WHERE game_id = ANY(window) → INSERT`. Идемпотентно при повторных запусках на пересекающихся окнах.
- **Пагинация:** API с ответами `{data, total}` выгружаются постранично (`fetch_paginated`, запрошенный размер страницы 200–1000). API сам режет страницу ниже `limit` (100 строк у отчётов полевых, 1000 у `game`), а без явного `sort` порядок записей между страницами плавает. Поэтому выгрузка всегда идёт с сортировкой по уникальному ключу (`playerId` / `teamId` / `id`, Задача 62).
- **Retry-логика:** до 10 попыток, экспоненциальный backoff, обработка HTTP 429 (Rate Limit) через заголовок `Retry-After`.
- **Таймаут запросов:** 30 секунд.
- **Транзакционность:** все INSERT выполняются в одной транзакции; при ошибке — `ROLLBACK`.

### Кэш сырых пер-игровых ответов

`ModernNhlLoader.fetch_game_json(game_id, endpoint)` — единственная точка, через которую
`build_game_rows` читает `gamecenter/{id}/play-by-play`, `gamecenter/{id}/boxscore` и
`gamecenter/{id}/landing` (звёзды матча).
Финальная игра неизменна, поэтому её ответ кэшируется на диске без TTL и инвалидации:
`all_data/raw/{season_id}/{game_id}.{pbp|box|landing}.json.gz` (каталог `all_data/` — в `.gitignore`,
в репозиторий не коммитится). Файл есть → читаем с диска; файла нет → идём в сеть и, если
`gameState` ответа `OFF` (результат утверждён лигой), пишем в кэш. Игру, у которой хоть один
из трёх ответов ещё не `OFF`, `build_game_rows` пропускает целиком до следующего прогона. Чтение сквозное, без флага
включения. Сезонные отчёты (`skater/summary`, `standings/now` и другие эндпоинты `build_*`)
через этот метод не идут и кэшу не подлежат — они обновляются каждый прогон.

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

### Модуль: `pipeline/scheduled_sync.py` (Задача 34, автообновление)

Планировщик, который вызывает `load_season_modern.py` без участия человека — сервис `sync`
в `docker-compose.yml`, тот же образ, что у бота. Только stdlib, новых зависимостей нет.
Прогоны — ночью каждые 30 минут, 18:00–08:00 UTC (21:00–11:00 МСК, Задача 56): завершённые
матчи попадают в БД по ходу ночи; `push_digest_job.py` идёт после каждого прогона и сам
решает, пора ли (Задачи 60, 63, 85): итог матча подписок чата — одним сообщением после конца
матча, дайджест с остальными матчами — как только загружена вся ночь, не позже последнего слота
(08:00 UTC); времени у подписчика нет. Днём, с 12:30 UTC
(15:30 МСК), слоты по той же сетке идут без загрузчика — только `push_digest_job.py --upcoming-only`, ради
превью предстоящих матчей (Задача 78; `loads_data`). Загрузчику днём грузить нечего, а в
12:00 UTC работают `retrain` и `predict` — первый дневной слот через полчаса, когда прогноз уже опубликован. Дневной прогон статус-файл не пишет: healthcheck
следит за свежестью данных, и успешная рассылка не скрывает упавшую ночную загрузку. Повторные
прогоны одного дня идемпотентны: per-game таблицы — `DELETE` по `game_id` окна и `INSERT` в одной транзакции, сезонные — UPSERT. Расписание, здоровье сервиса,
том с диск-кэшем/статусом и ручной запуск — `DEVELOPMENT.md` §Docker. Каждый такой прогон
загрузчика заодно обновляет `scheduled_games` (расписание на сегодня и завтра, Задача 22A; игры, у которых нет обеих команд в `teams` сезона, пропускаются с предупреждением).
Идущий матч (Stats REST `gameStateId` 3–6) ночной прогон не видит ни в `games`, ни в
`scheduled_games` — он появляется в `games`, когда gamecenter отдаёт `OFF`. Сезонные таблицы
(`teams_stats`, `players_season_stats`…) из Stats REST могут ночью уже учитывать матч, которого
ещё нет в `games`, — расхождение до следующего прогона после `OFF`. Если прогон в 07:30 UTC
затянется за 08:00, цикл перейдёт сразу к 18:00 и дайджест в этот день не уйдёт (прогон
окна — меньше минуты, риск принят). Тесты —
`tests/test_scheduled_sync.py`.

### Модуль: `pipeline/scheduled_retrain.py` (Задача 26, еженедельный retrain)

Планировщик retrain — сервис `retrain` в `docker-compose.yml` (профиль `modeling`: не входит в обычные `up`/`build`, поднимается `docker compose --profile modeling up -d retrain`). Переиспользует из
`scheduled_sync.py` `SyncCommand`, `run_once`, `check`, `_next_target` и запись статуса
(параметризованы порогом/названием/функцией слота, без копий). Свои части: недельный слот
(понедельник 12:00 UTC; цикл просыпается ежедневно, `should_run`: догон либо понедельник без прогона за 6 дней; при старте — только догон), предусловие «последний sync
успешен и свеж», догоняющий прогон (`needs_catch_up`: нет статуса / старше 7 дней / отказ по
предусловию, который прогоном не считается) и цепочка
`build-dataset --mode train` → `train --task home_win --no-promote`. `latest` автоматически
не двигается. Образ сервиса — стадия `modeling` многостадийного `Dockerfile` (поверх общей
`base`: `libgomp1` + `requirements-modeling.txt`); стадия `bot` идёт последней, поэтому
`build: .` у `bot`/`sync` собирает прежний образ без modeling-стека. Подробности —
`docs/modeling_training.md` §9 и `DEVELOPMENT.md` §Docker. Тесты — `tests/test_scheduled_retrain.py`.

### Модуль: `pipeline/scheduled_publish.py` (Задача 73, ежедневная публикация прогноза)

Сервис `predict` в `docker-compose.yml` (без профиля — выкатывается на прод; образ — стадия
`modeling`, в релизе `ghcr.io/karych72/nhl_bot:<тег>-modeling`). Раз в сутки в 12:00 UTC (слот
`scheduled_retrain.seconds_until_next_run`), после успешного свежего sync (`run_after_sync` из
`scheduled_retrain` — общее предусловие), прогоняет `build-dataset --mode predict` →
`predict` → `publish-predictions` → `publish-goals`; модели — из `deploy/models/` в образе, не
обучаются. Догоняющий прогон при старте — если статуса нет или он старше суток. Статус —
`all_data/publish_status.json` в томе `syncdata`, healthcheck — ok и моложе 26 часов. Тесты —
`tests/test_scheduled_publish.py`.

Тем же принципом (долгоживущий контейнер со своим циклом, не host cron) в `docker-compose.yml`
устроен и сервис `backup` (Задача 35): ежесуточный `pg_dump` тома `pgdata` на bind mount
`${BACKUP_DIR}` с ротацией на 14 дампов — детали и процедура восстановления в
`DEVELOPMENT.md` §Docker.

---

## Архитектура Telegram Bot

### Точка входа: `bot.py`

Использует aiogram 3 (asyncio). `build_dispatcher()` собирает `Dispatcher`: троттлинг
callback-кнопок (`throttle.py`) — outer-middleware `callback_query`, затем два роутера по
порядку — `standalone` (команды и inline-кнопки) и `tail` (свободный текст `on_free_text`,
Задача 81). Апдейт берёт первый подошедший хендлер. Состояний FSM у бота нет (Задача 92 убрала
диалог `/stats`): каждая кнопка несёт в `callback_data` всё, что нужно её хендлеру, и работает
в сообщении любой давности. `main()` запускает `start_polling()`; при старте `set_my_commands`
публикует меню команд. Хендлеры получают зависимости по имени параметра (DI aiogram): `bot`,
`match` — совпадение regex фильтра callback_data, `command` — аргументы `/start`. Состав регистрации и
маршрутизация настоящих апдейтов (`Dispatcher.feed_update`) закреплены в
`tests/test_bot_application.py`. Подписки (Задача 63): одна команда
`/subscriptions` и кнопки `sub:…` в `bot.py` правят одно сообщение через `edit_text`, а читают
и пишут `bot_subscriptions` через `subscription_repo.py`.

Сквозные сценарии «запрос → ответ» (`/standings`, `/stats` и `/team_stats` с пагинацией, карточка матча по `/start game_<id>`, `/today`)
закреплены в `tests/test_bot_integration.py`:
подменяются только границы БД (фикстура `fake_db_router` в `tests/conftest.py` —
соединение psycopg2 и TTL-обёртка над ним) и сети (`tests/_telegram_fakes.py::FakeSession` — HTTP-сессия aiogram,
записывающая каждый запрос к Bot API), остальной стек — от разбора `callback_data` до
сборки клавиатур и сериализации запросов aiogram — работает по-настоящему.

Все хендлеры в `bot.py` и `stats_handlers.py` — корутины (`async def`),
вызовы Bot API идут под `await`. Сообщение с нажатой кнопкой распаковывает
`keyboards.callback_message` (у `CallbackQuery.message` тип `Message | InaccessibleMessage |
None`); форму callback_data проверяет regex фильтра регистрации, хендлер получает готовое
совпадение `match`. Апдейты обрабатываются конкурентно (polling aiogram запускает каждый
задачей), но апдейты одного пользователя — по одному (`SimpleEventIsolation`), чтобы двойной
тап по матчу `/tonight` не обгонял запись реестра ответов. Асинхронен и скрипт рассылки `push_digest_job.py` (запускается сервисом `sync` после
каждого прогона, ночью — после загрузчика, см. «Модуль: `pipeline/scheduled_sync.py`» выше): он не часть
процесса бота, а отдельный запуск, который поднимает собственный `Bot` (без `Dispatcher` и
polling, таймаут сессии 300 с ради альбомов видео) и переиспользует
`dispatch_day_digest_messages`; точка входа — `asyncio.run(main())`.

Единственный event loop обслуживает и polling, и обработчики, поэтому блокирующая
работа выносится в поток: скачивание MP4 + два прохода ffmpeg
(`video_replay.download_goal_video`, до ~2 минут) вызывается из `stats_handlers`
через `asyncio.to_thread` — только для клипа, которого ещё нет в кэше `bot_goal_clips` (Задача 87),
как и `nhl_scoreboard.fetch_score` (`/tonight`). Запросы psycopg2
пока идут в loop'е синхронно.

### Троттлинг callback-кнопок: `throttle.py`

Задача 10: гасит N+1 нагрузку на PostgreSQL от спама кнопками пагинации — последний слой
после TTL-кэша Задачи 9 (`database.cached_fetch_all`). Outer-middleware
`enforce_callback_rate_limit` на `Dispatcher.callback_query` видит нажатие раньше фильтров
любого роутера. Per-user скользящее окно: `CALLBACK_RATE_LIMIT = 8` нажатий за
`CALLBACK_RATE_WINDOW_SEC = 3.0` секунд, ключ — `callback.from_user.id`. При превышении —
`answer()` с текстом, а следующий шаг цепочки не вызывается: нажатие не доходит до хендлеров. Троттлятся только нажатия кнопок; команды и текст
проходят без ограничений. Состояние — только в памяти
процесса (module-level `dict`, без Redis); истёкшие записи вычищаются по ходу вызова, без
отдельного планировщика. Тесты — `tests/test_bot_throttle.py` (поведение окна) и
`tests/test_bot_application.py` (регистрация и остановка спама в `Dispatcher`).

### Свободный текст: `ai_router.py` (Задача 81)

Текст в личке вне команд ловит `on_free_text` в роутере `tail`, после команд и кнопок.
`ai_router.ask` — один вызов `claude-sonnet-5-5` (effort `low`) с инструментами-экранами
(`build_tools`: строгие схемы, `enum` из `BOT_COMMANDS`, `COUNTRY_LABELS`,
`leaderboard_specs`, команд сезона); модель либо выбирает экран, либо пишет короткий отказ
или уточнение, фактов не сообщает. Экран рисует `bot.AI_SCREENS` тем же кодом, что команда или
кнопка, но новым сообщением; апдейты обрабатываются конкурентно, и ожидание модели не держит
остальные. Системный промпт и инструменты стабильны и кэшируются, дата —
в вопросе. Лимит (`ai_router.limit_refusal`, Задача 96): премиуму (`premium.is_premium`) — 20 вопросов
в сутки по МСК, бесплатным — 3 в календарную неделю МСК и не больше 8 за всё время; вопрос —
строка `ai_questions` или `ai_stats_log` (выбор `stats_question` не в счёт), сбои API не в счёт; каждый вызов модели — строка `ai_questions` (экран
или текст, токены, стоимость, задержка, `request_id`, ошибка). Сбой API — сообщение «попробуйте
командой» и строка с ошибкой. Выбор модели проверяет `make ai-eval`, маршрутизацию —
`tests/test_bot_ai_router.py`.

### Ответы на статистические вопросы: `ai_stats.py` (Задача 83)

Маршрутизатор 81 получает ещё инструмент `stats_question` (бесплатным тоже — в пределах их
лимита, решение человека 2026-10-29); выбранный (или любое сообщение в течение 30 минут после ответа 83) уходит в
`ai_stats.answer`. Это один диалог `claude-sonnet-5-5` (effort `medium`) с инструментами:
`run_sql` (один SELECT, до 50 строк; ошибка SQL, в том числе таймаут, возвращается модели как
попытка), `player_overview` (фиксированная выборка по игроку текущего сезона: сезон и прошлый,
10 последних игр, серия с очками; для вратаря свой набор) и `finish` (`answer` / `no_data` /
`off_topic` / `clarify` и признак продолжения). Принудительный `tool_choice` Sonnet 5.5 не
принимает, поэтому инструмент на каждом ходу требует промпт, а ответ без него — громкий сбой.
Черновик `answer` проверяет отдельный вызов с промптом ревьюера (инструмент `verdict`):
`ok` — черновик пользователю; `clarify` — только если сам вопрос неоднозначен (однофамильцы, «Нью-Йорк»):
вместо черновика уходит вопрос ревьюера; `fix` — вопрос однозначен, а черновик ошибочен: модель
получает замечание и делает одну повторную попытку (в тех же потолках), новый черновик снова на
ревью. Второй `fix` (исход `rejected`) или потолок на повторе — «Не удалось ответить точно —
попробуйте переформулировать вопрос»; отклонённые черновики остаются в журнале (шаги `review`),
пользователю не уходят. В обзоре игрока признак `game_log_incomplete` («лог игр неполный: N из M»,
если строк `game_player_stats` меньше игр сезона) убирает серию и просит модель сказать о неполноте.

SQL идёт под ролью `nhl_ai_reader` (миграция 0015; `SELECT` на таблицы данных из
`data_tables/ai_reader_grants.sql`) — граница безопасности, а не промпт. Слои: (1) любая `;` в
теле запроса модели — ошибка запроса (psycopg2 шлёт simple query, где через `;` идёт несколько
операторов; одной обёртки против `SELECT 1) AS x; SET …; SELECT … FROM (SELECT 1` мало);
(2) запрос оборачивается в `SELECT * FROM (<sql>) AS q LIMIT n`; (3) права роли (только `SELECT`
на таблицы данных) и read-only транзакция (`default_transaction_read_only` на роли и в параметрах
соединения); (4) сторожевой таймер клиента (`threading.Timer` → `conn.cancel`, таймаут + 1 с)
останавливает запрос, что бы ни случилось с серверным `statement_timeout` (5 с). Роль в PG16
вправе менять свои настройки (`ALTER ROLE … SET`), запретить это нельзя — защита не опирается на
неизменность `rolconfig`, параметры задаются заново при каждом подключении. Объём выдачи
ограничен: обёртка отбрасывает строки длиннее 4000 байт (`octet_length(ROW(q.*)::text)`, память бота),
Python обрезает ячейки до 300 символов и весь результат до 20 000 символов с флагом `truncated`
(стоимость вызова модели). Потолки на вопрос: 4
SQL-попытки, 7 вызовов модели, $0.10 по `usage`; сверх них — «уточните вопрос» и исход
`ceiling`. Промпты и аннотация схемы лежат в `telegram_bot/prompts/`; схема для модели
собирается из `data_tables/t.*.sql` разрешённых таблиц (`ai_stats.AI_TABLES`), системный блок
кэшируется, дата — в сообщении пользователя. Журнал — `ai_stats_log` (шаги, черновик, вердикт,
вызовы со стоимостью); он же источник истории (последние 3 пары) и учитывается в лимите
`ai_router.limit_refusal`. Проверки: `tests/test_bot_ai_stats.py` (цикл с подменённой
моделью), `TestAiReaderRole` в `tests/test_db_nhl.py` (роль на настоящей БД), `make
ai-stats-eval` (контрольный набор против настоящего API).

### Меню статистики: `/stats`, `/team_stats`

Задача 92 заменила FSM-диалог `/stats` (подменю, дайджест с вводом даты, турнирная таблица,
`/cancel`) меню без состояний; раскладка кнопок, заголовки таблиц и колонки — один
справочник `leaderboard_specs.py`.

| Команда | Меню | Кнопка → страница |
|---|---|---|
| `/stats` | полевые и вратари (`MAIN_STAT_ROWS`, первый ряд — очки, голы, передачи — выделен `style="primary"`); последняя кнопка «Расширенная статистика» (`sa:menu`) | `st:<таблица>:<колонка>:<offset>` |
| подменю расширенной статистики | расширенные и голы по типу броска (`ADVANCED_ROWS`, `advanced_menu_keyboard`), «« Назад» — `st:menu`; отдельной команды `/advanced` нет | `sa:<таблица>:<колонка>:<offset>` |
| `/team_stats` | голы и броски за/против в среднем за игру, большинство, меньшинство, вбрасывания (`TEAM_STAT_ROWS`, `TEAM_STAT_SPECS`) | `tm:<ключ>:<offset>` |

Страницу всех трёх меню рисует `stats_handlers.stat_page_reply`, нажатия разбирает
`callback_stat_screen` (`STAT_SCREEN_CALLBACK_PATTERN`): листание, `<префикс>:menu` — меню, из
которого открыта таблица, на месте страницы, `<префикс>:save` — страница остаётся в чате, её копия с кнопками
уходит ниже. Паттерн собран из ключей справочника: кнопки убранных экранов в старых сообщениях
ни один хендлер не берёт. Командная таблица — `bot_messages.team_stats_with_count`:
одна колонка `teams_stats` — рейтинг по ней с хвостом игр и баланса, пара «за/против» — рейтинг по
разнице. Дайджест дня — только `/today` и свободный текст (`digest_for_date`), турнирная
таблица — `/standings`, профиль клуба — `/team` и `/BOS`.

**Статистика по странам — команда `/countries`** (Задача 49; до неё —
пункт «По странам» в подменю игроков, Задача 42). Рейтинг стран — выровненная таблица и кнопки
стран; страница страны — standalone-callback `cn:<CODE>:<F|D|G>:<offset>` (нападающие /
защитники / вратари, листание, переключатель групп), `cn:list` — назад к рейтингу. Данные —
`country_rankings()` / `country_page()` в `bot_messages.py`: страна — `rosters.nationality`
(`nationalityCode` из bios, Задача 62),
порог `COUNTRY_MIN_PLAYERS`, игроки без страны — только сноской. Таблица группы —
`_player_group_table()` (общая с `/BOS_FULL`, группы `PLAYER_GROUPS`; вратари — только с хотя бы
одной игрой); все колонки — в rich-таблице `_table()`.

### Модульная структура бота

```
bot.py
  │
  ├── throttle.py             Троттлинг callback-кнопок:
  │                           enforce_callback_rate_limit() — outer-middleware
  │                           Dispatcher.callback_query, раньше всех роутеров
  │
  ├── config.py              Чтение переменных окружения
  │
  ├── keyboards.py           build_menu(), callback_message()
  │
  ├── leaderboard_specs.py   Справочник таблиц и раскладка кнопок меню
  │
  ├── stats_handlers.py      Меню /stats, /team_stats и листание
  │   │                      их таблиц (stat_page_reply, callback_stat_screen),
  │   │                      дайджест, /countries, /tonight, видео голов
  │   │
  │   └── bot_messages.py    Бизнес-логика формирования текстов:
  │       │                  day_digest(), player_stats(),
  │       │                  team_table(), country_page(), game_message()
  │       │
  │       ├── database.py    Пул (SimpleConnectionPool 1–5 conn),
  │       │                  get_connection(), fetch_all(), cached_fetch_all(),
  │       │                  whitelist-валидация (ALLOWED_TABLES, ALLOWED_COLUMNS)
  │       │
  │       └── template_funcs.py   read_template() + output_text()
  │                               → Jinja2 Template.render()
  │
  └── nhl_scoreboard.py      /today / /standings (запросы к Web API NHL,
                             форматирование расписания)
```

---

## Слой данных

### Connection Pool (`database.py`)

- `SimpleConnectionPool` из psycopg2 (1–5 соединений).
- Контекстный менеджер `get_connection()`: auto-rollback при ошибке, возврат в пул при выходе.

### Whitelist-валидация

Для защиты от SQL-инъекций при динамическом построении запросов используется двойная защита:
1. **Whitelist:** `ALLOWED_TABLES` (12 таблиц) и `ALLOWED_COLUMNS` (80+ колонок) — проверка перед использованием.
2. **psycopg2.sql:** идентификаторы оборачиваются в `sql.Identifier()`, параметры подставляются через `%s`.

### Функция `fetch_all()`

Универсальный метод выполнения SELECT-запросов:

```python
fetch_all(query_text, params, columns) → {col1: [...], col2: [...], 'count_rows': N}
```

Возвращает словарь, где ключи — имена колонок, значения — списки значений. Добавляется ключ `count_rows` с количеством строк.

### TTL-кэш `cached_fetch_all()`

`ttl_cache()` — декоратор-мемоизатор поверх `fetch_all()` (`CACHED_FETCH_ALL_TTL_SECONDS`,
5 минут); `cached_fetch_all = ttl_cache(fetch_all)` лежит рядом в `database.py`. Прямые
запросы счёта и событий в `game_message()` и `day_digest()` остаются на `fetch_all()`
без кэша — это живые данные идущей игры. Вспомогательный запрос формы/серии команды
(`_recent_team_outcomes()`, Задача 24 — общий для `_last_n_form_record()` и
`_current_streak()`) и остальные справочники/таблицы/лидеры в `bot_messages.py`
кэшируются через `cached_fetch_all()`, потому что меняются раз в загрузку данных.
Исключение — `game_exists()`: он сам не читает счёт, но управляет тем, какой экран
пользователь увидит (карточка матча vs. превью до его загрузки), поэтому остаётся на
`fetch_all()` — гейт живого экрана, а не свежесть числа.

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
| `team_id` | int | FK → teams (team_id, season_id) |
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
| `nationality` | varchar(10) | Национальность — `nationalityCode` отчётов `skater/bios` / `goalie/bios` у всех строк (не страна рождения, Задача 62); NULL — игрока ещё нет в bios (не сыграл или дебютант), допишется следующим sync |
| `captain` | boolean | Капитан |
| `alternate_captain` | boolean | Ассистент капитана |
| `rookie` | boolean | Новичок |
| `abbreviation` | varchar(10) | Код позиции (дублирует position) |
| `current_team_id` | int | FK → teams (team_id, season_id), nullable |

#### `players_season_stats` — Сезонная статистика полевых игроков

28 колонок, основные: `player_id`, `goals`, `assists`, `points`, `pim`, `shots`, `games`, `hits`, `blocked`, `plus_minus`, `time_on_ice_per_game`, `power_play_goals`, `power_play_points`, `face_off_pct`, `shot_pct`, `game_winning_goals`.
FK `(player_id, season_id) → rosters`; индекс `idx_players_season_stats_season (season_id)` под лидерборды.
То же самое (FK на rosters + индекс на season_id) — у `players_advanced_stats` и `players_shot_types`.

#### `goalies_season_stats` — Сезонная статистика вратарей

24 колонки, основные: `player_id`, `wins`, `losses`, `shutouts`, `save_percentage`, `goal_against_average`, `games`, `saves`, `shots_against`, `goals_against`.
FK `(player_id, season_id) → rosters`; индекс `idx_goalies_season_stats_season (season_id)` под лидерборды вратарей.

#### `games` — Матчи

| Колонка | Тип | Описание |
|---|---|---|
| `game_id` | bigint | UNIQUE. NHL game ID |
| `day` | date | Дата матча |
| `home_team_id` | bigint | FK → teams (team_id, season_id), nullable |
| `away_team_id` | bigint | FK → teams (team_id, season_id), nullable |
| `winner_id` | bigint | FK → teams (team_id, season_id), nullable |
| `is_overtime` | boolean | Овертайм |
| `is_shootouts` | boolean | Буллиты |
| `season` | varchar(10) | Метка сезона (25/26) |
| `season_id` | bigint | NOT NULL. Участвует в трёх FK на `teams` и в индексе `idx_games_season_day` |

Индекс `idx_games_season_day (season_id, day)` — под фильтры `WHERE season_id = %s`
(частично — с `day`), которыми пользуются день-дайджест, «форма команды» и датасет-билдер.

#### `scheduled_games` — Будущие игры (Задача 22A)

Цели predict-датасета: незавершённые игры регулярки на сегодня и завтра (UTC). `games` хранит
только сыгранные, поэтому будущие лежат отдельно; загрузчик заменяет содержимое сезона при каждом прогоне.

| Колонка | Тип | Описание |
|---|---|---|
| `game_id` | bigint | PK. NHL game ID |
| `day` | date | NOT NULL. Дата матча |
| `home_team_id` | bigint | NOT NULL. FK → teams (team_id, season_id) |
| `away_team_id` | bigint | NOT NULL. FK → teams (team_id, season_id) |
| `season_id` | bigint | NOT NULL |

#### `game_predictions` — Вероятности модели (Задачи 22B, 73)

Поток: `scheduled_games` → `build-dataset --mode predict` → `predict` (CSV) →
`publish-predictions` (`home_win`) и `publish-goals` (`over_5_5`, `model = goals`) →
`game_predictions` → бот (читает только PG). Ежедневно публикует сервис `predict`
(`pipeline/scheduled_publish.py`). Строки есть только у игр, где у обеих команд ≥ 5 игр в сезоне.
Бот читает обе задачи только для премиум-чата (Задача 96): `with_model_prediction` (запрос без
кэша по `game_id`) вызывают превью по кнопке `/tonight` и рассылка превью при отправке каждому чату,
общее превью `matchup_season_preview` прогноза не содержит. В подвал добавляется строка «Оценка модели (не совет): победа HOME — N%, тотал больше
5.5 — M%»; части без
строки в таблице пропускаются, нет ни одной — нет и строки.
Строки задачи целиком заменяются при каждой публикации; при непройденном гейте
(нет `latest` / `status != ok`) строки задачи удаляются. FK на `games` нет — игра ещё не сыграна.

| Колонка | Тип | Описание |
|---|---|---|
| `game_id` | bigint | PK (с `task`). NHL game ID |
| `task` | text | PK (с `game_id`). `home_win` / `over_5_5` |
| `model` | text | NOT NULL. `logreg` / `lgbm` |
| `run_id` | text | NOT NULL. Прогон обучения, давший вероятность (из `metadata.json`) |
| `probability` | double precision | NOT NULL, CHECK 0..1 |
| `computed_at` | timestamptz | NOT NULL, default `now()` |

#### `prediction_log` — Журнал прогнозов и исходов (Задача 77)

Простой журнал фактов: что модель показала и чем кончилась игра. Каждая публикация
(`modeling/publish_predictions.py`, та же транзакция, что и `game_predictions`) сперва дописывает
исход в строки журнала, чьи игры уже сыграны (`games.winner_id IS NOT NULL`, есть обе строки
`game_team_stats`): голы основного времени (сумма трёх периодов — рынок считает тотал без овертайма и
буллитов) и `home_won` (победа хозяев с овертаймом/буллитами), затем добавляет опубликованные строки. Повтор
в тот же день обновляет вероятность (в журнале — последнее показанное), строка за более ранний день
остаётся. Строки не удаляются; метрик и вердиктов нет. FK на `games` нет — игра ещё не сыграна.
Таблица создаётся только миграцией 0010 (без `data_tables/t.*.sql`), как `bot_subscriptions`:
журнал не восстановить повторной загрузкой, поэтому `db-drop`/`db-reset` его не стирают.

| Колонка | Тип | Описание |
|---|---|---|
| `game_id` | bigint | PK. NHL game ID |
| `task` | text | PK. `home_win` / `over_5_5` |
| `model` | text | NOT NULL |
| `run_id` | text | PK. Прогон обучения |
| `probability` | double precision | NOT NULL, CHECK 0..1. `home_win` — P(победа хозяев), `over_5_5` — P(тотал > 5.5) |
| `published_on` | date | PK. День публикации (UTC), default |
| `published_at` | timestamptz | NOT NULL. Время последней публикации за день |
| `home_goals`, `away_goals` | int | Голы основного времени; NULL, пока игра не сыграна |
| `home_won` | boolean | Победа хозяев с учётом овертайма/буллитов; NULL, пока игра не сыграна |
| `settled_at` | timestamptz | Когда дописан исход (NULL — исхода ещё нет) |

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
| `team_id` | int | логическая ссылка на teams; DDL FK невозможен — у all_goals нет season_id |
| `game_id` | bigint | FK → games; индекс `idx_all_goals_game_id` |
| `period` | int | Период |
| `time` | varchar(20) | Время гола в периоде |
| `goals_away` / `goals_home` | int | Счёт на момент гола |

#### `shot_attempts` — Попытки бросков из play-by-play (Задача 69A)

PRIMARY KEY (`game_id`, `event_id`), FK `game_id` → `games`. Одна строка на событие play-by-play
с `typeDescKey` ∈ `shot-on-goal`, `missed-shot`, `blocked-shot`, `goal`; буллитная серия (`periodType` SO)
не пишется. Отсутствующее поле API — NULL (у `blocked-shot` нет `shot_type`).

| Колонка | Тип | Описание |
|---|---|---|
| `game_id` / `event_id` | bigint / int | PK; `eventId` события в игре |
| `sort_order` | int | `sortOrder` — порядок событий в игре (для признака подбора) |
| `period` / `period_type` | int / varchar(4) | Номер периода; REG или OT |
| `time_in_period` | varchar(20) | Время в периоде |
| `team_id` | int | БРОСАВШАЯ команда: `eventOwnerTeamId` — бросавшая и у `blocked-shot` (проверено по `rosterSpots`); без FK (нет `season_id`) |
| `event_type` | varchar(20) | `shot-on-goal` / `missed-shot` / `blocked-shot` / `goal` |
| `x_coord` / `y_coord` | int | Координаты |
| `zone_code` | varchar(2) | Зона (O / N / D) |
| `shot_type` | varchar(20) | Тип броска |
| `situation_code` | varchar(4) | Состав на льду (`situationCode`) |
| `home_team_defending_side` | varchar(8) | Сторона, которую защищает хозяин (меняется по периодам) |
| `shooter_id` | bigint | `shootingPlayerId`, у гола — `scoringPlayerId` |
| `goalie_in_net_id` | bigint | Вратарь в воротах |

#### `game_team_stats` — Командная статистика за матч

UNIQUE(`game_id`, `team_id`). Содержит: `goals`, `field` (home/away), `pim`, `shots`, `face_off_win_percentage`, `blocked`, `takeaways`, `giveaways`, `hits`, `fst/snd/trd_period_goals`.

#### `game_player_stats` — Статистика полевого игрока за матч

UNIQUE(`game_id`, `player_id`). Содержит: `time_on_ice`, `goals`, `assists`, `shots`, `hits`, `blocked`, `plus_minus`, `power_play_goals`, `penalty_minutes`, и др.

#### `game_goalie_stats` — Статистика вратаря за матч

UNIQUE(`game_id`, `player_id`). Содержит: `timeOnIce`, `shots`, `saves`, `save_percentage`, `decision` (boolean: победа), детализация по ситуациям (PP/SH/EV).

#### `game_three_stars` — Три звезды матча

UNIQUE(`game_id`, `star`). `star` — 1, 2 или 3 (первая/вторая/третья звезда), `player_id` и
`team_id` — без FK (та же причина, что у остальных пер-игровых таблиц: нет `season_id`,
а звезда может не оказаться в `rosters` того сезона).

### PL/pgSQL функции

#### `get_game_stats(game_id)` → game_team_stats + games + teams

Возвращает: `goals`, `pim`, `blocked`, `hits`, `shots`, `is_overtime`, `is_shootouts`, `field`, `team_name`. Сортировка: `field DESC` (home первым).

#### `get_goals_game(game_id)` → all_goals + rosters (JOIN по scorer, assist1, assist2)

Возвращает: `scorer`, `scorer_position`, `assist_1`, `assist_2` (фамилии), `*_nationality` для каждого из троих (`rosters.nationality` — карточка пишет российских игроков капсом), `period`, `goal_time`, `home_score`, `away_score`, `is_ppg`, `is_shg`, `empty_net`, `winner_goal`, `goal_game_id`, `goal_event_id`. Сортировка: `period, time`.

#### `get_goalies_game(game_id)` → game_goalie_stats + rosters + games

Возвращает: `shots`, `saves`, `timeonice`, `lastname`, `save_percentage`, `is_home`. Сортировка: `is_home DESC` (домашний вратарь первым).

#### `get_three_stars_game(game_id)` → game_three_stars + rosters + teams + game_player_stats + game_goalie_stats + games

Возвращает: `star`, `lastname`, `player_position`, `abbreviation`, `goals`, `assists` (полевой игрок), `saves`, `shots`, `save_percentage` (вратарь) — по звезде одна строка, статистика не своей роли приходит `NULL` из пустого `LEFT JOIN`. Сортировка: `star`.

---

## Jinja2-шаблоны

Все шаблоны расположены в `telegram_bot/messages/` и рендерятся через `template_funcs.output_text()`.

### `game_message.txt` — Карточка матча

```
🏒 Rangers 3:2 Lightning (OT)            ← хозяева первыми, счёт между командами
Периоды: 1:1, 0:0, 1:1, 1:0             ← формы до матча в итоге нет (только в превью)

1-й период                              ← обычный текст, не <pre>; период без голов не выводится
05:23 · 1:0 — PANARIN [L]               ← время внутри периода · счёт после гола — автор
Fox, Zibanejad                          ← ассистенты строкой ниже; россияне капсом
12:45 · 1:1 — KUCHEROV [R] (ББ)         ← ББ/МБ/ПВ + ПШ (победная шайба)
Point
...
Овертайм                                ← «2-й овертайм»… по номеру периода; «Буллиты» — без времени

Броски: 32 - 28 · штрафное время: 6 - 8 ← броски, штрафы, вратари — один блок без жирного
Вратари:                                ← по строке на вратаря, хозяева первыми
Shesterkin — 26/28, 92.86%, 65:00
Vasilevskiy — 29/32, 90.63%, 65:00

Звёзды матча
1. Panarin (NYR) — 2+1
2. Kucherov (TBL) — 1+2
3. Shesterkin (NYR) — 26/28, 92.86%
```

Шапка и сводка `/today` считаются одной функцией `_game_score_header()`; сводка
(`day_digest_summary_body(game_ids)`, элемент на матч) — «1. Хозяева 3:2 Гости» обычным
шрифтом по левому краю (моноширинное выравнивание по счёту на телефоне выглядело криво, отзыв
2026-10-03); периоды — курсивом второй строкой матча с отступом, чтобы строка помещалась на экране
телефона; дата, число матчей, маркер обрезки и подсказка «Ещё: …» — подвал последнего сообщения
дайджеста (Задача 90); матчи разделены пустой строкой (`DIGEST_GAME_SEPARATOR`); обрезка под лимит Telegram —
целыми матчами. Кнопки видео голов —
«▶ 1:0 PANARIN 1п 05:23» (период коротко: «1п», «ОТ», «2ОТ», «Б»; `_period_names()`) в два столбца.

### Rich-сообщения и таблицы

Экраны бота — rich-сообщения Bot API 10.1+ (`send_rich_message`, `answer_rich`,
`edit_text(rich_message=…)`): таблицы, карточки матчей, дайджест, меню `/stats`, `/team_stats`,
`/countries`, превью матчей и рассылки. Тексты по-прежнему собираются построчным
HTML; `bot_messages.rich_message(text)` делает блок строк абзацем `<p>` с переносами
`<br>` (в rich-HTML перевод строки — пробел), пустую строку — зазором `<p>&nbsp;</p>` между
абзацами, а строки-блоки — таблицу, подвал `rich_footer()`, свёрнутый блок `rich_details()` и
список `rich_list()` — отдельными блоками (Задачи 88–89; правила стиля — `docs/telegram_bot.md`
§«Стиль сообщений»). Таблица — `bot_messages._table(rows, align)`:
`<table compact>` (компактные ячейки, Bot API 10.3; `striped` — у таблиц длиннее 10 строк) с шапкой `<th>`, выравниванием по
колонкам и, в турнирной таблице, строкой пунктира (`_PLAYOFF_LINE`) на всю ширину; колонки раскладывает
Telegram, поэтому ни обрезки фамилий, ни выбрасывания колонок под ширину телефона нет. Лимит —
`RICH_MESSAGE_MAX_LENGTH` (32 768 символов, считается по исходному HTML); `truncate_telegram_text`
режет целыми строками, чтобы не разорвать таблицу или тег. Меню статистики и их таблицы — rich:
правка идёт только rich → rich. Обычным текстом остаются служебные ответы (ответы ИИ
текстом), меню `/subscriptions` и подписи к видео. «Сохранить» в таблицах `/stats`
(и его расширенной статистики), `/team_stats` — `copy_message`.

### Лидерборды (без шаблона)

`player_stats_with_count()` / `team_stats_with_count()` рисуют страницу таблицей `_table` с
шапкой. Значение сортировки — колонка с подписью показателя
(`leaderboard_specs.STAT_COLUMN_LABELS`: «Очки», «Голы», «SAT%»…), рядом
постоянный «хвост» (Задача 18): игры/смены для полевых (players_season_stats и, через
`LEFT/INNER JOIN players_season_stats`, players_advanced_stats/players_shot_types),
игры и сейвы/броски против для вратарей, игры/баланс/очки для команд (аббревиатура клуба):

```
   | Игрок       | Ком | Очки | И  | Смен
1. | McDavid [C] | EDM |  138 | 82 | 1841
   | Вратарь     | Ком | Побед| И  | Сейвы
1. | Vasilevskiy | TBL |   39 | 58 | 1353/1483
  |Ком|%очк| И|  В-П-ОТ|  О
1.|COL|73.8|82|55-16-11|121
```

Страница расширенной статистики (кнопки `sa:...`) вместо «Готово» несёт «« К списку» (`sa:menu` —
подменю расширенной статистики в том же сообщении) и «Сохранить» (`sa:save` — страница остаётся в чате без
кнопок, её копия с кнопками уходит новым сообщением).

Кнопка матча `/tonight` (`callback_tonight_game`) шлёт карточку или превью один раз: повторное
нажатие отвечает коротким reply на уже отправленный ответ (тап по цитате прокручивает к нему),
предыдущая такая ссылка удаляется. Отправленные ответы — `stats_handlers._tonight_sent` по чатам, в памяти
процесса: после перезапуска бота первое нажатие снова шлёт ответ.

### `league_table.txt` — Турнирная таблица

Группировка по конференциям и дивизионам: Atlantic, Metropolitan, Central, Pacific. Колонки: место / Команда / Очки / Игры / % очков.
В дивизионе пунктирная черта отделяет топ-3 (прямой выход в плей-офф). Блок Wild Card
конференции — команды вне топ-3 своих дивизионов, не больше пяти (`_WILD_CARD_SHOWN`): `WC1`,
`WC2`, затем черта и три ближайших претендента. Команды, ещё не сыгравшие в сезоне, в `teams_stats`
отсутствуют и в таблицу не попадают.

### `team_profile.txt` — Статистика клуба (Задача 41, Фаза D; переделан 2026-10-01)

`bot_messages.team_profile(abbrev)`: места в дивизионе/конференции/лиге (`teams_stats ⋈ teams`
всей лиги, сортировка как в турнирной таблице), баланс, форма и серия, голы и броски за игру,
большинство/меньшинство/вбрасывания, топ-5 бомбардиров и топ-5 по среднему времени на льду
(`rosters ⋈ players_season_stats`, `_team_skaters()`) и вратари (`rosters ⋈ goalies_season_stats`)
для `current_team_id`/`season_id = config.SEASON_ID` — таблицами `_table()`.
В конце — `/BOS_FULL`: `team_full_stats()`, все игроки клуба тремя таблицами — нападающие,
защитники, вратари (с хотя бы одной игрой) — с колонками экрана страны (`_player_group_table()`),
тот же хендлер `cmd_team_profile` (группа `_FULL` в `TEAM_COMMAND_PATTERN`).
Вход — команда `/BOS` из списка `/team` (`team_list_text()`, хендлер `cmd_team_profile` в
`bot.py` по `TEAM_COMMAND_PATTERN`) и инструмент `team_profile` свободного текста; аббревиатура
резолвится через `_team_id_for_abbrev()`, как в `matchup_season_preview()` (Фаза C).

`players_season_stats` не хранит команду игрока — очки/голы за весь сезон целиком относятся
к тому `current_team_id`, что сейчас стоит в `rosters` (последний обработанный ростер,
`pipeline/load_season_modern.py:364`). Игрок, обменянный в течение сезона, попадёт в профиль
только своей последней команды — со всей суммой очков сезона, а не только с той её частью,
что набрана уже после обмена.

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
│  ├── /goalie/summary                 ├── /gamecenter/{id}/boxscore │
│  └── /game                           └── /gamecenter/{id}/landing  │
└─────────────┬───────────────────────────────────────────────────────┘
              │  HTTP GET (requests.Session, retry ×10, backoff)
              ▼
┌─────────────────────────────┐
│    ModernNhlLoader.run()    │
│    pipeline/                │
│    load_season_modern.py    │
│                             │
│  Парсинг JSON → tuple-ы    │
│  DELETE per-game / UPSERT   │
│  одной транзакцией          │
└─────────────┬───────────────┘
              │  psycopg2 (execute_values)
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         PostgreSQL                                   │
│                                                                      │
│  14 таблиц:                     4 PL/pgSQL функции:                  │
│  teams, teams_stats,            get_game_stats()                     │
│  rosters,                       get_goals_game()                     │
│  players_season_stats,          get_goalies_game()                   │
│  players_advanced_stats,        get_three_stars_game()               │
│  players_shot_types,                                                 │
│  goalies_season_stats,                                               │
│  games, all_goals, shot_attempts,                                    │
│  game_team_stats,                                                    │
│  game_player_stats,                                                  │
│  game_goalie_stats,                                                  │
│  game_three_stars                                                    │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │  psycopg2 (SimpleConnectionPool)
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                                  │
│                                                                      │
│  bot.py → Dispatcher: роутеры standalone и tail                      │
│     │                                                                │
│     ├── stats_handlers.py  Меню статистики, листание таблиц          │
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
make setup              # venv + зависимости
make env-example        # создать .env из шаблона
# отредактировать .env: TELEGRAM_BOT_TOKEN, PG_*, SEASON_ID
make db-reset-local     # создать таблицы и PL/pgSQL функции (DROP → CREATE)
make season-load-full   # загрузить данные из NHL API за текущий сезон
make bot                # запустить бота
```

### Повседневный запуск

```bash
make run-local            # бот без перезагрузки данных
make season-sync-month    # обновить данные за последние ~30 дней
```

---

## Известные особенности и ограничения

1. **Стратегия загрузки:** сезонные таблицы пишутся через UPSERT (`ON CONFLICT … DO UPDATE`), per-game таблицы — через `DELETE … WHERE game_id = ANY(window) → INSERT`. Полностью идемпотентно на пересекающихся окнах. Подробнее — `docs/data_loading.md` §6.3.
2. **FOREIGN KEY (Задача 4, 2026-08-12):** DDL объявляет составные FK `games.(home_team_id|away_team_id|winner_id, season_id) → teams.(team_id, season_id)`, `teams_stats.(team_id, season_id) → teams`, `rosters.(current_team_id, season_id) → teams`, `players_season_stats|players_advanced_stats|players_shot_types|goalies_season_stats.(player_id, season_id) → rosters`, и `game_team_stats|game_player_stats|game_goalie_stats|all_goals|game_three_stars|shot_attempts.game_id → games.game_id` (`shot_attempts` — Задача 69A: удаляется вместе с `all_goals` до `games`, вставляется после `all_goals`). `DELETE`+`INSERT`-стратегия per-game таблиц (`pipeline/load_season_modern.py`) не блокируется: удаление уже шло в порядке «дети раньше родителя» (`all_goals → game_player_stats → game_team_stats → game_goalie_stats → games`), а фактическая вставка в загрузчике — в порядке «родители раньше детей» (`teams → teams_stats → rosters → players_season_stats → players_advanced_stats → players_shot_types → goalies_season_stats → games → all_goals → game_team_stats → game_player_stats → game_goalie_stats`); оба порядка проверены на живой БД (6559 игр, 5 сезонов, 0 нарушений FK). `game_three_stars` (Задача 23.2) встроена в загрузчике в тот же порядок — первой при удалении, последней при вставке — и проверена на живой БД отдельно (прогон по всем 5 сезонам, 6560 игр, 2026-09-15…09-18): 0 строк с `game_id`, отсутствующим в `games`. FK по `team_id`/`player_id` на самих пер-игровых таблицах (`game_team_stats`, `game_player_stats`, `game_goalie_stats`, `all_goals`, `game_three_stars`, `shot_attempts`) не объявлены: эти таблицы не хранят `season_id`, а `teams`/`rosters` уникальны только по составному `(id, season_id)` — без `season_id` в дочерней строке корректная ссылка невозможна. `all_goals` по-прежнему без PK (только `event_id`), но получил индекс на `game_id` (обслуживает `telegram_bot/queries/get_goals_game.sql`) и FK на `games`. `games` получил индекс `(season_id, day)` под фактические паттерны бота (форма команды, дневной дайджест, датасет-билдер).
3. **Working directory:** Шаблоны загружаются по относительным путям (`messages/game_message.txt`) — бот должен запускаться из директории `telegram_bot/`.
4. **aiogram 3.31:** asyncio-API (`Dispatcher`, роутеры, DI зависимостей хендлеров), Bot API 10.3 — Rich Messages и компактные таблицы доступны типизированно. Переезд с python-telegram-bot 21.11.1 — решение человека 2026-10-26 (PTB отстал на Bot API 8.3–10.0, план — `plan/rich_messages/`). Планировщик не нужен: рассылка живёт вне процесса бота, в отдельном скрипте `push_digest_job.py` (запускается сервисом `sync`, Задача 34).
5. **Rosters `abbreviation`:** Pipeline записывает код позиции в колонку `abbreviation`, хотя по смыслу это поле предназначено для аббревиатуры команды.
6. **Датасет-билдер — мультисезонная сборка (Задача 30, 2026-09-12):** `python -m modeling.cli build-dataset` кладёт `dataset_{train,predict}.csv` и `metadata_*.json` плоско в `artifacts/datasets/` (дефолт CLI) — не коммитятся, `--season-ids` фиксирует, какие сезоны вошли, в `data_snapshot_id` метаданных (пустое значение пишет `seasons=all` и не восстанавливается из артефакта, поэтому прогон всегда с явным списком). Rolling-окна (`features.py::compute_team_rolling_features`) и as-of снэпшоты (`features.py::_snapshot_side`, `merge_asof`) группируются по `(team_id, season_id)`, а не только по `team_id` — без этого признаки первой игры нового сезона утекали статистику из прошлого. Из-за сброса окон на границе каждого сезона `apply_cold_start_policy` (train, `min_prior_games=5`) отбрасывает больше строк, чем при однолетней сборке: на 5 сезонах/6560 играх — 6049 строк (511 отброшено, а не только в начале первого сезона). Подробности и контракт — `docs/modeling_dataset_builder.md`.
