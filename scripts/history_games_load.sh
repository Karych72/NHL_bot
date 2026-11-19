#!/bin/sh
# Матчи 1917/18–2008/09 в базу (Задача 157). Запускать с ноутбука из корня чекаута, где лежит сырой
# слой all_data/raw (Задача 155), когда код задачи уже выкатился на стенд (мерж в develop → dev-стек;
# мерж релиза в master → прод), миграция 0033 применена, а итоги сезонов 1917/18–2008/09 загружены
# (scripts/history_season_totals.sh, Задача 158 — на них ссылаются матчи):
#
#   sh scripts/history_games_load.sh dev     # nhl_dev, /opt/nhl_bot_dev
#   sh scripts/history_games_load.sh prod    # postgres, /opt/nhl_bot
#
# Шаги: сверка кэша на ноутбуке с all_data/raw/history_collect_summary.tsv → место, память, дампы
# и размер базы на VM → архив кэша на VM (в том syncdata sync-сервиса) → в фоне (setsid nohup):
# проверка кода и миграции, EXPLAIN запросов текущего сезона до, на время загрузки остановлен
# ночной sync, по сезону от новых к старым `load_season_modern.py` (2004/05 не игрался), ANALYZE,
# сверки (матчи по сезонам, голы, ничьи, NULL, бросков до 2009/10 нет, обладатели Кубка),
# EXPLAIN после. Ноутбук только показывает лог: обрыв SSH или сон ноутбука загрузку не прерывают.
# Повторный запуск безопасен: лоадер идемпотентен. Откат — пересборка nhl_dev из дампа прода
# (DEVELOPMENT.md, «Dev-стенд»); на проде — восстановление из последнего дампа.
set -eu

ENVNAME=${1:-}
case $ENVNAME in
  dev) DIR=/opt/nhl_bot_dev; DB=nhl_dev ;;
  prod) DIR=/opt/nhl_bot; DB=postgres ;;
  *) echo "Использование: sh scripts/history_games_load.sh dev|prod" >&2; exit 1 ;;
esac
VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
RAW=${RAW:-all_data/raw}
SUMMARY=$RAW/history_collect_summary.tsv
TAR=${TMPDIR:-/tmp}/nhl_history_games_raw.tar

