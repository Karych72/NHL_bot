# Разработка NHL_bot

Краткие правила, на которые ориентируемся в команде и в CI.

## Окружение

- **Python 3.11** — целевая версия (как в GitHub Actions).
- Рабочее окружение: `make setup` → каталог `.venv`, зависимости из `requirements.txt`. Не полагаться на глобальный `pip` на macOS без venv (см. комментарий в `requirements.in` про архитектуру и NumPy).
- Линтер и mypy: `make setup-dev` (дополнительно ставит `requirements-dev.txt`).
- Секреты только в `.env`; в репозитории — шаблон `.env.example`. Токены бота и доступы к БД не коммитить.

## Docker

- Сборка образа бота из корня репозитория: `docker build -t nhl-bot .`
- Локальный стек PostgreSQL + бот: скопировать `.env.example` в `.env`, задать `TELEGRAM_BOT_TOKEN`, затем `docker compose up`. Имя compose-проекта закреплено полем `name: nhl_bot` в `docker-compose.yml`, поэтому named volume `nhl_bot_pgdata` под PGDATA не зависит от каталога, из которого запущена команда (основной чекаут или таск-воркtree). Сервис `db` пробрасывает порт `5432`, но только на loopback (`127.0.0.1:5432:5432`) — `POSTGRES_HOST_AUTH_METHOD: trust` остаётся, порт наружу не смотрит; в compose для бота выставлены `PG_HOST=db` и `PG_USER=postgres` (см. `docker-compose.yml`). Перед первым запуском бота примените DDL/SQL-функции к этой БД с хоста: `make PG_HOST=localhost PG_USER=postgres db-init` (когда контейнер `db` уже слушает порт). Переменные нужно передавать именно аргументами `make`, а не переменными окружения перед командой — Makefile делает `include .env` и `export`, и если в `.env` уже задан свой `PG_USER` (например, от локального нативного PostgreSQL), значение из `.env` перекрывает переменную окружения, но не аргумент командной строки `make`.
- Контейнер бота выполняется от non-root пользователя (`appuser`, uid/gid 1000); пакеты по-прежнему ставятся от root на этапе сборки. У образа есть `HEALTHCHECK` — он проверяет доступность PostgreSQL из контейнера бота (подключение psycopg2 по `PG_HOST`/`PG_PORT`/`PG_USER`/`PG_DATABASE`), а не то, жив ли процесс.
- В образ не копируется `.env`; при `docker compose up` используется `env_file: .env`.
- **Restart-политика (Задача 35).** У всех сервисов (`db`, `bot`, `sync`, `predict`, `backup`, а также `retrain` при включённом профиле `modeling`) — `restart: unless-stopped`: переживают падение процесса и перезапуск Docker Desktop/хоста без ручного вмешательства.
- **Автообновление данных (Задача 34).** Сервис `sync` в `docker-compose.yml` — тот же образ, что у бота, с `command: ["python", "-u", "scheduled_sync.py", "loop"]` и `working_dir: /app/pipeline`. Планировщик (`pipeline/scheduled_sync.py`) при старте сразу прогоняет загрузчик за окно `сегодня−2…сегодня`, затем ночью каждые 30 минут с 18:00 до 08:00 UTC включительно (21:00–11:00 МСК, Задача 56) прогоняет его снова — завершённые матчи попадают в БД по ходу ночи. Днём, с 12:30 UTC (15:30 МСК), слоты идут без загрузчика — только рассылка превью предстоящих матчей (Задача 78); статус-файл они не пишут. После каждого прогона следом идёт `telegram_bot/push_digest_job.py` (пропускается сам, если `ENABLE_PUSH_DIGEST` не включён в `.env`): он шлёт подписчикам итог матча их подписок вскоре после конца матча, а дайджест с остальными матчами — как только вся ночь уже в БД, не позже последнего слота, 08:00 UTC; отметки `bot_chat_game_sent` (чат, матч) и `bot_subscriptions.last_sent_night` не дают отправить матч и ночь дважды, в том числе после перезапуска (Задачи 60, 63, 85, `docs/telegram_bot.md` §7). Повторные прогоны одного окна идемпотентны (`docs/data_loading.md` §6.3); игра попадает в `games`, только когда все три ответа gamecenter в `gameState = OFF`. Диск-кэш загрузчика и файл статуса (`all_data/sync_status.json`) живут в именованном томе `syncdata:/app/all_data`, отдельном от `pgdata`, — переживают пересоздание контейнера. У сервиса свой `healthcheck: python scheduled_sync.py check` (перекрывает образный `HEALTHCHECK`, рассчитанный на бота): 0, если последний прогон успешен и не старше 26 часов, иначе 1 — `docker compose ps` покажет `unhealthy`.
  - Результат: `docker compose ps` (колонка health), `docker compose logs sync` (логи каждого прогона), `all_data/sync_status.json` внутри тома `syncdata`.
  - Ручной прогон в контейнере: `docker compose run --rm sync python scheduled_sync.py once` — учтите, что `once` запускает и рассылку (если `ENABLE_PUSH_DIGEST=1`; уже отправленная ночь не повторится) и не координируется с уже работающим `loop` в сервисе `sync`, так что оба могут наложиться друг на друга. Для ручного обновления только данных — с хоста, как и раньше, `make season-sync-week` (или сам загрузчик без дайджеста: `docker compose run --rm sync python load_season_modern.py --date-from … --date-to …`).
- **Еженедельный retrain (Задача 26).** **Отключён до Задачи 68** (модель победы; решение человека, 2026-10-10): модели `home_win` проигрывают Elo-эталону Задачи 66, каждый прогон был бы красным — профиль `modeling` не поднимать, см. `docs/modeling_training.md` §9. Сервис `retrain` в `docker-compose.yml` (профиль `modeling`: обычные `docker compose up -d` и `build` его не собирают и не запускают; поднять — `docker compose --profile modeling up -d retrain`) — образ со стадией `modeling` Dockerfile (`build: {target: modeling}`: lightgbm/sklearn, в образ бота не попадают) и `command: ["python", "-u", "pipeline/scheduled_retrain.py", "loop"]`. Раз в неделю (понедельник 12:00 UTC; цикл просыпается ежедневно в 12:00: повторяет прогон, отказанный по предусловию, а понедельничный слот пропускает, если реальный прогон был менее 6 дней назад) пересобирает датасет обучения и обучает `home_win` с `--no-promote`; стартует только после успешного sync, `latest` сам не двигается (`make modeling-promote`, см. `docs/modeling_training.md` §9). Артефакты — bind mount `./artifacts:/app/artifacts`, статус — `all_data/retrain_status.json` в томе `syncdata`.
  - Результат: `docker compose ps` (health: последний retrain ok и не старше 8 дней), `docker compose logs retrain` (там же `run_id` в строках `run_id=… status=…`), отчёт `artifacts/reports/<run_id>/summary.md`.
  - Ручной прогон: `docker compose --profile modeling run --rm retrain python -u pipeline/scheduled_retrain.py once` (нужен свежий успешный sync — иначе отказ по предусловию).
