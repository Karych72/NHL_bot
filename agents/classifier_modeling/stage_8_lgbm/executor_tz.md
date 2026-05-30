# ТЗ для исполнителя: этап 8 — Primary: LightGBM (без альтернатив в v1)

**Роль:** инженер-исполнитель.  
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 8. Primary: LightGBM (без альтернатив в v1)» **с обязательным учётом** «## Сквозные требования (читать перед каждым этапом)» того же документа.  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Связанные ТЗ темы:**
- [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) — этап 1 (контракт входа). **Единственный** вход в `train_lgbm.py` — `load_training_table_split(...)`; новый загрузчик не писать.
- [`../stage_2_config/executor_tz.md`](../stage_2_config/executor_tz.md) — этап 2 (конфигурация). Сетка `models.lgbm.grids`, `models.lgbm.monotone`, `random_seed`, `compute.num_threads` приходят из конфига.
- [`../stage_4_splits/executor_tz.md`](../stage_4_splits/executor_tz.md) — этап 4 (сплиты). LGBM обучается на `train_k`, early stopping — на `inner_val_k`, оценивается на `test_k`/`holdout`.
- [`../stage_5_metrics/executor_tz.md`](../stage_5_metrics/executor_tz.md) — этап 5 (метрики). Сравнительная таблица logreg vs lgbm строится метриками из `modeling/metrics.py`; свои формулы не дублировать.
- [`../stage_6_bootstrap/executor_tz.md`](../stage_6_bootstrap/executor_tz.md) — этап 6 (bootstrap). ДИ метрик считаются модулем bootstrap, не в `train_lgbm.py`.
- [`../stage_7_logreg/executor_tz.md`](../stage_7_logreg/executor_tz.md) — этап 7 (baseline-логрег). **Парный** этап: `modeling/artifacts.py` уже спроектирован там под переиспользование этим этапом; общий код (выбор конфигурации по `inner_val_k`, нарезка блоков, сохранение артефакта) переиспользуется, не дублируется.
- [`../stage_9_calibration/executor_tz.md`](../stage_9_calibration/executor_tz.md) — этап 9 (калибровка). **Downstream**: `train_lgbm.py` отдаёт **сырые** вероятности, калибровку в этом этапе **не** реализует.
- Этап 10 (CLI `train`, финальное переобучение, production-артефакт, симлинк `latest`) — **downstream**; в этом этапе **не** реализуется (см. §7).
- Этап 11 (тесты) — `tests/test_modeling_lgbm_monotone.py` из плана реализуется в этом этапе (см. §4).

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать `modeling/train_lgbm.py` — обучение **двух независимых** LightGBM-классификаторов (`home_win`, `over_5_5`) на временных сплитах этапа 4, с **фиксированной сеткой** гиперпараметров из YAML, **полным детерминизмом** от единого `random_seed` и **монотонными ограничениями** на физически интерпретируемые фичи.

Модуль обязан:

1. Обучать сырую LGBM-модель **только на `train_k`**, early stopping — по `inner_val_k`; предсказывать на `inner_val_k`/`calibration_k`/`test_k`/`holdout`.
2. Перебирать **фиксированную сетку** из YAML (`models.lgbm.grids`), выбирать лучшую конфигурацию по `binary_logloss` на `inner_val_k`. **Optuna/Bayes в v1 запрещены** (этап 15).
3. Быть **полностью детерминированным**: все seed-параметры LightGBM выводятся из `random_seed`, `deterministic=True`, `num_threads = compute.num_threads`.
4. Применять `monotone_constraints` по списку из манифеста/YAML (`models.lgbm.monotone.{home_win,over_5_5}`).
5. Возвращать предсказания и сырую модель в форме, пригодной для калибровки (этап 9) и отчёта (этап 5) — но **сам** калибровку, CLI, финальное переобучение и production-артефакт **не** реализует.

---

## 2. Сквозные требования (обязательны к применению на этом этапе)

Из раздела «## Сквозные требования (читать перед каждым этапом)» UPDATE-плана для `train_lgbm.py` действуют **все** пункты:

