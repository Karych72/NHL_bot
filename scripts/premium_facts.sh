#!/usr/bin/env bash
# Факты к решению Задачи 126 «Премиум без прогноза: чем наполнить» с продовой базы (postgres):
# платящие и пробники, цена вопроса к ИИ, сколько чатов упирается в лимит. Запускается с
# ноутбука через `gcloud compute ssh`, как scripts/bot_users_report.sh. Только чтение: сессия
# psql открыта с default_transaction_read_only=on — любой INSERT/UPDATE/DELETE/DDL база отклонит.
#
#   bash scripts/premium_facts.sh   →  вывод на экран и в $TMPDIR/premium_facts_<дата>.log
set -euo pipefail

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
LOG="${TMPDIR:-/tmp}/premium_facts_$(date +%Y%m%d_%H%M).log"

SQL=$(cat <<'EOF'
\pset pager off
\echo '--- последняя миграция'
SELECT max(version) AS last_migration FROM schema_migrations;

\echo '--- платежи Stars: всего, невозвращённых, чатов, действующих подписок'
SELECT count(*) AS payments,
       count(*) FILTER (WHERE refunded_at IS NULL) AS not_refunded,
       count(DISTINCT chat_id) AS chats,
       count(DISTINCT chat_id) FILTER (WHERE paid_until > now() AND refunded_at IS NULL) AS active_paid
FROM premium_payments;

\echo '--- premium_access: строк, действующих сейчас, выдано за 30 дней'
SELECT count(*) AS rows,
       count(*) FILTER (WHERE expires_at > now()) AS active_now,
       count(*) FILTER (WHERE granted_at > now() - interval '30 days') AS granted_30d
FROM premium_access;

\echo '--- цена вопроса, $: по журналам ИИ за 30 дней (ai_questions — маршрутизатор, ai_stats_log — ответы по БД)'
SELECT 'ai_questions' AS log, count(*) AS n, round(avg(cost_usd), 4) AS avg_usd,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY cost_usd)::numeric, 4) AS p90_usd,
       round(max(cost_usd), 4) AS max_usd, round(sum(cost_usd), 2) AS sum_usd
FROM ai_questions WHERE asked_at > now() - interval '30 days' AND cost_usd IS NOT NULL
UNION ALL
SELECT 'ai_stats_log', count(*), round(avg(cost_usd), 4),
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY cost_usd)::numeric, 4),
       round(max(cost_usd), 4), round(sum(cost_usd), 2)
FROM ai_stats_log WHERE asked_at > now() - interval '30 days';

\echo '--- расход на ИИ по чатам за 30 дней, $ (десять самых дорогих)'
SELECT chat_id, count(*) AS questions, round(sum(cost_usd), 2) AS sum_usd
FROM (SELECT chat_id, cost_usd FROM ai_questions WHERE asked_at > now() - interval '30 days'
      UNION ALL
      SELECT chat_id, cost_usd FROM ai_stats_log WHERE asked_at > now() - interval '30 days') q
GROUP BY chat_id ORDER BY sum_usd DESC NULLS LAST LIMIT 10;

\echo '--- отказы по лимиту ИИ за 30 дней: вид лимита, отказов, чатов'
SELECT data AS limit_kind, count(*) AS refusals, count(DISTINCT chat_id) AS chats
FROM bot_actions WHERE kind = 'ai_limit' AND at > now() - interval '30 days'
GROUP BY data ORDER BY data;
EOF
)

printf '%s\n' "$SQL" | gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --command \
  "cd /opt/nhl_bot && sudo -u deploy docker compose exec -T \
     -e PGOPTIONS='-c default_transaction_read_only=on' \
     db psql -X -U postgres -d postgres" 2>&1 | tee "$LOG"

echo "Лог: $LOG"
