# Задача 41. Экраны команд: дивизионы и профиль

**Статус:** выполнена (2026-09-28, ветки `sdd-task-41-team-screens` — Фаза B,
`sdd-task-41d-team-profile` — Фаза D; PR #41, #42; ручной проход в живом боте
ждёт человека) · **Блок:** продукт · **Надобность:** ⚪
**Область:** `telegram_bot/bot_messages.py`, `script_bot.py`, `dialog_states.py`, `stats_handlers.py`, `bot.py`, `messages/*.txt`, `tests/`, `docs/architecture.md`.
**Порядок:** после релиза, окнами между крупным; порядок с 37, 38, 42 любой.
**Источник:** [`../stats/team_and_country_stats_plan.md`](../stats/team_and_country_stats_plan.md), Фазы B и D; реестр — [`../open_tasks.md`](../open_tasks.md), «Продуктовый бэклог».

## Кратко

Остаток командного плана `stats/`: срезы по конференциям и дивизионам (Фаза B) и профиль
команды — состав, вклад позиций, топ бомбардиров клуба (Фаза D). Фаза C (сравнение двух
команд) реализована — `matchup_season_preview`, `bot_messages.py:611`, кнопка из `/tonight`;
Фаза A — частично (`TEAM_PROCENT_WINS`, `TEAM_POWER_PLAY`, `TEAM_POWER_KILL`,
`dialog_states.py:13-14`). Данные уже в БД: `teams.conference_name`, `teams.division_name`,
`rosters.position` / `current_team_id`, `players_season_stats`, `teams_stats`.

## Зачем и на что влияет

- **Зачем.** Дать пользователю больше контекста, чем одна таблица очков: как выглядит
  дивизион в целом, из кого состоит команда и кто в ней делает результат. Данные загружены
  и лежат без применения.
- **На что влияет.** Только бот: новые состояния FSM, кнопки, SQL-функции в
  `bot_messages.py`, шаблоны, тесты; `docs/architecture.md` (дерево шаблонов и список
  экранов). Схема БД и пайплайн не меняются.
- **Если не делать.** Ничего не ломается — чистый продуктовый бэклог.

## Степень важности

⚪ **Можно не делать в этом цикле.** Не влияет на релиз и на трек B; берётся окном между
крупными задачами.

## Подробно

### Текущее состояние (сверено 2026-09-22)

- `/table` уже группирует таблицу по дивизионам
  (`test_table_command_renders_divisions_with_rows_sorted_by_points`) — «таблица по
  дивизионам» как новый экран была бы дублем. Фаза B — это **сводные метрики** по
  конференции/дивизиону (средние голы за игру, PP%/PK%, суммарные очки, число команд),
  а не ещё одна турнирная таблица.
- Паттерны для переиспользования: `team_stats(name_stats, column_name)` с whitelist
  колонок (`database.py`), фабрика `_make_stats_handler` в `stats_handlers.py`, выбор
  команды по аббревиатуре (`_team_id_for_abbrev`, `bot_messages.py:423`;
  `/subscribe_team WSH`), навигация «Назад» (стандарт Задачи 6), `truncate_telegram_text`
  для лимита 4096.
- DDL: `teams(team_id, season_id, name, division_name, conference_name, abbreviation,
  short_name, …)`, `rosters(player_id, season_id, name, position, lastname, nationality,
  current_team_id, …)`.

### Что нужно сделать

**Фаза B — конференции и дивизионы**

1. Два запроса `GROUP BY t.conference_name` / `t.division_name` над `teams_stats ⋈ teams`
   по `(team_id, season_id)` для `config.SEASON_ID`; метрики — то, что есть в
   `teams_stats` (голы за игру, PP%, PK%, очки), без вычисляемых на лету «рейтингов».
2. Функции `conference_summary()` / `division_summary()` в `bot_messages.py`, шаблоны
   `messages/conference_stats.txt`, `division_stats.txt`.
3. Кнопки «По конференциям» / «По дивизионам» в разделе команд; состояния
   в `dialog_states.py` без дыр в `range()`; «Назад» на обеих ветках.

**Фаза D — профиль команды**

4. Выбор команды — тем же способом, что в Фазе C (inline-кнопки аббревиатур сезона).
5. SQL: `rosters ⋈ players_season_stats` по `(player_id, season_id)` при
   `current_team_id = %s`: агрегаты по `position` (игроков, очков, голов), топ-3
   бомбардира; краткая строка сезона из `teams_stats`.
6. Шаблон `team_profile.txt`; при переполнении — обрезка топа, не второе сообщение.

**Общее**

7. Тесты формата сообщений по образцу `tests/test_bot_integration.py` (фикстуры БД
   в `conftest.py`), включая невалидный callback и пустую выборку.
8. `docs/architecture.md`: новые шаблоны и экраны; план `stats/` — отметить фазы.
9. Если суммарный дифф B + D больше 300 строк — две ветки и два PR (Global Constraint 6).

### Критерии приёмки (из реестра)

- Новые экраны не дублируют существующую таблицу `/table`.
- Навигация «Назад» на всех новых ветках.
- Тесты на формат сообщений.

### Проверка

- `make ci-local`; ручной проход по обеим веткам в живом боте, включая «Назад»
  и команду без игроков в ростере.

### Подводные камни

- Идентификаторы в SQL — только через whitelist (`ALLOWED_COLUMNS`) или параметры `%s`;
  `team_id` брать из маппинга аббревиатур, не из текста пользователя.
- Сезон везде из `config`, без сезона в шаблонах.
- Пустой сезон (Задача 36): новые экраны обязаны отвечать текстом, а не пустой таблицей —
  писать сразу с этой веткой.
