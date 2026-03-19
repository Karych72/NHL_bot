# NHL_bot

Telegram bot with NHL stats:
- `pipeline/` loads NHL API data into PostgreSQL and CSV/JSON dumps
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

## Load NHL data (pipeline)

```bash
make pipeline
```

This will:
- create required data directories
- fetch data from NHL API
- insert data into PostgreSQL

## Run Telegram bot

```bash
make bot
```

In Telegram send `/stats`.

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

Pipeline run only:

```bash
make run-pipeline
```

Load full season using modern NHL API (`SEASON_ID`, `DATE_FROM`, `DATE_TO` from `.env`):

```bash
make season-load
```

Full local cycle (fresh venv + db-init-local + pipeline + bot):

```bash
make run-full
```

## Manual equivalent commands (without Makefile)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

mkdir -p all_data/dataframes/rosters all_data/dataframes/players all_data/dataframes/teams all_data/dataframes/myself_analyses

for f in data_tables/*.sql; do psql -h localhost -p 5432 -U postgres -d postgres -f "$f"; done
for f in telegram_bot/queries/*.sql; do psql -h localhost -p 5432 -U postgres -d postgres -f "$f"; done

cd pipeline && ../.venv/bin/python teams_and_players.py && ../.venv/bin/python pipeline.py
cd ../telegram_bot && TELEGRAM_BOT_TOKEN=your_bot_token ../.venv/bin/python bot.py
```