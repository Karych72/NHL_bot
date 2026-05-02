# Загрузка данных NHL в PostgreSQL

Полное руководство: установка окружения, поднятие схемы PostgreSQL, источники
данных в API NHL, стратегия записи, повседневные команды `make`, CLI и
диагностика.

> Запуск Telegram-бота — отдельный документ:
> [`telegram_bot.md`](telegram_bot.md).
> Контракт `NULL`-семантики и список оставшихся не-`NULL` default —
> [`pipeline_nulls_and_explicit_null_tz.md`](pipeline_nulls_and_explicit_null_tz.md).

---

## 0. Архитектура потока данных

```
NHL Stats API (api.nhle.com / api-web.nhle.com)
        │
        │  HTTP GET (JSON), retry с экспон. backoff на 429
        ▼
pipeline/load_season_modern.py     ← один скрипт, один процесс
        │
        │  одна транзакция psycopg2:
        │    DELETE per-game для game_id в окне
        │    UPSERT для сезонных таблиц
        │    INSERT для per-game таблиц
        │    COMMIT (или ROLLBACK при любой ошибке)
        ▼
PostgreSQL public.* (12 таблиц из data_tables/t.*.sql)
        │
        ▼
telegram_bot/bot.py (только чтение БД)  ── см. telegram_bot.md
```

Лоадер **единственный пишет** в боевые таблицы NHL. Бот и `modeling/` от БД
только читают.

---

## 1. Требования

- **Python 3** (проект использует venv `.venv`; в `requirements.txt`
  закреплён `python-telegram-bot==13.15`, остальные пакеты — `pandas`,
  `requests`, `psycopg2-binary`, `jinja2`, `pytest>=8.0`).
- **PostgreSQL** (≥ 12, проверено на 14/15/16) с возможностью подключиться по
  `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_DATABASE`.
- **Сеть** к `api.nhle.com` и `api-web.nhle.com` (без аутентификации, public
  API). Лоадер делает много запросов — ожидайте трафика порядка десятков МБ
  и десятков минут на полный сезон.
- **macOS / Linux**. Цели вида `season-sync-week` опираются на `date -v-…d`
  (BSD `date`), поэтому на Linux требуют ручного указания дат — см. §7.

---

## 2. Быстрый старт

```bash
make setup env-example          # venv + зависимости + .env из шаблона
# Отредактируйте .env: PG_*, SEASON_ID, CURRENT_SEASON, при желании DATE_FROM/DATE_TO
make db-reset-local             # DROP всех таблиц проекта + CREATE из data_tables/t.*.sql + SQL-функции
make season-sync-month          # загрузка за последние ~30 дней (сеть + NHL API)
make test-db                    # проверка схемы в PostgreSQL
RUN_DB_DATA_TESTS=1 make test-db  # проверка ссылочной целостности после загрузки
```

После — поднимаем бота: см. [`telegram_bot.md`](telegram_bot.md).

---

## 3. Установка окружения

### 3.1. `make setup`

```bash
make setup
```

Создаёт или **пересоздаёт** виртуальное окружение `.venv` и ставит
зависимости из `requirements.txt`. Логика пересоздания: если архитектура
машины (`uname -m`) не совпадает с `.venv/.arch` — `.venv` удаляется (важно
при переезде между Apple Silicon arm64 и Rosetta x86_64). Кэширование
архитектуры — `setup` в `Makefile`.

### 3.2. `.env`

```bash
make env-example
```

Скопирует `.env.example` → `.env`, **не перезаписывая** существующий
(`cp -n`). Дальше отредактируйте `.env` руками.

`Makefile` подхватывает `.env` из корня (`include .env; export`), так что
любая цель уже видит переменные. То же делает `telegram_bot/config.py` для
лоадера и для бота — переменные общие.

---

## 4. Переменные окружения

Полный список переменных, которые лоадер читает напрямую (через
`telegram_bot/config.py`) или через Makefile.

