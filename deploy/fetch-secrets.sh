#!/usr/bin/env bash
# Секрет из Secret Manager -> <каталог>/<файл> (Задачи 65, 80, 81). Запускается при загрузке VM
# юнитом nhl-bot-secrets.service до docker.service и вручную при ротации. Аргументы — секрет,
# каталог и имя файла (по умолчанию telegram_bot_token): токен прод-бота (nhl-bot-telegram-token,
# /run/nhl_bot), dev-бота (nhl-bot-telegram-token-dev, /run/nhl_bot_dev); ключ Claude API
# (nhl-bot-anthropic-key) — в подкаталог anthropic/ того же каталога, файл anthropic_api_key.
# Подкаталог — ради отката: скрипт до Задачи 81 третий аргумент игнорирует и пишет
# telegram_bot_token в переданный каталог, так что ключ не может лечь поверх токена бота.
# /run — tmpfs: секрет только в RAM, не на диске VM, не в снапшотах и не в бэкапах.
# Контейнеры получают файл как compose secret (deploy/compose.prod.yml, compose.dev.yml).

set -euo pipefail

USAGE="использование: fetch-secrets.sh <секрет> <каталог> [<файл>]"
SECRET=${1:?$USAGE}
DIR=${2:?$USAGE}
FILE=${3:-telegram_bot_token}

# Как в argus: на хосте в каталог 0700 заходит только root (bind-монтирует демон), а сам
# файл 0444 — его читает appuser контейнера (uid 1000, Dockerfile) через точку монтирования.
install -d -m 0700 -o root -g root "$DIR"
tmp="$(mktemp "$DIR/.secret.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

# Проект gcloud на VM берёт из metadata-сервера. Нет версии секрета или доступа — падаем:
# без секрета контейнеры всё равно не поднимутся, пустой файл лишь спрятал бы причину.
gcloud secrets versions access latest --secret="$SECRET" --out-file="$tmp"
[ -s "$tmp" ] || { echo "fetch-secrets: $SECRET пуст" >&2; exit 1; }
chmod 0444 "$tmp"
mv -f "$tmp" "$DIR/$FILE"
echo "fetch-secrets: $SECRET -> $DIR/$FILE"
