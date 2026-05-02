# План: сборка датасета NHL для обучения и для предикта

**Статус:** проектирование (отдельный план от modeling).

**Цель документа:** задать безошибочный и воспроизводимый процесс формирования датасета:
- для **обучения** (`train`): признаки + метки;
- для **инференса** (`predict`): те же признаки без меток;
- с жёстким контролем утечки, качества данных и версионирования.

**Почему это критично:** ошибка в сборке датасета (особенно leakage или несовпадение train/predict-фичей) полностью обесценивает метрики модели и прогнозы в проде.

---

## 1. Источники данных и фактический контракт схемы

Используются таблицы:
- `games` (`game_id`, `day`, `home_team_id`, `away_team_id`, `winner_id`, `season_id`, ...);
- `game_team_stats` (`game_id`, `team_id`, `goals`, `shots`, `pim`, `power_play_*`, ...);
- (опционально позже) `game_player_stats`.

### 1.1 Важные технические нюансы схемы

- В `games` идентификаторы команд типа `bigint`, в `game_team_stats.team_id` тип `int`.  
  **Policy:** в SQL всегда делать явный `CAST` к одному типу (рекомендуется `bigint`), чтобы исключить тихие несовпадения join.
- Базовый ключ матча: `games.game_id` (PK).
- В `game_team_stats` ожидается `UNIQUE (game_id, team_id)`; фактически для завершённой игры должны быть 2 строки (home/away).

### 1.2 Режимы данных

- **Finished games** (для train/валидации): `winner_id IS NOT NULL`.
- **Upcoming games** (для predict): `winner_id IS NULL` и известны `day`, `home_team_id`, `away_team_id`.

---

## 2. Канонический anti-leakage контракт (обязательный)

Для целевого матча `g` с датой `D = g.day`:

- Любая фича команды должна считаться **только** из игр с `day < D`.
- Статистика того же `game_id = g.game_id` запрещена в признаках.
- При отсутствии `timestamp` внутри дня нельзя безопасно использовать «раньшие матчи того же дня», поэтому правило строгое: только `< D`, не `<= D`.

Это правило одинаково для `train` и `predict`.

### 2.1 As-of принцип

Каждая строка датасета строится «как будто мы находимся перед матчем».
Минимальный ключ среза:
- `asof_day` (обычно равен `game_day` матча);
- `target_game_id`.

---

## 3. Архитектура сборки датасета (слои)

Рекомендуемые слои (временные таблицы/CTE или parquet-артефакты):

1. **Base games**
   - Нормализованный список матчей-кандидатов.
2. **Team game facts (long)**
   - 1 строка = команда в конкретном матче, включая opponent stats.
3. **Rolling features (team-level)**
   - Исторические агрегаты команды по предыдущим матчам.
4. **Match features (wide)**
   - Home/away признаки и их разности для целевого матча.
5. **Final datasets**
   - `train`: wide + labels;
   - `predict`: wide без labels + служебные quality-флаги.

---

## 4. Шаги сборки: режим `train`

## 4.1 Step T0: Выбор целевых матчей

Включаем только матчи, где:
- `winner_id IS NOT NULL`;
- `day IS NOT NULL`;
- `home_team_id IS NOT NULL`, `away_team_id IS NOT NULL`;
- `home_team_id != away_team_id`.

Дополнительно:
- ограничение по сезонам (из конфига);
- сортировка всегда `ORDER BY day, game_id`.

## 4.2 Step T1: Построить `team_game_facts`

Цель: для каждого завершённого матча получить две строки (home и away) с симметричной статистикой:
- `team_id`, `opponent_team_id`, `is_home`;
- `goals_for`, `goals_against`;
- `shots_for`, `shots_against`;
- `pim`, `power_play_percentage`, `power_play_opportunities`, и т.д.;
- `game_id`, `day`, `season_id`.

Ключевая проверка:
- для каждого `game_id` должно быть **ровно 2** строки в `game_team_stats`;
- иначе матч исключается и логируется в `data_quality_report`.

