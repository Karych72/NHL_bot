#!/bin/sh
# Итоги сезонов 1917/18–2025/26 в базу, режим «только сезонные отчёты» (Задача 158).
# Запускать с ноутбука, когда код задачи уже выкатился на стенд (мерж в develop → dev-стек;
# мерж релиза в master → прод) и миграция 0031 применена:
#
#   sh scripts/history_season_totals.sh dev     # nhl_dev, /opt/nhl_bot_dev
#   sh scripts/history_season_totals.sh prod    # postgres, /opt/nhl_bot
#
# Шаги на VM (в фоне, setsid nohup — обрыв SSH загрузку не прерывает): проверка кода и миграции →
# правило «у большинства» не обнуляет сезоны 2009/10+ (запрос до загрузки) → EXPLAIN rosters до →
# на время загрузки останавливается ночной sync → по сезону `load_season_modern.py --reports-only`
# (регулярка и плей-офф; 2004/05 пропущен — не игрался) → ANALYZE → сверки: голы игроков и вратарей
# против счетов матчей (12 известных расхождений — список KNOWN ниже), Гретцки, franchise_id, ничьи,
# NULL вместо нулей, EXPLAIN rosters после. Матчи в базу не пишутся. Повторный запуск безопасен:
# лоадер идемпотентен. Откат — миграция 0031 вниз либо пересборка nhl_dev из дампа прода.
set -eu

ENVNAME=${1:-}
case $ENVNAME in
  dev) DIR=/opt/nhl_bot_dev; DB=nhl_dev ;;
  prod) DIR=/opt/nhl_bot; DB=postgres ;;
  *) echo "Использование: sh scripts/history_season_totals.sh dev|prod" >&2; exit 1 ;;
esac
VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod

ssh_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" "$@"; }

echo "== Скрипт загрузки → VM ($ENVNAME)"
ssh_vm --command 'cat > /tmp/nhl_history_totals.sh' <<'REMOTE'
# Запускается отвязанным от SSH (setsid nohup); DIR, DB и LOADER_LOG — из окружения.
set -eu
cd "$DIR"
dc() { sudo -u deploy docker compose "$@" </dev/null; }
psql_db() {
  (cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db \
    psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -At -F ' ' "$@" </dev/null)
}
# 1917/18–2008/09 — сама задача; 2009/10–2025/26 — дописать franchise_id и ties уже загруженным
# сезонам (текущий делает ночной sync). 2004/05 не игрался.
SEASONS=$(awk 'BEGIN { for (y = 1917; y <= 2025; y++) if (y != 2004) printf "%d%d ", y, y + 1 }')
SYNC_STOPPED=0
trap 'exit 1' TERM
trap 'rc=$?; if [ "$SYNC_STOPPED" = 1 ]; then echo "== sync снова запущен"; dc up -d sync; fi
  if [ "$rc" = 0 ]; then echo "ГОТОВО: все сверки сошлись."; else echo "СБОЙ (код $rc) — см. выше"; fi' EXIT

echo "== Проверка: образ sync с кодом Задачи 158, миграция 0031 применена"
dc run --rm --no-deps -T sync grep -q reports-only /app/pipeline/load_season_modern.py \
  || { echo "Образ sync без Задачи 158 — код ещё не выкатился" >&2; exit 1; }
[ "$(psql_db -c "SELECT count(*) FROM information_schema.columns WHERE table_name = 'teams'
  AND column_name = 'franchise_id'")" = 1 ] || { echo "Миграции 0031 нет в $DB" >&2; exit 1; }

