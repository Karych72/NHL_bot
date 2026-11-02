#!/usr/bin/env bash
# Отчёт по пользователям обоих ботов (базы postgres и nhl_dev): подписки, вопросы ИИ,
# действия (Задача 99), премиум (Задача 96). Запускается с ноутбука через `gcloud compute ssh`.
# Только чтение: сессия psql открыта с default_transaction_read_only=on — любой
# INSERT/UPDATE/DELETE/DDL база отклонит. Инструкция — DEVELOPMENT.md, §«Премиум».
#
#   bash scripts/bot_users_report.sh   →  вывод на экран и в $TMPDIR/bot_users_<дата>.log
set -euo pipefail

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
LOG="${TMPDIR:-/tmp}/bot_users_$(date +%Y%m%d_%H%M).log"

SQL=$(cat <<'EOF'
\pset pager off
\echo '--- последняя миграция'
SELECT max(version) AS last_migration FROM schema_migrations;

\echo '--- пользователи (chat_id > 0 — личка, < 0 — группа); вопросы ИИ: всего / неделя МСК / сегодня МСК'
WITH chats AS (
    SELECT chat_id FROM bot_subscriptions
    UNION SELECT chat_id FROM ai_questions
    UNION SELECT chat_id FROM ai_stats_log
), q AS (
    SELECT chat_id, asked_at FROM ai_questions WHERE outcome <> 'error'
    UNION ALL
    SELECT chat_id, asked_at FROM ai_stats_log WHERE outcome <> 'error'
)
SELECT c.chat_id,
       CASE WHEN c.chat_id > 0 THEN 'личка' ELSE 'группа' END AS type,
       (SELECT string_agg(DISTINCT s.kind, ', ') FROM bot_subscriptions s
         WHERE s.chat_id = c.chat_id AND s.active) AS active_subscriptions,
       (SELECT min(s.created_at)::date FROM bot_subscriptions s WHERE s.chat_id = c.chat_id) AS first_sub,
       (SELECT count(*) FROM q WHERE q.chat_id = c.chat_id) AS ai_total,
       (SELECT count(*) FROM q WHERE q.chat_id = c.chat_id
          AND q.asked_at >= date_trunc('week', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow') AS ai_week,
       (SELECT count(*) FROM q WHERE q.chat_id = c.chat_id
          AND q.asked_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow') AS ai_today,
       (SELECT max(q.asked_at) FROM q WHERE q.chat_id = c.chat_id)::timestamp(0) AS ai_last
FROM chats c
ORDER BY ai_last DESC NULLS LAST, c.chat_id;

\echo '--- подписки по видам (активные / всего)'
SELECT kind, count(*) FILTER (WHERE active) AS active, count(*) AS total
FROM bot_subscriptions GROUP BY kind ORDER BY kind;

\echo '--- действия за 30 дней: команды, кнопки, отказы ИИ по лимиту, блокировка бота (таблицы нет до миграции 0017 — тогда будет ошибка)'
SELECT chat_id,
       count(*) FILTER (WHERE kind = 'command') AS commands,
       count(*) FILTER (WHERE kind = 'callback') AS buttons,
       count(*) FILTER (WHERE kind = 'ai_limit') AS ai_limit_refusals,
       bool_or(kind = 'blocked') AS blocked,
       count(DISTINCT user_id) AS users,
       max(at)::timestamp(0) AS last_action
FROM bot_actions WHERE at > now() - interval '30 days'
GROUP BY chat_id ORDER BY last_action DESC;

\echo '--- премиум (таблицы нет до миграции 0016 — тогда будет ошибка, это нормально)'
SELECT chat_id, expires_at, granted_at, expires_at > now() AS active FROM premium_access ORDER BY expires_at DESC;

\echo '--- старый повышенный лимит ИИ (есть только до миграции 0016)'
SELECT chat_id FROM ai_existing_users ORDER BY chat_id;
EOF
)

{
  for DB in postgres nhl_dev; do
    echo "================ база $DB ($([ "$DB" = postgres ] && echo 'прод @IcePulseNHLBot' || echo 'dev @auto_karych_test_bot'))"
    printf '%s\n' "$SQL" | gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --command \
      "cd /opt/nhl_bot && sudo -u deploy docker compose exec -T \
         -e PGOPTIONS='-c default_transaction_read_only=on' \
         db psql -X -U postgres -d $DB" 2>&1 || echo "!! ошибка выполнения для $DB"
    echo
  done
} | tee "$LOG"

echo "Лог: $LOG"
