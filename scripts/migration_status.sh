#!/usr/bin/env bash
# Какие миграции применены на стенде: последние строки schema_migrations базы nhl_dev (dev-бот) или
# postgres (прод). Ничего не меняет. Миграции применяет сам деплой (сервис migrate) после мержа в
# develop или master — скрипт только проверяет, что он это сделал. Запускается с ноутбука, ходит на
# VM по `gcloud compute ssh`. Инструкция — DEVELOPMENT.md, §«Dev-стенд».
#
#   bash scripts/migration_status.sh          — nhl_dev
#   bash scripts/migration_status.sh postgres — прод
set -euo pipefail

DB=${1:-nhl_dev}
[[ $DB == nhl_dev || $DB == postgres ]] || { echo "Использование: $0 [nhl_dev|postgres]" >&2; exit 1; }

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod

SQL="SELECT version, applied_at AT TIME ZONE 'Europe/Moscow' AS applied_msk
     FROM schema_migrations ORDER BY version DESC LIMIT 5;"

printf '%s\n' "$SQL" | gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --command \
  "cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U postgres -d $DB"
