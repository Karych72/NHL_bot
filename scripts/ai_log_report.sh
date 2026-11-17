#!/usr/bin/env bash
# Профиль расхода и отказов ИИ по журналам обоих ботов (базы postgres и nhl_dev) за N дней
# (Задача 142): маршрутизатор (`ai_questions`), ответы по статистике (`ai_stats_log`, его `calls`
# и `steps`), расход по дням, тексты вопросов. Запускается с ноутбука через `gcloud compute ssh`,
# как scripts/bot_users_report.sh. Только чтение: сессия psql с default_transaction_read_only=on.
# chat_id не выводится, но тексты вопросов — да: лог в репозиторий не класть.
#
#   bash scripts/ai_log_report.sh [дней=60]  →  вывод на экран и в $TMPDIR/ai_log_<дата>.log
set -euo pipefail

DAYS="${1:-60}"
case "$DAYS" in ''|*[!0-9]*) echo "дней — целое число, получено: $DAYS" >&2; exit 2 ;; esac

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
LOG="${TMPDIR:-/tmp}/ai_log_$(date +%Y%m%d_%H%M).log"

# :days — переменная psql (-v days=N). Стоимость в журналах посчитана по ai_router._PRICE_*.
SQL=$(cat <<'EOF'
\pset pager off
\pset null '-'
\echo '--- 0. объём журналов за период: строк, первая и последняя запись'
SELECT 'ai_questions' AS journal, count(*) AS rows, min(asked_at)::timestamp(0) AS first,
       max(asked_at)::timestamp(0) AS last
FROM ai_questions WHERE asked_at > now() - :days * interval '1 day'
UNION ALL
SELECT 'ai_stats_log', count(*), min(asked_at)::timestamp(0), max(asked_at)::timestamp(0)
FROM ai_stats_log WHERE asked_at > now() - :days * interval '1 day';

\echo '--- 1. маршрутизатор по исходам (screen — экран, в т.ч. stats_question; text — отказ или уточнение, error — сбой API); стоимость вызова, $'
SELECT outcome, count(*) AS n, round(avg(cost_usd), 5) AS avg_usd, max(cost_usd) AS max_usd,
       round(sum(cost_usd), 4) AS sum_usd, round(avg(latency_ms)) AS avg_ms
FROM ai_questions WHERE asked_at > now() - :days * interval '1 day'
GROUP BY ROLLUP (outcome) ORDER BY outcome NULLS LAST;

\echo '--- 2. маршрутизатор по экранам (open_command — с командой); stats_question уходит в ai_stats_log'
SELECT tool || coalesce(' ' || (tool_input ->> 'command'), '') AS screen, count(*) AS n
FROM ai_questions WHERE asked_at > now() - :days * interval '1 day' AND outcome = 'screen'
GROUP BY 1 ORDER BY n DESC, screen;

\echo '--- 3. маршрутизатор: кэш промпта — вызовы с чтением кэша / только с записью / без кэша; токены (в $ — по ценам ai_router._PRICE_*)'
SELECT count(*) FILTER (WHERE cache_read_tokens > 0) AS calls_read,
       count(*) FILTER (WHERE cache_read_tokens = 0 AND cache_write_tokens > 0) AS calls_write_only,
       count(*) FILTER (WHERE cache_read_tokens = 0 AND cache_write_tokens = 0) AS calls_no_cache,
       sum(input_tokens) AS input_tok, sum(output_tokens) AS output_tok,
       sum(cache_read_tokens) AS cache_read_tok, sum(cache_write_tokens) AS cache_write_tok,
       round(sum(cost_usd), 4) AS sum_usd
FROM ai_questions WHERE asked_at > now() - :days * interval '1 day' AND outcome <> 'error';

\echo '--- 4. ответы по статистике по исходам (answer, clarify, no_data, off_topic, ceiling, rejected, error); стоимость вопроса без маршрутизатора, $'
SELECT outcome, count(*) AS n, count(*) FILTER (WHERE continuation) AS continuation,
       round(avg(cost_usd), 5) AS avg_usd, max(cost_usd) AS max_usd, round(sum(cost_usd), 4) AS sum_usd,
       round(avg(jsonb_array_length(calls)), 1) AS avg_calls, round(avg(latency_ms)) AS avg_ms
FROM ai_stats_log WHERE asked_at > now() - :days * interval '1 day'
GROUP BY ROLLUP (outcome) ORDER BY outcome NULLS LAST;

\echo '--- 5. ответы по статистике по пути: готовая выборка (player_overview), SQL (run_sql), оба, ни одного; вердикты ревьюера'
WITH s AS (
    SELECT l.outcome, l.cost_usd,
           EXISTS (SELECT 1 FROM jsonb_array_elements(l.steps) e WHERE e ->> 'tool' = 'player_overview') AS po,
           EXISTS (SELECT 1 FROM jsonb_array_elements(l.steps) e WHERE e ->> 'tool' = 'run_sql') AS sq,
           (SELECT count(*) FROM jsonb_array_elements(l.steps) e WHERE e ->> 'verdict' = 'ok') AS v_ok,
           (SELECT count(*) FROM jsonb_array_elements(l.steps) e WHERE e ->> 'verdict' = 'fix') AS v_fix,
           (SELECT count(*) FROM jsonb_array_elements(l.steps) e WHERE e ->> 'verdict' = 'clarify') AS v_clarify
    FROM ai_stats_log l WHERE l.asked_at > now() - :days * interval '1 day'
)
SELECT CASE WHEN po AND sq THEN 'выборка+SQL' WHEN po THEN 'выборка' WHEN sq THEN 'SQL' ELSE 'без инструментов' END AS path,
       count(*) AS n,
       count(*) FILTER (WHERE outcome = 'answer') AS answer,
       count(*) FILTER (WHERE outcome NOT IN ('answer', 'error')) AS not_answer,
       count(*) FILTER (WHERE outcome = 'error') AS error,
       sum(v_ok) AS reviews_ok, sum(v_fix) AS reviews_fix, sum(v_clarify) AS reviews_clarify,
       count(*) FILTER (WHERE v_fix > 0) AS q_with_fix,
       round(avg(cost_usd), 5) AS avg_usd, round(sum(cost_usd), 4) AS sum_usd
FROM s GROUP BY 1 ORDER BY n DESC;

\echo '--- 6. ответы по статистике по ролям вызовов (answer — отвечающий, review — ревьюер): токены, кэш, стоимость'
WITH c AS (
    SELECT e ->> 'role' AS role, (e ->> 'input_tokens')::bigint AS inp, (e ->> 'output_tokens')::bigint AS outp,
           (e ->> 'cache_read_tokens')::bigint AS cr, (e ->> 'cache_write_tokens')::bigint AS cw,
           (e ->> 'cost_usd')::numeric AS usd
    FROM ai_stats_log l, jsonb_array_elements(l.calls) e
    WHERE l.asked_at > now() - :days * interval '1 day'
)
SELECT role, count(*) AS calls,
       count(*) FILTER (WHERE cr > 0) AS calls_read,
       count(*) FILTER (WHERE cr = 0 AND cw > 0) AS calls_write_only,
       sum(inp) AS input_tok, sum(outp) AS output_tok, sum(cr) AS cache_read_tok, sum(cw) AS cache_write_tok,
       round(sum(usd), 4) AS sum_usd,
       round(100 * sum(usd) / nullif((SELECT sum(usd) FROM c), 0), 1) AS pct_usd
FROM c GROUP BY ROLLUP (role) ORDER BY role NULLS LAST;

\echo '--- 7. попытки SQL на вопрос: сколько вопросов сделали 0, 1, 2… попыток, сколько из этих попыток упало с ошибкой и сколько вопросов получили ответ'
WITH q AS (
    SELECT l.outcome,
           (SELECT count(*) FROM jsonb_array_elements(l.steps) e WHERE e ->> 'tool' = 'run_sql') AS attempts,
           (SELECT count(*) FROM jsonb_array_elements(l.steps) e
             WHERE e ->> 'tool' = 'run_sql' AND jsonb_typeof(e -> 'error') = 'string') AS failed
    FROM ai_stats_log l WHERE l.asked_at > now() - :days * interval '1 day'
)
SELECT attempts, count(*) AS questions, sum(failed) AS failed_attempts,
       count(*) FILTER (WHERE outcome = 'answer') AS answered
FROM q GROUP BY ROLLUP (attempts) ORDER BY attempts NULLS LAST;

\echo '--- 8. ошибки SQL модели: текст ошибки (первые 120 символов) и сколько раз'
SELECT left(regexp_replace(e ->> 'error', '\s+', ' ', 'g'), 120) AS sql_error, count(*) AS n
FROM ai_stats_log l, jsonb_array_elements(l.steps) e
WHERE l.asked_at > now() - :days * interval '1 day'
  AND e ->> 'tool' = 'run_sql' AND jsonb_typeof(e -> 'error') = 'string'
GROUP BY 1 ORDER BY n DESC LIMIT 30;

\echo '--- 9. расход по дням МСК: вопросы и $ по журналам (error не в счёт вопросов)'
WITH d AS (
    SELECT (asked_at AT TIME ZONE 'Europe/Moscow')::date AS day, 'router' AS j, outcome, cost_usd
    FROM ai_questions WHERE asked_at > now() - :days * interval '1 day'
    UNION ALL
    SELECT (asked_at AT TIME ZONE 'Europe/Moscow')::date, 'stats', outcome, cost_usd
    FROM ai_stats_log WHERE asked_at > now() - :days * interval '1 day'
)
SELECT day,
       count(*) FILTER (WHERE j = 'router' AND outcome <> 'error') AS router_q,
       round(coalesce(sum(cost_usd) FILTER (WHERE j = 'router'), 0), 4) AS router_usd,
       count(*) FILTER (WHERE j = 'stats' AND outcome <> 'error') AS stats_q,
       round(coalesce(sum(cost_usd) FILTER (WHERE j = 'stats'), 0), 4) AS stats_usd,
       round(coalesce(sum(cost_usd), 0), 4) AS total_usd
FROM d GROUP BY ROLLUP (day) ORDER BY day NULLS LAST;

\echo '--- 10. тексты: маршрутизатор ответил текстом (отказ или уточнение) — вопрос и ответ'
SELECT asked_at::timestamp(0) AS at, left(regexp_replace(question, '\s+', ' ', 'g'), 200) AS question,
       left(regexp_replace(answer, '\s+', ' ', 'g'), 200) AS answer
FROM ai_questions WHERE asked_at > now() - :days * interval '1 day' AND outcome = 'text'
ORDER BY asked_at;

\echo '--- 11. тексты: статистика без ответа (clarify, no_data, off_topic, ceiling, rejected, error) — вопрос, ответ, причина ревьюера или ошибка'
SELECT asked_at::timestamp(0) AS at, outcome, left(regexp_replace(question, '\s+', ' ', 'g'), 200) AS question,
       left(regexp_replace(coalesce(answer, ''), '\s+', ' ', 'g'), 200) AS answer,
       left(regexp_replace(coalesce(verdict_reason, error, ''), '\s+', ' ', 'g'), 150) AS reason
FROM ai_stats_log WHERE asked_at > now() - :days * interval '1 day' AND outcome <> 'answer'
ORDER BY outcome, asked_at;

\echo '--- 12. тексты: статистика с ответом — вопрос и путь'
SELECT l.asked_at::timestamp(0) AS at, left(regexp_replace(l.question, '\s+', ' ', 'g'), 200) AS question,
       (SELECT string_agg(DISTINCT e ->> 'tool', ',') FROM jsonb_array_elements(l.steps) e
         WHERE e ->> 'tool' <> 'review') AS tools,
       l.cost_usd
FROM ai_stats_log l WHERE l.asked_at > now() - :days * interval '1 day' AND l.outcome = 'answer'
ORDER BY l.asked_at;

\echo '--- 13. тексты: маршрутизатор выбрал экран (кроме stats_question — они в 11–12) — вопрос и параметры экрана'
SELECT asked_at::timestamp(0) AS at, left(regexp_replace(question, '\s+', ' ', 'g'), 200) AS question,
       tool, tool_input
FROM ai_questions WHERE asked_at > now() - :days * interval '1 day' AND outcome = 'screen'
  AND tool <> 'stats_question'
ORDER BY asked_at;

\echo '--- 14. повторяющиеся вопросы (без учёта регистра и пробелов) — кандидаты на готовые выборки'
WITH q AS (
    SELECT lower(regexp_replace(trim(question), '\s+', ' ', 'g')) AS question, 'router' AS j
    FROM ai_questions WHERE asked_at > now() - :days * interval '1 day' AND outcome <> 'error'
    UNION ALL
    SELECT lower(regexp_replace(trim(question), '\s+', ' ', 'g')), 'stats'
    FROM ai_stats_log WHERE asked_at > now() - :days * interval '1 day' AND outcome <> 'error'
)
SELECT left(question, 200) AS question, count(*) FILTER (WHERE j = 'router') AS router,
       count(*) FILTER (WHERE j = 'stats') AS stats
FROM q GROUP BY question
HAVING count(*) FILTER (WHERE j = 'router') > 1 OR count(*) FILTER (WHERE j = 'stats') > 1
ORDER BY count(*) DESC, question LIMIT 50;
EOF
)

{
  echo "Период: последние $DAYS дн."
  for DB in postgres nhl_dev; do
    echo "================ база $DB ($([ "$DB" = postgres ] && echo 'прод @IcePulseNHLBot' || echo 'dev @auto_karych_test_bot'))"
    printf '%s\n' "$SQL" | gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --command \
      "cd /opt/nhl_bot && sudo -u deploy docker compose exec -T \
         -e PGOPTIONS='-c default_transaction_read_only=on' \
         db psql -X -U postgres -d $DB -v days=$DAYS" 2>&1 || echo "!! ошибка выполнения для $DB"
    echo
  done
} | tee "$LOG"

echo "Лог: $LOG"