echo "== До загрузки: сезоны 2009/10+, где хиты, блоки или время на льду пусты у большинства (ждали пусто)"
EMPTY=$(psql_db -c "SELECT season_id, game_type, count(*) FROM players_season_stats
  WHERE season_id BETWEEN 20092010 AND 20252026 GROUP BY 1, 2
  HAVING 2 * count(*) FILTER (WHERE coalesce(hits, 0) = 0) > count(*)
      OR 2 * count(*) FILTER (WHERE coalesce(blocked, 0) = 0) > count(*)
      OR 2 * count(*) FILTER (WHERE time_on_ice IS NULL) > count(*)")
[ -z "$EMPTY" ] || { echo "$EMPTY"; echo "!! правило обнулило бы эти сезоны" >&2; exit 1; }

echo "== EXPLAIN rosters до загрузки"
psql_db -c "EXPLAIN (ANALYZE, COSTS OFF) SELECT name FROM rosters
  WHERE season_id = 20252026 AND lastname = 'Ovechkin'"

echo "== sync остановлен на время загрузки"
dc stop sync
SYNC_STOPPED=1

for sid in $SEASONS; do
  echo "== Сезон $sid $(date -u +%H:%M)"
  if ! dc run --rm --no-deps -T -w /app/pipeline sync python -u load_season_modern.py \
      --season-id "$sid" --reports-only >>"$LOADER_LOG" 2>&1; then
    tail -40 "$LOADER_LOG"; exit 1
  fi
  grep 'Loaded:' "$LOADER_LOG" | tail -1
done

echo "== ANALYZE"
psql_db -c "ANALYZE"
FAIL=0

echo "== Сверка голов (игроки + вратари против счетов): сезон тип игроки счета разница"
# Известные расхождения (сезон:тип:игроки−счета), сверено с API 2026-11-19: в плей-офф 1917/18–1925/26
# в счета входят финалы Кубка Стэнли с клубами PCHA/WCHL, чьих игроков в отчётах нет (в 1919/20
# отчётов игроков нет совсем); 1987/88 и 2019/20 — прерванный и квалификационный матчи.
# Остальное — провал.
KNOWN="19171918:2:-2 19171918:3:-39 19181919:3:-29 19191920:3:-26 19201921:3:-24 19211922:3:-25
19221923:3:-21 19231924:3:-18 19241925:3:-24 19251926:3:-13 19871988:3:6 20192020:3:-2"
GAPS=$(grep 'Goals check' "$LOADER_LOG" | grep -v 'diff=0$' || true)
if [ -n "$GAPS" ]; then
  echo "$GAPS"
  FOUND=$(echo "$GAPS" | sed 's/.*season=\([0-9]*\) game_type=\([0-9]\).* diff=\(.*\)$/\1:\2:\3/')
  UNEXPECTED=$(for g in $FOUND; do case " $(echo $KNOWN) " in (*" $g "*) ;; (*) echo "$g" ;; esac; done)
  [ -z "$UNEXPECTED" ] || { echo "!! неожиданные расхождения:"; echo "$UNEXPECTED"; FAIL=1; }
else
  echo "расхождений нет"
fi
[ "$(grep -c 'Goals check' "$LOADER_LOG")" = "$(( $(echo $SEASONS | wc -w) * 2 ))" ] \
  || { echo "!! сверок голов меньше, чем сезонов × 2"; FAIL=1; }

echo "== Гретцки, регулярка: игры голы передачи очки (ждали 1487 894 1963 2857)"
GRETZKY=$(psql_db -c "SELECT sum(games), sum(goals), sum(assists), sum(points)
  FROM players_season_stats WHERE player_id = 8447400 AND game_type = 2")
echo "$GRETZKY"
[ "$GRETZKY" = "1487 894 1963 2857" ] || { echo "!! Гретцки не сошёлся"; FAIL=1; }

echo "== franchise_id: Квебек и Колорадо — один; нынешняя и историческая Оттава — разные"
psql_db -c "SELECT t.season_id, t.name, t.franchise_id FROM teams t
  WHERE (t.name LIKE 'Quebec Nordiques' AND t.season_id = 19941995)
     OR (t.name LIKE 'Colorado Avalanche' AND t.season_id = 19951996)
     OR (t.name LIKE 'Ottawa Senators%' AND t.season_id IN (19171918, 20252026))
  ORDER BY 1"
[ "$(psql_db -c "SELECT count(DISTINCT franchise_id) FROM teams
  WHERE (name = 'Quebec Nordiques' AND season_id = 19941995)
     OR (name = 'Colorado Avalanche' AND season_id = 19951996)")" = 1 ] \
  || { echo "!! Квебек и Колорадо — не один franchise_id"; FAIL=1; }
[ "$(psql_db -c "SELECT count(DISTINCT franchise_id) FROM teams
  WHERE name LIKE 'Ottawa Senators%' AND season_id IN (19171918, 20252026)")" = 2 ] \
  || { echo "!! две Оттавы — не два franchise_id"; FAIL=1; }
NOFRANCHISE=$(psql_db -c "SELECT count(*) FROM teams WHERE franchise_id IS NULL
  AND season_id < 20262027")
[ "$NOFRANCHISE" = 0 ] || { echo "!! клубов без franchise_id: $NOFRANCHISE"; FAIL=1; }

echo "== Ничьи: клубов, где 2 × победы + ничьи + ot ≠ очки (до 2003/04), и с ties после 2005/06 (ждали 0 0)"
BADTIES=$(psql_db -c "SELECT
  (SELECT count(*) FROM teams_stats WHERE game_type = 2 AND season_id <= 20032004
     AND 2 * wins + ties + coalesce(ot, 0) <> points),
  (SELECT count(*) FROM teams_stats WHERE season_id >= 20052006 AND ties IS NOT NULL)")
echo "$BADTIES"
[ "$BADTIES" = "0 0" ] || { echo "!! ничьи не сошлись"; FAIL=1; }

echo "== NULL вместо нулей: игроки 1980-х с хитами/блоками/временем (ждали 0), первые сезоны с данными"
OLDZERO=$(psql_db -c "SELECT count(*) FROM players_season_stats
  WHERE season_id < 19971998 AND (hits = 0 OR blocked = 0 OR time_on_ice = '00:00')
     OR season_id < 20052006 AND (hits IS NOT NULL OR blocked IS NOT NULL)")
[ "$OLDZERO" = 0 ] || { echo "!! нулей вместо NULL: $OLDZERO"; FAIL=1; }
FIRST=$(psql_db -c "SELECT (SELECT min(season_id) FROM players_season_stats WHERE hits > 0),
  (SELECT min(season_id) FROM players_season_stats WHERE time_on_ice IS NOT NULL)")
echo "первый сезон с хитами и со временем на льду: $FIRST (ждали 20052006 19971998)"
[ "$FIRST" = "20052006 19971998" ] || { echo "!! первые сезоны не сошлись"; FAIL=1; }

echo "== currentage прошлых сезонов (ждали 0 строк с возрастом)"
AGES=$(psql_db -c "SELECT count(*) FROM rosters WHERE season_id < 20262027
  AND currentage IS NOT NULL")
[ "$AGES" = 0 ] || { echo "!! возраст у старых составов: $AGES"; FAIL=1; }

echo "== Объёмы: сезонов, игроков-сезонов, составов"
psql_db -c "SELECT (SELECT count(DISTINCT season_id) FROM players_season_stats),
  (SELECT count(*) FROM players_season_stats), (SELECT count(*) FROM rosters),
  pg_size_pretty(pg_database_size('$DB'))"

echo "== EXPLAIN rosters после загрузки"
psql_db -c "EXPLAIN (ANALYZE, COSTS OFF) SELECT name FROM rosters
  WHERE season_id = 20252026 AND lastname = 'Ovechkin'"
[ "$FAIL" = 0 ] || exit 1
REMOTE

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/nhl_history_totals_$STAMP.log
echo "== Загрузка на VM запущена в фоне, лог — $LOG"
PID=$(ssh_vm --command "DIR=$DIR DB=$DB LOADER_LOG=/tmp/nhl_history_totals_loader_$STAMP.log \
  setsid nohup sh /tmp/nhl_history_totals.sh >$LOG 2>&1 </dev/null & echo \$!")
case $PID in ''|*[!0-9]*) echo "Не разобрал PID загрузки: $PID" >&2; exit 1 ;; esac
FOLLOW="tail --pid=$PID -n +1 -f $LOG"
echo "Если связь оборвётся, следить дальше:"
echo "  gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command '$FOLLOW'"
ssh_vm --command "$FOLLOW"
ssh_vm --command "tail -1 $LOG" | grep -q '^ГОТОВО'