| Переменная | Где используется | Default | Что значит |
|------------|------------------|---------|-----------|
| `PG_HOST` | лоадер + бот + `psql` в `Makefile` | `localhost` | Хост PostgreSQL. |
| `PG_PORT` | то же | `5432` | Порт. |
| `PG_USER` | то же | в коде — текущий пользователь ОС (`getpass.getuser()`); в `Makefile` — `postgres`, при пустой переменной локальные `_local`-цели подставляют `$(id -un)` | Имя роли БД. |
| `PG_DATABASE` | то же | `postgres` | Имя базы. |
| `DATE_FROM` | лоадер | `2025-10-01` | Начало окна загрузки **завершённых** игр (ISO `YYYY-MM-DD`). |
| `DATE_TO` | лоадер | `date.today()` | Конец окна (включительно). |
| `SEASON_ID` | лоадер + бот | `20252026` | Numeric `seasonId` NHL Stats API. Пишется в `games.season_id`, `*_season_stats.season_id` и т.д. |
| `CURRENT_SEASON` | лоадер + бот | `25/26` | Текстовая метка, попадает в `games.season`. |
| `TELEGRAM_BOT_TOKEN` | только бот | пусто | Не нужно для загрузки. |
| `SEASON_START` | только Makefile-цель `season-load-full` | `2025-10-01` | Начало сезона для full-reload. |
| `RUN_DB_SCHEMA_TESTS` | `make test-db` | `1` | См. §10.2. |
| `RUN_DB_DATA_TESTS` | `make test-db` | пусто | См. §10.2. |

`SEASON` в `.env.example` — **legacy-переменная**, текущий код её не
использует (читается только `SEASON_ID`). Можно удалить из своего `.env`.

> **Подсказка:** для разовых переопределений CLI > env > config:
>
> ```bash
> DATE_FROM=2026-01-01 DATE_TO=2026-01-31 make season-sync
> SEASON_START=2024-10-01 make season-load-full
> PG_USER=myuser make db-sync-local
> ```

---

## 5. База данных

### 5.1. Состав таблиц

Все DDL — в `data_tables/t.*.sql`, чистый `CREATE TABLE` без `ALTER` /
миграций. 12 таблиц, разбиты по гранулярности:

**Сезонные «измерения и агрегаты»** (PK включает `season_id`):

| Таблица | PK | Гранулярность |
|---------|----|----|
| `teams` | `(team_id, season_id)` | команда × сезон (название, дивизион, конференция) |
| `teams_stats` | `(team_id, season_id)` | агрегаты команды за сезон |
| `rosters` | `(player_id, season_id)` | состав: игроки, привязанные к команде на сезон |
| `players_season_stats` | `(player_id, season_id)` | сезонная статистика полевых |
| `players_advanced_stats` | `(player_id, season_id)` | Corsi/Fenwick/GF%/zone-start и т.п. |
| `players_shot_types` | `(player_id, season_id)` | броски по типам (wrist/snap/slap/…) |
| `goalies_season_stats` | `(player_id, season_id)` | сезонная статистика вратарей |

**Игры и per-game-факты:**

| Таблица | PK / UNIQUE | Гранулярность |
|---------|------------|----|
| `games` | `PRIMARY KEY (game_id)` | одна игра |
| `game_team_stats` | `UNIQUE (game_id, team_id)` | команда × игра (по 2 строки на игру) |
| `game_player_stats` | `UNIQUE (game_id, player_id)` | полевой × игра |
| `game_goalie_stats` | `UNIQUE (game_id, player_id)` | вратарь × игра |
| `all_goals` | без PK, есть `event_id` | каждый забитый гол |

Порядок создания зафиксирован в `Makefile` (`DDL_TABLES`): сначала
сезонные «измерения», потом `games`, потом per-game-факты, в конце
`all_goals` (логически зависит от игр и игроков, но FK не выставлены, чтобы
не блокировать `DELETE`+`INSERT`-стратегию).

