# Пайплайн NHL: семантика `NULL` и оставшиеся явные default

Документ фиксирует контракт «нет данных в источнике → SQL `NULL`» в загрузчике
PostgreSQL и список оставшихся **намеренных** non-NULL default. Исходный
сценарий (до рефакторинга) сохранён в §4 «Архив».

**Исходный код записи в БД:** только `pipeline/load_season_modern.py` (вставки
через `psycopg2.extras.execute_values`). Бот и modeling в Postgres матчи не
пишут.

---

## 1. Текущий контракт коэрсии

В загрузчике две семьи хелперов:

| Семья | Поведение при `None` / `""` / невалидном вводе |
|-------|------------------------------------------------|
| `to_int(value, default=0)` | возвращает **eager default** (`0`). Используется только для **обязательных** колонок (PK / `NOT NULL`) или там, где бизнес-логика требует конкретного значения. |
| `optional_int`, `optional_float`, `optional_str`, `optional_pct_from_ratio`, `optional_split_sv`, `optional_seconds_to_mmss`, `optional_mmss`, `optional_age_from_birthdate`, `safe_pct` | возвращают `None` → в Postgres попадает **`NULL`**. Используются для всех nullable колонок. |

`safe_pct(numerator, denominator)` отдельно: возвращает `None`, если знаменатель
`None` или ≤ 0 (нет попыток / возможностей). Раньше в этом случае писали `0.0`,
что было неотличимо от «реального 0%».

## 2. Где теперь пишется `NULL` вместо синтетического значения

Все колонки ниже — **nullable** в DDL (`data_tables/t.*.sql`), отдельной
миграции схемы не потребовалось.

### 2.1. `all_goals`

| Колонка (DDL) | Раньше при отсутствии в API | Теперь |
|---------------|-----------------------------|--------|
| `goal_player_id`, `assist_player1_id`, `assist_player2_id` | сентинел **-9999** | **`NULL`** |
| `total_goals`, `assist_total_1`, `assist_total_2` | **0** | **`NULL`** |
| `period`, `time` | **0** / **`""`** | **`NULL`** |
| `goals_away`, `goals_home`, `event_id` | **0** | **`NULL`** |

Внутри одной игры PPG/SHG-агрегации (`player_pp_assists`, `player_sh_goals`,
`player_sh_assists`) теперь фильтруют `is not None` вместо сравнения с `-9999`.

### 2.2. `game_player_stats`

| Колонка | Раньше | Теперь |
|---------|--------|--------|
| `time_on_ice` | **`"00:00"`** при отсутствии `toi` | **`NULL`** |
| `assists`, `goals`, `shots`, `hits`, `power_play_goals`, `penalty_minutes`, `takeaways`, `giveaways`, `blocked`, `plus_minus` | **0** | **`NULL`**, если поле не пришло |
| `face_off_pct` | **0.0** | **`NULL`** |

`power_play_assists`, `face_off_wins`, `face_off_taken`, `short_handed_goals`,
`short_handed_assists` агрегируются из PBP, поэтому остаются с **0**: «реально
не было таких событий у игрока в этой игре».

### 2.3. `game_goalie_stats`

| Колонка | Раньше | Теперь |
|---------|--------|--------|
| `timeonice` | **`"00:00"`** | **`NULL`** для отсутствующих |
| `assists`, `goals`, `pim`, `shots`, `saves`, `power_play_saves`, `short_handed_saves`, `even_saves`, `*_shots_against` | **0** | **`NULL`** при отсутствии |
| `decision` | **`False`** для всего, что не `"W"` | **`NULL`**, если в API нет поля |
| `save_percentage`, `*_save_percentage` | **0.0** при `pp_shots == 0` и т.д. | **`NULL`** через `safe_pct` |

### 2.4. `game_team_stats`

| Колонка | Раньше | Теперь |
|---------|--------|--------|
| `power_play_percentage` | **0.0** при нулевых попытках | **`NULL`** |
| `face_off_win_percentage` | **0.0** при нулевых вбрасываниях | **`NULL`** |
| `shots` (`sog`) | **0** при отсутствии в боксе | **`NULL`** |

