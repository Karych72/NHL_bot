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
	data_tables/t.game_three_stars.sql \
	data_tables/t.game_team_stats.sql \
	data_tables/t.game_player_stats.sql \
	data_tables/t.game_goalie_stats.sql \
	data_tables/t.all_goals.sql
FN_FILES  := $(wildcard telegram_bot/queries/*.sql)
# Migrations: data_tables/migrations/NNNN_slug.{up,down}.sql, applied in ascending
# version order and tracked in schema_migrations (see DEVELOPMENT.md). Ordering is a
# plain string sort ($(sort ...) here, `ORDER BY version` in SQL) — NNNN must stay the
# same width (zero-padded) across all migrations or the order breaks.
MIGRATION_FILES := $(sort $(wildcard data_tables/migrations/*.up.sql))
MIGRATIONS_TABLE_DDL := CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())

# Portable "30 days ago" for season-sync-month (requires Python)
MONTH_AGO := $(shell $(PYTHON) -c "from datetime import date, timedelta; print((date.today()-timedelta(days=30)).isoformat())")

# tests/test_db_nhl.py: schema checks default on; skip with RUN_DB_SCHEMA_TESTS=0 make test-db
export RUN_DB_SCHEMA_TESTS ?= 1

.PHONY: setup setup-dev modeling-dev modeling-train env-example db-drop db-tables db-tables-local db-reset db-reset-local db-init db-init-local db-sync db-sync-local db-functions db-functions-local db-migrate db-migrate-down season-sync season-load-full season-reload-current season-sync-week season-sync-month season-sync-today season-load season-update bot run-bot run-local verify-skater-schema test-skater-bot test-fast test-db test-db-data all-tests lint typecheck lock ci-local

setup:
	@if [ ! -d "$(VENV)" ] || [ ! -f "$(VENV_ARCH_FILE)" ] || [ "$$(cat "$(VENV_ARCH_FILE)")" != "$(ARCH)" ]; then \
		rm -rf "$(VENV)"; \
		$(PYTHON) -m venv "$(VENV)"; \
		echo "$(ARCH)" > "$(VENV_ARCH_FILE)"; \
	fi
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt

setup-dev: setup modeling-dev
	$(PIP) install -r requirements-dev.txt

modeling-dev: setup
	$(PIP) install -r requirements-modeling.txt

modeling-train: modeling-dev
	$(PY) -m modeling.cli train --config configs/modeling_default.yaml

env-example:
	cp -n .env.example .env || true
	@echo ".env created (if it did not exist)"

db-drop:
	@echo "=== Dropping NHL_bot tables ==="
	@$(PSQL) -v ON_ERROR_STOP=1 -f $(DROP_SQL)

db-tables:
	@echo "=== Creating tables ==="
	@set -e; for f in $(DDL_TABLES); do \
		echo "  $$f"; \
		$(PSQL) -v ON_ERROR_STOP=1 -f $$f; \
	done

db-tables-local:
	$(MAKE) db-tables PG_USER="$$(id -un)"

# Full reset: DROP all NHL_bot tables, CREATE from data_tables/t.*.sql, load SQL
# functions, apply migrations. db-drop only drops DDL_TABLES — bot_subscriptions and
# schema_migrations survive it, so a migration touching a DDL_TABLES table must also be
# mirrored (idempotently) into its t.*.sql — see DEVELOPMENT.md § «Миграции схемы БД».
db-reset: db-drop db-tables db-functions db-migrate
	@echo "=== db-reset complete ==="

db-reset-local:
	$(MAKE) db-reset PG_USER="$$(id -un)"

# Same as db-reset (destructive). Use after pulling DDL changes.
db-init: db-reset

db-init-local: db-reset-local

# Apply DDL (fails if tables already exist), then SQL functions, then migrations. No DROP.
db-sync: db-tables db-functions db-migrate
	@echo "=== db-sync complete ==="

db-sync-local:
	$(MAKE) db-sync PG_USER="$$(id -un)"

verify-skater-schema:
	@$(PSQL) -v ON_ERROR_STOP=1 -f scripts/verify_skater_reports_schema.sql

# Apply all unapplied data_tables/migrations/*.up.sql (ascending version), each in the
# same transaction as its schema_migrations row. Already-applied versions are skipped.
db-migrate:
	@echo "=== Applying migrations ==="
	@$(PSQL) -v ON_ERROR_STOP=1 -q -c "SET client_min_messages=warning; $(MIGRATIONS_TABLE_DDL)"
	@set -e; for f in $(MIGRATION_FILES); do \
		version=$$(basename $$f .up.sql | cut -d_ -f1); \
		applied=$$($(PSQL) -t -A -v ON_ERROR_STOP=1 -c "SELECT 1 FROM schema_migrations WHERE version = '$$version'"); \
		if [ "$$applied" = "1" ]; then \
			echo "  $$version already applied, skipping"; \
		else \
			echo "  applying $$version ($$f)"; \
			$(PSQL) -v ON_ERROR_STOP=1 --single-transaction -f $$f -c "INSERT INTO schema_migrations (version) VALUES ('$$version')"; \
		fi; \
	done
	@echo "Migrations applied."

# Roll back the single most recently applied migration (its *.down.sql + schema_migrations
# row, one transaction). No applied migrations is a clean no-op, not an error; a missing
# *.down.sql for an applied version is a hard failure.
db-migrate-down:
	@echo "=== Rolling back last migration ==="
	@$(PSQL) -v ON_ERROR_STOP=1 -q -c "SET client_min_messages=warning; $(MIGRATIONS_TABLE_DDL)"
	@set -e; version=$$($(PSQL) -t -A -v ON_ERROR_STOP=1 -c "SELECT version FROM schema_migrations ORDER BY applied_at DESC, version DESC LIMIT 1"); \
	if [ -z "$$version" ]; then \
		echo "No applied migrations, nothing to roll back."; \
	else \
		downfile=$$(ls data_tables/migrations/$${version}_*.down.sql 2>/dev/null | head -1); \
		if [ -z "$$downfile" ]; then \
			echo "Missing down migration for applied version $$version: data_tables/migrations/$${version}_*.down.sql" >&2; \
			exit 1; \
		fi; \
		echo "  rolling back $$version ($$downfile)"; \
		$(PSQL) -v ON_ERROR_STOP=1 --single-transaction -f $$downfile -c "DELETE FROM schema_migrations WHERE version = '$$version'"; \
		echo "Rollback complete."; \
	fi

test-skater-bot:
	@$(PY) -m unittest tests.test_skater_reports_bot -v

# Pytest over tests/* except unittest DB module (see all-tests / test-db).
test-fast:
	@echo "=== pytest (без tests/test_db_nhl.py) ==="
	@$(PY) -m pytest tests/ -q --ignore=tests/test_db_nhl.py

test-db:
	@$(PY) -m unittest tests.test_db_nhl -v

# Data-integrity checks (TestNhlLoadedData) against a DB you've already loaded,
# e.g. via `make season-sync-month`. NOT run in CI: the CI Postgres service has
# schema (db-sync) but no loaded games/rosters/stats, so these would just fail.
test-db-data:
	@RUN_DB_DATA_TESTS=1 $(PY) -m unittest tests.test_db_nhl -v

# Полный прогон: быстрые тесты + проверки схемы БД (нужны PostgreSQL и DDL, см. README).
all-tests: test-fast test-db
	@echo "=== all-tests завершён ==="

lint:
	@$(PY) -m ruff check telegram_bot modeling pipeline tests

typecheck:
	@$(PY) -m mypy telegram_bot modeling pipeline

# Перегенерация трёх requirements*.txt из requirements*.in (pip-tools, requirements-dev.in).
# Порядок важен: сначала рантайм-слой, затем modeling и dev констрейнтятся им
# (-c requirements.txt), а dev — ещё и modeling-слоем (-c requirements-modeling.txt), чтобы
# `pip install -r requirements.txt -r requirements-dev.txt -r requirements-modeling.txt`
# (см. ci.yml) всегда получал непротиворечивый набор версий. Без --upgrade pip-compile
# держит уже закоммиченные пины, двигая только то, что реально требуется — см. DEVELOPMENT.md.
lock: setup-dev
	$(VENV)/bin/pip-compile --generate-hashes --allow-unsafe --output-file=requirements.txt requirements.in
	$(VENV)/bin/pip-compile --generate-hashes --allow-unsafe --output-file=requirements-modeling.txt -c requirements.txt requirements-modeling.in
	$(VENV)/bin/pip-compile --generate-hashes --allow-unsafe --output-file=requirements-dev.txt -c requirements.txt -c requirements-modeling.txt requirements-dev.in

# Same checks as GitHub Actions (no DB-backed tests). setup-dev pulls in
# requirements-modeling.txt (via modeling-dev) so tests/test_modeling_*.py actually collect.
ci-local: setup-dev lint typecheck
	@$(PY) -m compileall -q telegram_bot modeling pipeline
	@$(PY) -m pytest tests/ -q --ignore=tests/test_db_nhl.py

db-functions:
	@set -e; for f in $(FN_FILES); do \
		echo "  $$f"; \
		$(PSQL) -v ON_ERROR_STOP=1 -q -f $$f; \
	done
	@echo "Functions synced."

db-functions-local:
	$(MAKE) db-functions PG_USER="$$(id -un)"

# Unified entry: pass DATE_FROM and DATE_TO (SEASON_ID is required, via .env or
# --season-id — see telegram_bot/config.py / load_season_modern.py).
# Example: make season-sync DATE_FROM=2026-09-01 DATE_TO=2026-10-15
season-sync: setup env-example
	@if [ -z "$(strip $(DATE_FROM))" ] || [ -z "$(strip $(DATE_TO))" ]; then \
		echo "Usage: make season-sync DATE_FROM=YYYY-MM-DD DATE_TO=YYYY-MM-DD"; \
		exit 1; \
	fi
	cd pipeline && DATE_FROM="$(DATE_FROM)" DATE_TO="$(DATE_TO)" ../$(PY) -u load_season_modern.py

# Full season to today (aggregates + all finished games from the season start
# derived from SEASON_ID — config.derive_season(), Задача 33 — through today).
# DATE_FROM is computed fresh here, not taken from $(DATE_FROM): Makefile does
# `include .env; export`, so a DATE_FROM a user set in .env for a one-off
# `season-sync` would otherwise leak in here and silently override the season
# start. For a custom start date, call `make season-sync DATE_FROM=... DATE_TO=...` directly.
season-load-full: setup env-example
	$(MAKE) season-sync \
		DATE_FROM="$$($(PY) -c 'import sys; sys.path.insert(0, "telegram_bot"); import config; print(config.derive_season(config.SEASON_ID)[1])')" \
		DATE_TO="$$(date +%Y-%m-%d)"

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

bot:
	cd telegram_bot && ../$(PY) bot.py

run-bot: setup env-example bot

run-local:
	$(MAKE) run-bot