Список таблиц для `DROP` — в `scripts/db_drop_all_tables.sql`
(`DROP TABLE IF EXISTS … CASCADE` в правильном порядке).

### 5.2. SQL-функции

Помимо таблиц, в БД деплоятся **PL/pgSQL-функции** для бота из
`telegram_bot/queries/*.sql`:

- `get_game_stats(game_id)` — основная статистика матча;
- `get_goals_game(game_id)` — голы матча с фамилиями игроков (LEFT JOIN на `rosters`, NULL → имя «Unknown» в боте);
- `get_goalies_game(game_id)` — вратари матча.

Они применяются автоматически целью `db-functions` (входит в `db-reset` и
`db-sync`).

### 5.3. Полный сброс и создание (`db-reset` / `db-init`)

**Деструктивная** цепочка `DROP → CREATE → функции`:

```bash
make db-reset-local    # PG_USER = $(id -un), удобно на macOS без отдельной роли postgres
make db-reset          # PG_* из .env (значение PG_USER из переменной)
```

Эквиваленты:

- `make db-init` ≡ `make db-reset` (то же самое, оставлено для совместимости);
- `make db-init-local` ≡ `make db-reset-local`.

Под капотом:

```
db-drop      → psql -f scripts/db_drop_all_tables.sql
db-tables    → psql -f data_tables/t.*.sql (в порядке DDL_TABLES)
db-functions → psql -f telegram_bot/queries/*.sql
```

**Когда применять:**

- первый запуск проекта;
- после `git pull`, если в `data_tables/` или `telegram_bot/queries/`
  что-то поменялось;
- если бот падает с `ON CONFLICT … no unique constraint`.

### 5.4. Только CREATE без DROP (`db-sync`)

```bash
make db-sync          # PG_* из .env
make db-sync-local    # PG_USER = $(id -un)
```

Применяет только `CREATE TABLE` и SQL-функции. На уже существующих таблицах
команда **упадёт** с ошибкой Postgres — это намеренно, чтобы случайно не
залезть в чужую схему. Сначала нужен `db-reset-local`.

### 5.5. Проверки схемы

```bash
make verify-skater-schema   # SQL-самопроверка: колонки и PK для skater reports
make test-db                # unittest по схеме (RUN_DB_SCHEMA_TESTS=1 по умолчанию)
```

`verify-skater-schema` — низкоуровневый SQL-скрипт
(`scripts/verify_skater_reports_schema.sql`), он `RAISE EXCEPTION` при
отсутствии нужных колонок / PK. Удобен в CI, не требует Python.

`test-db` — Python-юнит-тесты из `tests/test_db_nhl.py`, см. §10.

---

## 6. Загрузка данных NHL (`pipeline/load_season_modern.py`)

### 6.1. Фазы выполнения

Метод `ModernNhlLoader.run()` идёт строго последовательно:

1. **`load_team_reference()`** — справочник команд (`/stats/rest/en/team`) +
   текущая турнирная таблица (`/v1/standings/now`) для названий
   дивизионов / конференций.
2. **`build_teams_and_stats()`** — `teams` + `teams_stats` из
   `team/summary` за сезон.
3. **`build_rosters()`** — обход всех команд через `/v1/roster/{TRI}/{SEASON_ID}`.
4. **`build_player_season_stats()`** — `players_season_stats` из совокупности
   отчётов: `skater/summary`, `skater/timeonice`,
   `skater/faceoffpercentages`, `skater/shootout`, `skater/realtime`
   (хиты + блоки, которых больше нет в summary).
5. **`build_goalie_season_stats()`** — `goalies_season_stats` из
   `goalie/summary` + `goalie/savesByStrength` для разбивки по EV/PP/SH.
6. **`build_player_advanced_stats()`** — `players_advanced_stats` из
   `skater/goalsForAgainst` + `skater/puckPossessions`.
7. **`build_player_shot_types()`** — `players_shot_types` из
   `skater/shottype`.