- **Ежедневная публикация прогноза (Задача 73).** Сервис `predict` (без профиля — поднимается обычным `up` и выкатывается на прод) — образ стадии `modeling`, `command: ["python", "-u", "pipeline/scheduled_publish.py", "loop"]`. Каждый день в 12:00 UTC, после успешного свежего sync, пишет в `game_predictions` вероятность победы хозяев и тотала больше 5.5 по моделям из `deploy/models/` — они в репозитории и приезжают в образе, сервис их **не обучает**. Обновить модели — `docs/modeling_training.md` («Прогноз в боте»). При старте прогоняет цепочку, если последней публикации нет или она старше суток. Статус — `all_data/publish_status.json` в томе `syncdata`.
  - Результат: `docker compose ps` (health: последняя публикация ok и не старше 26 часов), `docker compose logs predict` (строки `published_rows=…`).
  - Ручной прогон: `docker compose run --rm predict python -u pipeline/scheduled_publish.py once` (нужен свежий успешный sync).
- **Развёртывание с нуля (Задача 35).** Локальный стек на Mac (Docker Desktop); прод — VM в GCP, см. «Прод-деплой» ниже.
  1. `cp .env.example .env`, заполнить `TELEGRAM_BOT_TOKEN`, `SEASON_ID`, `BACKUP_DIR` (абсолютный путь вне репозитория и вне тома `pgdata` — комментарий в `.env.example`).
  2. `docker compose up -d db` — поднять только БД (том `pgdata` создаётся автоматически).
  3. `make PG_HOST=localhost PG_USER=postgres db-init` — применить DDL и SQL-функции к пустой БД, когда `db` уже слушает `127.0.0.1:5432`.
  4. Первичная загрузка данных с хоста, пока `bot`/`sync` ещё не подняты: `make season-load-full` — весь текущий сезон от даты старта (`SEASON_ID`) до сегодня (другие окна — `README.md`).
  5. `docker compose up -d` — поднять `bot`, `sync`, `predict`, `backup`; `backup` снимет первый дамп сразу при старте. `retrain` (трек B, в релиз не входит) — отдельно: `docker compose --profile modeling up -d retrain`.

  Обновление образа после изменения кода бота/пайплайна: `docker compose build && docker compose up -d` — пересобирает и пересоздаёт `migrate`, `bot` и `sync` (`build: .`); `migrate` при каждом `up` применяет SQL-функции и новые миграции (`make db-functions db-migrate` внутри образа), и только после его успешного выхода стартуют `bot` и `sync` — на пустой БД он падает, сначала шаг 3; `retrain` (профиль `modeling`, стадия `modeling` — образ с lightgbm/sklearn) — только с `--profile modeling`; `restart: unless-stopped` вернёт их после падения, но не подхватит новый образ без `up -d`. `db` и `backup` используют готовый образ `postgres:16.6-alpine` — `docker compose build` их не трогает.
- **Бэкап (Задача 35).** Сервис `backup` в `docker-compose.yml` — тот же принцип, что `sync`: долгоживущий контейнер со своим циклом (не host cron), образ `postgres:16.6-alpine`, как у `db`, — версия `pg_dump` совпадает с версией сервера. Раз в сутки, первый дамп — сразу при старте: `pg_dump -h db -U postgres -Fc postgres` пишется во временный файл и переименовывается в `nhl_<UTC-метка>.dump` только после успешного завершения, чтобы оборванный дамп не выглядел свежим. При ошибке `pg_dump` контейнер падает (`set -e`) — `restart: unless-stopped` поднимает его заново, а падение видно в `docker compose ps` (`Exit`/`Restarting`) и `docker compose logs backup`. Хранит 14 последних дампов, более старые удаляются при следующем успешном прогоне. Каталог — bind mount `${BACKUP_DIR}:/backups` (переменная обязательна: без неё `docker compose config`/`up` падает с понятным сообщением); дампы вне дерева репозитория и вне тома `pgdata`, поэтому записи в `.gitignore`/`.dockerignore` не нужны.
  - Проверить: `.env` не экспортируется в интерактивный shell, поэтому сначала `BACKUP_DIR=$(grep '^BACKUP_DIR=' .env | cut -d= -f2-)`, затем `docker compose ps` (колонка health — `healthy`, если в `$BACKUP_DIR` есть дамп моложе 26 часов) и `ls -la "$BACKUP_DIR"`.