PIM по командам считается суммой событий-пенальти из PBP. Если у пенальти нет
поля `duration`, оно **пропускается** (раньше подставляли **2**). При полном
отсутствии пенальти у команды значение остаётся **0** — это валидное «без
пенальти», а не отсутствие данных.

### 2.5. `players_season_stats` / `goalies_season_stats`

| Колонка | Раньше | Теперь |
|---------|--------|--------|
| Все TOI-строки (`time_on_ice`, `*_time_on_ice`, `*_time_on_ice_per_game`) | **`"00:00"`** | **`NULL`**, если в `timeonice` отчёте нет поля или там `0` |
| Все целочисленные счётчики | **0** | **`NULL`** при отсутствии (реальный 0 сохраняется) |
| Все процентные метрики (`shootout_pct`, `face_off_pct`, `savePct`, …) | **0.0** | **`NULL`** при отсутствии |
| `goalies_season_stats.time_on_ice` / `time_on_ice_per_game` | жёстко **`"00:00"`** (поля нет в summary/savesByStrength) | **`NULL`** с комментарием |
| `goalies_season_stats.power_play_save_percentage` и аналоги | **0.0** при `pp_shots == 0` | **`NULL`** через `safe_pct` |

### 2.6. `players_advanced_stats` / `players_shot_types`

Все nullable числовые поля (Corsi/Fenwick/GF%/zone-start/shot-type счётчики и
т.п.) переведены на `optional_*`: реальный 0 сохраняется как 0, а отсутствие
поля в API — как `NULL`.

### 2.7. `rosters`

| Колонка | Раньше | Теперь |
|---------|--------|--------|
| `name`, `lastname`, `position`, `nationality`, `abbreviation` | **`""`** при `default = ""` | **`NULL`** через `optional_str` |
| `jersey_number` | **0** | **`NULL`** при отсутствии |
| `currentage` | **0** при пустой / невалидной `birthDate` | **`NULL`** |
| `captain`, `alternate_captain`, `rookie` | **`False`** | **`NULL`** — поля не приходят с используемых эндпоинтов, статус «неизвестно» отличается от «точно нет» |
| `current_team_id` | уже было **`NULL`** для landing | без изменений |

### 2.8. `teams` / `teams_stats`

Числовые статы команды используют `optional_int` / `optional_float` /
`optional_pct_from_ratio`. Названия / город / аббревиатура нормализуются через
`optional_str` (пустая строка → `NULL`).

`arena`, `first_year_of_play` остаются `NULL` — эти поля не приходят с
используемых эндпоинтов (используется stats summary, а не v1/teams/{abbrev}).

---

## 3. Намеренные не-`NULL` default (после рефакторинга)

| Поле / контекст | Значение | Обоснование |
|-----------------|----------|-------------|
| Любые ключи: `player_id`, `team_id`, `season_id`, `game_id` | `to_int(...)` (0 при ошибке) | DDL объявляет их `NOT NULL` / PK; «без id» — это сломанная запись, её бы отбраковала БД. |
| `games.is_overtime`, `games.is_shootouts` | bool, выводятся из PBP | детерминированно вычисляются из `periodDescriptor` / `shootoutInUse`, а не из API-поля. |
| `all_goals.empty_net`, `winner_goal`, `is_ppg`, `is_shg` | bool, выводятся из `situationCode` / `goalModifier` | если строка ситуации пустая, считаем «не PPG / не SHG / не EN» по факту разбора. Колонки nullable в DDL, но семантика «не определили → не было». |
| `game_team_stats.pim`, `hits`, `blocked`, `takeaways`, `giveaways`, `*_period_goals` | агрегаты из PBP (могут быть 0) | сумма событий PBP — реальный 0 неотличим от «событий не было». |
| `game_player_stats.power_play_assists`, `face_off_wins`, `face_off_taken`, `short_handed_goals`, `short_handed_assists` | агрегаты из PBP | то же: 0 = «не было». |
| `goalies_season_stats.ties` | **0** | NHL не фиксирует ties в регулярном времени с локаута 2005 года; API не отдаёт это поле. 0 — известный бизнес-факт. |
| `teams.active` | **`True`** | мы пишем команду, потому что она нашлась в summary за этот сезон, → активна по контракту лоадера. |