8. **`supplement_rosters_from_reports()`** — добавляет в `rosters` тех, кто
   есть в сезонных отчётах, но кого не вернул `/v1/roster/{TRI}` (call-up,
   короткие контракты). Точечные пропуски добиваются через
   `/v1/player/{pid}/landing`.
9. **`fetch_final_games()`** — список **завершённых** игр (`gameStateId=7`)
   за `SEASON_ID` в окне дат через
   `https://api.nhle.com/stats/rest/en/game?cayenneExp=...`.
10. **`build_game_rows()`** — для каждой игры два запроса:
    `/v1/gamecenter/{game_id}/play-by-play` и `/v1/gamecenter/{game_id}/boxscore`.
    Из них собираются `games`, `game_team_stats`, `game_player_stats`,
    `game_goalie_stats`, `all_goals`. Каждые 50 игр в логе пишется прогресс
    `Processed games: N/total`.
11. **Запись в Postgres**: одна транзакция (`autocommit = False`):
    1. `DELETE FROM all_goals|game_player_stats|game_team_stats|game_goalie_stats|games WHERE game_id = ANY(window_ids)` — удаление текущей версии данных по этим играм;
    2. `UPSERT` в сезонные таблицы (`ON CONFLICT … DO UPDATE`);
    3. `INSERT` в per-game таблицы;
    4. `COMMIT`. На любом исключении выше — `ROLLBACK` всей транзакции,
       подключение закрывается.

В логах в начале выводятся окно дат и `season_id`:

```
Date window 2025-10-01 .. 2026-03-29 (season_id=20252026, games.season=25/26)
```

### 6.2. Источники данных

| URL | Что забираем |
|-----|--------------|
| `https://api.nhle.com/stats/rest/en/team` | Справочник всех команд (id, triCode, fullName). |
| `https://api-web.nhle.com/v1/standings/now` | Текущая турнирная таблица (для дивизиона/конференции). |
| `https://api.nhle.com/stats/rest/en/team/summary?cayenneExp=seasonId=…&gameTypeId=2` | Командные сезонные агрегаты (regular, `gameTypeId=2`). |
| `https://api-web.nhle.com/v1/roster/{TRI}/{SEASON_ID}` | Текущий ростер команды на сезон. |
| `https://api.nhle.com/stats/rest/en/skater/{summary,timeonice,faceoffpercentages,shootout,realtime,goalsForAgainst,puckPossessions,shottype}?cayenneExp=…` | Сезонные отчёты по полевым. |
| `https://api.nhle.com/stats/rest/en/goalie/{summary,savesByStrength}?cayenneExp=…` | Сезонные отчёты по вратарям. |
| `https://api-web.nhle.com/v1/player/{playerId}/landing` | Дополняющий лукап для редких игроков, отсутствующих в роли тима и сезонных отчётах. |
| `https://api.nhle.com/stats/rest/en/game?cayenneExp=…` | Список завершённых игр в окне дат. |
| `https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play` | Полное PBP игры (голы, пенальти, faceoffs, hits, …). |
| `https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore` | Бокс-скор: per-player и per-team метрики игры. |

Обработка сети (`get_json` в лоадере): до 10 попыток на запрос с задержкой
1 с между ошибками; на `429 Too Many Requests` ждём `Retry-After` (или 2с,
если заголовка нет) с экспоненциальным backoff до 60с. Таймаут запроса —
30 секунд.

### 6.3. Стратегия записи в БД

| Таблицы | Стратегия | Конфликты |
|---------|-----------|----------|
| `teams`, `teams_stats`, `rosters`, `players_season_stats`, `goalies_season_stats`, `players_advanced_stats`, `players_shot_types` | **UPSERT** | `ON CONFLICT (PK) DO UPDATE SET …` для всех не-ключевых колонок. |
| `games`, `all_goals`, `game_team_stats`, `game_player_stats`, `game_goalie_stats` | **DELETE по `game_id` в окне → INSERT** | дубли по `game_id` из NHL API при пагинации устраняются дедупом по PK перед записью. |