- **Восстановление (Задача 35).** `.env` не экспортируется в интерактивный shell (его читают только compose и `make`), поэтому перед любой командой ниже, где встречается `$BACKUP_DIR` или `$COUNT_QUERY`, выполнить в шелле хоста:
    ```
    BACKUP_DIR=$(grep '^BACKUP_DIR=' .env | cut -d= -f2-)
    COUNT_QUERY="select 'games', count(*) from games
    union all select 'teams', count(*) from teams
    union all select 'rosters', count(*) from rosters
    union all select 'all_goals', count(*) from all_goals
    union all select 'game_team_stats', count(*) from game_team_stats
    union all select 'game_player_stats', count(*) from game_player_stats
    union all select 'game_goalie_stats', count(*) from game_goalie_stats
    union all select 'game_three_stars', count(*) from game_three_stars
    union all select 'teams_stats', count(*) from teams_stats
    union all select 'players_season_stats', count(*) from players_season_stats
    union all select 'goalies_season_stats', count(*) from goalies_season_stats
    union all select 'players_advanced_stats', count(*) from players_advanced_stats
    union all select 'players_shot_types', count(*) from players_shot_types
    union all select 'bot_subscriptions', count(*) from bot_subscriptions
    union all select 'prediction_log', count(*) from prediction_log
    union all select 'schema_migrations', count(*) from schema_migrations
    order by 1;"
    ```
    (список таблиц — по `data_tables/t.*.sql` плюс миграции: `bot_subscriptions`
    (`data_tables/migrations/0001_bot_subscriptions.up.sql`) и `prediction_log`
    (`0010_prediction_log.up.sql`, журнал прогнозов) — данные, не восстановимые повторной
    загрузкой из NHL API, и служебная `schema_migrations`
    (`Makefile`, `MIGRATIONS_TABLE_DDL`) — все переживают `db-drop`/`db-reset`, `pg_dump`
    снимает все. `BACKUP_DIR` — только абсолютный путь, см. `.env.example`, так что
    подстановка без раскрытия `~` работает как есть.)
  - *Разово проверить дамп на одноразовой БД* (рабочую `postgres` не трогает; точное сравнение числа строк по всем таблицам, а не оценка `pg_stat_user_tables`):
    ```
    docker compose cp "$BACKUP_DIR/nhl_<метка>.dump" db:/tmp/check.dump
    docker compose exec db createdb -U postgres nhl_check
    docker compose exec db pg_restore -U postgres -d nhl_check /tmp/check.dump
    docker compose exec db psql -U postgres -d postgres -c "$COUNT_QUERY"    # источник
    docker compose exec db psql -U postgres -d nhl_check -c "$COUNT_QUERY"   # восстановленная — сравнить построчно с источником
    docker compose exec db dropdb -U postgres nhl_check
    docker compose exec db rm /tmp/check.dump
    ```
  - *Полное восстановление рабочей БД* (потеря/порча `pgdata`) — сначала остановить `bot` и `sync`, чтобы не писали поверх восстановления:
    ```
    docker compose stop bot sync
    docker compose cp "$BACKUP_DIR/nhl_<метка>.dump" db:/tmp/restore.dump
    docker compose exec db pg_restore -U postgres -d postgres --clean --if-exists /tmp/restore.dump
    docker compose exec db rm /tmp/restore.dump
    docker compose start bot sync
    ```
- **`trust` только на loopback (Задача 35).** `POSTGRES_HOST_AUTH_METHOD: trust` у `db` допустим, пока порт опубликован на `127.0.0.1:5432:5432`, а не `0.0.0.0`. Если площадка когда-нибудь потребует открыть порт наружу — `trust` меняется на пароль (`POSTGRES_PASSWORD` в compose) тем же коммитом, что и смена порта; `telegram_bot/database.py` и загрузчик сейчас подключаются без пароля (`config.PG_HOST/PG_PORT/PG_USER/PG_DATABASE`, см. `database.py:120-123`), так что переход на пароль потребует добавить и его чтение в код подключения — это уже отдельная задача, не только смена compose.
- **Пересоздание `db`** при смене параметров контейнера (образ, переменные, healthcheck) не теряет данные — они на named volume `pgdata`, а не в самом контейнере. Перед командой убедиться в имени тома: `docker volume ls | grep nhl_bot_pgdata`, затем `docker compose up -d --force-recreate db` и перезапустить `bot` (пул соединений): `docker compose restart bot`.

## Прод-деплой

Прод — одна VM в GCP (Compute Engine, `e2-small`, Debian 12) с тем же `docker-compose.yml`, что и локально. Разницы две: `NHL_BOT_IMAGE` в `.env` на VM указывает на образ из GHCR (`ghcr.io/karych72/nhl_bot:<тег>`), и `bot`/`sync`/`migrate` берут его вместо локальной сборки (`predict` — образ стадии `modeling`, `NHL_BOT_MODELING_IMAGE` = `ghcr.io/karych72/nhl_bot:<тег>-modeling`); токен бота не в `.env`, а в Secret Manager (ниже, «Токен бота»). Порт `db` опубликован только на loopback VM, наружу открыт лишь SSH. `retrain` (профиль `modeling`, трек B) релиз не выкатывает — на проде модели не обучаются, `predict` берёт их из `deploy/models/` в образе.

**Ветки.** `develop` — ветка по умолчанию: PR задач идут в неё; её мерж выкатывает dev-стек (ниже, «Dev-стенд»), прод не трогает. `master` — то, что на проде: в него попадает только релизный PR `develop → master`. Обе ветки защищены: мерж только через PR с зелёными `quality` и `db-tests`, без force-push. Релизный PR вливать **merge commit**, не squash: после squash `develop` и `master` расходятся по истории, и следующий релизный PR тянет конфликты. Хотфикс — PR от `master` в `master`, затем `master` вливается обратно в `develop` (тоже PR).

**Релиз:** мерж релизного PR в `master`, если он меняет прод-код (`telegram_bot/`, `pipeline/`, `data_tables/`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `deploy/`, `Makefile`, `requirements.txt`, `modeling/`, `requirements-modeling.txt`; мерж только плана или документации прод не трогает), — job `tag` сам ставит следующий patch-тег от последнего `v*` (`v1.2.0` → `v1.2.1`); minor/major и хотфикс — вручную: `git tag v1.3.0 && git push origin v1.3.0`. Ожидать деплоя может только один прогон: новый (мерж или ручной тег) отменяет ожидающий. Дальше `.github/workflows/release.yml`: проверки `ci.yml` → образы `ghcr.io/karych72/nhl_bot:<тег>` и `…:<тег>-modeling` → job `deploy` (environment `production`) заходит на VM по SSH, переключает чекаут `/opt/nhl_bot` на тег, пишет `NHL_BOT_IMAGE` и `NHL_BOT_MODELING_IMAGE` в `.env`, делает `docker compose pull` и `up -d` (сначала `migrate`) и ждёт `healthy` у `bot` — иначе деплой красный. **Откат** — «Re-run jobs» у прогона предыдущего тега во вкладке Actions: миграции вперёд-назад он не откатывает, для этого `make db-migrate-down` вручную.

**Разовая настройка.**

