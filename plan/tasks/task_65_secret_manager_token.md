# Задача 65. Токен бота на проде — из Secret Manager

**Блок:** релиз (прод-инфраструктура) · **Надобность:** 🟡
**Область:** `docker-compose.yml` или прод-оверрайд к нему, новый `deploy/` (скрипт загрузки
секрета и systemd-юнит), `DEVELOPMENT.md` §«Прод-деплой», `docs/architecture.md` (если появится
каталог), `.env.example`. Вне репозитория — `../nhl_bot_gcp_setup.sh` (шаг `env`).
**Зависит от:** ничего.
**Источник:** просьба человека 2026-10-08 после случайного перевыпуска токена в BotFather;
образец — проект argus (`argus/deploy/fetch-secrets.sh`). Реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

Сейчас `TELEGRAM_BOT_TOKEN` на проде лежит открытым текстом в `/opt/nhl_bot/.env` на VM. Туда
его один раз скопировал шаг `env` скрипта `nhl_bot_gcp_setup.sh` из локального `.env` ноутбука.
Деплой (`release.yml`) токен не трогает, и сменить токен можно только правкой файла по SSH.

Сделать: токен хранится в GCP Secret Manager (проект `nhl-bot-prod`, секрет
`nhl-bot-telegram-token`). При загрузке VM systemd-юнит до старта Docker кладёт его в `/run`
(tmpfs, то есть только в RAM), а `bot` и `sync` берут его оттуда. Из `.env` на VM токен уходит.
Локальный запуск не меняется: токен по-прежнему в локальном `.env`.

## Зачем и на что влияет

- Токен не лежит на диске VM, в её снапшотах и в бэкапах.
- Смена токена — одна команда на ноутбуке и одна на VM, без ручной правки `.env` по SSH.
  Источник истины один: Secret Manager, а не «какой-то `.env` на какой-то машине».
- Так же устроен argus: один подход на оба прод-проекта.

## Степень важности

🟡. Прод работает и без этого, `.env` на VM закрыт `chmod 600`. Выигрыш — в гигиене секрета
и в простоте ротации, а ротация нужна редко.

## Подробно

### Текущее состояние (снимок на 2026-10-08)

- VM `nhl-bot`, проект `nhl-bot-prod`, зона `europe-west1-b`, Debian 12. Создана
  `gcloud compute instances create` без `--scopes` и `--service-account`, то есть с сервисным
  аккаунтом Compute по умолчанию и **скоупами по умолчанию**. В них нет `cloud-platform`, и
  Secret Manager с VM недоступен, даже если выдать IAM-роль.
- `bot` и `sync` в `docker-compose.yml` читают `env_file: .env`. `sync` тоже нужен токен:
  `push_digest_job.py` рассылает подписчикам.
- `release.yml` на VM: `git checkout` тега, правка `NHL_BOT_IMAGE` в `.env`, `pull`, `up -d`.
- Из секретов на VM только токен: у PostgreSQL `trust` и порт на loopback, пароля нет.
  `SEASON_ID`, `BACKUP_DIR`, `ENABLE_PUSH_DIGEST` — настройки, не секреты, остаются в `.env`.

### Как это будет работать

**Хранение.** Секрет `nhl-bot-telegram-token` в Secret Manager проекта `nhl-bot-prod`.
Новый токен — новая версия секрета (`gcloud secrets versions add ... --data-file=-`, ввод
скрытый, в историю шелла не попадает). Сервисному аккаунту VM — роль
`roles/secretmanager.secretAccessor` **только на этот секрет**, не на проект.

**Загрузка на VM.** `deploy/fetch-secrets.sh` (по образцу argus) читает последнюю версию секрета
и пишет `/run/nhl_bot/secrets.env` одной строкой `TELEGRAM_BOT_TOKEN=...`: каталог `0700`,
файл `0600`, владелец root. Запускается systemd-юнитом `nhl-bot-secrets.service`
(`Type=oneshot`, `Before=docker.service`, `After=network-online.target`,
`Restart=on-failure` — metadata-сервер в первые секунды после загрузки может не ответить).
`/run` — tmpfs, после перезагрузки файл пуст, и юнит заполняет его заново до того, как Docker
поднимет контейнеры (`restart: unless-stopped`).