Дополнительно `execute_upsert` в лоадере **внутри одного INSERT**
дедуплицирует входной список по `conflict_columns` — иначе пагинированные
NHL-отчёты могут вернуть одного игрока дважды и Postgres ругнётся
`CardinalityViolation`.

Транзакция обнимает **все** INSERT/UPSERT/DELETE одной игры-загрузки:
если запись падает на любом этапе, БД откатывается в исходное состояние.

### 6.4. NULL-семантика

Если NHL API не возвращает поле, в Postgres пишется `NULL`, а не **0**,
**0.0**, **`"00:00"`** или сентинел **-9999**. Реальный 0 (например «0
голов в этой игре у скейтера») сохраняется как 0.

Полный контракт, список оставшихся намеренных не-`NULL` default
(агрегаты PBP, ключи, derived booleans, `goalies_season_stats.ties = 0`,
`teams.active = True`) — в
[`pipeline_nulls_and_explicit_null_tz.md`](pipeline_nulls_and_explicit_null_tz.md).

### 6.5. Идемпотентность и повторный запуск

Лоадер можно **безопасно перезапускать** на пересекающихся окнах:

- сезонные таблицы переписываются через UPSERT;
- per-game таблицы пересоздаются после `DELETE` тех же `game_id`.

Это значит: если за день ничего не поменялось в API — запись будет
идентичной. Если NHL поправил данные постфактум (типичная ситуация на
свежие игры) — повторный запуск это подхватит.

**Порядок ON CONFLICT** в UPSERT-ах сохраняет именно ключевые колонки:
`(team_id, season_id)` или `(player_id, season_id)`. Никаких суррогатных
ключей нет.

---

## 7. Команды `make` для загрузки

Все цели ниже автоматически подтягивают `setup` и `env-example` (то есть
ставят зависимости и копируют шаблон, если он не скопирован), окно дат
передаётся через ENV в дочерний процесс лоадера.

| Команда | DATE_FROM | DATE_TO | Когда применять |
|---------|-----------|---------|-----------------|
| `make season-sync DATE_FROM=… DATE_TO=…` | как задано | как задано | универсальная цель; **обязательны** оба аргумента, иначе ошибка `Usage:` |
| `make season-load-full` | `$(SEASON_START)` (default `2025-10-01`) | `$(date +%Y-%m-%d)` | **полный** реload текущего сезона до сегодня |
| `make season-reload-current` | то же | то же | алиас на `season-load-full` |
| `make season-load` | то же | то же | алиас на `season-load-full` |
| `make season-sync-week` | `$(date -v-7d +%Y-%m-%d)` (BSD `date`) | `$(date +%Y-%m-%d)` | последние 7 дней (свежие игры) |
| `make season-update` | то же | то же | алиас на `season-sync-week` |
| `make season-sync-month` | вычислено через Python-однострочник в Makefile (30 дней назад) | `$(date +%Y-%m-%d)` | последние 30 дней (рекомендуется как «обновить локалку») |
| `make season-sync-today` | `$(date +%Y-%m-%d)` | то же | только сегодня |

**macOS:** все `season-sync-week` / `-today` работают «из коробки».
**Linux:** опции `date -v…` нет, поэтому `season-sync-week` упадёт. Решение:

```bash
make season-sync DATE_FROM=2026-03-22 DATE_TO=2026-03-29
```

(или линуксовый аналог — `DATE_FROM="$(date -d '7 days ago' +%F)"`).

---

## 8. CLI лоадера

Прямой запуск без `make` (полезно для отладки или CI):

```bash
cd pipeline && ../.venv/bin/python -u load_season_modern.py \
  --date-from 2025-10-01 \
  --date-to 2026-03-29 \
  --season-id 20252026 \
  --current-season "25/26"
```

Аргументы:

| Флаг | Что переопределяет |
|------|--------------------|
| `--date-from YYYY-MM-DD` | `DATE_FROM` |
| `--date-to YYYY-MM-DD` | `DATE_TO` (включительно) |
| `--season-id INT` | `SEASON_ID` |
| `--current-season "LABEL"` | `CURRENT_SEASON`, попадает в колонку `games.season` |