- **Источник истины фич — `metadata_train.json`.** Порядок и состав фич X берутся **строго** из `feature_manifest` (через контракт этапа 1, `load_training_table_split`). `monotone_constraints` строятся **по имени** колонки из этого же манифеста — индексная привязка к фактическому порядку X обязательна (см. §3.4). При расхождении YAML-справки (`feature_set_version`, `rolling_windows`, `features_hash`) с метадатой — `ConfigError` с диффом; эту проверку делает контракт входа этапа 1, `train_lgbm.py` её **не дублирует**, но обязан пробросить ошибку наверх.
- **Воспроизводимость и единый `random_seed`.** **Все** seed-параметры LightGBM (`random_state`/`seed`, `feature_fraction_seed`, `bagging_seed`, `data_random_seed`, `extra_seed` при наличии) выводятся из `random_seed` из YAML — допустимо присвоить им **одно и то же** значение `random_seed`. Независимые/производные seeds (`random_seed + fold`, `random_seed * 7 + grid_idx` и т.п.) **запрещены**. Дополнительно: `deterministic=True`, `force_row_wise=True` (или `force_col_wise=True` — зафиксировать одно), чтобы исключить недетерминизм гистограмм. Версии библиотек (`lightgbm`, `numpy`, `pandas`, `sklearn`) логируются в `metadata.json` артефакта (фактический лог пишет CLI этапа 10; `train_lgbm.py` отдаёт нужные поля наверх).
- **Лимит потоков (`compute.num_threads`).** LightGBM `num_threads` (он же `n_jobs`) **обязан** браться из `compute.num_threads`. Запрещено `num_threads=-1`, `num_threads=0` («все ядра»), `os.cpu_count()`, чтение `OMP_NUM_THREADS`/`MKL_NUM_THREADS`. walk-forward × сетка легко выжигает CPU — это явное требование плана.
- **Логи.** Свой `run.log` `train_lgbm.py` **не** создаёт — это делает CLI этапа 10. Но обязан возвращать структурированный результат (`dataclass`/`dict`) с выбранной конфигурацией сетки, метриками по блокам, числом итераций early stopping, чтобы CLI записал INFO-строки и `metrics.json`.
- **`<run_id>`.** `train_lgbm.py` не генерирует `<run_id>` и не зависит от текущего времени.
- **Никакого доступа к PostgreSQL.** В `modeling/train_lgbm.py` **запрещены** импорты `psycopg2`, `modeling.dataset_builder.*` и любых модулей, тянущих БД. Вход — только структуры от `load_training_table_split`. Проверяется AST-тестом `tests/test_modeling_no_db_access.py` (этап 11); минимум на этом этапе — не вводить таких импортов.

---

## 3. Объём работ

### 3.1 Размещение кода

- Основной модуль — `modeling/train_lgbm.py`.
- Общий с этапом 7 код (выбор конфигурации по `inner_val_k`, нарезка X/y по блокам сплита, сериализация сырого артефакта через `modeling/artifacts.py`) выносить в утилиты (`modeling/train_common.py` или функции в существующем общем модуле) — **без** дублирования между `train_logreg.py` и `train_lgbm.py`.
- Сохранение сырой модели — через `modeling/artifacts.py` (joblib + JSON-метаданные), как в этапе 7. Если `artifacts.py` ещё нет — см. §3.7.

### 3.2 Публичный API

Минимум (имена допустимо уточнять, семантика — фикс):

- Функция обучения одной задачи на одном outer-блоке:
  - вход: `X_train, y_train`, `X_val, y_val` (для early stopping), опционально `X_eval` блоки (`calibration_k`/`test_k`/`holdout`) для предсказаний;
  - параметры: `grid: Sequence[Mapping]` (фиксированная сетка из YAML), `monotone_constraints: Sequence[int]` (или `None`), `random_seed: int`, `num_threads: int`, `early_stopping_rounds: int`, `epsilon`/клиппинг — **не здесь** (клип на стороне `metrics`);
  - возврат: сырая обученная модель лучшей конфигурации + структура результата (выбранные гиперпараметры, `best_iteration`, метрика на `inner_val_k`, предсказания на переданных eval-блоках в виде сырых вероятностей в `[0,1]`).
