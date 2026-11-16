#!/bin/sh
# Загрузка истории 2009/10–2020/21 в nhl_dev (Задача 121). Запускать с ноутбука из корня
# чекаута, где лежит сырой слой all_data/raw, когда код Задачи 121 уже выкатился на dev-стенд
# (мерж в develop, Release зелёный):
#
#   sh scripts/dev_history_load.sh
#
# Шаги: сверка кэша на ноутбуке → место и память на VM → архив кэша на VM → загрузка на VM
# в фоне (setsid nohup): кэш в том nhl_bot_dev_syncdata, build-dataset до, двенадцать сезонов
# сервисом sync dev-стека (сам sync на время остановлен), build-dataset после, сверки матчей,
# бросков с координатами и справочника команд. Ноутбук только показывает лог: обрыв SSH или
# сон ноутбука загрузку не прерывают, следить дальше — командой, которую скрипт печатает.
# Повторный запуск безопасен: лоадер идемпотентен, кэш перезаписывается теми же файлами.
# Откат — пересборка nhl_dev из дампа прода (DEVELOPMENT.md, «Dev-стенд»).
set -eu

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
RAW=${RAW:-all_data/raw}
TAR=${TMPDIR:-/tmp}/nhl_history_raw.tar
# Сезон:матчей регулярки и плей-офф (карточка Задачи 121, сверено с API при сборе кэша).
EXPECTED="20092010:1319 20102011:1319 20112012:1316 20122013:806 20132014:1323 20142015:1319 20152016:1321 20162017:1317 20172018:1355 20182019:1358 20192020:1212 20202021:952"

