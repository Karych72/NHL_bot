# Архитектура NHL_bot — карта проекта

Карта: что где лежит и кто кого вызывает. Подробности — в тематических документах (раздел
«Тема → документ») и в docstring-ах модулей; здесь их нет намеренно. Правило — в конце файла.

## Обзор

Telegram-бот со статистикой NHL. Python; PostgreSQL (`psycopg2`) — единственное хранилище;
бот на `aiogram` 3 (long polling), тексты сообщений — шаблоны Jinja2; данные грузит
`pipeline/` из NHL API; `modeling/` строит датасеты и обучает модели (sklearn, LightGBM).
Две долгоживущие ветки — два бота на одной VM (`develop` — dev-бот, `master` — прод): см.
`CLAUDE.md` §«Ветки и выкатка» и `DEVELOPMENT.md`.

## Дерево проекта

```
pipeline/                       загрузка данных и планировщики (отдельные сервисы)
  load_season_modern.py         загрузчик NHL API -> PostgreSQL (ModernNhlLoader, CLI, дисковый кэш ответов)
  scheduled_sync.py             ночной автосинк: вызывает загрузчик, окно времени и повторы
  scheduled_retrain.py          еженедельный retrain: пересборка датасета и обучение
  scheduled_publish.py          ежедневная публикация прогнозов в game_predictions
telegram_bot/                   бот: читает БД, рисует меню, рассылает
  bot.py                        точка входа: Dispatcher aiogram, роутеры, команды, тикер расписаний
  config.py                     общая конфигурация (токен, PG_*, параметры загрузчика); импортируют все слои
  database.py                   потокобезопасный пул соединений с statement_timeout, безопасное выполнение запросов, валидация идентификаторов
  bot_messages.py               дайджест дня: сводка матчей ночи, кнопки матчей, голы ночи
  game_messages.py              карточка матча и превью предстоящего (сравнение команд)
  team_messages.py              экраны клуба: форма и серии, профиль, полная статистика, список команд, составы
  leaderboard_messages.py       лидерборды игроков и команд, турнирная таблица, таблица группы игроков
  country_messages.py           экраны стран: рейтинг, страница, профиль, блоки игроков ночи
  rich_markup.py                примитивы rich-сообщений без БД: _table, rich_*, форматтеры, MSK, COUNTRY_LABELS, обрезка
  template_funcs.py             рендер шаблонов messages/*.txt через Jinja2
  stats_handlers.py             пагинация и callback-хендлеры /stats, /team_stats, дайджест, /countries
  leaderboard_specs.py          заголовки и раскладка кнопок таблиц лидеров
  keyboards.py                  помощники inline-клавиатур (сетка кнопок, нажатая кнопка)
  help_text.py                  тексты /start и /help, справочник команд COMMANDS
  nhl_scoreboard.py             NHL scoreboard API: живая афиша для /tonight
  video_replay.py               видео голов: NHL Replay API -> Brightcove -> MP4, кэш клипов
  throttle.py                   троттлинг: outer-middleware нажатий callback-кнопок, throttled для отправок рассылки
  subscription_repo.py          подписки на рассылку (bot_subscriptions): чтение, включение, отключение
  schedule_repo.py              расписания экранов (bot_schedules): расчёт следующей отправки
  push_digest_job.py            отдельный скрипт рассылок: итоги, дайджест, превью, посты в канал
  push_subscriptions.py         только docstring с дизайном push-подписок; кода и импортов нет
  premium.py                    премиум-доступ чата: лимиты ИИ и расписаний
  ai_router.py                  кэшированный ИИ-текст превью рассылки; свободный текст в личке: Claude выбирает экран или отвечает; лимит ИИ
  ai_stats.py                   ИИ-ответы на статистические вопросы: SQL под ролью nhl_ai_reader
  messages/                     шаблоны сообщений (*.txt, Jinja2)
  prompts/                      промпты ai_stats (*.md) и справка правил НХЛ для ai_router (nhl_rules.md)
  queries/                      SQL-функции БД (*.sql, ставит `make db-functions`)
modeling/                       датасеты и модели (БД читает dataset_builder, пишет publish_predictions)
  cli.py                        единая CLI: build-dataset, train, evaluate-goals, fit-goals, evaluate-xg,
                                promote, predict, publish-predictions, publish-goals
  config.py                     загрузка и разбор YAML-конфигурации обучения (configs/)
  dataset_builder/              сборка train/predict датасетов из БД
    __init__.py                 пакет
    base.py                     оркестрация режимов train/predict
    team_game_facts.py          канонические team-game факты и целостность пар
    features.py                 скользящие и матчевые признаки, строгие as-of join
    assemble.py                 сборка итоговых датасетов из снимков признаков
    validate.py                 проверки качества данных и анти-утечки
  feature_schema.py             общий манифест признаков и проверки паритета (без БД)
  train_input.py                загрузка артефактов датасета (CSV + metadata) для обучения
  splits.py                     посезонные временные сплиты
  train_common.py               общее для классификаторов: метки задач, потоки, сетки, monotone
  train_logreg.py               базовый классификатор: логистическая регрессия
  train_lgbm.py                 основной классификатор: LightGBM
  calibrate.py                  калибровка вероятностей без утечки
  elo.py                        Elo команд: признак diff_elo и бенчмарк
  metrics.py                    офлайн-метрики классификаторов
  bootstrap.py                  доверительные интервалы log loss и Brier
  report.py                     сборка и запись отчётов прогона
  artifacts.py                  сохранение и загрузка моделей (model.joblib + metadata.json)
  acceptance.py                 приёмка прогона: бенчмарк-гейт и его отражение в отчётах
  train_runner.py               оркестрация прогона обучения
  predict_runner.py             инференс latest-модели на predict-датасете
  publish_predictions.py        запись вероятностей в game_predictions
  goal_model.py                 голевая модель: Пуассон, сетка счётов, вероятность тотала
  xg_model.py                   модель ожидаемых голов (xG) по броскам
  __init__.py                   пакет
data_tables/                    схема БД: t.*.sql (таблицы и представления NHL), ai_reader_grants.sql,
                                migrations/ (NNNN_*.up.sql / .down.sql — таблицы бота и правки схемы)
configs/                        YAML-конфигурации modeling (default, smoke)
artifacts/                      выход modeling (датасеты, отчёты, модели)
deploy/                         compose dev/prod, systemd-юнит секретов, fetch-secrets.sh, models/
scripts/                        вспомогательные скрипты: оценки ИИ, фикстуры, dev-стенд, бэкфиллы (`backfill_goal_goalie.sh`), отчёты
tests/                          pytest-тесты и фикстуры NHL API (tests/fixtures/)
docs/                           тематические документы; docs/superpowers/ — планы и спецификации
plan/                           реестр задач (open_tasks.md), карточки (tasks/), архив (archive/)
```

