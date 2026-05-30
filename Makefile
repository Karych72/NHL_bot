ifneq (,$(wildcard .env))
include .env
export
endif

PYTHON := python3
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python
ARCH := $(shell uname -m)
VENV_ARCH_FILE := $(VENV)/.arch

PG_HOST ?= localhost
PG_PORT ?= 5432
PG_USER ?= postgres
PG_DATABASE ?= postgres

PG_HOST_EFF := $(if $(strip $(PG_HOST)),$(PG_HOST),localhost)
PG_PORT_EFF := $(if $(strip $(PG_PORT)),$(PG_PORT),5432)
PG_USER_EFF := $(if $(strip $(PG_USER)),$(PG_USER),$(shell id -un))
PG_DATABASE_EFF := $(if $(strip $(PG_DATABASE)),$(PG_DATABASE),postgres)

PSQL := psql -h $(PG_HOST_EFF) -p $(PG_PORT_EFF) -U $(PG_USER_EFF) -d $(PG_DATABASE_EFF)

DROP_SQL := scripts/db_drop_all_tables.sql
# Create order: season-scoped dimensions, then games, then per-game tables
DDL_TABLES := \
	data_tables/t.teams.sql \
	data_tables/t.teams_stats.sql \
	data_tables/t.rosters.sql \
	data_tables/t.players_season_stats.sql \
	data_tables/t.players_advanced_stats.sql \
	data_tables/t.players_shot_types.sql \
	data_tables/t.goalies_season_stats.sql \
	data_tables/t.games.sql \
	data_tables/t.game_team_stats.sql \
	data_tables/t.game_player_stats.sql \
	data_tables/t.game_goalie_stats.sql \
	data_tables/t.all_goals.sql
