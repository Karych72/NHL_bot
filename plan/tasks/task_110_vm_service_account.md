# Задача 110. VM: свой сервисный аккаунт вместо Editor на весь проект

**Блок:** релиз · **Надобность:** 🟠
**Область:** GCP-проект `nhl-bot-prod` (IAM, VM `nhl-bot`), `DEVELOPMENT.md` §«Прод-деплой»;
код репозитория не меняется.
**Зависит от:** ничего.
**Источник:** аудит Задачи 82, находка N3 в [`../../docs/security.md`](../../docs/security.md);
реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

VM работает под default compute SA с ролью `roles/editor` на проект и scope `cloud-platform`.
Metadata-сервер доступен из контейнеров (из dev-бота — HTTP 200): любой код в контейнере
получает токен редактора проекта. Нужен свой SA только с тем, что VM реально использует.

## Зачем и на что влияет

- Редактор проекта может менять metadata VM (дописать SSH-ключ → root), создавать VM и
  прочие ресурсы за счёт владельца, читать и менять диски и снапшоты.
- Путь к этому — уязвимость любой зависимости бота или код ветки `develop`, без доступа к хосту.
- VM сейчас использует от SA только Secret Manager (три секрета, `fetch-secrets.sh`).

## Степень важности

🟠 — подтверждённый риск с большим ущербом, но нужна сначала чужая RCE в контейнере.

## Подробно

1. Перед работой проверить, что VM ещё использует: логирование и мониторинг (ops agent есть?),
   `gcloud` на VM (только `secrets versions access`), GHCR — публичный, SA не нужен.
2. Создать SA `nhl-bot-vm`, выдать `roles/secretmanager.secretAccessor` на три секрета
   (`nhl-bot-telegram-token`, `nhl-bot-telegram-token-dev`, `nhl-bot-anthropic-key`) и,
   если нужно по п. 1, `roles/logging.logWriter`, `roles/monitoring.metricWriter`.
3. Остановить VM (простой обоих ботов ~1–2 мин), `gcloud compute instances set-service-account
   --service-account nhl-bot-vm@… --scopes cloud-platform`, запустить. Проверить: юнит
   `nhl-bot-secrets` зелёный, оба бота отвечают.
4. Снять у default compute SA роль `roles/editor` (или удалить привязку проекта целиком, если
   других VM нет).
5. Обновить `DEVELOPMENT.md` (команды создания VM и выдачи доступа к секретам) и
   `docs/security.md` (N3 → исправлено).

**Приёмка:** из контейнера metadata отдаёт токен нового SA; запрос этим токеном к Compute API
(`instances.get`) → 403; боты работают после перезагрузки VM.