Без аргументов лоадер читает значения из `telegram_bot/config.py` (то есть
из `.env`).

Логирование — `INFO` по умолчанию, формат
`%(asctime)s - %(name)s - %(levelname)s - %(message)s`. Для отладки PBP
парсинга включается `DEBUG` (поправьте `level` в начале
`__main__`-блока).

---

## 9. Несколько сезонов в одной БД

Все ключевые таблицы содержат `season_id`, поэтому в одной БД спокойно
живут данные нескольких сезонов одновременно. Бот фильтрует выдачу по
`config.SEASON_ID` (= `.env`-переменная `SEASON_ID`), переключиться на
другой сезон — поменять `SEASON_ID` в `.env` и перезапустить бота.

Чтобы загрузить второй сезон (например прошлый), не теряя текущий:

```bash
SEASON_ID=20242025 CURRENT_SEASON="24/25" \
  make season-sync DATE_FROM=2024-10-01 DATE_TO=2025-06-30
```

Строки сезона `20252026` не трогаются: UPSERT-ы идут только по своим PK,
а `DELETE … WHERE game_id = ANY(...)` фильтрует только игры из переданного
окна, чьи `game_id` в любом случае уникальны на уровне NHL.

Проверить, какие сезоны лежат в базе:

```sql
SELECT DISTINCT season_id FROM games ORDER BY season_id;
SELECT season_id, COUNT(*) FROM games GROUP BY season_id ORDER BY season_id;
```

---

## 10. Тесты и аудит данных

### 10.1. `make test-fast` / `make all-tests`

```bash
make test-fast   # pytest по tests/, кроме test_db_nhl.py (БД не нужна)
make all-tests   # test-fast + test-db (нужна поднятая БД)
```

`test-fast` запускает:

- `tests/test_modeling_dataset_build.py` — контракты сборки модельного датасета;
- `tests/test_nhl_scoreboard.py` — UI «расписание»;
- `tests/test_pipeline_optional_helpers.py` — юниты по `optional_*` хелперам и контрактные тесты `build_game_rows` (NULL для пропущенных полей, отсутствие `-9999`);
- `tests/test_skater_reports_bot.py` — текстовые лидерборды бота.

### 10.2. `make test-db` (схема + данные в БД)

```bash
make test-db                       # включает RUN_DB_SCHEMA_TESTS=1 — проверяет PK/колонки
RUN_DB_DATA_TESTS=1 make test-db   # + ссылочная целостность (после загрузки)
RUN_DB_SCHEMA_TESTS=0 make test-db # только TestNhlLoadedData (если RUN_DB_DATA_TESTS=1)
```

Файл — `tests/test_db_nhl.py`, два класса:

- **`TestNhlSchema`** (включается `RUN_DB_SCHEMA_TESTS=1`) — проверяет
  primary keys и наличие колонок типа `games.season_id bigint`.
- **`TestNhlLoadedData`** (включается `RUN_DB_DATA_TESTS=1`) — проверяет
  `games` непустые, `season_id` совпадает с `config.SEASON_ID`, нет
  «orphan» игр без teams/team_id, ростеры ссылаются на существующие
  команды.

Если переменная не выставлена — соответствующие тесты помечаются
`@unittest.skipUnless`. Это сделано, чтобы CI без БД проходил.

### 10.3. `make verify-skater-schema`

Низкоуровневый SQL-скрипт, проверяет наличие колонок и PK для skater-отчётов
(`players_advanced_stats`, `players_shot_types`,
`players_season_stats.{shootout_*, *_faceoff_pct}`). `RAISE EXCEPTION` при
любом расхождении — удобно ставить в pre-deploy/CI.

### 10.4. Отчёт по `NULL` в матчевых таблицах

После загрузки — посмотреть, какие колонки пустые и в какой доле:

