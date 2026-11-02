# Задача 99. Журнал действий пользователей и история подписок

**Блок:** продукт · **Надобность:** 🟡
**Область:** миграция `0017` (таблицы `bot_actions`, `bot_subscription_events`, триггер на
`bot_subscriptions`), новая outer-middleware в `telegram_bot/bot.py` (`build_dispatcher`) или рядом с
`throttle.py`, `telegram_bot/ai_router.py` (отказ по лимиту), `telegram_bot/subscription_repo.py`
(`deactivate_chat`), `docs/telegram_bot.md`, `docs/architecture.md`.
**Зависит от:** ни от чего; миграция идёт после 0016 (Задача 96).
**Источник:** обсуждение с человеком 2026-10-31; реестр — [`../closed_tasks.md`](../closed_tasks.md).

## Кратко

Писать в БД то, что бот сейчас теряет: команды, нажатия кнопок, отказы ИИ по лимиту и блокировку
бота — одна таблица `bot_actions`. Историю включения и выключения подписок пишет триггер в
отдельную `bot_subscription_events`. Вопросы к ИИ не дублируются: они уже в `ai_questions` и
`ai_stats_log`.

## Зачем и на что влияет

Сейчас в БД есть только вопросы к ИИ (`ai_questions`, `ai_stats_log`) и текущее состояние подписок.
Поэтому не ответить на три вопроса:

- **Сколько у бота пользователей и сколько возвращается.** Кто смотрит статистику без подписок и
  без ИИ, в БД не оставляет следа вообще; «список пользователей» сейчас — объединение подписок и
  ИИ-таблиц (ручной снимок прода 2026-10-31).
- **Какими экранами пользуются, а какие никто не открывает** — что развивать, а что удалять.
- **Воронка к премиуму (96, 97):** что человек делает до того, как упирается в лимит ИИ. Сам отказ
  по лимиту не пишется нигде (миграция 0012: «отказ по лимиту модель не вызывает и строки не
  пишет»).

Плюс история подписок: строка `bot_subscriptions` перезаписывается на месте (`subscription_repo.py`),
так что «поставил → снял → поставил» не видно; `updated_at` двигает и рассылка (`last_sent_night`),
а отписку не отличить от блокировки бота — обе гасят `active`.

## Степень важности

🟡: продуктовая аналитика, ничего не блокирует. Ценность растёт со временем — журнал не
восстановить задним числом, поэтому раньше лучше, чем позже (особенно до продаж 97).

## Подробно

**Состояние на 2026-10-31** (`origin/develop` = `f99229c`).
- Логирование — только `logging` в stdout на INFO (`bot.py:627`): служебные события и ошибки,
  ни команд, ни нажатий.
- Троттлинг нажатий — outer-middleware `callback_query` (`throttle.py`,
  `bot.py: build_dispatcher`), держит метки только в памяти.
- Кнопки подписок — переключатели (`SUBSCRIPTIONS_CALLBACK_PATTERN = ^sub:(m|d|u|T|C|x|D:…|t:…|c:…)$`,
  `bot.py:232`): по `callback_data` видно «нажал», но не «включил» или «выключил».
- Блокировка бота — `subscription_repo.deactivate_chat` (`subscription_repo.py:247`), вызывается
  из `push_digest_job.py` на `TelegramForbiddenError` (три места).

**Что делать.**
1. **Миграция 0017.** Только миграция, без `data_tables/t.*.sql` — как `ai_questions`: журнал не
   восстановить повторной загрузкой.
   - `bot_actions(id bigserial PK, chat_id bigint NOT NULL, user_id bigint, at timestamptz NOT NULL
     DEFAULT now(), kind text NOT NULL CHECK (kind IN ('command', 'callback', 'ai_limit', 'blocked')),
     data text)`, индекс `(chat_id, at)`. `user_id` — `from_user.id` апдейта (решение человека
     2026-10-31): в группе чат общий, а действует конкретный человек. У `blocked` он `NULL` — это
     событие чата, а не человека.
   - `bot_subscription_events(id bigserial PK, subscription_id bigint NOT NULL, chat_id bigint NOT NULL,
     kind text NOT NULL, team_id bigint, country text, active boolean NOT NULL,
     at timestamptz NOT NULL DEFAULT now())`.
   - Триггер `AFTER INSERT OR UPDATE OF active ON bot_subscriptions`: пишет строку при вставке и при
     **фактической смене** `active` (`OLD.active IS DISTINCT FROM NEW.active`). Отметка рассылки
     (`last_sent_night`) событий не даёт. Python в `subscription_repo.py` не меняется — покрыты все
     пути записи сразу.
   - Down-миграция удаляет триггер, функцию и обе таблицы.
