# Запуск Telegram-бота

Запуск бота на локальной машине после того, как уже подняты PostgreSQL и
загружены данные.

Установка окружения и загрузка данных — отдельный документ:
[`data_loading.md`](data_loading.md).

## Требования

- **Python 3** (в проекте используется venv `.venv`).
- **Токен Telegram-бота** от [@BotFather](https://t.me/BotFather).
- **PostgreSQL** с теми же `PG_*`, что и у лоадера, и **уже загруженными
  данными** (см. `data_loading.md` §4 «Загрузка данных NHL»).
- Сеть для запросов к Telegram.

## Быстрый старт (кратко)

Предполагаем, что окружение и БД уже подняты по `data_loading.md`:

```bash
# В .env должен быть TELEGRAM_BOT_TOKEN
make bot
```

В Telegram отправьте боту `/stats`.

---

## 1. Переменные окружения (`.env`)

Бот использует следующие переменные:

| Переменная | Назначение |
|------------|------------|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather; **обязателен** для `make bot`. |
| `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_DATABASE` | Те же `PG_*`, что использует лоадер. |
| `SEASON_ID` | Сезон, по которому бот фильтрует ответы (`config.SEASON_ID`). |
| `CURRENT_SEASON` | Подпись сезона в сообщениях, например `25/26`. |
| `ENABLE_PUSH_DIGEST` | `0`/`1` — разрешить скрипт рассылки `push_digest_job.py` (по умолчанию выкл.). |
| `PUSH_SEND_INTERVAL_SEC` | Пауза между сообщениями при массовой отправке (по умолчанию `0.05`). |

Модуль `telegram_bot/config.py` читает эти переменные. `Makefile` подключает
`.env` автоматически.

---

## 2. Запуск

### 2.1. Базовый

```bash
make bot
```

Цель `check-token` проверяет, что `TELEGRAM_BOT_TOKEN` не пустой; затем
выполняется `telegram_bot/bot.py` через интерпретатор из `.venv`.

### 2.2. С автоматической подготовкой окружения

```bash
make run-bot    # setup + env-example + bot
make run-local  # синоним run-bot
```

Полезно при первом запуске или после `git pull`, когда могли измениться
зависимости.

### 2.3. Полный цикл «с нуля»

```bash
make setup env-example
# заполнить .env (TELEGRAM_BOT_TOKEN, PG_*, SEASON_ID, CURRENT_SEASON)
make db-reset-local
make season-load-full
make bot
```

Пересоздаёт venv, пересоздаёт схему БД, прогоняет актуальный NHL-пайплайн
(`load_season_modern.py`) и поднимает бота. Подробности по `season-*`
целям и окнам дат — в [`data_loading.md`](data_loading.md).

---

## 3. Тесты бота

```bash
make test-skater-bot   # текст лидербордов, без БД
make test-fast         # все pytest-тесты, кроме требующих PostgreSQL
make all-tests         # test-fast + test-db (с поднятой БД)
```

Тесты бота находятся в `tests/test_skater_reports_bot.py`,
`tests/test_nhl_scoreboard.py`, `tests/test_pipeline_optional_helpers.py`.

---

## 4. Команды бота

Список команд формируется в `telegram_bot/help_text.py` и регистрируется в
`telegram_bot/bot.py`. Краткий список:

| Команда | Что делает |
|---------|------------|
| `/start`, `/help` | Стартовое сообщение и список команд. |
| `/stats` | Главное меню статистики. |
| `/day_games` | Игры за выбранную дату. |
| `/tonight` | Сегодняшние игры (по календарю Москвы). |
| `/table` | Турнирная таблица сезона. |
| `/leaders` | Топ игроков по очкам / голам / передачам. |
| `/advanced` | Топ по advanced-статам (Corsi / Fenwick / GF% / OZ-старт / shootout %). |
| `/game` | Детальная карточка игры по `game_id`. |
| `/cancel` | Прервать диалог. |
| `/subscribe_digest`, `/unsubscribe_digest` | Opt-in / отписка от утренней рассылки дайджеста (`push_digest_job.py`). |
| `/subscribe_team`, `/unsubscribe_team` | Подписка на краткие итоги игр команды по аббревиатуре сезона. |

Подробный пользовательский сценарий: [`user_journey_stats.md`](user_journey_stats.md).

---

## 5. Типичные проблемы

| Симптом | Что проверить |
|---------|----------------|
| Бот не стартует | `TELEGRAM_BOT_TOKEN` (не пустой и валиден у @BotFather), доступ к сети, параметры `PG_*`. |
| `ConnectionRefusedError` к Postgres | Что PostgreSQL запущен, `PG_*` совпадают с тем, что использовал лоадер. |
| «Нет данных за сегодня» / пустые таблицы | Сделать загрузку: `make season-sync-month` (или нужное окно дат) — см. [`data_loading.md`](data_loading.md). |
| Лидерборды пустые | Проверить, что `SEASON_ID` в `.env` совпадает с `season_id` загруженных данных (`SELECT DISTINCT season_id FROM games`). |
| `Unknown` вместо имени игрока в карточке гола | Игрок отсутствует в `rosters` для этого `season_id`; будет добавлен на следующей загрузке через дополнение из landing-эндпоинта. |

---

## 6. Связанная документация

- [`data_loading.md`](data_loading.md) — установка окружения, БД, загрузка данных NHL.
- [`pipeline_nulls_and_explicit_null_tz.md`](pipeline_nulls_and_explicit_null_tz.md) — контракт `NULL`-семантики; важно для понимания, почему бот в некоторых местах рендерит `—` или «Unknown».
- [`architecture.md`](architecture.md) — общая архитектура и схема таблиц БД.
- [`api_data_research.md`](api_data_research.md) — какие поля NHL API маппятся в какие колонки.

---

## 7. Подписки и push (`bot_subscriptions`)

Модель данных и ограничения уникальности — в docstring
[`push_subscriptions.py`](../telegram_bot/push_subscriptions.py).

Создание таблицы:

```bash
make db-bot-subscriptions
```

Поля: `chat_id`, `kind` (`morning_digest` | `team_scores`), `team_id` (для команды),
опционально `timezone`, `active`, временные метки. Согласие — команды
`/subscribe_digest`, `/subscribe_team`; отписка — `/unsubscribe_digest`,
`/unsubscribe_team` (см. `/help`).

Рассылка: [`push_digest_job.py`](../telegram_bot/push_digest_job.py),
та же логика что `day_digest` / `dispatch_day_digest_messages`, с
`attach_conv_nav_on_last=False`. Включение: `ENABLE_PUSH_DIGEST=1`, паузы
`PUSH_SEND_INTERVAL_SEC`, обработка `429` и блокировки бота — см. исходники скрипта.

Это отдельный процесс из cron, а не задача внутри бота: скрипт поднимает свой
`Application` (без polling и без `JobQueue`), рассылает и завершается, поэтому
запускать его можно и при выключенном боте. Поведение закреплено в
`tests/test_push_digest_job.py`.

```bash
cd telegram_bot && ENABLE_PUSH_DIGEST=1 ../.venv/bin/python push_digest_job.py
```
