# Разработка NHL_bot

Краткие правила, на которые ориентируемся в команде и в CI.

## Окружение

- **Python 3.11** — целевая версия (как в GitHub Actions).
- Рабочее окружение: `make setup` → каталог `.venv`, зависимости из `requirements.txt`. Не полагаться на глобальный `pip` на macOS без venv (см. комментарий в `requirements.txt` про архитектуру и NumPy).
- Линтер и mypy: `make setup-dev` (дополнительно ставит `requirements-dev.txt`).
- Секреты только в `.env`; в репозитории — шаблон `.env.example`. Токены бота и доступы к БД не коммитить.

## Docker

- Сборка образа бота из корня репозитория: `docker build -t nhl-bot .`
- Локальный стек PostgreSQL + бот: скопировать `.env.example` в `.env`, задать `TELEGRAM_BOT_TOKEN`, затем `docker compose up`. Сервис `db` пробрасывает порт `5432`; в compose для бота выставлены `PG_HOST=db` и `PG_USER=postgres` (см. `docker-compose.yml`). Перед первым запуском бота примените DDL/SQL-функции к этой БД с хоста, например `make db-init` с `PG_HOST=localhost` (когда контейнер `db` уже слушает порт).
- В образ не копируется `.env`; при `docker compose up` используется `env_file: .env`.

## Тесты и качество

- **Минимум перед PR / слиянием:** `make ci-local` (ruff, mypy, `compileall`, `pytest` без `test_db_nhl`) — совпадает с GitHub Actions.
- Только быстрые тесты без линтера: `make test-fast`.
- **Полный контур локально:** при изменениях DDL, SQL-функций или загрузчика — поднять БД, `make db-init` / `db-init-local`, затем `make all-tests` (схема и при необходимости данные — см. `README.md`).
- Ломающие изменения в `data_tables/*.sql` или `telegram_bot/queries/*.sql` сопровождаем понятным порядком применения (как в `Makefile`: `DDL_TABLES`, затем функции).

## Структура проекта

| Область | Назначение |
|--------|------------|
| `pipeline/` | Загрузка NHL API → PostgreSQL |
| `telegram_bot/` | Telegram-бот; запросы к БД — в том числе `telegram_bot/queries/*.sql` |
| `modeling/` | Сборка датасетов, CLI |
| `data_tables/` | DDL таблиц |
| `docs/` | Описание пайплайнов и архитектуры |
| `plan/` | Черновики планов (не дублируем договорённости из этого файла без обновления) |

## Зависимости и изменения кода

- Пакеты приложения — в `requirements.txt`; инструменты CI/линтера — в `requirements-dev.txt`, по необходимости с коротким комментарием «зачем».
- **Зависимости моделирования** — отдельный файл `requirements-modeling.txt`: семь ML-пакетов этапа 3 (LightGBM, scikit-learn, matplotlib и др.) плюс `pydantic` v2 для `modeling/config.py` (этап 2). Установка: `make modeling-dev`. Они **не** входят в `requirements.txt`, **не** ставятся через `make setup` / `make run-bot` и **не** попадают в Docker-образ бота. CatBoost в v1 не используется (опционально — этап 15 UPDATE-плана). Подробнее: [`docs/modeling_training.md`](docs/modeling_training.md#dependencies).
- Следуем стилю существующих модулей (импорты, имена, обработка ошибок) вместо разнобоя.
- Большие бинарные артефакты и выгрузки данных не коммитить без явной договорённости (см. `.gitignore`).

## CI

Файл `.github/workflows/ci.yml` (runner `ubuntu-24.04`, Python 3.11): установка `requirements.txt` и `requirements-dev.txt`, затем **Ruff** (`telegram_bot`, `modeling`, `pipeline`), **mypy** (те же каталоги, настройка в `mypy.ini`), `compileall`, **pytest** без `tests/test_db_nhl.py`.
