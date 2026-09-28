# NHL_bot

Telegram bot with NHL stats:
- `pipeline/load_season_modern.py` loads NHL API data into PostgreSQL
- `telegram_bot/` reads PostgreSQL and shows stats in Telegram menus

## Quick start

From project root:

```bash
cd /Users/petrkarol/Desktop/projects/NHL_bot
make setup
make env-example
```

Then open `.env` and fill at least:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
PG_HOST=localhost
PG_PORT=5432
PG_USER=
PG_DATABASE=postgres
SEASON_ID=20262027
DATE_FROM=2026-03-18
DATE_TO=2026-03-18
```

If `PG_USER` is empty, Makefile uses your current macOS user automatically.

## Initialize database

```bash
make db-init
```

If your local PostgreSQL user is not `postgres`, use:

```bash
make db-init-local
```

This applies:
- table DDL from `data_tables/*.sql`
- SQL functions from `telegram_bot/queries/*.sql`

## Load NHL data

```bash
make season-sync-month        # last ~30 days
make season-load-full         # full current season, from the derived season start (SEASON_ID) to today
make season-sync DATE_FROM=2026-09-01 DATE_TO=2027-03-29  # explicit window
```

Подробности — [`docs/data_loading.md`](docs/data_loading.md).

## Run Telegram bot

```bash
make bot
```

In Telegram send `/stats`.

## Tests

- **`make test-fast`** — `pytest` по всем файлам в `tests/`, кроме `test_db_nhl.py` (без обязательной БД).
- **`make test-db`** — `unittest` для схемы PostgreSQL; нужен доступ к БД из `.env`, таблицы из `make db-init` / `db-init-local`. Включает `RUN_DB_SCHEMA_TESTS=1` (см. `Makefile`).
- **`make test-db-data`** — проверки загруженных данных (`TestNhlLoadedData`); нужна БД с данными.
- **`make all-tests`** — сначала `test-fast`, затем `test-db` (полный контур для разработчика с поднятой БД).

Общие правила для разработчиков — [`DEVELOPMENT.md`](DEVELOPMENT.md).

Проверки **загруженных данных** (`TestNhlLoadedData`) по умолчанию выключены. После загрузки сезона:

```bash
make test-db-data
```

## One-command flows

Stable local start (setup + bot). Use this when NHL API is unavailable:

```bash
make run-local
```

`setup` recreates `.venv` only if Python architecture changed (arm64/x86_64).

Bot run (with setup + env copy helper):

```bash
make run-bot
```

Load full season using modern NHL API (`SEASON_ID` from `.env`, required; the start date is
derived from it, `DATE_TO` defaults to today — `.env`'s `DATE_FROM`/`DATE_TO`, if set, are
cleared for this target, see `Makefile`):

```bash
make season-load-full
```

## Manual equivalent commands (without Makefile)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

for f in data_tables/*.sql; do psql -h localhost -p 5432 -U postgres -d postgres -f "$f"; done
for f in telegram_bot/queries/*.sql; do psql -h localhost -p 5432 -U postgres -d postgres -f "$f"; done

cd pipeline && ../.venv/bin/python -u load_season_modern.py \
    --date-from 2026-09-01 --date-to 2027-03-29 \
    --season-id 20262027
cd ../telegram_bot && TELEGRAM_BOT_TOKEN=your_bot_token ../.venv/bin/python bot.py
```