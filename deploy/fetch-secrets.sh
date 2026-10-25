#!/usr/bin/env bash
# Токен бота из Secret Manager -> <каталог>/telegram_bot_token (Задачи 65, 80). Запускается
# при загрузке VM юнитом nhl-bot-secrets.service до docker.service и вручную при ротации.
# Аргументы — секрет и каталог: прод-бот (nhl-bot-telegram-token, /run/nhl_bot) и dev-бот
# (nhl-bot-telegram-token-dev, /run/nhl_bot_dev); юнит вызывает скрипт для обоих.
# /run — tmpfs: токен только в RAM, не на диске VM, не в снапшотах и не в бэкапах.
# bot и sync получают файл как compose secret (deploy/compose.prod.yml, compose.dev.yml).

set -euo pipefail

SECRET=${1:?использование: fetch-secrets.sh <секрет> <каталог>}
DIR=${2:?использование: fetch-secrets.sh <секрет> <каталог>}

# Как в argus: на хосте в каталог 0700 заходит только root (bind-монтирует демон), а сам
# файл 0444 — его читает appuser контейнера (uid 1000, Dockerfile) через точку монтирования.
install -d -m 0700 -o root -g root "$DIR"
tmp="$(mktemp "$DIR/.token.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

# Проект gcloud на VM берёт из metadata-сервера. Нет версии секрета или доступа — падаем:
# без токена bot и sync всё равно не поднимутся, пустой файл лишь спрятал бы причину.
gcloud secrets versions access latest --secret="$SECRET" --out-file="$tmp"
[ -s "$tmp" ] || { echo "fetch-secrets: $SECRET пуст" >&2; exit 1; }
chmod 0444 "$tmp"
mv -f "$tmp" "$DIR/telegram_bot_token"
echo "fetch-secrets: $SECRET -> $DIR/telegram_bot_token"