echo "== Кэш на ноутбуке: $RAW"
DIRS=""
for spec in $EXPECTED; do
  sid=${spec%:*}; want=${spec#*:}
  for ep in pbp box landing; do
    n=$(ls "$RAW/$sid" 2>/dev/null | grep -c "\.$ep\.json\.gz\$" || true)
    [ "$n" = "$want" ] || { echo "$sid: $ep-файлов $n, ждали $want" >&2; exit 1; }
  done
  DIRS="$DIRS $sid"
done
echo "все 12 сезонов на месте (14 917 матчей × 3 файла)"

ssh_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" "$@"; }

echo "== Место и память на VM (места нужно ≥ 4 ГБ: архив, том, база)"
ssh_vm --command 'sh -s' <<'REMOTE'
set -eu
df -h /
free -m
echo "последний дамп прода:"; sudo ls -lt /srv/nhl_bot_backups | sed -n 2p
cd /opt/nhl_bot
echo "nhl_dev сейчас: $(sudo -u deploy docker compose exec -T db psql -U postgres -At \
  -c "SELECT pg_size_pretty(pg_database_size('nhl_dev'))" </dev/null)"
[ "$(df --output=avail -BG / | tail -1 | tr -dc 0-9)" -ge 4 ] || { echo "Мало места" >&2; exit 1; }
REMOTE

echo "== Архив кэша → VM"
tar -cf "$TAR" -C "$RAW" $DIRS
ls -lh "$TAR"
gcloud compute scp "$TAR" "$VM:/tmp/nhl_history_raw.tar" --zone "$ZONE" --project "$PROJECT"
rm -f "$TAR"

echo "== Скрипт загрузки → VM"
ssh_vm --command 'cat > /tmp/nhl_history_load.sh' <<'REMOTE'
# Запускается отвязанным от SSH (setsid nohup); EXPECTED и LOADER_LOG — из окружения.
set -eu
cd /opt/nhl_bot_dev
dc() { sudo -u deploy docker compose "$@" </dev/null; }
psql_dev() {
  (cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db \
    psql -U postgres -d nhl_dev -v ON_ERROR_STOP=1 -At -F ' ' "$@" </dev/null)
}
dataset_rows() {
  dc run --rm --no-deps -T predict sh -c 'python -m modeling.cli build-dataset --mode train \
      --output-dir /tmp/ds >/dev/null && cd /tmp/ds && wc -l dataset_train.csv games_train.csv shots_train.csv'
}
SYNC_STOPPED=0
# Сигнал идёт через exit, чтобы EXIT-ловушка вернула sync и в этом случае (dash иначе её не зовёт).
trap 'exit 1' HUP INT TERM
trap 'rc=$?; if [ "$SYNC_STOPPED" = 1 ]; then echo "== sync dev-стека снова запущен"; dc up -d sync; fi
  if [ "$rc" = 0 ]; then echo "ГОТОВО: все сверки сошлись."; else echo "СБОЙ (код $rc) — см. выше"; fi' EXIT

echo "== Проверка: dev-стек на коде Задачи 121"
dc run --rm --no-deps -T sync grep -q standings-season /app/pipeline/load_season_modern.py \
  || { echo "Образ sync без Задачи 121 — develop ещё не выкатился" >&2; exit 1; }
dc run --rm --no-deps -T predict grep -q first_season_id /app/modeling/config.py \
  || { echo "Образ predict без Задачи 121" >&2; exit 1; }

echo "== Кэш → том nhl_bot_dev_syncdata"
sudo -u deploy docker compose run --rm --no-deps -T sync \
  sh -c 'mkdir -p /app/all_data/raw && tar -xf - -C /app/all_data/raw' </tmp/nhl_history_raw.tar
rm -f /tmp/nhl_history_raw.tar

echo "== Ночной sync dev-стека остановлен на время загрузки"
dc stop sync
SYNC_STOPPED=1

echo "== build-dataset до загрузки"
BEFORE=$(dataset_rows); echo "$BEFORE"

for spec in $EXPECTED; do
  sid=${spec%:*}; y=$(echo "$sid" | cut -c1-4)
  echo "== Сезон $sid: $y-09-01..$((y + 1))-10-31 (лог лоадера — $LOADER_LOG) $(date -u +%H:%M)"
  if ! dc run --rm --no-deps -T -w /app/pipeline sync python -u load_season_modern.py \
      --season-id "$sid" --date-from "$y-09-01" --date-to "$((y + 1))-10-31" >>"$LOADER_LOG" 2>&1; then
    tail -40 "$LOADER_LOG"; exit 1
  fi
  grep 'Loaded:' "$LOADER_LOG" | tail -1
done

echo "== build-dataset после загрузки"
AFTER=$(dataset_rows); echo "$AFTER"
FAIL=0
[ "$BEFORE" = "$AFTER" ] || { echo "!! датасет изменился"; FAIL=1; }

echo "== Матчи по сезонам: сезон регулярка плей-офф всего (ждали)"
for spec in $EXPECTED; do
  sid=${spec%:*}; want=${spec#*:}
  row=$(psql_dev -c "SELECT count(*) FILTER (WHERE game_type = 2), count(*) FILTER (WHERE game_type = 3),
                            count(*) FROM games WHERE season_id = $sid")
  echo "$sid $row ($want)"
  [ "${row##* }" = "$want" ] || { echo "!! $sid: матчей ${row##* }, ждали $want"; FAIL=1; }
done

echo "== Матчи без бросков в shot_attempts (ждали пусто)"
NOSHOTS=$(psql_dev -c "SELECT g.season_id, count(*) FROM games g WHERE g.season_id < 20212022
  AND NOT EXISTS (SELECT 1 FROM shot_attempts s WHERE s.game_id = g.game_id) GROUP BY 1 ORDER BY 1")
[ -z "$NOSHOTS" ] || { echo "$NOSHOTS"; FAIL=1; }

echo "== Броски без координат: сезон бросков без_координат % (порог 1%)"
psql_dev -c "SELECT g.season_id, count(*), count(*) FILTER (WHERE s.x_coord IS NULL OR s.y_coord IS NULL),
  round(100.0 * count(*) FILTER (WHERE s.x_coord IS NULL OR s.y_coord IS NULL) / count(*), 2)
  FROM shot_attempts s JOIN games g ON g.game_id = s.game_id WHERE g.season_id < 20212022
  GROUP BY 1 ORDER BY 1"
NOCOORD=$(psql_dev -c "SELECT count(*) FROM (SELECT g.season_id FROM shot_attempts s
  JOIN games g ON g.game_id = s.game_id WHERE g.season_id < 20212022 GROUP BY 1
  HAVING count(*) FILTER (WHERE s.x_coord IS NULL OR s.y_coord IS NULL) > 0.01 * count(*)) t")
[ "$NOCOORD" = 0 ] || { echo "!! сезонов с > 1% бросков без координат: $NOCOORD"; FAIL=1; }

echo "== Клубы: сезон клубов без_дивизиона без_конференции (в 20202021 конференций не было)"
psql_dev -c "SELECT season_id, count(*), count(*) FILTER (WHERE division_name IS NULL),
  count(*) FILTER (WHERE conference_name IS NULL) FROM teams WHERE season_id < 20212022
  GROUP BY 1 ORDER BY 1"
BADTEAMS=$(psql_dev -c "SELECT count(*) FROM teams WHERE season_id < 20212022
  AND (division_name IS NULL OR (conference_name IS NULL AND season_id <> 20202021))")
[ "$BADTEAMS" = 0 ] || { echo "!! клубов с NULL: $BADTEAMS"; FAIL=1; }
psql_dev -c "SELECT season_id, abbreviation, division_name, conference_name FROM teams
  WHERE abbreviation IN ('ATL', 'PHX') ORDER BY 1, 2"

echo "== Место после"
df -h /
psql_dev -c "SELECT pg_size_pretty(pg_database_size('nhl_dev'))"
[ "$FAIL" = 0 ] || exit 1
REMOTE

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/tmp/nhl_history_load_$STAMP.log
echo "== Загрузка на VM запущена в фоне, лог — $LOG"
PID=$(ssh_vm --command "EXPECTED='$EXPECTED' LOADER_LOG=/tmp/nhl_history_loader_$STAMP.log \
  setsid nohup sh /tmp/nhl_history_load.sh >$LOG 2>&1 </dev/null & echo \$!")
FOLLOW="tail --pid=$PID -n +1 -f $LOG"
echo "Если связь оборвётся, следить дальше:"
echo "  gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command '$FOLLOW'"
ssh_vm --command "$FOLLOW"
ssh_vm --command "tail -1 $LOG" | grep -q '^ГОТОВО'