1. VM (проект и зона — свои):
   ```
   gcloud compute instances create nhl-bot --zone=europe-west1-b --machine-type=e2-small \
     --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=20GB \
     --scopes=cloud-platform
   ```
   Скоуп `cloud-platform` нужен Secret Manager: со скоупами по умолчанию вызов с VM даёт `403` даже при выданной роли.
2. На VM (`gcloud compute ssh nhl-bot`): Docker (`curl -fsSL https://get.docker.com | sh`), пользователь для деплоя в группе `docker`, чекаут и каталог бэкапов:
   ```
   sudo useradd -m -s /bin/bash -G docker deploy
   sudo install -d -o deploy /opt/nhl_bot /srv/nhl_bot_backups
   sudo -u deploy git clone https://github.com/Karych72/NHL_bot.git /opt/nhl_bot
   ```
   В `/opt/nhl_bot/.env` (`chmod 600`, владелец `deploy`) — как в `.env.example`, с `BACKUP_DIR=/srv/nhl_bot_backups`, `COMPOSE_FILE=docker-compose.yml:deploy/compose.prod.yml` и **без** `TELEGRAM_BOT_TOKEN` — токен заводится по «Токену бота» ниже.
3. Ключ деплоя: `ssh-keygen -t ed25519 -N '' -f nhl_deploy`; `nhl_deploy.pub` — в `~deploy/.ssh/authorized_keys` на VM.
4. GitHub → Settings → Environments → `production`, секреты: `DEPLOY_HOST` (внешний IP VM), `DEPLOY_USER` (`deploy`), `DEPLOY_SSH_KEY` (содержимое `nhl_deploy`), `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan <IP>`). Там же можно включить «Required reviewers» — деплой будет ждать подтверждения. Приватный ключ после этого удалить с диска.
5. Первая БД — до первого тега, образом, собранным на VM (в GHCR его ещё нет): `docker compose up -d db`, `docker compose build migrate`, `docker compose run --rm migrate make db-sync`, затем полная загрузка сезона `docker compose run --rm --no-deps -w /app/pipeline sync python -u load_season_modern.py`.
6. Первый тег. После первой публикации пакет `nhl_bot` в GHCR сделать публичным (Package settings → Change visibility) — VM тянет образ без логина; секретов в образе нет (`.dockerignore`).

**Боты и секреты (Задача 79, 2026-10-25).** Секреты — в Secret Manager проекта `nhl-bot-prod`, у каждого роль `roles/secretmanager.secretAccessor` для сервисного аккаунта VM. Значения не выводить и не вставлять в чаты; ввод — `gcloud secrets versions add <секрет> --data-file=- --project nhl-bot-prod`, затем Ctrl-D дважды без Enter (Enter допишет перевод строки в значение).

| Секрет | Что | Кто читает |
|---|---|---|
| `nhl-bot-telegram-token` | токен продового бота @IcePulseNHLBot (ветка `master`) | `fetch-secrets.sh` → `bot`, `sync` прод-стека |
| `nhl-bot-telegram-token-dev` | токен dev-бота @auto_karych_test_bot (ветка `develop`, бывший продовый) | `fetch-secrets.sh` → `bot`, `sync` dev-стека (`/run/nhl_bot_dev`, Задача 80) |
| `nhl-bot-anthropic-key` | ключ Anthropic API, один на dev и прод | `fetch-secrets.sh` → `bot` обоих стеков (`anthropic_api_key`, Задача 81) |

Локально — свой `.env` (`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`). Локальный `make bot` с продовым или dev-токеном не запускать, пока тот же бот работает на VM: второй polling-клиент на один токен получает `Conflict`. Для локальной отладки — отдельный личный бот.

**Токен бота (Задача 65).** Хранится в Secret Manager проекта `nhl-bot-prod`, секрет `nhl-bot-telegram-token`; на диске VM его нет. При загрузке VM юнит `nhl-bot-secrets.service` (`deploy/systemd/`, до `docker.service`) запускает `deploy/fetch-secrets.sh <секрет> <каталог> [<файл>]` — для прода и для dev-бота (Задача 80), каждый раз скриптом из своего чекаута: тот читает последнюю версию секрета и пишет `<каталог>/<файл>`, для токена — `<каталог>/telegram_bot_token`, у прода — `/run/nhl_bot/telegram_bot_token` (tmpfs; каталог `0700 root`, файл `0444` — его читает `appuser` контейнера). `deploy/compose.prod.yml` монтирует файл в `bot` и `sync` как compose secret, а entrypoint-обёртка на `sh` кладёт его в окружение и `exec`-ом запускает `command` — и сервиса, и `docker compose run`. Через `env_file` нельзя: Docker хранит `Config.Env` открытым текстом в `config.v2.json` на диске и показывает в `docker inspect`. Оверрайд включает `COMPOSE_FILE` в `.env` на VM, поэтому его видят и `release.yml`, и ручной `docker compose`. Нет файла — контейнер не стартует (`bind source path does not exist`), и при перезагрузке VM тоже. Локально оверрайд не подключён, токен — в локальном `.env`. `release.yml` секрет не читает: после перезагрузки VM его восстанавливает юнит. Чекаут тега старше Задачи 65 (откат «Re-run») не содержит `deploy/compose.prod.yml`, и compose на нём упадёт; такой откат — сначала вернуть токен в `.env` и убрать `COMPOSE_FILE`.

На VM пользователь из `gcloud compute ssh` не в группе `docker`, а `.env` принадлежит `deploy`: команды `docker compose` и правка `.env` — через `sudo -u deploy` (ниже).