**В контейнеры.** На VM `bot` и `sync` получают второй `env_file` — `/run/nhl_bot/secrets.env`.
Как подключить его только на проде, решить в начале задачи. Рекомендация — прод-оверрайд
`deploy/compose.prod.yml` с этим `env_file` и `COMPOSE_FILE=docker-compose.yml:deploy/compose.prod.yml`
в `.env` на VM. Тогда и `release.yml`, и ручной `docker compose` на VM видят оверрайд, а
локально он не подключён. Нет файла на проде — compose падает громко (Global Constraint 4).
Вариант `required: false` у `env_file` в общем compose — это молчаливый fallback, его не брать.

**Деплой не трогается.** `release.yml` секрет не загружает: токен меняется редко, а после
перезагрузки VM его восстанавливает юнит. Шаг загрузки в каждом релизе — «на будущее»
(Global Constraint 1); решение человека 2026-10-08.

**Ротация** (процедура в `DEVELOPMENT.md`):
1. BotFather → новый токен.
2. На ноутбуке: `gcloud secrets versions add nhl-bot-telegram-token --data-file=- --project nhl-bot-prod`.
3. На VM: `sudo systemctl restart nhl-bot-secrets && cd /opt/nhl_bot && docker compose up -d --force-recreate bot sync`.

### Что делать

1. Включить API `secretmanager.googleapis.com`, создать секрет, положить текущий токен.
2. Сменить скоупы VM на `cloud-platform`: `gcloud compute instances set-service-account` работает
   **только на остановленной VM** — это простой бота на минуту-две, согласовать время с
   человеком. Выдать роль на секрет.
3. `deploy/fetch-secrets.sh` и `deploy/systemd/nhl-bot-secrets.service`; установка на VM
   (копия в `/opt/nhl_bot` уже есть — из чекаута, `systemctl enable`).
4. Подключение `secrets.env` к `bot` и `sync` на проде (см. выше).
5. Убрать `TELEGRAM_BOT_TOKEN` из `/opt/nhl_bot/.env` на VM — источник один (Global Constraint 3).
6. `nhl_bot_gcp_setup.sh`, шаг `env`: вместо копирования токена в `.env` на VM — `gcloud secrets
   versions add` из локального `.env`; добавить включение API, роль и скоупы в шаги `vm`/`host`.
   Скрипт лежит вне репозитория — правка отдельно, в отчёте сказать о ней.
7. Документация: `DEVELOPMENT.md` §«Прод-деплой» (хранение, ротация, юнит), комментарий в
   `.env.example` (локально токен в `.env`, на проде — Secret Manager), `docs/architecture.md`,
   если появится `deploy/`.

### Критерии приёмки

- В `/opt/nhl_bot/.env` на VM нет `TELEGRAM_BOT_TOKEN`; токен есть только в Secret Manager и в
  `/run/nhl_bot/secrets.env`.
- `sudo reboot` VM → после загрузки `bot` и `sync` `healthy`, бот отвечает на `/start`.
- Ротация по процедуре из `DEVELOPMENT.md` с новым токеном → бот отвечает, старый токен не нужен.
- Очередной релиз через `release.yml` проходит без правок workflow.
- Локальный `docker compose up` и `make bot` работают как раньше, с токеном из локального `.env`.
- `make ci-local` зелёный.

### Подводные камни

- **Скоупы VM.** Без `cloud-platform` любой вызов Secret Manager с VM даёт `403` при выданной
  роли — легко принять за ошибку IAM.
- **`gcloud` на VM.** В образах Debian от GCP `google-cloud-cli` обычно предустановлен, но
  проверить перед работой. Если его нет, обойтись `curl` к metadata-серверу за токеном доступа
  и REST API Secret Manager, а не ставить пакет.
- **Порядок старта.** Если юнит не успел до Docker, контейнеры с `restart: unless-stopped`
  стартуют без файла и упадут. Поэтому `Before=docker.service` и проверка перезагрузкой
  в приёмке.
- **Два бота на одном токене.** Если локально тоже запущен бот с тем же токеном, Telegram
  отдаёт `409 Conflict` на `getUpdates` одному из них. При проверке локальный бот выключен.
- Токен не печатать: ни в логах `fetch-secrets.sh`, ни в выводе команд в отчёте.