- Предсказание: `predict_proba`-эквивалент (LightGBM `Booster.predict` уже даёт вероятность при `objective='binary'`) — возвращать массив `[0,1]`, **без** клиппинга (клип — ответственность `metrics`/калибровки).
- Две задачи (`home_win`, `over_5_5`) — **независимые** вызовы, общий код, раздельные модели и артефакты.

### 3.3 Параметры LightGBM (фикс из плана)

- `objective='binary'`, `metric='binary_logloss'`.
- Early stopping — по `inner_val_k` (валидационный сет в `lgb.train(..., valid_sets=[val], callbacks=[lgb.early_stopping(early_stopping_rounds)])`). `early_stopping_rounds` — из YAML (поле сетки или отдельный параметр; согласовать с этапом 2). `num_boost_round` — верхняя граница из YAML; фактическое число — `best_iteration`.
- **Фиксированная сетка** из `models.lgbm.grids`: `num_leaves`, `min_data_in_leaf`, `feature_fraction`, `bagging_fraction`, `lambda_l1`, `lambda_l2`, `learning_rate`. Перебор — полный grid по комбинациям (или плоский список конфигов из YAML — согласовать формат сетки с этапом 2). **Optuna/`optuna.*`/любой Bayes-поиск — запрещены** (этап 15).
- Выбор лучшей конфигурации — по `binary_logloss` на `inner_val_k` (минимум). При равенстве — детерминированный tie-break (первая по порядку из YAML).

### 3.4 Детерминизм (критично)

- В параметрах модели задать **все** seed-поля LightGBM из `random_seed`:
  - `seed` (или `random_state`), `feature_fraction_seed`, `bagging_seed`, `data_random_seed`, `deterministic=True`.
- Зафиксировать `force_row_wise=True` **или** `force_col_wise=True` (одно из, чтобы убрать авто-выбор и связанный недетерминизм/варнинг).
- `num_threads = compute.num_threads` (детерминизм LightGBM гарантируется только при `deterministic=True`; число потоков на детерминизм при `deterministic=True` не влияет, но фиксируется по сквозному требованию лимита потоков).
- **Тест детерминизма** (см. §4): два прогона обучения с одним `random_seed` дают **идентичные** предсказания.

### 3.5 Монотонные ограничения

- Списки знаков задаются в YAML `models.lgbm.monotone.{home_win,over_5_5}` **по имени фичи**; план задаёт направления:
  - `home_win`: **+1** на `diff_goal_diff_roll_mean_*`, `diff_gf_roll_mean_*`; **−1** на `diff_ga_roll_mean_*`;
  - `over_5_5`: **+1** на `sum_gf_roll_mean_*`, `sum_ga_roll_mean_*`.
- Точные имена/паттерны и знаки **берутся из манифеста** (`feature_manifest`) после сборки датасета и фиксируются в YAML. `train_lgbm.py`:
  - строит вектор `monotone_constraints: list[int]` длины `len(feature_manifest)`, **позиционно соответствующий** фактическому порядку колонок X (тот же, что отдаёт контракт этапа 1); для фич без ограничения — `0`.
  - сопоставление YAML-паттернов с реальными именами колонок — детерминированное; **fail-fast**, если паттерн из YAML не сматчил **ни одной** колонки (опечатка в имени) или сматчил фичу, отсутствующую в манифесте — `ConfigError` с понятным сообщением. Молчаливое игнорирование несматченного паттерна **запрещено** (иначе ограничение тихо не применится).
  - если `models.lgbm.monotone` для задачи пуст/не задан — обучать без ограничений, но это явный осознанный кейс (лог INFO), а не результат «паттерн не сматчился».
- `monotone_constraints` передаются в параметры LightGBM (`params['monotone_constraints'] = [...]`).

### 3.6 Контракт ошибок

