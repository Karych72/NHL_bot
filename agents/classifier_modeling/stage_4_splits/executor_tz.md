# ТЗ для исполнителя: этап 4 — временные сплиты без утечки

**Роль:** инженер-исполнитель.  
**Источник требований:** `plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`, раздел «### 4. Временные сплиты без утечки» **с обязательным учётом** «Сквозные требования (читать перед каждым этапом)» (раздел перед списком этапов).  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Связанные ТЗ темы:** [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) (этап 1, контракт входа — переиспользуется как источник `X`, ключей, меток).

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать модуль `modeling/splits.py` — единственный источник истины для **walk-forward** временных сплитов прематч-классификаторов. Сплиты обязаны:

1. Не допускать утечки информации из будущего в train ни в одном из последующих этапов (метрики, обучение, калибровка, holdout-отчёт).
2. Обеспечивать минимум 5 усредняющих outer-окон, чтобы bootstrap-ДИ (этап 6) были информативны.
3. Внутри каждого outer-окна выдавать три **последовательных непересекающихся** блока сразу за train: `inner_val_k`, `calibration_k`, `test_k`.
4. Выделять единый финальный `holdout` строго после всех `test_k` walk-forward.
5. Быть полностью детерминированными при фиксированном входе и конфиге.

Этап **не реализует** обучение моделей, калибровку, метрики или CLI `train` — только сплиты + их валидацию + тесты + минимальную интеграцию с конфигом.

---

## 2. Сквозные требования (обязательны к применению на этом этапе)

Из раздела «Сквозные требования» UPDATE-плана для `modeling/splits.py` действуют **все** перечисленные пункты:

- **Источник истины фич — `metadata_train.json`.** Сплиттер сам по себе не читает фичи, но если получает `metadata` (например, для парити-проверок) — расхождение `feature_manifest` / `features_hash` / `feature_set_version` между YAML и метадатой считается `ConfigError` с диффом полей.
- **Воспроизводимость.** Любой случайный шаг (если вообще понадобится — на этом этапе быть его не должно, см. §3) выводится из единого `random_seed` из YAML. Никаких локальных `np.random` без явного `Generator(seed=...)`.
- **Лимит потоков (`compute.num_threads`).** Сам сплиттер однопоточен; параметр сюда не пробрасывается, но любые вспомогательные операции (если добавятся) не должны автоматически захватывать все CPU.
- **Логи.** Сплиттер не создаёт `run.log` сам — это делает CLI. Но **обязан** выдавать структурированный `dict` / `dataclass` с метаданными каждого outer-блока (даты, число игр, диапазоны `game_id`), пригодный для записи в `metrics.json` и `run.log` на этапе 10.
- **`<run_id>`.** Сплиттер не генерирует id, но возвращаемые структуры не должны зависеть от текущего времени — иначе нарушается воспроизводимость.
- **Никакого доступа к PostgreSQL.** `modeling/splits.py` **запрещено** импортировать `psycopg2`, `modeling.dataset_builder.*` и любые модули, которые тянут БД-зависимости. Вход — только результат `load_training_table_split(...)` из `modeling/train_input.py` (этап 1).

---

## 3. Объём работ

### 3.1 Модуль `modeling/splits.py`

Реализовать **публичный API**, минимум следующего вида (имена допустимо уточнять, семантика — фикс):

- Датакласс конфигурации сплитов (например `SplitConfig`), описывающий поля из YAML раздела `split.*`:
  - `method: Literal["calendar_month", "fixed_games"]`;
  - `n_test_windows: int` — **≥ 5**, иначе `ConfigError`;
  - `inner_val_games: int` — **≥ 300**, иначе `ConfigError`;
  - `calibration_games: int` — **≥ 300**, иначе `ConfigError`;
  - `holdout_fraction_or_date_range`: либо `float ∈ (0, 0.5)` (доля времени, рекомендуется 0.15–0.20), либо явный диапазон дат (`{from: "YYYY-MM-DD", to: "YYYY-MM-DD"}`). Иные значения — `ConfigError`.
  - При `method == "fixed_games"` обязателен `outer_block_games: int` (≥ `inner_val_games + calibration_games + minimal_test_games`).
