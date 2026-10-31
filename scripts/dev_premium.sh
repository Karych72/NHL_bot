#!/usr/bin/env bash
# Премиум чата на dev-боте (база nhl_dev) для живой проверки Задачи 96: выдать на 2 часа или снять.
# Запускается с ноутбука: ходит на VM по `gcloud compute ssh` и меняет `premium_access` через psql
# в контейнере db прод-стека. Прод (база postgres) не трогает. Инструкция — DEVELOPMENT.md, §«Премиум».
#
#   bash scripts/dev_premium.sh grant  <chat_id>   — премиум до now() + 2 часа (повтор — снова 2 часа от текущего момента)
#   bash scripts/dev_premium.sh revoke <chat_id>   — снять немедленно
#   bash scripts/dev_premium.sh status <chat_id>   — показать строку, ничего не меняя
set -euo pipefail

usage() {
  echo "Использование: $0 grant|revoke|status <chat_id>" >&2
  exit 1
}

[ $# -eq 2 ] || usage
ACTION=$1
CHAT_ID=$2
[[ $CHAT_ID =~ ^-?[0-9]+$ ]] || usage

DB=nhl_dev
VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod

STATUS_SQL="SELECT chat_id, expires_at AT TIME ZONE 'Europe/Moscow' AS expires_msk,
                   expires_at > now() AS active
            FROM premium_access WHERE chat_id = $CHAT_ID;"

case "$ACTION" in
  grant)
    SQL="INSERT INTO premium_access (chat_id, expires_at) VALUES ($CHAT_ID, now() + interval '2 hours')
         ON CONFLICT (chat_id) DO UPDATE SET expires_at = EXCLUDED.expires_at, granted_at = now();
         $STATUS_SQL" ;;
  revoke)
    SQL="DELETE FROM premium_access WHERE chat_id = $CHAT_ID;
         $STATUS_SQL" ;;
  status)
    SQL="$STATUS_SQL" ;;
  *)
    usage ;;
esac

printf '%s\n' "$SQL" | gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --command \
  "cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U postgres -d $DB"