- Сетка пуста / `models.lgbm.grids` отсутствует — `ConfigError`.
- Паттерн `monotone` не сматчил ни одной колонки или ссылается на фичу вне манифеста — `ConfigError` (см. §3.5).
- `num_threads ≤ 0` — ошибка конфигурации (не «все ядра»).
- Несовпадение длины `monotone_constraints` с числом колонок X — внутренний `AssertionError`/`ValueError` (не должно случаться при корректной сборке; защита от регресса).
- Любая попытка фолда без `inner_val_k` (early stopping невозможен) — понятная ошибка, а не тихое обучение без early stopping.

### 3.7 Если этапы 2 / 7 / `artifacts.py` ещё не готовы

Допустимо (выбрать и зафиксировать в MR):

- Принимать сетку, `monotone`, `random_seed`, `num_threads`, `early_stopping_rounds` как **обычные параметры функции** (не зависеть от dataclass конфига этапа 2). Связь с `modeling/config.py` — на этапе 10.
- Если общий с этапом 7 модуль утилит ещё не создан — создать минимальный `modeling/train_common.py` в этом MR **строго** под общие нужды (нарезка блоков, выбор по `inner_val_k`, сериализация артефакта); не тащить туда логрег-специфику.
- Если `modeling/artifacts.py` ещё нет — реализовать минимальное сохранение (joblib + JSON с `features_hash`, диапазонами дат, числом строк, версиями библиотек, `git_commit` или `null`) по контракту этапа 7 §«Сохранение через `modeling/artifacts.py`». Не расширять сверх минимума.

**Вне скоупа этого этапа** в любом случае: калибровка (этап 9), CLI `train` и финальное переобучение/`latest` (этап 10), полный отчёт/reliability PNG (этап 5). `train_lgbm.py` отдаёт сырые предсказания и модель наверх — собирает их CLI.

---

## 4. Тесты

Создать/дополнить:

1. **`tests/test_modeling_lgbm_monotone.py`** (явно из плана, этап 11):
   - на синтетическом датасете с заведомо **монотонной** зависимостью таргета от одной фичи (например, `y ~ Bernoulli(sigmoid(a*feat))`, `a>0`) обученный LGBM с `monotone_constraints=+1` на этой фиче **не нарушает знак**: при сканировании признака по возрастанию (остальные фиксированы на медиане) предсказанная вероятность **монотонно не убывает**. Для `−1` — симметрично (не возрастает).
   - проверка строится на `Booster.predict` по сетке значений фичи; допустима мягкая толерантность к численному шуму (зафиксировать в комментарии).
2. **Детерминизм:** два обучения с одинаковым `random_seed` на одних данных → `np.testing.assert_allclose(pred_a, pred_b, atol=0)` (идентичные предсказания). Это защита от незаданного/производного seed.
3. **Применение ограничений из имён:** на манифесте-моке проверить, что вектор `monotone_constraints` строится **позиционно по именам**: правильные индексы получают `+1`/`−1`, остальные — `0`; перестановка порядка колонок не ломает соответствие.
4. **Fail-fast на несматченный паттерн:** YAML-паттерн `monotone`, не совпавший ни с одной колонкой манифеста → `ConfigError` (а не тихое игнорирование).
5. **Выбор по `inner_val_k`:** на мини-сетке из 2 конфигов выбирается та, что даёт меньший `binary_logloss` на `inner_val_k` (детерминированный tie-break).
6. **Лимит потоков:** при `num_threads ≤ 0` — ошибка; параметры модели содержат `num_threads = compute.num_threads`, а не `-1`.
7. **Без БД-импортов:** дополнить/проверить `tests/test_modeling_no_db_access.py`, что `modeling/train_lgbm.py` не импортирует `psycopg2`, `modeling.dataset_builder.*`.

Тесты обязаны быть быстрыми (синтетические мини-датасеты, малый `num_boost_round`), без обращения к БД и к реальному `dataset_train.csv`.

---

## 5. Сравнение с logreg (общая таблица отчёта)

