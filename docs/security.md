# Безопасность NHL_bot: аудит 2026-11-02 (Задача 82)

Что проверено в стеке (бот на aiogram, Postgres и сервисы в Docker Compose на одной VM GCP,
деплой через GitHub Actions, Claude API), что нашлось и какое решение принято по каждой находке.
Это снимок на 2026-11-02, а не постоянная гарантия. Секреты проверялись только по форме и
счётчикам (`grep -c`, длина, совпадение sha256, `getMe` → `ok`/код ошибки); значения нигде не
печатались. Исходный список кандидатов — карточка
[`plan/tasks/task_82_security_research.md`](../plan/tasks/task_82_security_research.md).

Статусы: **исправлено** — в Задаче 82 · **задача N** — исправление заведено в
[`plan/open_tasks.md`](../plan/open_tasks.md) · **принято** — риск принят, причина указана ·
**снято** — проверка не подтвердила проблему · **человек** — нужен шаг, который агент не делает
(BotFather, консоль Anthropic, закрытие алерта).

## Классы уязвимостей и источники

| Класс | Что проверялось | Источник |
|---|---|---|
| Секреты в логах и трейсбэках | URL Bot API содержит токен; логгеры HTTP-клиентов; текст исключений | [Bot API: авторизация](https://core.telegram.org/bots/api#authorizing-your-bot), [httpx: logging](https://www.python-httpx.org/logging/), [aiogram: aiohttp session](https://docs.aiogram.dev/en/latest/api/session/aiohttp.html), [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) |
| Хранение секретов и данных на хосте | `.env`, `Config.Env` контейнеров, логи docker, бэкапы | [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/), [json-file driver](https://docs.docker.com/engine/logging/drivers/json-file/), [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) |
| Доступ контейнеров к БД и хосту | `pg_hba`, общая сеть прод- и dev-стека, группа `docker` | [PostgreSQL: trust](https://www.postgresql.org/docs/16/auth-trust.html), [Docker daemon attack surface](https://docs.docker.com/engine/security/#docker-daemon-attack-surface) |
| Облачные права VM | сервисный аккаунт VM, metadata-сервер из контейнеров | [GCP: default service account](https://cloud.google.com/compute/docs/access/service-accounts#default_service_account) |
| CI/CD и цепочка поставки | права `GITHUB_TOKEN`, окружение `production`, форки, actions и образы по тегам, CVE | [GitHub Actions: security hardening](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions), [Environments: deployment branches](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment), [Secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning) |
| Публичный репозиторий | секреты в истории git | то же, secret scanning |
| Сеть VM | файрвол GCP, слушающие порты, `sshd` | — |
| Ввод пользователя и ИИ | SQL, prompt injection в text-to-SQL (83), подмена `callback_data`, платежи Stars | [OWASP LLM01: Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), [Bot API: payments](https://core.telegram.org/bots/payments-stars) |
| Персональные данные | `chat_id` и тексты вопросов в логах и таблицах | OWASP Logging Cheat Sheet |

Ресёрч добавил к списку карточки классы «облачные права VM», «публичный репозиторий»,
«text-to-SQL», «платежи и `callback_data`», «образы по тегам» — находки N1–N10 ниже. Вебхуков у бота
нет (long polling), входящей HTTP-поверхности на VM нет — этот класс снят.

## Кандидаты 1–16 из карточки

| # | Находка | Проверка 2026-11-02 | Решение |
|---|---|---|---|
| 1 | Токен в URL от `httpx` INFO | После переезда на aiogram (aiohttp) фикс `httpx → WARNING` ушёл вместе с PTB. `AiohttpSession` URL не логирует; `httpx` остался только у SDK Anthropic — пишет `POST https://api.anthropic.com/v1/messages`, ключ в заголовке. В логах всех 9 контейнеров VM — 0 строк вида токена и ключа (контейнеры пересозданы релизом 2026-11-02, логи с утечкой 10-25 ушли вместе со старыми). Оба токена (прод и dev) с тех пор не менялись и действуют | **снято** (код); ротация — **задача 113**, **человек** |
| 2 | PTB `InvalidToken` с токеном в тексте | PTB заменён на aiogram 3.31: `validate_token` пишет «Token is invalid!» без значения, отказ сервера — `TelegramUnauthorizedError` «Telegram server says - Unauthorized» | **снято** |
| 3 | `logger.warning(..., exc)` в `push_digest_job.py` | `TelegramAPIError` — «Telegram server says - …» без URL; `TelegramNetworkError` — имя и текст `aiohttp.ClientError` (хост:порт, без пути). URL с токеном в тексте ошибки aiohttp бывает только при `raise_for_status` в `stream_content` (скачивание файлов) — бот файлы не скачивает | **снято** |
| 4 | `scheduled_sync.py` логирует argv | argv — интерпретатор, скрипт, `--date-from/--date-to`, `--upcoming-only`; секреты дочерним процессам идут окружением | **снято** |
| 5 | Токены в транскриптах Claude Code | В `~/.claude/projects/` 8 вхождений действующего токена dev-бота (sha256 совпадает с секретом) и 35 вхождений ещё одной строки вида токена — она не совпадает ни с одной версией секретов; проверить её `getMe` агенту не разрешено. Ключа Anthropic в транскриптах нет | ротация dev — **задача 113**; вторая строка — **человек** |
| 6 | Логи docker без ротации | `/etc/docker/daemon.json` нет, драйвер `json-file` без `max-size` | **исправлено**: `x-logging` в `docker-compose.yml` (10 МБ × 3 на контейнер), действует и в dev-оверрайде; на VM — с первым деплоем |
| 7 | Бэкапы `644` | 15 дампов `root:root 644`; на VM два пользователя, оба в группе `docker` (то есть root) | **исправлено**: `umask 077` в сервисе `backup`, существующие дампы — `chmod 600` на VM. Шифрование дампов — **принято**: читать их может только root, диск шифрует GCP |
| 8 | `/opt/nhl_bot_dev/.env` — копия продового | В обоих `.env` только `SEASON_ID`, `BACKUP_DIR`, `ENABLE_PUSH_DIGEST`, `COMPOSE_*`, `NHL_BOT_*IMAGE`; строк вида токена и ключа — 0. Права `600 deploy`. Но dev получает **тот же ключ Anthropic**, что прод (общий секрет `nhl-bot-anthropic-key`) | `.env` — **снято**; общий ключ — **задача 111** |
| 9 | `trust` в Postgres, dev-стек в сети прода | Подтверждено: `host all all all trust`, контейнеры `nhl_bot_dev-*` в сети `nhl_bot_default`; код ветки `develop` входит в продовую базу `postgres` суперпользователем без пароля | **задача 111** |
| 10 | `deploy` в группе `docker`, ключ деплоя в GitHub | Подтверждено. Плюс: юнит `nhl-bot-secrets` при загрузке VM запускает **от root** `fetch-secrets.sh` из чекаута dev (`/opt/nhl_bot_dev`, владелец `deploy`) — код ветки `develop` исполняется как root | группа `docker` и ключ — **принято** (деплой без root невозможен; окружение теперь ограничено ветками, см. N5); скрипт из dev-чекаута — **задача 111** |
| 11 | Actions по тегам, а не SHA | Подтверждено: `checkout@v4`, `setup-python@v5`, `docker/*@v3/v6`; `sha_pinning_required: false` | **задача 112** |
| 12 | Секреты `production` из форков | `ci.yml` — `pull_request` (форкам секреты не выдаются), `release.yml` — только `push` в `master`/`develop` и теги. Но у окружения `production` не было ни правил, ни ограничения веток: деплой с ключом мог запустить workflow любой ветки | **исправлено**: окружению разрешены только `master`, `develop`, теги `v*` |
| 13 | Нет проверки CVE | Dependabot alerts были выключены | **исправлено**: Dependabot alerts включены (на 2026-11-02 открытых 0); обновление пинов actions — **задача 112** |
| 14 | RDP открыт миру, SSH миру, LLMNR | `default-allow-rdp` `tcp:3389` с `0.0.0.0/0`; SSH — только ключи (`permitrootlogin no`, `passwordauthentication no`); `5355` слушает `0.0.0.0`, снаружи закрыт файрволом | RDP — **исправлено** (правило удалено); SSH и LLMNR — **принято**: ключи и файрвол достаточны для одной VM, IAP-туннель ломает деплой из Actions |
| 15 | SQL и prompt injection | Запросы бота параметризованы; f-строки только из констант (`ai_router.py`, `ai_stats.py` — списки колонок журнала). SQL, который пишет модель (83), идёт ролью `nhl_ai_reader`: `SELECT` только на таблицы данных NHL (без `bot_*`, `ai_*`, платежей), read-only транзакция, запрет `;`, обёртка-подзапрос, таймаут сервера и клиента. Текст вопроса в лог процесса не пишется (в логе — `chat_id` и `request_id`), он хранится в `ai_questions`/`ai_stats_log`; ключ Anthropic в исключениях SDK не печатается | **принято** |
| 16 | `chat_id` в логах и таблицах | `chat_id` — в предупреждениях о неудачной отправке (на VM 0 строк), в таблицах подписок, действий (99), расписаний, платежей, вопросов к ИИ; тексты вопросов уходят в Claude API | **принято**: данные нужны функциям бота, третьих сторон, кроме Anthropic, нет; логи теперь ротируются (6). Сроки хранения пересмотреть при росте аудитории |

## Новые находки (ресёрч)

| # | Находка | Проверка | Решение |
|---|---|---|---|
| N1 | Репозиторий **публичный**, в истории — токен бота (`telegram_bot/buttons.py`, коммит `0361cb5`, 2022; удалён в 2026-03) | `getMe` → 401: токен отозван. Это же нашёл secret scanning (алерт #1) | **принято** (токен мёртв, историю не переписываем); алерт #1 закрыть как «revoked» — **человек** (агенту запись в алерты не разрешена) |
| N2 | Secret scanning и push protection выключены | — | **исправлено**: включены |
| N3 | VM работает под default compute SA с ролью **`roles/editor`** на проект и scope `cloud-platform`; metadata-сервер доступен из контейнеров (из `nhl_bot_dev-bot-1` — 200) | Любой код в контейнере (уязвимость зависимости, ветка `develop`) получает токен редактора проекта: менять metadata VM (SSH-ключи → root), создавать ресурсы за деньги владельца | **задача 110** |
| N4 | `GITHUB_TOKEN` по умолчанию с правом записи | `default_workflow_permissions: write`; `ci.yml` прав не задаёт | **исправлено**: по умолчанию `read`, одобрение PR из Actions запрещено (`release.yml` задаёт права сам) |
| N5 | Окружение `production` без ограничения веток | см. 12 | **исправлено** |
| N6 | Образы по тегам (`postgres:16.6-alpine`, база `Dockerfile`), не по digest | Подмена тега в реестре = чужой код с доступом к БД | **задача 112** |
| N7 | Старые версии секретов включены | Токен прода: v1 (enabled) = текущий токен dev-бота; dev v1/v2 отозваны в Telegram, но enabled; ключ Anthropic — 3 enabled версии, отозваны ли старые в консоли, не видно | **задача 113** |
| N8 | Подмена `callback_data` (кнопки шлёт клиент) | Удаление расписания — `WHERE id = %s AND chat_id = %s`; подписки и премиум — по `chat_id` отправителя | **снято** |
| N9 | Платежи Stars: `pre_checkout` подтверждается без проверки, payload и валюта не сверяются | `successful_payment` присылает только Telegram, счёт выставляет только бот; подделать платёж клиент не может | **принято** |
| N10 | Защита веток без `enforce_admins` и ревью | Единственный участник — владелец; PR и зелёный CI обязательны | **принято**; 2FA на GitHub-аккаунте — проверить **человеку** (компрометация аккаунта = root на VM через ключ деплоя) |

## Что сделано в Задаче 82

- `docker-compose.yml`: ротация логов `json-file` 10 МБ × 3 у всех сервисов; `umask 077` у `backup`.
- VM: существующие дампы `/srv/nhl_bot_backups/nhl_*.dump` → `600`.
- GCP: удалено правило файрвола `default-allow-rdp`.
- GitHub: secret scanning, push protection, Dependabot alerts включены; окружение `production` —
  только `master`, `develop`, теги `v*`; `GITHUB_TOKEN` по умолчанию `read`.

## Шаги человека

1. Ротировать токены прод- и dev-бота (`/revoke` в BotFather) — порядок в задаче 113.
2. Закрыть алерт secret scanning #1 как «revoked» (токен 2022 года, `getMe` → 401).
3. Проверить, что за вторая строка вида токена в транскриптах (35 вхождений, не совпадает ни с
   одной версией секретов); действующую — отозвать.
4. В консоли Anthropic: отозвать старые ключи (в Secret Manager три версии), поставить лимит
   расходов; отдельный ключ для dev — задача 111.
5. Проверить, что на GitHub-аккаунте включена 2FA.