## 4.3 Step T2: Team-level rolling фичи с обязательным сдвигом

На уровне команды:
- сортировка `team_id, day, game_id`;
- rolling-агрегаты по окнам `N` (например 5/10/20);
- логический эквивалент `shift(1)` обязателен (то есть текущая игра не входит в свои фичи).

Примеры:
- `gf_roll_mean_N`, `ga_roll_mean_N`, `goal_diff_roll_mean_N`;
- `shots_for_roll_mean_N`, `shots_against_roll_mean_N`;
- `pp_pct_roll_mean_N`, `pim_roll_mean_N`;
- `rest_days`, `is_b2b`, `games_last_7d`.

## 4.4 Step T3: Match-level wide фичи

Для каждого целевого `game_id`:
- взять home rolling snapshot;
- взять away rolling snapshot;
- сформировать:
  - home/away абсолютные фичи;
  - разности `home - away`;
  - симметричные суммы (особенно для тотала): например `pace_sum`, `defense_sum`.

## 4.5 Step T4: Сбор меток

- `y_home_win = 1`, если `winner_id = home_team_id`, иначе `0`.
- `y_over_5_5 = 1`, если `home_goals + away_goals > 5.5`, иначе `0`.

Для `y_over_5_5` источник:
- предпочтительно из `game_team_facts` (`goals_for`) после валидации «2 команды на матч».

## 4.6 Step T5: Политика холодного старта

Параметры конфига:
- `min_prior_games` (например 5);
- `cold_start_policy_train = drop`.

Правило:
- если у home или away меньше `min_prior_games` валидной истории, матч исключается из train.
- сохранить флаг причины исключения в отчёт качества.

## 4.7 Step T6: Финальная валидация train-датасета

Fail-fast проверки:
- уникальность `game_id` в финальном датасете;
- отсутствие NaN в обязательных числовых фичах;
- отсутствие бесконечностей;
- корректный диапазон вероятностных/процентных полей (например `pp_pct` в [0,1] или согласованном масштабе);
- `row_count > min_rows_threshold` (из конфига).

---

## 5. Шаги сборки: режим `predict`

`predict` обязан использовать тот же код построения фич, что и `train` (без дублирования логики).

## 5.1 Step P0: Выбор матчей для прогноза

Матчи-кандидаты:
- `winner_id IS NULL`;
- `day = target_day` (или диапазон дней);
- заполнены `home_team_id`, `away_team_id`, `day`.

## 5.2 Step P1: Исторический срез для фич

Для каждого матча с датой `D`:
- в расчёт rolling идут только игры с `day < D`;
- завершённые игры (`winner_id IS NOT NULL`) используются как история.

## 5.3 Step P2: Построить wide фичи (тот же feature_set_version)

Тот же набор колонок и тот же порядок, что в train:
- проверка `feature_manifest` (имена, типы, порядок);
- если колонка отсутствует/лишняя — pipeline падает до предикта.

## 5.4 Step P3: Политика холодного старта в предикте

Параметры:
- `cold_start_policy_predict`:
  - `drop` (не предсказывать матч),
  - или `allow_with_flag` (разрешить, но с флагом низкой надёжности).

Рекомендуемый v1:
- `allow_with_flag`, но в UI/боте показывать disclaimer и признак `low_history_confidence = 1`.

## 5.5 Step P4: Финальная валидация predict-датасета

- уникальность `game_id`;
- точное совпадение feature schema с train artifact;
- отсутствие запрещённых полей (никаких `winner_id`, итоговых голов и т.п.);
- датафрейм не пустой (иначе «нет матчей для прогноза» как штатный сценарий).

---

## 6. Data contract финальных датасетов

## 6.1 `dataset_train`

Обязательные поля:
- ключи: `game_id`, `day`, `season_id`, `home_team_id`, `away_team_id`;
- метки: `y_home_win`, `y_over_5_5`;
- признаки: все колонки из `feature_set_version`;
- служебные: `feature_set_version`, `dataset_built_at`, `source_snapshot_id`.