Настройка (разово). С ноутбука, `P=nhl-bot-prod`:
```
gcloud services enable secretmanager.googleapis.com --project $P
gcloud secrets create nhl-bot-telegram-token --replication-policy=automatic --project $P
gcloud secrets versions add nhl-bot-telegram-token --data-file=- --project $P   # ввести токен, Ctrl-D
SA=$(gcloud compute instances describe nhl-bot --zone europe-west1-b --project $P --format='value(serviceAccounts[0].email)')
gcloud secrets add-iam-policy-binding nhl-bot-telegram-token --project $P \
  --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
```
Роль выдаётся на секрет, не на проект. Если VM создана без `--scopes=cloud-platform`, скоупы меняются только на остановленной VM (простой бота на минуту-две): `gcloud compute instances stop nhl-bot`, `gcloud compute instances set-service-account nhl-bot --service-account $SA --scopes cloud-platform`, `gcloud compute instances start nhl-bot` (все с `--zone europe-west1-b --project $P`). Дальше на VM — когда чекаут `/opt/nhl_bot` уже на теге с `deploy/` (его выкатывает релиз, `deploy/**` в фильтре путей):
```
cd /opt/nhl_bot \
  && sudo install -m 644 deploy/systemd/nhl-bot-secrets.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload && sudo systemctl enable --now nhl-bot-secrets \
  && sudo test -s /run/nhl_bot/telegram_bot_token \
  && sudo -u deploy docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --no-deps --force-recreate sync \
  && sleep 15 && sudo -u deploy docker compose ps sync
```
Пробный шаг — `sync` (для пользователей бота некритичен) уже с оверрайдом, `.env` ещё не тронут. Дальше — только если `sync` в статусе `Up`, а не `Restarting`:
```
cd /opt/nhl_bot \
  && sudo -u deploy sed -i '/^TELEGRAM_BOT_TOKEN=/d' .env \
  && echo 'COMPOSE_FILE=docker-compose.yml:deploy/compose.prod.yml' | sudo -u deploy tee -a .env >/dev/null \
  && sudo -u deploy docker compose up -d --force-recreate bot sync
```

**Ключ Claude API (Задача 81).** Тот же механизм: юнит пишет ключ в `/run/nhl_bot/anthropic/anthropic_api_key` и `/run/nhl_bot_dev/anthropic/anthropic_api_key` (`fetch-secrets.sh nhl-bot-anthropic-key <каталог>/anthropic anthropic_api_key`), `compose.prod.yml` и `compose.dev.yml` монтируют его только в `bot`, entrypoint `bot` кладёт в окружение токен и ключ. Без файла `bot` не стартует, без переменной — падает `config.validate_env`. Подкаталог `anthropic/` — защита от отката: `fetch-secrets.sh` старше Задачи 81 третий аргумент игнорирует и пишет `telegram_bot_token` в переданный каталог, то есть в безвредный `anthropic/telegram_bot_token`, а не поверх токена бота. Поэтому новый юнит можно ставить в любой момент, и откат «Re-run» на старый тег ничего не ломает. Выкатка:
1. До мержа PR в `develop` (иначе dev-деплой красный: `bot` без файла) — секрет `nhl-bot-anthropic-key` с версией и ролью доступа (как у токена выше), затем на VM ключ для dev-стека напрямую, без скрипта: в `/opt/nhl_bot_dev` до мержа старый `fetch-secrets.sh`.
   ```
   sudo install -d -m 0700 /run/nhl_bot_dev/anthropic \
     && sudo gcloud secrets versions access latest --secret=nhl-bot-anthropic-key --out-file=/run/nhl_bot_dev/anthropic/anthropic_api_key \
     && sudo chmod 0444 /run/nhl_bot_dev/anthropic/anthropic_api_key
   ```
2. После мержа в `develop` (dev-чекаут уже с новым скриптом) — новый юнит: `sudo install -m 644 /opt/nhl_bot_dev/deploy/systemd/nhl-bot-secrets.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart nhl-bot-secrets`. До релиза прод-строка ключа пишет `anthropic/telegram_bot_token` скриптом прода — безвредно, прод-коду ключ не нужен.
3. До мержа релизного PR в `master` — ключ для прода скриптом dev-чекаута: `sudo /opt/nhl_bot_dev/deploy/fetch-secrets.sh nhl-bot-anthropic-key /run/nhl_bot/anthropic anthropic_api_key`. После релиза его же пишет юнит при каждой загрузке VM.

Ротация ключа — новая версия `nhl-bot-anthropic-key`, `sudo systemctl restart nhl-bot-secrets` и `docker compose up -d --force-recreate bot` в обоих чекаутах. Месячный лимит расходов — в Anthropic Console.

**Ротация токена:** BotFather → новый токен; на ноутбуке `gcloud secrets versions add nhl-bot-telegram-token --data-file=- --project nhl-bot-prod` (ввод не попадает в историю шелла, Ctrl-D); на VM `sudo systemctl restart nhl-bot-secrets && cd /opt/nhl_bot && sudo -u deploy docker compose up -d --force-recreate bot sync`. Dev-бот — то же с `nhl-bot-telegram-token-dev` и `cd /opt/nhl_bot_dev`.

### Dev-стенд (Задача 80)

Два бота на одной VM: `develop` → dev-бот @auto_karych_test_bot, `master` → продовый @IcePulseNHLBot. Postgres один — контейнер `db` прод-стека; баз две: прод — `postgres`, dev — `nhl_dev`. Общая база отклонена: `bot_subscriptions` хранит только `chat_id`, dev-рассылка ушла бы продовым подписчикам от dev-бота, а на `Forbidden` рассылка **деактивирует** подписку; и `migrate` из `develop` менял бы схему под продовым кодом.

Dev-стек — тот же `docker-compose.yml` в отдельном compose-проекте `nhl_bot_dev`, чекаут `/opt/nhl_bot_dev`. Оверрайд `deploy/compose.dev.yml` убирает `db` и `backup` (профиль, который никто не включает), подключает `migrate`, `bot`, `sync`, `predict` к сети прод-стека `nhl_bot_default` и ставит им `PG_DATABASE=nhl_dev`; токен — файл `/run/nhl_bot_dev/telegram_bot_token`. Том `syncdata` у dev свой (`nhl_bot_dev_syncdata`). Дампы `backup` снимают только `postgres`: `nhl_dev` всегда можно пересобрать из продового дампа.

**Выкатка.** Мерж в `develop` → `release.yml`: `ci.yml` → образы `ghcr.io/karych72/nhl_bot:dev-<sha7>` и `…-modeling` (git-тег не ставится) → `deploy` в `/opt/nhl_bot_dev` на коммит `develop`. Своя группа `concurrency`: dev-деплой не вытесняет ожидающий деплой прода. Секреты `DEPLOY_*` — те же, environment `production`. Миграция из `develop` применяется к `nhl_dev`; на `postgres` она попадёт только релизом в `master`. Откат `develop` без мержа — `make db-migrate-down` на `nhl_dev` или пересборка базы (ниже).