Все остальные значения, не попавшие в этот список, должны быть либо реальными
данными, либо `NULL`.

---

## 4. Архив: исходное поведение до рефакторинга (для истории)

> Свёрнутая таблица из старой версии документа. Оставлена, чтобы при ревью
> миграции старых данных был ориентир, **что именно** заменялось на `NULL`.
> На код после рефакторинга **не ссылаться**.

<details>
<summary>Старые eager-default'ы</summary>

| Место | Поведение при отсутствии / ошибке парсинга |
|--------|---------------------------------------------|
| `to_int(value, default=0)` | `None`, `""` или невалидное значение → `default` (по умолчанию **0**). |
| `to_float(value, default=0.0)` | Аналогично → **0.0**. |
| `pct_from_ratio(value, default=0.0)` | `None` → **0.0**; доли ≤ 1 умножаются на 100. |
| `split_sv(value)` | Пустая строка или нет `"/"` → **(0, 0)**. |
| `seconds_to_mmss(value, default="00:00")` | `None` → 0 секунд → строка **`00:00`**. |
| `age_from_birthdate(birth_date)` | Пусто / невалидная дата → **0**. |
| `all_goals` IDs | сентинел **-9999** для `scoringPlayerId` / `assist1` / `assist2`. |
| `all_goals` totals | **0**. |
| Penalty `duration` | **2** минуты, если поля нет. |
| Faceoff `winningPlayerId` / `losingPlayerId` | **0**; строки с `pid == 0` не копились в счётчики. |
| Box score TOI | `p.get("toi") or "00:00"` (и для скейтеров, и для вратарей). |
| Roster `captain` / `alternate_captain` / `rookie` | жёстко **`False`**. |
| Goalie season `time_on_ice` / `time_on_ice_per_game` | жёстко **`"00:00"`** в summary/savesByStrength. |

</details>

---

## 5. Миграция уже загруженных данных (вне scope этого ТЗ)

В рамках задачи **новые** загрузки пишут `NULL` корректно. Сезонные таблицы
обновляются через UPSERT — повторная загрузка сезона нивелирует прошлые
синтетические нули. Матчевые таблицы переписываются по `game_id` для окна
дат: чтобы переписать всё, можно перезапустить лоадер на нужное окно
(`make season-load-full SEASON_START=…`).

Если требуется пересчитать все исторические данные точечным `UPDATE …` без
переисточника, это отдельная задача (TZ §2.4 остаётся вне scope).

---

## 6. Тестовое покрытие

- `tests/test_pipeline_optional_helpers.py` — юнит-тесты §1 на `to_int`,
  `optional_*` и `safe_pct`, плюс контрактные тесты `build_game_rows` на
  синтетическом payload (PPG-флаг, отсутствие `-9999`, NULL для пропущенного
  `toi` / `decision` / `duration`).
- `tests/test_pipeline_season_rows.py` и `tests/test_pipeline_game_rows.py` —
  сборка строк для всех целевых таблиц на урезанных **реальных** ответах API
  (`tests/fixtures/nhl_*.json`, снимаются `scripts/capture_nhl_fixtures.py`):
  для каждой таблицы из §2 показаны оба случая — пришедшее значение (включая
  настоящий 0) сохраняется, отсутствующее поле даёт `None`; отдельно
  проверены намеренные не-`NULL` default из §3.
- `tests/test_db_nhl.py` (включается через `RUN_DB_DATA_TESTS=1`) — ссылочная
  целостность; запускается после `make season-sync-month` на тестовом окне.

После реальной загрузки можно сверить долю `NULL` через
`artifacts/reports/nulls_by_season_report.py` — для новых сезонов колонки из
§2 должны иметь ненулевую долю `NULL` там, где API эпизодически опускает поля.

---

## 7. Ссылки

- Реализация: `pipeline/load_season_modern.py`, секция «Coercion helpers» в
  начале файла + методы `build_*` / `build_game_rows`.
- Использование в боте: `telegram_bot/bot_messages.py.game_message` (рендер
  голов и вратарей корректно обрабатывает `None`), `telegram_bot/queries/*.sql`
  (используют `LEFT JOIN`, поэтому NULL-id в `all_goals` дают NULL-фамилии в
  тексте «Unknown»).