## Поток данных

```
NHL API (api-web.nhle.com, api.nhle.com/stats)
   |  pipeline/load_season_modern.py   <-- pipeline/scheduled_sync.py (ночью)
   v
PostgreSQL: таблицы данных NHL (data_tables/t.*.sql)
   |                                         \
   | telegram_bot/bot.py                      \  modeling: dataset_builder -> train -> artifacts/
   |  (запросы, шаблоны, меню)                 \   ^ pipeline/scheduled_retrain.py (еженедельно)
   v                                            v
Telegram-пользователь       game_predictions <- publish_predictions <- pipeline/scheduled_publish.py (ежедневно)
   ^
   |  рассылки: telegram_bot/push_digest_job.py, его запускает scheduled_sync
   |            после каждого прогона (bot_subscriptions, bot_schedules)
   |  свободный текст: ai_router.py -> ai_stats.py -> БД под ролью nhl_ai_reader
```

## Таблицы БД

Источник истины — `data_tables/t.*.sql` и `data_tables/migrations/`; колонок здесь нет.

Данные NHL (`data_tables/t.*.sql`):
- `teams`, `teams_stats` — команды и их сезонная статистика
- `games`, `scheduled_games` — сыгранные игры и расписание будущих
- `game_team_stats`, `game_player_stats`, `game_goalie_stats` — командные, полевые и вратарские показатели игры
- `all_goals` (с вратарём гола `goalie_id`), `game_three_stars`, `shot_attempts` — голы, три звезды, попытки бросков
- `rosters` — составы
- `players_season_stats`, `goalies_season_stats`, `players_advanced_stats`, `players_shot_types` — сезонная статистика
- `game_predictions` — прогнозы вероятностей по играм
- `playoff_series` — представление: серии плей-офф
- `player_career_stats`, `goalie_career_stats` — представления итогов карьеры по игроку и типу игры
- `franchise_stats` — представление командных итогов по franchise_id и типу игры, с переездами;
  все три создаются db-tables и миграцией 0034, читаются ролью nhl_ai_reader

Бот и сервисные (`migrations/`):
- `bot_subscriptions`, `bot_game_sent`, `bot_chat_game_sent` — подписки на рассылку и учёт отправленного
- `bot_subscription_events` — журнал событий подписок (пишет функция `bot_subscription_event`)
- `bot_schedules` — расписания экранов
- `bot_actions` — журнал действий пользователей
- `bot_goal_clips` — кэш видеоклипов голов
- `bot_channel_posts`, `bot_channel_weekly_posts` — учёт постов в канал
- `premium_access`, `premium_payments` — премиум-доступ и платежи
- `ai_questions`, `ai_stats_log` — журналы ИИ-вопросов и ИИ-ответов по статистике
- `prediction_log` — журнал прогнозов и исходов
- `schema_migrations` — применённые миграции (ведёт `make db-migrate`)

Функции БД (`telegram_bot/queries/*.sql`): `get_game_stats`, `get_goals_game`, `get_goalies_game`,
`get_three_stars_game` — карточка матча; `player_display_name` — имя игрока у однофамильцев сезона.

## Тема -> документ или файл

| Тема | Где |
|---|---|
| Запуск бота, команды, подписки и push, ИИ-текст, журнал действий, расписания | `docs/telegram_bot.md` |
| Загрузка NHL API -> БД | `docs/data_loading.md` |
| Сбор датасета моделирования | `docs/modeling_dataset_builder.md` |
| Обучение, калибровка, приёмка, голевая и xG модели | `docs/modeling_training.md` |
| Премиум-доступ и оплата | `docs/premium.md` |
| Возможности ИИ бота | `docs/ai_capabilities.md` |
| Исследование данных NHL API | `docs/api_data_research.md` |
| Семантика NULL в пайплайне, явные default | `docs/pipeline_nulls_and_explicit_null_tz.md` |
| Окружение, Makefile-цели, запуск, CI, деплой, dev-стенд | `DEVELOPMENT.md` |
| Переменные окружения | `.env.example` |
| Схема БД | `data_tables/`, `data_tables/migrations/` |
| Правила работы агентов, ветки, ограничения | `CLAUDE.md` |
| Задачи и их статус | `plan/open_tasks.md` (закрытые — `plan/archive/closed_tasks.md`) |

**Правило (Global Constraint 7):** появился, исчез или переехал модуль — поправь его строку
в дереве в том же коммите.
