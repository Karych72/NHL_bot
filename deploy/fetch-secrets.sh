#!/usr/bin/env bash
# Токен бота из Secret Manager -> /run/nhl_bot/telegram_bot_token (Задача 65). Запускается
# при загрузке VM юнитом nhl-bot-secrets.service до docker.service и вручную при ротации.
# /run — tmpfs: токен только в RAM, не на диске VM, не в снапшотах и не в бэкапах.
# bot и sync получают файл как compose secret (deploy/compose.prod.yml).

set -euo pipefail

SECRET=nhl-bot-telegram-token
DIR=/run/nhl_bot

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
