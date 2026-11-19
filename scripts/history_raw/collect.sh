#!/bin/sh
# Сбор сырого слоя матчей 1917/18–2008/09 в all_data/raw (Задача 155). С ноутбука, из корня
# чекаута:
#
#   sh scripts/history_raw/collect.sh
#
# Без записи в БД. Повторный запуск докачивает недостающее (полный ответ из кэша не
# запрашивается). caffeinate не даёт ноутбуку уснуть, пока идёт сбор. Сверка по сезонам —
# в выводе и в all_data/raw/history_collect_summary.tsv; лог — all_data/raw/history_collect.log.
set -eu
cd "$(dirname "$0")/../.."
LOG=all_data/raw/history_collect.log
mkdir -p all_data/raw
caffeinate -i .venv/bin/python scripts/history_raw/collect.py "$@" 2>&1 | tee -a "$LOG"