- Функция (или класс) построения сплитов на входе:
  - Pandas-DataFrame ключей с обязательными колонками `day` (datetime64) и `game_id`;
  - `SplitConfig`.
  Возвращает упорядоченную последовательность outer-окон + единый holdout. Каждое окно — структура с явными полями:
  - `k: int` (1..n_test_windows);
  - `train_idx`, `inner_val_idx`, `calibration_idx`, `test_idx` — массивы **позиционных индексов** в отсортированном датафрейме (или массивы `game_id` — выбрать одно и зафиксировать в docstring);
  - `train_days`, `inner_val_days`, `calibration_days`, `test_days` — диапазоны `[min_day, max_day]` для логов/отчётов;
  - `train_size`, `inner_val_size`, `calibration_size`, `test_size`.
  Отдельно — структура `holdout` с теми же полями (без `inner_val`/`calibration`, но плюс `holdout_days`, `holdout_size`).

### 3.2 Семантика walk-forward

- **Стабильная сортировка** входа по `(day, game_id)` перед любой нарезкой. Не полагаться на исходный порядок DataFrame.
- **Сначала отрезается holdout** с конца таймлайна по `holdout_fraction_or_date_range`. Всё, что попало в holdout, **исключается** из walk-forward.
- Оставшийся диапазон делится на outer-окна по `method`:
  - `calendar_month`: outer-окно = последний хвост вида `[inner_val_k | calibration_k | test_k]`, где `test_k` равен **очередному календарному месяцу** (после предыдущего `test_{k-1}`); `inner_val_k` и `calibration_k` — последние `split.inner_val_games` и `split.calibration_games` строк train-префикса перед `test_k`;
  - `fixed_games`: то же, но `test_k` равен фиксированному числу игр (`outer_block_games − inner_val_games − calibration_games`).
- Для каждого `k`: `train_k` — **всё, что строго раньше** начала `inner_val_k` (полный история до этого момента — expanding window). Никаких rolling-окон train на этом этапе.
- `inner_val_k`, `calibration_k`, `test_k` — три последовательных **непересекающихся** блока сразу после `train_k`.
- Финальный holdout — после всех `test_k`. Если последний `test_k` пересекается с holdout по `game_id` — это баг конфигурации, fail-fast `SplitError`.

### 3.3 Валидация на выходе (внутри сплиттера, не только в тестах)

Перед возвратом сплитов модуль **обязан** проверить и при нарушении бросить `SplitError`:

1. Монотонность по времени для каждого outer-блока:
   `max(day(train_k)) < min(day(inner_val_k)) ≤ max(day(inner_val_k)) < min(day(calibration_k)) ≤ max(day(calibration_k)) < min(day(test_k)) ≤ max(day(test_k)) < min(day(holdout))`.
2. Отсутствие пересечения `holdout` с любым `train_k`/`inner_val_k`/`calibration_k`/`test_k` по `game_id`.
3. Попарно: `inner_val_k`, `calibration_k`, `test_k` не пересекаются с одноимёнными блоками других окон **по своим временным границам**; train-блоки могут пересекаться между окнами (expanding window — это нормально).
4. `|inner_val_k| ≥ split.inner_val_games`, `|calibration_k| ≥ split.calibration_games`, `|test_k| > 0`, `|train_k| > 0`.
5. `n_test_windows` фактически построено столько, сколько задано конфигом; если данных не хватает — fail-fast с понятным сообщением «не хватает истории для N окон при текущих параметрах».

### 3.4 Embargo

- Embargo **не используется**. Это политика проекта, а не TODO.
- Зафиксировать решение **комментарием в `splits.py`** с явной ссылкой на этап 4 UPDATE-плана и обоснованием: rolling-фичи в датасете построены с `shift(1)` и не используют признаки «текущего» матча, поэтому утечка через границу блока исключена.

### 3.5 Запреты в `modeling/`

- В `modeling/splits.py` (и в любом коде, который сюда импортируется из `modeling/`) **запрещены**: `KFold(shuffle=True)`, `StratifiedKFold(shuffle=True)`, `ShuffleSplit` по матчам, `train_test_split` без явного temporal-протокола.
- Если требуется случайность (на этом этапе не должна требоваться), — только `numpy.random.default_rng(seed)` с seed из `random_seed`.

### 3.6 Интеграция с конфигом этапа 2

- Этап 2 (`modeling/config.py`) может быть ещё не реализован к моменту работы над этим этапом. Допустимо:
  - Либо завести в `modeling/splits.py` собственный `SplitConfig` (dataclass) и принимать его прямо;
  - Либо завести минимальный загрузчик YAML только для секции `split.*` (не подменять полный конфиг этапа 2).
- При расхождении YAML и `metadata_train.json` (по полям `feature_set_version`, `features_hash`, `rolling_windows`) — `ConfigError` с диффом, даже если эти поля на сплиттер напрямую не влияют. Это требование общего раздела «Сквозные требования» и проверять его удобнее на входе сплиттера, чем дублировать на каждом этапе.