**Разовая настройка** (на VM, `gcloud compute ssh nhl-bot`; секрет `nhl-bot-telegram-token-dev` — «Боты и секреты» выше). Нужен Docker Compose ≥ 2.20 (`depends_on.required`), на VM 2026-10-25 — v5.5.1. Настройка — **до** первого деплоя `develop`: иначе он упадёт на `cd /opt/nhl_bot_dev` (тогда — настроить и «Re-run jobs»):
```
# чекаут и .env dev-стека: .env прода без образов и COMPOSE_FILE, плюс проект и оверрайды dev
sudo install -d -o deploy /opt/nhl_bot_dev
sudo -u deploy git clone -b develop https://github.com/Karych72/NHL_bot.git /opt/nhl_bot_dev
sudo -u deploy sh -c 'grep -vE "^(COMPOSE_FILE|COMPOSE_PROJECT_NAME|NHL_BOT_IMAGE|NHL_BOT_MODELING_IMAGE)=" /opt/nhl_bot/.env > /opt/nhl_bot_dev/.env \
  && printf "COMPOSE_PROJECT_NAME=nhl_bot_dev\nCOMPOSE_FILE=docker-compose.yml:deploy/compose.prod.yml:deploy/compose.dev.yml\n" >> /opt/nhl_bot_dev/.env \
  && chmod 600 /opt/nhl_bot_dev/.env'

# база nhl_dev из последнего дампа прода; подписки и отметки рассылок — пустые
cd /opt/nhl_bot
DUMP=$(sudo ls -1 /srv/nhl_bot_backups/ | grep -E '^nhl_.*\.dump$' | sort | tail -1)
sudo -u deploy docker compose exec -T db createdb -U postgres nhl_dev
sudo cat "/srv/nhl_bot_backups/$DUMP" | sudo -u deploy docker compose exec -T db pg_restore -U postgres -d nhl_dev --no-owner
sudo -u deploy docker compose exec -T db psql -U postgres -d nhl_dev -c 'TRUNCATE bot_subscriptions CASCADE'

# юнит: токены обоих ботов (скрипт dev-строки — из /opt/nhl_bot_dev)
sudo install -m 644 /opt/nhl_bot_dev/deploy/systemd/nhl-bot-secrets.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart nhl-bot-secrets
sudo test -s /run/nhl_bot_dev/telegram_bot_token && echo dev-token-ok
```
Чекаут должен содержать `deploy/compose.dev.yml` и `fetch-secrets.sh` с аргументами — они в `develop` с Задачи 80 (до её мержа клон брался с `-b task-80-dev-stand`). Дальше чекаут ведёт деплой. Пересборка `nhl_dev` — `docker compose stop` в `/opt/nhl_bot_dev`, `dropdb -U postgres --force nhl_dev` и те же три команды базы, затем `docker compose up -d` там же.

Dev-контейнеры подключены к сети прод-стека `nhl_bot_default`: `docker compose down` в `/opt/nhl_bot` её не удалит (`has active endpoints`), а пересоздание сети прода отрывает от неё dev — после него `docker compose up -d --force-recreate` в `/opt/nhl_bot_dev`.

Память `e2-small` (2 ГБ) — замер `docker stats --no-stream` до и после подъёма dev-стека: Задача 80 в `plan/archive/`. `predict` обоих стеков стартует в 12:00 UTC.

Нельзя запускать локальный `make bot` с dev-токеном, пока dev-бот работает на VM: второй polling-клиент получает `Conflict`.

## Тесты и качество

- **Минимум перед PR / слиянием:** `make ci-local` (ruff, mypy, `compileall`, `pytest` без `test_db_nhl`) — совпадает с GitHub Actions. Цель сама ставит `requirements-dev.txt` и `requirements-modeling.txt` (через `setup-dev`/`modeling-dev`), поэтому собирает и выполняет и `tests/test_modeling_*.py`.
- Только быстрые тесты без линтера: `make test-fast`.
- **Полный контур локально:** при изменениях DDL, SQL-функций или загрузчика — поднять БД, `make db-init` / `db-init-local`, затем `make all-tests` (схема и при необходимости данные — см. `README.md`).
- Проверки на уже загруженных данных: `make test-db-data` (`RUN_DB_DATA_TESTS=1`); нужна БД с данными (например, после `make season-sync-month`). В CI не запускается — там БД пустая (только схема). `test_games_config_season_id_present` и `test_season_stats_season_id_not_orphaned_and_config_season_present` рассчитаны на мультисезонную БД: они не требуют, чтобы вся таблица была одним сезоном, а проверяют, что сезон из `config.SEASON_ID` в таблице представлен, и (для четырёх stats-таблиц) что ни у одной строки `season_id` не ссылается на сезон, отсутствующий в `teams`.
- Ломающие изменения в `data_tables/*.sql` или `telegram_bot/queries/*.sql` сопровождаем понятным порядком применения (как в `Makefile`: `DDL_TABLES`, затем функции).

## Миграции схемы БД

Точечные изменения схемы (не пересоздание таблицы целиком через `data_tables/t.*.sql`)
идут через `data_tables/migrations/`: пары файлов `NNNN_slug.up.sql` / `NNNN_slug.down.sql`,
`NNNN` — версия с ведущими нулями (`0001`, `0002`, …). Ведущие нули обязательны для порядка
применения — обычная строковая сортировка (`$(sort $(wildcard ...))` в Makefile), а не
численная, так что версии разной ширины (`1` и `0010`) отсортируются неверно; при откате они
работают как тай-брейк. Сам откат выбирает не по номеру версии, а последнюю применённую
миграцию по времени (`ORDER BY applied_at DESC, version DESC LIMIT 1`). Применённые версии
учитываются в таблице `schema_migrations`, которую создаёт сам раннер.

- **Добавить миграцию:** взять следующий номер по порядку, создать пару
  `data_tables/migrations/NNNN_slug.up.sql` (само изменение) и `NNNN_slug.down.sql`
  (обратное действие), обе с шапкой-комментарием. Применяется `make db-migrate` —
  уже применённые версии пропускаются, up и вставка строки в `schema_migrations`
  коммитятся одной транзакцией. `*.up.sql` должен быть совместим с `--single-transaction`
  (`psql` применяет его и вставку в `schema_migrations` одной транзакцией): без операторов,
  требующих быть вне транзакции (`CREATE INDEX CONCURRENTLY`, `VACUUM`), и без собственных
  `BEGIN;`/`COMMIT;` внутри файла — иначе `INSERT` в `schema_migrations` уедет отдельной
  транзакцией, и учёт разойдётся с реальным состоянием схемы.
