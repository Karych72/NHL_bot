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

.PHONY: setup env-example dirs db-init db-init-local pipeline season-load bot run-pipeline run-bot run-local run-full check-token

setup:
	@if [ ! -d "$(VENV)" ] || [ ! -f "$(VENV_ARCH_FILE)" ] || [ "$$(cat "$(VENV_ARCH_FILE)")" != "$(ARCH)" ]; then \
		rm -rf "$(VENV)"; \
		$(PYTHON) -m venv "$(VENV)"; \
		echo "$(ARCH)" > "$(VENV_ARCH_FILE)"; \
	fi
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt

env-example:
	cp -n .env.example .env || true
	@echo ".env created (if it did not exist)"

dirs:
	mkdir -p all_data/dataframes/rosters
	mkdir -p all_data/dataframes/players
	mkdir -p all_data/dataframes/teams
	mkdir -p all_data/dataframes/myself_analyses

db-init:
	for f in data_tables/*.sql; do $(PSQL) -f $$f; done
	for f in telegram_bot/queries/*.sql; do $(PSQL) -f $$f; done

db-init-local:
	$(MAKE) db-init PG_USER="$$(id -un)"

pipeline: dirs
	cd pipeline && ../$(PY) teams_and_players.py && ../$(PY) pipeline.py

season-load: setup env-example
	cd pipeline && ../$(PY) -u load_season_modern.py

check-token:
	@if [ -z "$$TELEGRAM_BOT_TOKEN" ]; then \
		echo "TELEGRAM_BOT_TOKEN is empty. Put it into .env or export it in shell."; \
		exit 1; \
	fi

bot: check-token
	cd telegram_bot && ../$(PY) bot.py

run-pipeline: setup env-example dirs db-init-local pipeline

run-bot: setup env-example bot

run-full: setup env-example dirs db-init-local pipeline bot

run-local:
	$(MAKE) run-bot