echo "== Кэш на ноутбуке: $RAW"
# Сезоны 1917/18–2008/09 и число матчей — из итога сбора Задачи 155 (колонка 2).
EXPECTED=$(awk -F'\t' '$1 < 20092010 && $2 > 0 { printf "%s:%s ", $1, $2 }' "$SUMMARY")
DIRS=""
for spec in $EXPECTED; do
  sid=${spec%:*}; want=${spec#*:}
  for ep in pbp box landing; do
    n=$(ls "$RAW/$sid" 2>/dev/null | grep -c "\.$ep\.json\.gz\$" || true)
    # 1917/18: у двух матчей boxscore в кэш не лёг (payload_complete) — лоадер их пропускает.
    [ "$n" -ge $((want - 2)) ] || { echo "$sid: $ep-файлов $n, ждали $want" >&2; exit 1; }
  done
  DIRS="$DIRS $sid"
done
echo "сезонов в кэше: $(echo $DIRS | wc -w)"

ssh_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" "$@"; }

echo "== Место, память, дампы и база на VM (места нужно ≥ 4 ГБ: архив, том, рост базы)"
ssh_vm --command "DB=$DB sh -s" <<'REMOTE'
set -eu
df -h /
free -m
echo "дампы prod (число, последний):"; sudo ls -1 /srv/nhl_bot_backups | wc -l; sudo ls -lt /srv/nhl_bot_backups | sed -n 2p
cd /opt/nhl_bot
echo "размер $DB сейчас: $(sudo -u deploy docker compose exec -T db psql -U postgres -At \
  -c "SELECT pg_size_pretty(pg_database_size('$DB'))" </dev/null)"
[ "$(df --output=avail -BG / | tail -1 | tr -dc 0-9)" -ge 4 ] || { echo "Мало места" >&2; exit 1; }
! pgrep -f nhl_history_games.sh >/dev/null || { echo "Прошлая загрузка ещё идёт" >&2; exit 1; }
REMOTE

echo "== Архив кэша → VM"
tar -cf "$TAR" -C "$RAW" $DIRS
ls -lh "$TAR"
gcloud compute scp "$TAR" "$VM:/tmp/nhl_history_games_raw.tar" --zone "$ZONE" --project "$PROJECT"
rm -f "$TAR"

echo "== Скрипт загрузки → VM ($ENVNAME)"
ssh_vm --command 'cat > /tmp/nhl_history_games.sh' <<'REMOTE'
# Запускается отвязанным от SSH (setsid nohup); DIR, DB, EXPECTED и LOADER_LOG — из окружения.
set -eu
cd "$DIR"
dc() { sudo -u deploy docker compose "$@" </dev/null; }
psql_db() {
  (cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db \
    psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -At -F ' ' "$@" </dev/null)
}
# От новых к старым; 2004/05 не игрался.
SEASONS=$(awk 'BEGIN { for (y = 2008; y >= 1917; y--) if (y != 2004) printf "%d%d ", y, y + 1 }')
SYNC_STOPPED=0
trap 'exit 1' TERM
trap 'rc=$?; if [ "$SYNC_STOPPED" = 1 ]; then echo "== sync снова запущен"; dc up -d sync; fi
  if [ "$rc" = 0 ]; then echo "ГОТОВО: все сверки сошлись."; else echo "СБОЙ (код $rc) — см. выше"; fi' EXIT

echo "== Проверка: образ sync с кодом Задачи 157, миграция 0033 применена"
dc run --rm --no-deps -T sync grep -q FIRST_SHOT_ATTEMPT_SEASON /app/pipeline/load_season_modern.py \
  || { echo "Образ sync без Задачи 157 — код ещё не выкатился" >&2; exit 1; }
[ "$(psql_db -c "SELECT count(*) FROM schema_migrations WHERE version = '0033'")" = 1 ] \
  || { echo "Миграции 0033 нет в $DB" >&2; exit 1; }
[ "$(psql_db -c "SELECT count(*) FROM teams WHERE season_id = 19171918")" -ge 3 ] \
  || { echo "Итогов сезонов Задачи 158 в $DB нет — сначала history_season_totals.sh" >&2; exit 1; }

# Запросы экранов текущего сезона: время до и после, полных проходов games и матчевых таблиц быть не должно.
explain() {
  psql_db -c "EXPLAIN (ANALYZE, COSTS OFF) SELECT a.game_id FROM all_goals a
    JOIN games g ON g.game_id = a.game_id WHERE g.season_id = 20252026 AND g.day = '2026-03-18'"
  psql_db -c "EXPLAIN (ANALYZE, COSTS OFF) SELECT game_id, day FROM games
    WHERE season_id = 20252026 AND (home_team_id = 10 OR away_team_id = 10) ORDER BY day DESC LIMIT 10"
  psql_db -c "EXPLAIN (ANALYZE, COSTS OFF) SELECT sum(goals) FROM game_player_stats
    WHERE player_id = 8478402 AND game_id IN (SELECT game_id FROM games WHERE season_id = 20252026)"
}
echo "== EXPLAIN ANALYZE до загрузки"
explain

echo "== Кэш → том syncdata ($ENVNAME)"
# Без dc(): она перенаправляет stdin в /dev/null, а архив идёт именно через stdin.
sudo -u deploy docker compose run --rm --no-deps -T sync \
  sh -c 'mkdir -p /app/all_data/raw && tar -xf - -C /app/all_data/raw' </tmp/nhl_history_games_raw.tar
rm -f /tmp/nhl_history_games_raw.tar

echo "== Ночной sync остановлен на время загрузки"
dc stop sync
SYNC_STOPPED=1

for sid in $SEASONS; do
  y=$(echo "$sid" | cut -c1-4)
  echo "== Сезон $sid $(date -u +%H:%M)"
  if ! dc run --rm --no-deps -T -w /app/pipeline sync python -u load_season_modern.py \
      --season-id "$sid" --date-from "$y-09-01" --date-to "$((y + 1))-10-31" >>"$LOADER_LOG" 2>&1; then
    tail -40 "$LOADER_LOG"; exit 1
  fi
  grep 'Loaded:' "$LOADER_LOG" | tail -1
  df -h / | tail -1
done

echo "== ANALYZE"
psql_db -c "ANALYZE"
FAIL=0

echo "== Матчи по сезонам: сезон матчей (ждали; 1917/18 — минус 2 без boxscore)"
for spec in $EXPECTED; do
  sid=${spec%:*}; want=${spec#*:}
  got=$(psql_db -c "SELECT count(*) FROM games WHERE season_id = $sid")
  [ "$sid" = 19171918 ] && want=$((want - 2))
  [ "$got" = "$want" ] || { echo "!! $sid: матчей $got, ждали $want"; FAIL=1; }
done
echo "сверено сезонов: $(echo $EXPECTED | wc -w)"

echo "== Голы в all_goals против счетов матчей до 2005/06 (ждали пусто)"
# Голы игроков (game_player_stats) со счётом не сверяются: у вратарей в boxscore нет поля голов,
# а гол вратаря (8 матчей 1979–2003) в строках игроков не виден.
GOALS=$(psql_db -c "SELECT g.season_id, sum(t.s), sum(a.c) FROM games g
  JOIN (SELECT game_id, sum(goals) s FROM game_team_stats GROUP BY 1) t USING (game_id)
  JOIN (SELECT game_id, count(*) c FROM all_goals GROUP BY 1) a USING (game_id)
  WHERE g.season_id < 20052006 GROUP BY 1 HAVING sum(t.s) <> sum(a.c) ORDER BY 1")
[ -z "$GOALS" ] || { echo "$GOALS"; FAIL=1; }

echo "== Ничьи: матчей с ничейным счётом и winner_id, не-ничьих без winner_id (ждали 0 0)"
TIES=$(psql_db -c "SELECT
  count(*) FILTER (WHERE g.winner_id IS NOT NULL AND t.h = t.a),
  count(*) FILTER (WHERE g.winner_id IS NULL AND t.h <> t.a)
  FROM games g JOIN (SELECT game_id,
    max(goals) FILTER (WHERE field = 'home') h, max(goals) FILTER (WHERE field = 'away') a
    FROM game_team_stats GROUP BY 1) t USING (game_id) WHERE g.season_id < 20092010")
echo "$TIES"
[ "$TIES" = "0 0" ] || { echo "!! ничьи не сошлись"; FAIL=1; }

echo "== Бросков в shot_attempts до 2009/10 (ждали 0)"
SHOTS=$(psql_db -c "SELECT count(*) FROM shot_attempts s JOIN games g USING (game_id)
  WHERE g.season_id < 20092010")
[ "$SHOTS" = 0 ] || { echo "!! броски до 2009/10: $SHOTS"; FAIL=1; }

echo "== NULL вместо нулей: матчи 1980-х, время на льду / хиты / блоки (ждали 0 не-NULL)"
EIGHTIES=$(psql_db -c "SELECT count(*) FROM game_player_stats p JOIN games g USING (game_id)
  WHERE g.season_id BETWEEN 19801981 AND 19891990
    AND (p.time_on_ice IS NOT NULL OR p.hits IS NOT NULL OR p.blocked IS NOT NULL
      OR p.takeaways IS NOT NULL OR p.giveaways IS NOT NULL)")
[ "$EIGHTIES" = 0 ] || { echo "!! значений вместо NULL в 1980-х: $EIGHTIES"; FAIL=1; }

echo "== Обладатели Кубка: сезон, победитель серии самого большого раунда (1918/19 — NULL, финал не доигран)"
CUPS=$(psql_db -c "SELECT s.season_id, coalesce(t.name, 'NULL') FROM playoff_series s
  LEFT JOIN teams t ON t.team_id = s.winner_id AND t.season_id = s.season_id
  WHERE s.round = (SELECT max(round) FROM playoff_series m WHERE m.season_id = s.season_id)
    AND s.season_id < 20092010 ORDER BY 1")
echo "$CUPS"
NOCUP=$(echo "$CUPS" | grep -c ' NULL$' || true)
[ "$NOCUP" -le 1 ] || { echo "!! сезонов без обладателя Кубка: $NOCUP (допустим один — 1918/19)"; FAIL=1; }

echo "== Объёмы и место после"
psql_db -c "SELECT (SELECT count(*) FROM games), (SELECT count(*) FROM game_player_stats),
  (SELECT count(*) FROM all_goals), pg_size_pretty(pg_database_size('$DB'))"
df -h /

echo "== EXPLAIN ANALYZE после загрузки"
explain
[ "$FAIL" = 0 ] || exit 1
REMOTE

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/nhl_history_games_$STAMP.log
echo "== Загрузка на VM запущена в фоне, лог — $LOG"
PID=$(ssh_vm --command "DIR=$DIR DB=$DB EXPECTED='$EXPECTED' LOADER_LOG=/tmp/nhl_history_games_loader_$STAMP.log \
  setsid nohup sh /tmp/nhl_history_games.sh >$LOG 2>&1 </dev/null & echo \$!")
case $PID in ''|*[!0-9]*) echo "Не разобрал PID загрузки: $PID" >&2; exit 1 ;; esac
FOLLOW="tail --pid=$PID -n +1 -f $LOG"
echo "Если связь оборвётся, следить дальше:"
echo "  gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command '$FOLLOW'"
ssh_vm --command "$FOLLOW"
ssh_vm --command "tail -1 $LOG" | grep -q '^ГОТОВО'