- **Откатить последнюю миграцию:** `make db-migrate-down` — выполняет её `*.down.sql`
  и удаляет строку из `schema_migrations`, тоже одной транзакцией. Без применённых
  миграций — не ошибка, просто сообщение. Откатывается только одна, последняя,
  миграция за вызов.
- **Деплой загрузчика и бота:** миграции 0003 (`scheduled_games`) и 0004 (`game_predictions`)
  нужно применить (`make db-migrate PG_USER=postgres`) до запуска нового загрузчика — иначе
  его прогон падает на отсутствующей таблице.
- `db-sync` и `db-reset` (и их `-local` варианты) уже включают `db-migrate` после
  `db-functions` — отдельно звать его нужно только вне этих целей.
- `db-reset` не сбрасывает мигрированные объекты: `db-drop` (`scripts/db_drop_all_tables.sql`)
  удаляет только таблицы из `DDL_TABLES`, не трогая `bot_subscriptions` и
  `schema_migrations`. Поэтому правка уже применённой миграции `db-reset`-ом заново не
  накатится, пока её версия не уйдёт из `schema_migrations` (например, через
  `db-migrate-down`) — так и задумано, ради живых подписок.
- **Миграция, трогающая таблицу из `DDL_TABLES`** (например, `ALTER TABLE games …`),
  требует двух условий сразу, иначе учёт в `schema_migrations` разойдётся со схемой:
  (а) то же изменение тем же коммитом переносится в соответствующий `data_tables/t.*.sql`
  — `db-reset`/`db-tables` пересоздают эти таблицы из DDL-файлов, минуя миграции, так что
  без этого пересозданная таблица останется на старой схеме, а `schema_migrations` будет
  врать, что миграция применена; (б) сама `*.up.sql` пишется идемпотентно (`ADD COLUMN IF
  NOT EXISTS`, `DROP … IF EXISTS` и т.п.), чтобы не упасть на «already exists» при
  применении к уже актуальной (из DDL) таблице — сценарий чистой БД (CI `db-tests`,
  `make db-sync`), где DDL уже содержит новое состояние, а `db-migrate` всё равно
  прогоняет все файлы по порядку.
- **Роль `nhl_ai_reader` (миграция 0015, Задача 83).** SQL, который пишет модель, идёт под
  отдельной ролью только на чтение; пароля нет (Postgres на `trust`, порт на `127.0.0.1`), права
  ограничивает сама роль. Роль кластерная: `nhl_dev` и `postgres` живут в одном кластере, миграция
  создаёт её один раз (проверка в `DO`), `USAGE` на схему и `SELECT` выдаются в текущей базе
  файлом `data_tables/ai_reader_grants.sql` — его подключает миграция (`\ir`) и применяет
  `make db-tables`, так что `db-reset` (он сносит таблицы вместе с правами, а применённую миграцию
  не повторяет) права возвращает. Новая таблица данных, которую модель должна видеть, —
  в этот файл и в `ai_stats.AI_TABLES` (тест `TestAiReaderRole` сверяет списки). Откат 0015 в одной
  базе роль не удаляет, пока права на неё остались в другой.
  **Выкатка на `nhl_dev`:** миграция применяется сама сервисом `migrate` при `up` (dev-стенд,
  §«Dev-стенд»); на `postgres` (прод) — только мержем `develop` → `master`. Переменная бота
  (в `.env` стека): `AI_PG_USER` — роль (по умолчанию `nhl_ai_reader`); хост, порт и база — те же
  `PG_*`. Контрольный прогон: `make ai-stats-eval` (нужны `ANTHROPIC_API_KEY` и база с миграцией).
- **Премиум `premium_access` (миграция 0016, Задача 96).** Премиум-чату — ИИ 20 вопросов в сутки
  и прогноз модели в превью; остальным — 3 вопроса в неделю, не больше 8 всего. Админской команды
  нет: выдаёт и продлевает человек SQL-ом в каждой базе отдельно (`nhl_dev` — dev-бот, `postgres` —
  прод). `chat_id` личного чата: написать боту любой вопрос и найти его в журнале —
  `SELECT chat_id, question, asked_at FROM ai_questions ORDER BY asked_at DESC LIMIT 5;`.
  Выдать или продлить (бессрочного нет — дату ставит человек):

  ```bash
  cd /opt/nhl_bot   # контейнер db — в стеке прода, у dev-стека своей базы нет
  sudo -u deploy docker compose exec -T db psql -U postgres -d nhl_dev -c \
    "INSERT INTO premium_access (chat_id, expires_at) VALUES (123456789, '2027-01-01 00:00+03')
     ON CONFLICT (chat_id) DO UPDATE SET expires_at = EXCLUDED.expires_at"
  ```

  Снять — `DELETE FROM premium_access WHERE chat_id = …` (или срок в прошлом).

  Для живой проверки на dev-боте — `scripts/dev_premium.sh`, с ноутбука (нужен `gcloud` с доступом
  к `nhl-bot-prod`; заходит на VM по SSH, меняет только `nhl_dev`, прод не трогает):

  ```bash
  bash scripts/dev_premium.sh grant  <chat_id>   # премиум на 2 часа; повтор — снова 2 часа от текущего момента
  bash scripts/dev_premium.sh revoke <chat_id>   # снять сразу
  bash scripts/dev_premium.sh status <chat_id>   # срок по МСК и активен ли, без изменений
  ```

  После `grant` и `revoke` скрипт печатает строку чата (после `revoke` — пустую). Признак бот читает
  на каждом запросе, перезапуск не нужен. Проверка: с премиумом в превью `/tonight` есть «Оценка
  модели» и ИИ отвечает до 20 раз в сутки; без премиума строки нет, а 4-й вопрос за неделю МСК
  получает отказ.

  Кто пользуется ботами и у кого премиум — `bash scripts/bot_users_report.sh` (тоже с ноутбука).
  Обе базы, только чтение (`default_transaction_read_only=on`): последняя миграция; по каждому
  чату — активные подписки и вопросы ИИ всего / за неделю / за сегодня МСК (без сбоев API);
  действия за 30 дней из `bot_actions`; `premium_access`. Имён бот не хранит — только `chat_id`.
  Вывод — на экран и в `$TMPDIR/bot_users_<дата>.log`. Таблицы, до которых база ещё не
  доросла (на проде до релиза — `premium_access`, `bot_actions`), дают ошибку только своего
  блока, остальные выводятся.

  Откат 0016 удаляет `premium_access` и восстанавливает список текущих пользователей миграции 0012 по подпискам.