2. **Что пишется в `bot_actions`.**

   | `kind` | Где | `data` |
   |---|---|---|
   | `command` | outer-middleware на `message`: только текст, начинающийся с сущности `bot_command` (как `is_plain_text` в `bot.py`) | имя команды без `/` и `@бот`, в нижнем регистре: `start`, `help`, `today`, `tonight`, `standings`, `team`, `team_<abbr>`, `stats`, `team_stats`, `countries`, `subscriptions`; неизвестная команда пишется как есть |
   | `callback` | outer-middleware на `callback_query`, **после** троттлинга (регистрируется вторым — отброшенные нажатия не пишутся) | `callback_data` как есть: экраны статистики, раскрытие игры дайджеста, `tn:`, `cn:`, `gv:`, `sub:` |
   | `ai_limit` | `bot.py` (`on_free_text`), где `ai_router.limit_refusal` вернул отказ | какой лимит: `day` (премиум, сутки) / `week` / `total` |
   | `blocked` | `subscription_repo.deactivate_chat` | — |

   Свободный текст к ИИ **не пишется**: он целиком в `ai_questions` / `ai_stats_log` (GC3 — не
   дублировать).
3. **Middleware** — одна функция на оба типа апдейта, по образцу `enforce_callback_rate_limit`:
   пишет строку (`chat_id`, `user_id`) и передаёт апдейт дальше. Запись в БД синхронная (`get_connection`) — вызывать через
   `asyncio.to_thread`, как остальные обращения к БД из хендлеров. Ошибку записи не глушить (GC4):
   БД у бота одна, без неё и сам хендлер не ответит.
4. **Документация.** `docs/telegram_bot.md` — раздел «Журнал действий»: что пишется, чего нет и
   почему (прочтение сообщений Telegram Bot API не отдаёт), как отличить блокировку от отписки
   (`blocked` в `bot_actions` и одновременное гашение всех подписок чата в
   `bot_subscription_events`), 2–3 примера запросов: активные за неделю, популярность экранов,
   действия перед `ai_limit`. `docs/architecture.md` — миграция 0017.

**Решения человека 2026-10-31.**
1. В `bot_actions` пишется и `user_id`, не только `chat_id`.
2. Журнал не чистится — ни срока хранения, ни задания очистки.

**Критерии приёмки.**
- Тесты (`feed_update` собранного `Dispatcher`): команда даёт строку `command` с нормализованным
  именем, `chat_id` и `user_id`; нажатие — строку `callback` с `callback_data`; нажатие, отброшенное троттлингом, строки
  не даёт; свободный текст строки в `bot_actions` не даёт; отказ по лимиту — `ai_limit` с видом
  лимита; `deactivate_chat` — `blocked`.
- Триггер на БД (`tests/test_db_nhl.py`): вставка подписки — событие; выключение и повторное
  включение — ещё два; `last_sent_night` и повторная запись того же `active` — ни одного.
- Миграция 0017 вверх и вниз на БД (`make all-tests`; `tests/test_db_nhl.py` прогнать самому — CI
  гоняет его отдельно).
- Живая проверка на dev-боте: пройтись по командам, кнопкам и подпискам, убедиться запросом, что
  строки на месте.
- `make ci-local` зелёный; прогон pytest с `PG_PORT=1`, как в CI.

**Подводные камни.**
- Порядок outer-middleware у `callback_query`: aiogram вызывает их в порядке регистрации — журнал
  ставить после троттлинга, иначе в него попадут отброшенные нажатия.
- Тесты бота без БД (`PG_PORT=1` в CI): middleware ходит в БД на каждый апдейт — существующие
  тесты `feed_update` её подменяют, иначе упадут все сразу.
- На проде миграции 0016 ещё нет (прод на 0014): 0017 туда поедет вместе с ней одним релизом.
- Бюджет GC6: оценка ~150 строк с тестами.