Ограничения:
- одна строка на матч (`game_id` уникален);
- метки бинарные {0,1}.

## 6.2 `dataset_predict`

Обязательные поля:
- ключи: `game_id`, `day`, `home_team_id`, `away_team_id`;
- признаки: 1:1 со schema train (кроме y);
- служебные: `feature_set_version`, `dataset_built_at`, `low_history_confidence`, `quality_warnings`.

---

## 7. Версионирование и воспроизводимость

Фиксировать и сохранять вместе с датасетом:
- `feature_set_version`;
- `features_hash` (имена + порядок + параметры окон + cold-start policy);
- `data_snapshot_id` (границы дат, список сезонов, время выгрузки);
- `code_version` (git commit hash);
- `random_seed` (если есть стохастические шаги).

Правило:
- модель может делать предикт только если `feature_set_version` и `features_hash` совпадают с обучающим артефактом.

---

## 8. Проверки качества данных (обязательный чеклист)

Перед публикацией любого датасета:

1. **Completeness**
   - нет NULL в ключевых ключах;
   - есть минимум строк.
2. **Uniqueness**
   - `game_id` уникален в финальном wide.
3. **Consistency**
   - в train метки согласованы с `games.winner_id` и суммой голов;
   - для каждого использованного исторического матча есть валидная пара команд.
4. **Range checks**
   - статистики в допустимых диапазонах;
   - `rest_days >= 0`.
5. **Leakage checks**
   - ни один feature-row не использует текущий `game_id`;
   - все источники фич имеют `hist_day < target_day`.

При провале любого пункта:
- датасет помечается `FAILED`;
- инференс/обучение не запускаются.

---

## 9. Автотесты (минимальный обязательный набор)

`tests/test_modeling_dataset_build.py`:

1. **test_no_same_game_leakage**
   - проверяет, что фичи матча не зависят от его же статистики.
2. **test_strict_past_only_by_day**
   - проверяет контракт `hist_day < target_day`.
3. **test_train_predict_feature_parity**
   - schema train (без y) == schema predict.
4. **test_two_team_rows_or_drop**
   - матчи с неконсистентным `game_team_stats` корректно отбрасываются/логируются.
5. **test_cold_start_policy**
   - корректное поведение при недостатке истории.

---

## 10. Порядок реализации (практический roadmap)

1. Реализовать `dataset_builder/base.py`: выборка матчей train/predict, базовые фильтры.
2. Реализовать `dataset_builder/team_game_facts.py`: канонический long слой + проверки парности команд.
3. Реализовать `dataset_builder/features.py`: rolling + rest/b2b (единый код для train/predict).
4. Реализовать `dataset_builder/assemble.py`: wide-склейка + labels (только train).
5. Реализовать `dataset_builder/validate.py`: полный data-quality и anti-leakage чеклист.
6. Реализовать CLI:
   - `python -m modeling.cli build-dataset --mode train ...`
   - `python -m modeling.cli build-dataset --mode predict ...`
7. Добавить автотесты и golden fixture на небольшом срезе.

---

## 11. Критерии готовности (Definition of Done)

Сборка считается готовой, если:

1. Есть две команды сборки (`train` и `predict`) и обе воспроизводимы.
2. Все проверки из раздела 8 выполняются автоматически.
3. Автотесты anti-leakage и schema parity зелёные.
4. Для train и predict формируется metadata-файл с версиями/хэшами.
5. При несовпадении feature schema инференс блокируется до исправления.

---

## 12. Операционные рекомендации

- Никогда не «чинить» дырки в predict ручным добавлением колонок вне общего feature builder.
- Любое изменение фичи (формула, окно, cold-start policy) повышает `feature_set_version`.
- Логи сборки хранить как артефакт рядом с датасетом (`data_quality_report.json`).
- На старте каждого дня запускать `predict`-сборку сначала в режиме `validate-only`, затем full run.

