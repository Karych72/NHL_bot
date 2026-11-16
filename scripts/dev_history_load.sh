#!/bin/sh
# Загрузка истории 2009/10–2020/21 в nhl_dev (Задача 121). Запускать с ноутбука из корня
# чекаута, где лежит сырой слой all_data/raw, когда код Задачи 121 уже выкатился на dev-стенд
# (мерж в develop, Release зелёный):
#
#   sh scripts/dev_history_load.sh
#
# Шаги: сверка кэша на ноутбуке → место на VM → архив кэша на VM → том nhl_bot_dev_syncdata →
# build-dataset до → загрузка двенадцати сезонов из кэша сервисом sync dev-стека (сам sync
# на время остановлен) → build-dataset после → сверка матчей, бросков и справочника команд.
# Повторный запуск безопасен: лоадер идемпотентен, кэш перезаписывается теми же файлами.
# Откат — пересборка nhl_dev из дампа прода (DEVELOPMENT.md, «Dev-стенд»).
set -eu

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
RAW=${RAW:-all_data/raw}
TAR=${TMPDIR:-/tmp}/nhl_history_raw.tar
# Сезон:матчей регулярки и плей-офф (карточка Задачи 121, сверено с API при сборе кэша).
EXPECTED="20092010:1319 20102011:1319 20112012:1316 20122013:806 20132014:1323 20142015:1319
20152016:1321 20162017:1317 20172018:1355 20182019:1358 20192020:1212 20202021:952"

echo "== Кэш на ноутбуке: $RAW"
DIRS=""
for spec in $EXPECTED; do
  sid=${spec%:*}; want=${spec#*:}
  got=$(ls "$RAW/$sid" 2>/dev/null | grep -c '\.pbp\.json\.gz$' || true)
  [ "$got" = "$want" ] || { echo "$sid: pbp-файлов $got, ждали $want" >&2; exit 1; }
  for ep in box landing; do
    n=$(ls "$RAW/$sid" | grep -c "\.$ep\.json\.gz\$" || true)
    [ "$n" = "$want" ] || { echo "$sid: $ep-файлов $n, ждали $want" >&2; exit 1; }
  done
  DIRS="$DIRS $sid"
done
echo "все 12 сезонов на месте (14 917 матчей × 3 файла)"

ssh_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" "$@"; }

echo "== Место на VM (нужно ≥ 4 ГБ: архив, том, база)"
ssh_vm --command 'sh -s' <<'REMOTE'
set -eu
df -h /
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

ssh_vm --command "EXPECTED='$EXPECTED' sh -s" <<'REMOTE'
set -eu
cd /opt/nhl_bot_dev
LOG=/tmp/nhl_history_load_$(date -u +%Y%m%dT%H%M%SZ).log
# stdin ssh — это сам скрипт (sh -s): docker его не читает, иначе съест остаток
dc() { sudo -u deploy docker compose "$@" </dev/null; }
psql_dev() {
  (cd /opt/nhl_bot && sudo -u deploy docker compose exec -T db \
    psql -U postgres -d nhl_dev -v ON_ERROR_STOP=1 -At -F ' ' "$@" </dev/null)
}
dataset_rows() {
  dc run --rm --no-deps -T predict sh -c 'python -m modeling.cli build-dataset --mode train \
      --output-dir /tmp/ds >/dev/null && cd /tmp/ds && wc -l dataset_train.csv games_train.csv shots_train.csv'
}

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
trap 'echo "== sync dev-стека снова запущен"; dc up -d sync' EXIT

echo "== build-dataset до загрузки"
BEFORE=$(dataset_rows); echo "$BEFORE"

for spec in $EXPECTED; do
  sid=${spec%:*}; y=$(echo "$sid" | cut -c1-4)
  echo "== Сезон $sid: $y-09-01..$((y + 1))-10-31 (лог — $LOG)"
  if ! dc run --rm --no-deps -T -w /app/pipeline sync python -u load_season_modern.py \
      --season-id "$sid" --date-from "$y-09-01" --date-to "$((y + 1))-10-31" >>"$LOG" 2>&1; then
    tail -40 "$LOG" >&2; exit 1
  fi
  grep 'Loaded:' "$LOG" | tail -1
done

echo "== build-dataset после загрузки"
AFTER=$(dataset_rows); echo "$AFTER"
FAIL=0
[ "$BEFORE" = "$AFTER" ] || { echo "!! датасет изменился" >&2; FAIL=1; }

echo "== Матчи по сезонам: сезон регулярка плей-офф всего (ждали)"
for spec in $EXPECTED; do
  sid=${spec%:*}; want=${spec#*:}
  row=$(psql_dev -c "SELECT count(*) FILTER (WHERE game_type = 2), count(*) FILTER (WHERE game_type = 3),
                            count(*) FROM games WHERE season_id = $sid")
  echo "$sid $row ($want)"
  [ "${row##* }" = "$want" ] || { echo "!! $sid: матчей ${row##* }, ждали $want" >&2; FAIL=1; }
done

echo "== Матчи без бросков в shot_attempts (ждали пусто)"
NOSHOTS=$(psql_dev -c "SELECT g.season_id, count(*) FROM games g WHERE g.season_id < 20212022
  AND NOT EXISTS (SELECT 1 FROM shot_attempts s WHERE s.game_id = g.game_id) GROUP BY 1 ORDER BY 1")
[ -z "$NOSHOTS" ] || { echo "$NOSHOTS"; FAIL=1; }

echo "== Клубы: сезон клубов без_дивизиона без_конференции (в 20202021 конференций не было)"
psql_dev -c "SELECT season_id, count(*), count(*) FILTER (WHERE division_name IS NULL),
  count(*) FILTER (WHERE conference_name IS NULL) FROM teams WHERE season_id < 20212022
  GROUP BY 1 ORDER BY 1"
BADTEAMS=$(psql_dev -c "SELECT count(*) FROM teams WHERE season_id < 20212022
  AND (division_name IS NULL OR (conference_name IS NULL AND season_id <> 20202021))")
[ "$BADTEAMS" = 0 ] || { echo "!! клубов с NULL: $BADTEAMS" >&2; FAIL=1; }
psql_dev -c "SELECT season_id, abbreviation, division_name, conference_name FROM teams
  WHERE abbreviation IN ('ATL', 'PHX') ORDER BY 1, 2"

echo "== Место после"
df -h /
psql_dev -c "SELECT pg_size_pretty(pg_database_size('nhl_dev'))"
echo "Полный лог лоадера: $LOG"
[ "$FAIL" = 0 ] && echo "Готово: все сверки сошлись." || { echo "Есть расхождения — выше." >&2; exit 1; }
REMOTE