## Структура проекта

| Область | Назначение |
|--------|------------|
| `pipeline/` | Загрузка NHL API → PostgreSQL |
| `telegram_bot/` | Telegram-бот; запросы к БД — в том числе `telegram_bot/queries/*.sql` |
| `modeling/` | Сборка датасетов, CLI |
| `data_tables/` | DDL таблиц; `data_tables/migrations/` — точечные изменения схемы (`make db-migrate`) |
| `docs/` | Описание пайплайнов и архитектуры |
| `plan/` | Черновики планов (не дублируем договорённости из этого файла без обновления) |

## Зависимости и изменения кода

- **Источник истины — `requirements.in` / `requirements-dev.in` / `requirements-modeling.in`.**
  Три `requirements*.txt` — это lock-файлы, сгенерированные из соответствующих `.in`
  через `pip-compile` (пакет `pip-tools`, ставится вместе с `requirements-dev.txt`):
  полный список транзитивных зависимостей, версии зафиксированы, у каждого пакета —
  хеши (`--generate-hashes`), которые pip проверяет при установке. **`.txt`-файлы руками
  не редактировать** — правка потеряется при следующей перегенерации, а хеши для
  вручную вписанной версии никто не посчитает.
  Чтобы добавить, поднять или убрать зависимость: правишь нужный `.in` → `make lock`
  (перегенерирует все три `.txt` сразу, в порядке рантайм → modeling → dev, чтобы слои
  оставались взаимно совместимы — CI ставит их одной pip-командой) → коммитишь `.in` и
  `.txt` вместе. `make lock` без `--upgrade`-подобных флагов держит уже закоммиченные
  версии транзитивных пакетов, если они всё ещё подходят под требования `.in` — двигается
  только то, что реально нужно менять.
  Пакеты приложения — в `requirements.in`; инструменты CI/линтера — в `requirements-dev.in`,
  по необходимости с коротким комментарием «зачем».
  **Известный косметический баг pip-tools 7.6.1:** в шапке каждого сгенерированного
  `.txt` (`# by the following command: pip-compile ... --no-index ...`) значится
  `--no-index`, хотя фактически `pip-compile` резолвит через реальный PyPI-индекс —
  проверено контрольным прогоном с пустым `PIP_CACHE_DIR` на заведомо незакэшированном
  пакете. Флаг никому не передавался и не влияет на результат; это не повод отлаживать
  «почему offline-резолв не падает».
  Dev-слой (`requirements-dev.txt`) собран с `--allow-unsafe` — без него `pip`/`setuptools`/
  `wheel` остались бы без хешей в графе транзитивных зависимостей `pip-tools`, а строгий
  hash-режим на пакетах без хеша падает. Следствие: `pip==26.2.1`, `setuptools==84.0.0` и
  `wheel==0.48.0` тоже запинингованы лок-файлом, и установка dev-слоя (`make setup-dev`,
  CI `quality`) приводит их к этим версиям — это ожидаемо, а не баг; поднимаются они, как и
  всё остальное, через `make lock`.
- **Зависимости моделирования** — отдельный файл `requirements-modeling.txt` (источник —
  `requirements-modeling.in`): семь ML-пакетов этапа 3 (LightGBM, scikit-learn, matplotlib и др.) плюс `pydantic` v2 для `modeling/config.py` (этап 2). Установка: `make modeling-dev`. Они **не** входят в `requirements.txt`, **не** ставятся через `make setup` / `make run-bot` и **не** попадают в Docker-образ бота. CatBoost в v1 не используется (опционально — этап 15 UPDATE-плана). Подробнее: [`docs/modeling_training.md`](docs/modeling_training.md#dependencies).
- **Async-тесты бота.** Хендлеры `telegram_bot/` — корутины (aiogram 3), поэтому тест, который вызывает хендлер напрямую, объявляется `async def` и помечается `@pytest.mark.asyncio` (`pytest-asyncio` в `requirements-dev.txt`, строгий режим по умолчанию — конфиг-файла pytest в репозитории нет). В `unittest.TestCase` этот маркер не работает: такие классы наследуем от `unittest.IsolatedAsyncioTestCase` (stdlib). Забытый маркер строгий режим не пропускает молча — тест будет пропущен с предупреждением.
- Следуем стилю существующих модулей (импорты, имена, обработка ошибок) вместо разнобоя.
- Большие бинарные артефакты и выгрузки данных не коммитить без явной договорённости (см. `.gitignore`).

## CI

Файл `.github/workflows/ci.yml` (runner `ubuntu-24.04`, Python 3.11), два job. Тот же workflow вызывает `release.yml` перед сборкой образа (`workflow_call`) — см. «Прод-деплой»:

- **`quality`** — установка `requirements.txt`, `requirements-dev.txt` и `requirements-modeling.txt` (иначе `tests/test_modeling_*.py` не собираются — нет `sklearn`/`lightgbm`/…), затем **Ruff** (`telegram_bot`, `modeling`, `pipeline`), **mypy** (те же каталоги, настройка в `mypy.ini`), `compileall`, **pytest** без `tests/test_db_nhl.py`. Без БД, гоняется на каждый PR быстро.
- **`db-tests`** — поднимает service-контейнер `postgres:16.6-alpine` (trust-аутентификация, без пароля, как в `docker-compose.yml`), затем `make setup`, `make db-sync` (применяет `DDL_TABLES`, потом SQL-функции, потом `data_tables/migrations/*.up.sql` — тот же порядок, что `make db-init`) и `make test-db` (схемные проверки `tests/test_db_nhl.py`). Данные в этой БД не загружаются, поэтому `test-db-data` тут не вызывается.
