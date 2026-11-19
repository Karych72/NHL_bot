#!/bin/sh
# Вратари голов в all_goals.goalie_id из кэша play-by-play (Задача 160). Запускать с ноутбука
# из корня чекаута, где лежит all_data/raw, когда код Задачи 160 выкатился на стенд (мерж в
# develop → dev-стек; мерж релиза в master → прод) и миграция 0032 применена:
#
#   sh scripts/backfill_goal_goalie.sh dev     # nhl_dev
#   sh scripts/backfill_goal_goalie.sh prod    # postgres
#
# Шаги: миграция применена → TSV (game_id, event_id, goalie_id) собирается из кэша на ноутбуке
# и по SSH уходит в psql на VM (временная таблица, один UPDATE в транзакции) → сверки: NULL
# только у голов в пустые ворота, goalie_id совпадает с shot_attempts.goalie_in_net_id, доля
# NULL по сезонам. Сеть и NHL API не нужны. Повторный запуск безопасен. Откат — миграция 0032 вниз.
set -eu

ENVNAME=${1:-}
case $ENVNAME in
  dev) DB=nhl_dev ;;
  prod) DB=postgres ;;
  *) echo "Использование: sh scripts/backfill_goal_goalie.sh dev|prod" >&2; exit 1 ;;
esac
VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
RAW=${RAW:-all_data/raw}
PY=${PY:-.venv/bin/python}
TSV=${TMPDIR:-/tmp}/nhl_goal_goalie.tsv

ssh_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" "$@"; }
# psql в контейнере db; stdin пробрасывается (-T), SQL — из stdin.
PSQL="cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db psql -U postgres -d $DB -v ON_ERROR_STOP=1"

echo "== TSV из кэша $RAW"
"$PY" scripts/backfill_goal_goalie.py --raw "$RAW" > "$TSV"
LINES=$(wc -l < "$TSV" | tr -d ' ')
echo "голов с вратарём в кэше: $LINES"
[ "$LINES" -gt 0 ] || { echo "Пустой TSV" >&2; exit 1; }

echo "== Миграция 0032 на $DB"
HAS=$(ssh_vm --command "$PSQL -At -c \"SELECT count(*) FROM information_schema.columns WHERE table_name = 'all_goals' AND column_name = 'goalie_id'\"" </dev/null)
[ "$HAS" = 1 ] || { echo "На $DB нет all_goals.goalie_id — код ещё не выкатился" >&2; exit 1; }

echo "== Заливка в $DB"
{
  printf 'BEGIN;\nCREATE TEMP TABLE goal_goalie (game_id bigint, event_id int, goalie_id bigint);\n'
  printf 'COPY goal_goalie FROM STDIN;\n'
  cat "$TSV"
  printf '\\.\n'
  printf 'UPDATE all_goals g SET goalie_id = t.goalie_id FROM goal_goalie t\n'
  printf '  WHERE g.game_id = t.game_id AND g.event_id = t.event_id;\nCOMMIT;\n'
} | ssh_vm --command "$PSQL"

echo "== Сверки"
RES=$(ssh_vm --command "$PSQL -At -F ' '" <<'SQL'
SELECT
  count(*) FILTER (WHERE g.goalie_id IS NULL AND NOT g.empty_net),
  count(*) FILTER (WHERE s.goalie_in_net_id IS NOT NULL AND g.goalie_id IS DISTINCT FROM s.goalie_in_net_id),
  count(*) FILTER (WHERE s.goalie_in_net_id IS NOT NULL)
FROM all_goals g JOIN games m USING (game_id)
LEFT JOIN shot_attempts s ON s.game_id = g.game_id AND s.event_id = g.event_id AND s.event_type = 'goal'
WHERE m.season_id >= 20092010;
SQL
)
echo "NULL без empty_net / расхождений с shot_attempts / сверено с shot_attempts: $RES"
echo "== Доля NULL по сезонам, % (ждём 3–6)"
ssh_vm --command "$PSQL -At -F ' '" <<'SQL'
SELECT m.season_id, round(100.0 * count(*) FILTER (WHERE g.goalie_id IS NULL) / count(*), 1)
FROM all_goals g JOIN games m USING (game_id) WHERE m.season_id >= 20092010
GROUP BY 1 ORDER BY 1;
SQL
BAD=$(echo "$RES" | awk '{ print ($1 > 0 || $2 > 0) ? 1 : 0 }')
[ "$BAD" = 0 ] || { echo "СБОЙ: сверки не сошлись" >&2; exit 1; }
echo "ГОТОВО: сверки сошлись."