---

## 4. Тесты

Создать `tests/test_modeling_splits.py`. Минимум:

1. **Монотонность по времени** для каждого outer-блока на синтетическом календарном датасете (≥ 2 сезонов, ≥ 5 outer-окон): `max(day(train_k)) < min(day(inner_val_k)) < min(day(calibration_k)) < min(day(test_k)) < min(day(holdout))`.
2. **Отсутствие пересечения holdout** с train/val/cal по `game_id` — для всех `k`.
3. **Минимальные размеры**: `|inner_val_k| ≥ split.inner_val_games`, `|calibration_k| ≥ split.calibration_games`.
4. **Стабильная сортировка**: на двух перетасованных копиях одного и того же датафрейма сплиттер возвращает идентичные структуры (по `game_id` в каждом блоке).
5. **Fail-fast** на конфигах:
   - `n_test_windows < 5` — `ConfigError`;
   - `inner_val_games < 300` или `calibration_games < 300` — `ConfigError`;
   - данных не хватает на запрошенное число окон — `SplitError` с понятным сообщением.
6. **Embargo-комментарий**: тест (или статическая проверка в `test_modeling_no_shuffle_cv.py`) подтверждает, что в `modeling/splits.py` есть явный комментарий с «embargo» и обоснованием.

Параллельно — `tests/test_modeling_no_shuffle_cv.py`: AST-запрет `KFold(shuffle=True)`, `StratifiedKFold(shuffle=True)`, `ShuffleSplit`, `train_test_split` без `shuffle=False` в любом файле под `modeling/`. Этот тест относится формально к этапу 11, но его базовая версия должна появиться вместе с этим этапом, чтобы запрет работал сразу — иначе случайный `shuffle=True` в следующих этапах не отловится.

Желательно (не блокер): мини-проверка `tests/test_modeling_no_db_access.py` хотя бы для `modeling/splits.py` — что модуль не импортирует `psycopg2` и `modeling.dataset_builder.*`. Полный охват остальных файлов — на этапе 11.

---

## 5. Критерии приёмки

1. `modeling/splits.py` реализован, принимает DataFrame ключей + `SplitConfig`, возвращает упорядоченные outer-окна и единый holdout.
2. Все 5 проверок §3.3 выполняются **внутри** сплиттера и бросают `SplitError`/`ConfigError` с понятными сообщениями.
3. Embargo-комментарий присутствует в `splits.py` с явной ссылкой на этап 4 UPDATE-плана.
4. Тесты §4 проходят локально (`pytest tests/test_modeling_splits.py tests/test_modeling_no_shuffle_cv.py`).
5. `modeling/splits.py` **не импортирует** `psycopg2`, `modeling.dataset_builder.*`, `sklearn.model_selection.KFold(shuffle=True)`/`StratifiedKFold(shuffle=True)`/`ShuffleSplit`.
6. Документация: короткий раздел в `docs/modeling_training.md` (создать, если этого файла ещё нет, по образцу `docs/modeling_dataset_builder.md`) с диаграммой `[ train_k | inner_val_k | calibration_k | test_k ] ... [ holdout ]` и таблицей обязательных полей YAML `split.*`. Если файл `docs/modeling_training.md` уже создан на этапе 1 — добавить раздел туда.

---

## 6. Ограничения и вне скоупа

- Не реализовывать метрики (этап 5), bootstrap (этап 6), обучение logreg/LGBM (этапы 7–8), калибровку (этап 9), CLI `train` (этап 10) — даже минимальные заготовки. Сплиттер должен быть пригоден к подключению из этих этапов, но не зависеть от них.
- Не менять `modeling/train_input.py`, `modeling/dataset_builder/*` без явной необходимости. Если выявится расхождение контрактов — отдельный пункт в MR с обоснованием.
- Не вводить `random_state` параметры в API сплиттера — детерминизм обеспечен сортировкой по `(day, game_id)`, а не рандомом.
- Не делать early-stopping / hyperparameter search в этом этапе — это этапы 7–8.

---

## 7. Что отдать в MR

- `modeling/splits.py` + `SplitConfig` (или эквивалент).
- `tests/test_modeling_splits.py`.
- `tests/test_modeling_no_shuffle_cv.py` (базовая версия запрета — расширяется на этапе 11).
- Опционально: `tests/test_modeling_no_db_access.py` (минимальная проверка для `splits.py`).
- Раздел в `docs/modeling_training.md` (или создание этого файла) с описанием схемы сплитов.
- В описании MR — явная отметка: «реализован этап 4 UPDATE-плана; этапы 5–10 не затрагиваются».
