# Задача 112. CI и образы: actions по SHA, базовые образы по digest

**Блок:** релиз · **Надобность:** 🟡
**Область:** `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `.github/dependabot.yml`
(новый), `Dockerfile`, `docker-compose.yml`. Пуш изменений `.github/workflows` требует
`gh auth refresh -s workflow`.
**Зависит от:** ничего.
**Источник:** аудит Задачи 82, кандидат 11 и находка N6 в [`../../docs/security.md`](../../docs/security.md);
реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

Actions подключены по плавающим тегам (`actions/checkout@v4`, `docker/build-push-action@v6`…),
образы — по тегам (`python:3.11-slim-bookworm`, `postgres:16.6-alpine`). Перевес тега на чужой
коммит или образ исполняется с правом `packages: write`, ключом деплоя или доступом к базе.

## Зачем и на что влияет

- Подмена тегов actions уже случалась (tj-actions/changed-files, 2025).
- Job `deploy` держит `DEPLOY_SSH_KEY` — то есть root на VM.

## Степень важности

🟡 — нужен сторонний инцидент; дёшево закрыть, но обновления потом нужно вести.

## Подробно

1. Все `uses:` — полный SHA с комментарием версии (`actions/checkout@<sha> # v4.2.2`).
2. `.github/dependabot.yml` с экосистемами `github-actions` и `docker` (раз в неделю, PR в
   `develop`) — иначе пины устареют. `pip` не включать: lock-файлы с хэшами собирает `make lock`,
   а алерты по CVE Dependabot уже даёт (включены в Задаче 82).
3. `FROM python:3.11-slim-bookworm@sha256:…`, `postgres:16.6-alpine@sha256:…` в compose.
4. Включить `sha_pinning_required` в настройках Actions репозитория.

**Приёмка:** `grep -E 'uses: [^@]+@v' .github/workflows/*` пуст; CI и релиз зелёные; первый PR
Dependabot пришёл в `develop`.