- LGBM и logreg (этап 7) обучаются **на тех же сплитах** (одни и те же `train_k`/`inner_val_k`/`calibration_k`/`test_k`/`holdout`).
- `train_lgbm.py` отдаёт метрики/предсказания в форме, совместимой с тем, что отдаёт `train_logreg.py`, чтобы CLI этапа 10 / отчёт этапа 5 свёл их в **одну** сравнительную таблицу. Саму таблицу `train_lgbm.py` **не** рисует — это этап 5/10.
- Если этап 7 ещё не реализован — обеспечить совместимый формат результата (структура `dataclass`/`dict` с полями метрик по блокам), чтобы сведение стало возможным позже без переделки.

---

## 6. Критерии приёмки

1. `modeling/train_lgbm.py` обучает две независимые модели (`home_win`, `over_5_5`) на сплитах этапа 4; вход — только `load_training_table_split` (этап 1).
2. `objective='binary'`, `metric='binary_logloss'`, early stopping по `inner_val_k`, `num_boost_round`/`early_stopping_rounds` из YAML.
3. **Фиксированная сетка** из `models.lgbm.grids`, выбор по `binary_logloss` на `inner_val_k`. **Никакой Optuna/Bayes.**
4. Полный детерминизм: все seed-поля LightGBM из `random_seed`, `deterministic=True`, `force_row/col_wise` зафиксирован; тест детерминизма проходит.
5. `num_threads = compute.num_threads`; нет `-1`/`os.cpu_count()`/env-переменных потоков.
6. `monotone_constraints` строятся позиционно по именам из манифеста/YAML; fail-fast на несматченный паттерн; знаки соответствуют плану.
7. `tests/test_modeling_lgbm_monotone.py` и тесты §4 проходят локально.
8. `modeling/train_lgbm.py` не импортирует `psycopg2`, `modeling.dataset_builder.*`.
9. Общий с этапом 7 код не дублируется (вынесен в утилиты).
10. В описании MR явно сказано: «реализован этап 8 UPDATE-плана; этапы 9 (калибровка), 10 (CLI/финал/`latest`), 5 (полный отчёт) не затрагиваются (кроме совместимого формата результата)», и какой вариант §3.7 выбран.

---

## 7. Ограничения и вне скоупа

- **Не** реализовывать калибровку (этап 9): `train_lgbm.py` отдаёт **сырые** вероятности; `CalibratedClassifierCV` запрещён в проекте (этап 9 §123 плана).
- **Не** реализовывать CLI `train`, walk-forward-оркестрацию, финальное переобучение `model_final`, production-артефакт и симлинк `latest` — это этап 10.
- **Не** реализовывать reliability PNG, ECE, breakdown по командам, тривиальный baseline, bootstrap-ДИ — это этапы 5 и 6.
- **Не** добавлять CatBoost, Optuna, time-decay weights, `class_weight='balanced'` — это этап 15.
- **Не** менять `modeling/train_input.py`, `modeling/splits.py`, `modeling/dataset_builder/*`.
- **Не** добавлять `home_team_id`/`away_team_id` как фичи специально для LGBM в v1 — состав фич определяется манифестом этапа сборки датасета (id-фичи для CatBoost — этап 15).
- **Не** вводить новые YAML-поля сверх зафиксированных в UPDATE-плане (`models.lgbm.grids`, `models.lgbm.monotone.{home_win,over_5_5}`, `random_seed`, `compute.num_threads`). Загрузка YAML — этап 2.

---

## 8. Что отдать в MR

- `modeling/train_lgbm.py` (+ при необходимости `modeling/train_common.py` и/или минимальный `modeling/artifacts.py` по §3.7).
- `tests/test_modeling_lgbm_monotone.py` и тесты §4; правка `tests/test_modeling_no_db_access.py` при необходимости.
- Раздел в `docs/modeling_training.md` (если файл уже создан этапами 1/4): параметры LGBM, детерминизм, формат сетки, правила построения `monotone_constraints` из имён манифеста, дефолты. Если файла ещё нет — кратко в docstring модуля + отметка в MR, что полная страница появится на этапе 10/13.
- В описании MR — явная отметка скоупа (см. §6.10) и выбранный вариант §3.7.
