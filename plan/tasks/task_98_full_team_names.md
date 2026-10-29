# Задача 98. Полные названия команд в rich-экранах

**Блок:** продукт · **Надобность:** 🟡
**Область:** `telegram_bot/bot_messages.py`, `telegram_bot/push_digest_job.py`,
`telegram_bot/video_replay.py`, `telegram_bot/queries/get_game_stats.sql`,
`telegram_bot/queries/get_three_stars_game.sql`, `messages/team_profile.txt`, тесты,
`docs/telegram_bot.md`, `docs/architecture.md`.
**Зависит от:** нет.
**Источник:** просьба человека 2026-10-29; реестр — [`../open_tasks.md`](../open_tasks.md).

## Кратко

После перехода на rich-сообщения (Задачи 88–91) таблицы раскладывает Telegram, и место под
название есть. Везде в тексте экранов команда пишется полным названием `teams.name`
(«Minnesota Wild»), а не кодом («MIN») и не прозвищем `short_name` («Wild»).

## Зачем и на что влияет

- Код и прозвище читаются хуже полного названия: «Wild» не говорит, что это Миннесота.
- Это видно на всех экранах со счётом, таблицами и звёздами матча. Логику бота задача не меняет.

## Степень важности

🟡: качество чтения, ничего не блокирует.

## Подробно

**Сделано заранее** (ветка `plan-task-98-full-team-names`, тот же PR, что заводит задачу):
`/standings` берёт `teams.name`, а строка «линия плей-офф» в таблице заменена пунктиром
`_PLAYOFF_LINE`.

**Решения человека 2026-10-29.**
1. Название — полное, `teams.name`; город отдельно не выделяется.
2. **Кнопки остаются с кодами**: подписка (4 в ряд), «1. NYR – BOS» в `/today`,
   «MIN @ DAL · 19:00 ET» в `/tonight`. Полное название в них не помещается. Всплывающие ответы
   (`callback.answer`, «Подписка на BOS включена») не rich и тоже остаются с кодами.
3. Таблицы не ужимать: шрифт и отступы Telegram не настраиваются, а убирать содержимое
   человек не стал.

**Что перевести** (снимок 2026-10-29, перед работой сверить):

| Экран | Где | Источник сейчас |
|---|---|---|
| Строка счёта карточки и `/today` («Home 4:2 Away») | `_game_score_header`, `_score_line`, `day_digest_summary_body` | `get_game_stats.sql`: `t.short_name AS team_name`, тип `varchar(20)` |
| Звёзды матча «Kaprizov (MIN)» | `_format_three_star_line` | `get_three_stars_game.sql`: `t.abbreviation`, `varchar(10)` |
| Превью матча («SEA @ DET», шапка таблицы, «победа DET») | `matchup_season_preview`, `_matchup_compare_table`, `_model_prediction_line` | код из NHL API; SQL уже джойнит `teams` по коду: добавить `t.name` для показа |
| Лидеры игроков `/stats`, колонка «Ком» | `player_stats_with_count` | `t.abbreviation AS team` |
| Лидеры команд `/team_stats`, колонка «Ком» | `team_stats_with_count` (докстринг объясняет код шириной телефона — устарело) | `t.abbreviation` |
| Страница страны `/countries`, колонка «Ком» | `_GROUP_SKATERS_SQL` / `_GROUP_GOALIES_SQL` | `t.abbreviation` |
| Блок страны в пуше и карточке «Lastname (ABC, ц)» | `_country_blocks`, `_night_skater_lines` | `t.abbreviation` |
| Профиль команды: «Washington Capitals (WSH)» | `messages/team_profile.txt` | убрать «(WSH)», название уже есть |
| `/WSH_FULL`: «WSH: все игроки» | `team_full_stats` | код; нужен `teams.name` по `_team_id_for_abbrev` |
| Подвал пуша «По подписке: TOR · BOS · Россия» | `push_digest_job.subscribed_games` | `trim(abbreviation)` |
| Экран подписок «Команды: BOS, WSH» (текст, не кнопки) | `bot.py`, `season_teams_by_division` | `trim(abbreviation)` |
| Подпись к видео гола «(BOS)», «BOS 1:2 SJS» | `video_replay.goal_caption` | NHL ppt-replay `abbrev`; подпись не rich, но это текст: название — по коду из `teams` |

**Не трогать** (код здесь — ключ, а не подпись): команды `/ABC` и `/ABC_FULL` и ссылки на них,
`_team_id_for_abbrev`, сопоставление кода NHL API с `teams.abbreviation` в превью, callback-и
`tn:`, `sub:t:`, `dg:`, `gv:`, перечень `season_team_abbrevs()` для ИИ, параметр `team` у
`player_overview` в `ai_stats.py`, `ORDER BY abbreviation` как тай-брейк.

**Подводные камни.**
- `get_game_stats` и `get_three_stars_game` объявлены через `RETURNS TABLE`: `CREATE OR REPLACE`
  тип колонки не меняет, перед ним нужен `DROP FUNCTION`. Имя поля — до 30 символов
  (`teams.name varchar(30)`), «Columbus Blue Jackets» — 21. Применить на `nhl_dev`, тест
  `test_db_nhl` прогнать на БД (память: CI гоняет его без pytest).
- Тай-брейк `/standings` после этого PR сортирует по `name`, а Wild Card опознаёт топ-3
  по названию; если трогать, ключом брать `team_id`.
- Шапка `_matchup_compare_table` с двумя полными названиями — проверить на телефоне.
- ИИ: `ai_stats._OVERVIEW_PLAYERS_SQL` отдаёт модели `t.abbreviation AS team`. Ответ модель пишет
  по промпту (`prompts/stats_answer.md`: «привычное русское название»), поэтому
  подпись в ответе ИИ задача не меняет.
- Тестов на коды много (`test_bot_messages`, `test_bot_integration`, `test_push_digest_job`,
  `test_db_nhl`, `test_bot_stats_handlers`, `test_skater_reports_bot`), бюджет в 300 строк
  будет превышен в основном за счёт тестов. Если прирост кода больше 300 строк, разбить на
  98A (SQL-функции и карточка) и 98B (остальные экраны).

**Критерии приёмки.** На экранах из таблицы выше нет трёхбуквенных кодов и прозвищ, кнопки
не изменились; миграция функций применена на `nhl_dev`; `make ci-local` и `test_db_nhl` на БД
зелёные; живая проверка на dev-боте: `/standings`, `/today`, `/tonight` (превью), `/stats`,
`/team_stats`, `/WSH`, `/WSH_FULL`, `/countries`.
