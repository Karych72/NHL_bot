# Реализация отчётов по полевым игрокам (раздел 2.1)

## Обзор

Реализация высокоприоритетных отчётов по полевым игрокам из Stats API (раздел 2.1): создание 2 новых таблиц (`players_advanced_stats`, `players_shot_types`), расширение `players_season_stats` колонками зональных вбрасываний и буллитов, обновление pipeline-загрузчика и добавление обработчиков бота.

### Задачи

1. Создать DDL: `data_tables/t.players_advanced_stats.sql` и `data_tables/t.players_shot_types.sql`
2. ALTER `players_season_stats`: добавить 7 колонок (oz/dz/nz faceoff pct + поля буллитов), обновить DDL-файл
3. Pipeline: добавить метод `build_player_advanced_stats()` (goalsForAgainst + puckPossessions)
4. Pipeline: добавить метод `build_player_shot_types()` (отчёт shottype)
5. Pipeline: расширить `build_player_season_stats()` данными faceoffpercentages + shootout
6. Pipeline: обновить `run()` — TRUNCATE, build, INSERT для новых таблиц и расширенных колонок
7. Обновить `database.py`: добавить новые таблицы в ALLOWED_TABLES и колонки в ALLOWED_COLUMNS
8. Добавить обработчики бота: функции в bot_messages.py, хендлеры в stats_handlers.py, состояния в dialog_states.py, меню в bot.py
9. Создать Jinja2-шаблоны для отображения продвинутой статистики и типов бросков

---

## Анализ: новые таблицы vs. расширение существующих

Из 17 отчётов в разделе 2.1, `timeonice` уже реализован. Из оставшихся, 5 имеют высокий приоритет. Решение по каждому:

**2 новые таблицы** — для принципиально других данных:

- `players_advanced_stats` — Corsi/Fenwick/PDO/zone starts (из `goalsForAgainst` + `puckPossessions`)
- `players_shot_types` — разбивка по типу броска (из `shottype`)

**Расширить `players_season_stats`** — для нескольких дополнительных колонок:

- Зональные вбрасывания (из `faceoffpercentages`): 3 колонки
- Буллиты (из `shootout`): 4 колонки

Обоснование: `players_season_stats` (28 колонок) уже содержит `face_off_pct` — зональная разбивка логически продолжает эти данные. Буллитная статистика — тоже часть сезонной. А Corsi/Fenwick и типы бросков — это концептуально другие наборы данных, которым место в отдельных таблицах.

---

## Шаг 1: DDL — новые таблицы и ALTER

### 1a. Файл `data_tables/t.players_advanced_stats.sql`

```sql
CREATE TABLE IF NOT EXISTS players_advanced_stats(
    player_id                   int,
    sat_pct                     double PRECISION,  -- Corsi%
    usat_pct                    double PRECISION,  -- Fenwick%
    goals_pct                   double PRECISION,  -- Goals For %
    oz_start_pct                double PRECISION,
    dz_start_pct                double PRECISION,
    nz_start_pct                double PRECISION,
    on_ice_shooting_pct         double PRECISION,
    ev_goals_for                int,
    ev_goals_against            int,
    ev_goals_for_pct            double PRECISION,
    pp_goals_for                int,
    pp_goals_against            int,
    sh_goals_for                int,
    sh_goals_against            int
);
```

Источники API:

- `goalsForAgainst` → `evenStrengthGoalsFor`, `evenStrengthGoalsAgainst`, `evenStrengthGoalsForPct`, `powerPlayGoalFor`, `powerPlayGoalsAgainst`, `shortHandedGoalsFor`, `shortHandedGoalsAgainst`
- `puckPossessions` → `satPct`, `usatPct`, `goalsPct`, `offensiveZoneStartPct`, `defensiveZoneStartPct`, `neutralZoneStartPct`, `onIceShootingPct`

### 1b. Файл `data_tables/t.players_shot_types.sql`

```sql
CREATE TABLE IF NOT EXISTS players_shot_types(
    player_id                   int,
    goals_wrist                 int,
    shots_wrist                 int,
    goals_slap                  int,
    shots_slap                  int,
    goals_snap                  int,
    shots_snap                  int,
    goals_backhand              int,
    shots_backhand              int,
    goals_tip_in                int,
    shots_tip_in                int,
    goals_deflected             int,
    shots_deflected             int,
    goals_wrap_around           int,
    shots_wrap_around           int
);
```

Источник API: `shottype` → `goalsWrist`/`shotsOnNetWrist`, `goalsSlap`/`shotsOnNetSlap`, etc.

### 1c. ALTER `players_season_stats` — 7 новых колонок

Добавить в `data_tables/t.players_season_stats.sql` + миграция через ALTER:

```sql
-- зональные вбрасывания (из отчёта faceoffpercentages)
oz_faceoff_pct    double PRECISION,
dz_faceoff_pct    double PRECISION,
nz_faceoff_pct    double PRECISION,
-- буллиты (из отчёта shootout)
shootout_goals    int,
shootout_shots    int,
shootout_pct      double PRECISION,
shootout_gd_goals int   -- решающие голы
```

---

## Шаг 2: Pipeline — загрузка данных

Файл: `pipeline/load_season_modern.py`

### 2a. Новый метод `build_player_advanced_stats()`

Паттерн аналогичен `build_player_season_stats()`:

1. Запрос `goalsForAgainst` → `gfa_by_player` (dict по `playerId`)
2. Запрос `puckPossessions` → `pp_by_player`
3. Мержить по `playerId`, сформировать tuple-ы