```bash
.venv/bin/python artifacts/reports/nulls_by_season_report.py \
  --tables games,game_team_stats,game_player_stats,game_goalie_stats,all_goals \
  --output artifacts/reports/nulls_by_season_report.csv
```

Без `--seasons` — берёт все сезоны из `games`. Полезно, чтобы видеть
эффект NULL-семантики (см. §6.4) — после нового пайплайна часть колонок
ожидаемо переходит из «0 / 00:00» в `NULL`.

---

## 11. Типичные проблемы

| Симптом | Что проверить и как починить |
|---------|------------------------------|
| `ON CONFLICT … no unique constraint matching the ON CONFLICT specification` | DDL не совпадает с тем, что ожидает лоадер. Запустить `make db-reset-local`. |
| `relation "rosters" does not exist` (или другая таблица) | БД не инициализирована — `make db-reset-local`. |
| `Empty list of finished games` / лоадер пишет 0 игр | Проверить `DATE_FROM`/`DATE_TO`, `SEASON_ID`. NHL отдаёт игры **только после** перехода `gameStateId=7`. Свежие LIVE-матчи ещё не попадут. |
| `429 Too Many Requests` | Лоадер сам делает backoff, но если падает повторно — сократите окно дат. Не запускайте параллельно несколько `season-*` процессов. |
| Ошибка подключения к БД (`could not connect to server`) | Хост/порт/`pg_hba.conf`/пароль (`PGPASSWORD` если требуется). Проверить вручную: `psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c '\dt'`. |
| `psycopg2.errors.CardinalityViolation: ON CONFLICT DO UPDATE command cannot affect row a second time` | Дедуп по конфликтным ключам в лоадере уже есть; если всё же случилось — это означает, что NHL вернул один и тот же `(player_id, season_id)` дважды на разных страницах с разными значениями. Перезапустить — обычно лечится. |
| `RuntimeError: Request failed: …` после 10 попыток | Сеть (DNS, корпоративный прокси) или временная недоступность `api-web.nhle.com`. Подождать 1–2 минуты и перезапустить. |
| Вратарь в карточке матча с `—` вместо TOI | Норма после введения `NULL`-семантики: вратарь либо не играл (NULL), либо API не отдало `toi`. См. `pipeline_nulls_and_explicit_null_tz.md`. |
| `Loaded: teams=0, rosters=0, …` | `SEASON_ID` указывает на сезон, по которому ещё нет агрегатов в `team/summary`. Поставить актуальный `SEASON_ID`. |

---

## 12. Объёмные ориентиры

Для понимания «сколько ждать» при полной загрузке регулярного сезона
(~1300 игр):

| Этап | Время | Замечания |
|------|-------|-----------|
| Сезонные отчёты (teams + skaters + goalies + advanced + shot types + roster supplement) | ~1–2 минуты | ~12 HTTP-запросов на отчёт + 32 ростера + точечные landing-фолбэки. |
| Загрузка PBP+boxscore по играм | ~25–40 минут на 1300 игр | по 2 запроса на игру; зависит от сети и текущего rate-limit NHL. |
| Запись в Postgres | ~10–30 секунд | одна транзакция, `execute_values` с `page_size=1000…5000`. |

Узкое место — сеть, не БД. На небольших окнах (`season-sync-week` /
`-today`) загрузка занимает секунды.

---

## 13. Дальше

После успешной загрузки — поднимаем Telegram-бота:
[`telegram_bot.md`](telegram_bot.md).

Связанная документация:

- [`pipeline_nulls_and_explicit_null_tz.md`](pipeline_nulls_and_explicit_null_tz.md) — что пишется как `NULL` и какие default остались осознанно;
- [`api_data_research.md`](api_data_research.md) — какие поля NHL API маппятся в какие колонки БД и что пока не парсится;
- [`architecture.md`](architecture.md) — полная карта проекта;
- [`modeling_dataset_builder.md`](modeling_dataset_builder.md) — как из этих таблиц собирается датасет для моделирования.