FN_FILES  := $(wildcard telegram_bot/queries/*.sql)

# NHL regular season start for full reloads (override: make season-load-full SEASON_START=2024-10-01)
SEASON_START ?= 2025-10-01
# Portable "30 days ago" for season-sync-month (requires Python)
MONTH_AGO := $(shell $(PYTHON) -c "from datetime import date, timedelta; print((date.today()-timedelta(days=30)).isoformat())")

# tests/test_db_nhl.py: schema checks default on; skip with RUN_DB_SCHEMA_TESTS=0 make test-db
export RUN_DB_SCHEMA_TESTS ?= 1

.PHONY: setup setup-dev modeling-dev env-example db-drop db-tables db-tables-local db-reset db-reset-local db-init db-init-local db-sync db-sync-local db-functions db-functions-local db-bot-subscriptions season-sync season-load-full season-reload-current season-sync-week season-sync-month season-sync-today season-load season-update bot run-bot run-local check-token verify-skater-schema test-skater-bot test-fast test-db all-tests lint typecheck ci-local

setup:
	@if [ ! -d "$(VENV)" ] || [ ! -f "$(VENV_ARCH_FILE)" ] || [ "$$(cat "$(VENV_ARCH_FILE)")" != "$(ARCH)" ]; then \
		rm -rf "$(VENV)"; \
		$(PYTHON) -m venv "$(VENV)"; \
		echo "$(ARCH)" > "$(VENV_ARCH_FILE)"; \
	fi
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt

setup-dev: setup
	$(PIP) install -r requirements-dev.txt

modeling-dev: setup
	$(PIP) install -r requirements-modeling.txt

env-example:
	cp -n .env.example .env || true
	@echo ".env created (if it did not exist)"

db-drop:
	@echo "=== Dropping NHL_bot tables ==="
	@$(PSQL) -v ON_ERROR_STOP=1 -f $(DROP_SQL)

db-tables:
	@echo "=== Creating tables ==="
	@for f in $(DDL_TABLES); do \
		echo "  $$f"; \
		$(PSQL) -v ON_ERROR_STOP=1 -f $$f; \
	done

db-tables-local:
	$(MAKE) db-tables PG_USER="$$(id -un)"

# Full reset: DROP all NHL_bot tables, CREATE from data_tables/t.*.sql, load SQL functions.
db-reset: db-drop db-tables db-functions
	@echo "=== db-reset complete ==="

db-reset-local:
	$(MAKE) db-reset PG_USER="$$(id -un)"

# Same as db-reset (destructive). Use after pulling DDL changes.
db-init: db-reset

db-init-local: db-reset-local

# Apply CREATE TABLE scripts only (fails if tables already exist).
db-sync: db-tables db-functions
	@echo "=== db-sync complete ==="

db-sync-local:
	$(MAKE) db-sync PG_USER="$$(id -un)"

verify-skater-schema:
	@$(PSQL) -v ON_ERROR_STOP=1 -f scripts/verify_skater_reports_schema.sql

db-bot-subscriptions:
	@echo "=== Creating bot_subscriptions (push / digest opt-in) ==="
	@$(PSQL) -v ON_ERROR_STOP=1 -f scripts/create_bot_subscriptions.sql

test-skater-bot:
	@$(PY) -m unittest tests.test_skater_reports_bot -v

# Pytest over tests/* except unittest DB module (see all-tests / test-db).
test-fast:
	@echo "=== pytest (без tests/test_db_nhl.py) ==="
	@$(PY) -m pytest tests/ -q --ignore=tests/test_db_nhl.py

test-db:
	@$(PY) -m unittest tests.test_db_nhl -v

# Полный прогон: быстрые тесты + проверки схемы БД (нужны PostgreSQL и DDL, см. README).
all-tests: test-fast test-db
	@echo "=== all-tests завершён ==="

lint:
	@$(PY) -m ruff check telegram_bot modeling pipeline

typecheck:
	@$(PY) -m mypy telegram_bot modeling pipeline

# Same checks as GitHub Actions (no DB-backed tests).
ci-local: lint typecheck
	@$(PY) -m compileall -q telegram_bot modeling pipeline
	@$(PY) -m pytest tests/ -q --ignore=tests/test_db_nhl.py

db-functions:
	@for f in $(FN_FILES); do \
		echo "  $$f"; \
		$(PSQL) -q -f $$f; \
	done
	@echo "Functions synced."

db-functions-local:
	$(MAKE) db-functions PG_USER="$$(id -un)"

# Unified entry: pass DATE_FROM and DATE_TO (and optional SEASON_ID / CURRENT_SEASON via .env).
# Example: make season-sync DATE_FROM=2025-10-01 DATE_TO=2026-03-29
season-sync: setup env-example
	@if [ -z "$(strip $(DATE_FROM))" ] || [ -z "$(strip $(DATE_TO))" ]; then \
		echo "Usage: make season-sync DATE_FROM=YYYY-MM-DD DATE_TO=YYYY-MM-DD"; \
		exit 1; \
	fi
	cd pipeline && DATE_FROM="$(DATE_FROM)" DATE_TO="$(DATE_TO)" ../$(PY) -u load_season_modern.py

# Full season to today (aggregates + all finished games from SEASON_START through today).
season-load-full: setup env-example
	$(MAKE) season-sync DATE_FROM="$(SEASON_START)" DATE_TO="$$(date +%Y-%m-%d)"

season-reload-current: season-load-full

# Last 7 days inclusive (macOS date; same as historical season-update).
season-sync-week: setup env-example
	$(MAKE) season-sync DATE_FROM="$$(date -v-7d +%Y-%m-%d)" DATE_TO="$$(date +%Y-%m-%d)"

# Last 30 days inclusive through today (DATE_FROM via Python; DATE_TO uses date(1)).
season-sync-month: setup env-example
	$(MAKE) season-sync DATE_FROM="$(MONTH_AGO)" DATE_TO="$$(date +%Y-%m-%d)"

# Single calendar day (finished games that day only).
season-sync-today: setup env-example
	$(MAKE) season-sync DATE_FROM="$$(date +%Y-%m-%d)" DATE_TO="$$(date +%Y-%m-%d)"

season-load: season-load-full

season-update: season-sync-week

check-token:
	@if [ -z "$$TELEGRAM_BOT_TOKEN" ]; then \
		echo "TELEGRAM_BOT_TOKEN is empty. Put it into .env or export it in shell."; \
		exit 1; \
	fi

bot: check-token
	cd telegram_bot && ../$(PY) bot.py

run-bot: setup env-example bot

run-local:
	$(MAKE) run-bot