```python
def build_player_advanced_stats(self) -> List[tuple]:
    gfa_url = (
        "https://api.nhle.com/stats/rest/en/skater/goalsForAgainst"
        f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
    )
    gfa_rows = self.fetch_paginated(gfa_url, page_size=1000)
    gfa_by_player = {to_int(r.get("playerId")): r for r in gfa_rows}

    pp_url = (
        "https://api.nhle.com/stats/rest/en/skater/puckPossessions"
        f"?cayenneExp=seasonId={self.season_id}%20and%20gameTypeId=2"
    )
    pp_rows = self.fetch_paginated(pp_url, page_size=1000)

    out = []
    for r in pp_rows:
        pid = to_int(r.get("playerId"))
        gfa = gfa_by_player.get(pid, {})
        out.append((
            pid,
            pct_from_ratio(r.get("satPct")),
            pct_from_ratio(r.get("usatPct")),
            pct_from_ratio(r.get("goalsPct")),
            pct_from_ratio(r.get("offensiveZoneStartPct")),
            pct_from_ratio(r.get("defensiveZoneStartPct")),
            pct_from_ratio(r.get("neutralZoneStartPct")),
            pct_from_ratio(r.get("onIceShootingPct")),
            to_int(gfa.get("evenStrengthGoalsFor")),
            to_int(gfa.get("evenStrengthGoalsAgainst")),
            pct_from_ratio(gfa.get("evenStrengthGoalsForPct")),
            to_int(gfa.get("powerPlayGoalFor")),
            to_int(gfa.get("powerPlayGoalsAgainst")),
            to_int(gfa.get("shortHandedGoalsFor")),
            to_int(gfa.get("shortHandedGoalsAgainst")),
        ))
    return out
```

### 2b. Новый метод `build_player_shot_types()`

Один запрос: `shottype`.

### 2c. Расширить `build_player_season_stats()`

Добавить 2 дополнительных запроса:

- `faceoffpercentages` → `fop_by_player`
- `shootout` → `so_by_player`

Мержить по `playerId`, добавить 7 полей к каждому tuple.

### 2d. Обновить `run()` — вызовы + TRUNCATE + INSERT

- Добавить TRUNCATE для `players_advanced_stats`, `players_shot_types`
- Добавить вызовы `build_player_advanced_stats()`, `build_player_shot_types()`
- Добавить `execute_insert()` для каждой

---

## Шаг 3: Whitelist базы данных

Файл: `telegram_bot/database.py`

- Добавить `"players_advanced_stats"`, `"players_shot_types"` в `ALLOWED_TABLES`
- Добавить новые колонки в `ALLOWED_COLUMNS`:
  - advanced: `sat_pct`, `usat_pct`, `goals_pct`, `oz_start_pct`, `dz_start_pct`, `nz_start_pct`, `on_ice_shooting_pct`, `ev_goals_for`, `ev_goals_against`, `ev_goals_for_pct`, `pp_goals_for`, `pp_goals_against`, `sh_goals_for`, `sh_goals_against`
  - shot types: `goals_wrist`, `shots_wrist`, `goals_slap`, `shots_slap`, `goals_snap`, `shots_snap`, `goals_backhand`, `shots_backhand`, `goals_tip_in`, `shots_tip_in`, `goals_deflected`, `shots_deflected`, `goals_wrap_around`, `shots_wrap_around`
  - расширение season stats: `oz_faceoff_pct`, `dz_faceoff_pct`, `nz_faceoff_pct`, `shootout_goals`, `shootout_shots`, `shootout_pct`, `shootout_gd_goals`

---

## Шаг 4: Бот — новые обработчики

### 4a. Новые функции в `telegram_bot/bot_messages.py`

- `player_advanced_stats()` — топ-10 по Corsi% (или Fenwick%, PDO и т.д.)
- `player_shot_types_stats(player_name)` — разбивка по типам бросков (специфичный формат)
- Расширить существующий `player_stats()` для работы с новыми таблицами/колонками

### 4b. Jinja2-шаблоны

- `messages/season_leaders_advanced.txt` — шаблон для Corsi/Fenwick топ-10
- `messages/player_shot_types.txt` — шаблон для типов бросков

### 4c. Новые callback ID в `telegram_bot/dialog_states.py`

Добавить state-константы: `PLAYER_CORSI`, `PLAYER_SHOT_TYPES`, (другие по необходимости).

### 4d. Обработчики в `telegram_bot/stats_handlers.py`

```python
bot_player_corsi = _make_stats_handler(
    partial(player_stats, 'Лидеры по Corsi%', 'players_advanced_stats', 'sat_pct')
)
bot_player_shot_types = _make_stats_handler(
    partial(player_stats, 'Лидеры по голам с кистевого', 'players_shot_types', 'goals_wrist')
)
```

### 4e. Обновить меню в `telegram_bot/bot.py` — регистрация новых хендлеров

---

## Шаг 5: Тестирование

- `make db-sync-local` — применить новые DDL
- `make season-load` — загрузить данные с новыми отчётами
- `make bot` — проверить новые пункты меню

---

## Какие отчёты НЕ включены (и почему)

- `realtime` — большинство данных дублирует `summary` (уже в `players_season_stats`)
- `penalties` — низкий приоритет, специфичные данные
- `penaltykill`, `powerplay` — частично есть в `summary`
- `summaryshooting` — пересекается с `summary`
- `scoringRates` — API тайм-аутит; можно добавить позже в `players_advanced_stats`
- `scoringpergame` — легко вычислить из имеющихся данных
- `penaltyShots` — редко используется
- `bios` — профиль игрока, не числовая статистика
- `percentages` — PDO интересен, но дублирует `puckPossessions` + `goalsForAgainst`
