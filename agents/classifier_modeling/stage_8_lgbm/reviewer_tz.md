# ТЗ для ревьюера: этап 8 — Primary: LightGBM (без альтернатив в v1)

**Роль:** независимый reviewer.  
**Источник:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 8. Primary: LightGBM (без альтернатив в v1)», с учётом «## Сквозные требования (читать перед каждым этапом)».  
**Пара ТЗ для исполнителя:** [`executor_tz.md`](executor_tz.md).

Пути — от корня репозитория.

---

## 1. Принцип ревью

Приоритет:

1. **Полный детерминизм** — все seed-поля LightGBM из единого `random_seed`, `deterministic=True`, тест на идентичность предсказаний.
2. **Корректность монотонных ограничений** — позиционная привязка по именам манифеста, fail-fast на несматченный паттерн, правильные знаки.
3. **Отсутствие утечки и доступа к БД** — обучение только на `train_k`, early stopping по `inner_val_k`, никакого `psycopg2`/`dataset_builder`.
4. **Минимальный скоуп MR** — этап 8 не реализует калибровку (9), CLI/финал/`latest` (10), полный отчёт/bootstrap (5/6); не тащит CatBoost/Optuna (15).

Если хоть один из пунктов §3–§7 нарушен — **request changes** с указанием конкретного места в коде/тесте.

---

## 2. Размещение и публичный API

- [ ] Основной модуль — `modeling/train_lgbm.py`. Общий с этапом 7 код вынесен в утилиты (`modeling/train_common.py` или существующий общий модуль); **дублирования** логики выбора по `inner_val_k` / нарезки блоков / сериализации между `train_logreg.py` и `train_lgbm.py` нет.
- [ ] Вход модели — **только** структуры от `load_training_table_split` (этап 1). Нового загрузчика нет; прямого чтения `dataset_train.csv`/SQL нет.
- [ ] Две задачи (`home_win`, `over_5_5`) обучаются **независимо**, общий код, раздельные модели/предсказания.
- [ ] Предсказание возвращает вероятности в `[0,1]` **без** клиппинга (клип — ответственность `metrics`/калибровки).

---

## 3. Детерминизм (критично)

- [ ] В параметрах LightGBM заданы **все** seed-поля из `random_seed`: `seed`/`random_state`, `feature_fraction_seed`, `bagging_seed`, `data_random_seed`. Все равны `random_seed` (или явно из него выведены **без** производных типа `+fold`/`*grid_idx`).
- [ ] `deterministic=True` присутствует. Зафиксирован `force_row_wise=True` **или** `force_col_wise=True` (одно из).
- [ ] **Нет** производных/независимых seed по фолду, конфигу сетки, задаче. Любой `random_seed + k` / `random_seed * c` — **блокер** (сквозное требование «все seed-параметры выводятся из единого `random_seed`»).
- [ ] **Нет** глобального `np.random.seed(...)`, `random.seed(...)`.
- [ ] Есть тест: два обучения с одним `random_seed` → идентичные предсказания (`assert_allclose(atol=0)`).

---

## 4. Гиперпараметры и сетка

- [ ] `objective='binary'`, `metric='binary_logloss'`.
- [ ] Early stopping настроен по **`inner_val_k`** (валидационный сет — именно inner_val, не test/holdout/calibration). `num_boost_round`/`early_stopping_rounds` берутся из YAML, не захардкожены.
- [ ] Перебирается **фиксированная** сетка из `models.lgbm.grids` (`num_leaves`, `min_data_in_leaf`, `feature_fraction`, `bagging_fraction`, `lambda_l1`, `lambda_l2`, `learning_rate`). Выбор лучшей — по `binary_logloss` на `inner_val_k`, tie-break детерминированный.
- [ ] **Нет** `import optuna` / любого Bayes-/random-поиска гиперпараметров (это этап 15 — **блокер**, если есть).
- [ ] Фолд без `inner_val_k` (early stopping невозможен) → понятная ошибка, а не тихое обучение на полном `num_boost_round`.

---

## 5. Монотонные ограничения

- [ ] `monotone_constraints` строится как вектор длины `len(feature_manifest)`, **позиционно соответствующий** фактическому порядку колонок X (тому, что отдаёт контракт этапа 1). Привязка — **по имени** колонки, не по «магическому» индексу.
- [ ] Знаки соответствуют плану:
  - `home_win`: **+1** на `diff_goal_diff_roll_mean_*`, `diff_gf_roll_mean_*`; **−1** на `diff_ga_roll_mean_*`;
  - `over_5_5`: **+1** на `sum_gf_roll_mean_*`, `sum_ga_roll_mean_*`.
- [ ] **Fail-fast**, если YAML-паттерн `monotone` не сматчил **ни одной** колонки или ссылается на фичу вне манифеста → `ConfigError`. **Тихое игнорирование несматченного паттерна — блокер** (ограничение незаметно не применится).
- [ ] Если `monotone` пуст/не задан — обучение без ограничений, но это явный кейс (лог/комментарий), не результат «паттерн не сматчился».
- [ ] Есть тест `tests/test_modeling_lgbm_monotone.py`: на синтетике с монотонной зависимостью обученный LGBM не нарушает знак ограничения при сканировании признака.

---

## 6. Изоляция, потоки, утечка

- [ ] `modeling/train_lgbm.py` **не** импортирует `psycopg2`, `modeling.dataset_builder.*` (grep + AST-тест `tests/test_modeling_no_db_access.py`).
- [ ] `num_threads = compute.num_threads`. **Нет** `num_threads=-1`/`0`, `n_jobs=-1`, `os.cpu_count()`, `OMP_NUM_THREADS`/`MKL_NUM_THREADS`. `num_threads ≤ 0` → ошибка.
- [ ] Модель `fit` **только на `train_k`**; `inner_val_k` используется лишь для early stopping/выбора сетки; `calibration_k`/`test_k`/`holdout` — только predict (transform), не участвуют в обучении. Утечки нет.
- [ ] Сырые предсказания отдаются наверх для калибровки (этап 9); калибровка в этом модуле **не** делается.

---

## 7. Скоуп MR

- [ ] В MR **нет** калибровки (`modeling/calibrate.py`), **нет** `CalibratedClassifierCV` (запрещён планом).
- [ ] В MR **нет** CLI `train`, walk-forward-оркестратора, финального переобучения `model_final`, production-`final/`, симлинка `latest` — это этап 10.
- [ ] В MR **нет** reliability PNG, ECE, breakdown по командам, тривиального baseline, bootstrap-ДИ — это этапы 5/6 (`train_lgbm.py` лишь отдаёт совместимый формат результата для последующего сведения).
- [ ] В MR **нет** CatBoost, Optuna, time-decay weights, `class_weight='balanced'`, stacking — это этап 15.
- [ ] `home_team_id`/`away_team_id` не добавлены как фичи специально для LGBM в v1 (состав фич — из манифеста).
- [ ] `modeling/train_input.py`, `modeling/splits.py`, `modeling/dataset_builder/*` не изменены.
- [ ] Новых YAML-полей сверх `models.lgbm.grids`, `models.lgbm.monotone.{home_win,over_5_5}`, `random_seed`, `compute.num_threads` не введено.
- [ ] В описании MR явно указан скоуп и выбранный вариант §3.7 ТЗ исполнителя (как разрешены незакрытые этапы 2/7/`artifacts.py`).

---

## 8. Тесты

- [ ] `tests/test_modeling_lgbm_monotone.py` — монотонность предсказаний при `+1`/`−1` ограничении (см. §5).
- [ ] Детерминизм: одинаковый `random_seed` → идентичные предсказания (`atol=0`).
- [ ] Построение `monotone_constraints` по именам: правильные индексы получают `+1`/`−1`, остальные — `0`; устойчивость к перестановке порядка колонок.
- [ ] Fail-fast на несматченный паттерн `monotone` → `ConfigError`.
- [ ] Выбор конфигурации по `binary_logloss` на `inner_val_k` (детерминированный tie-break).
- [ ] `num_threads ≤ 0` → ошибка; в параметрах модели нет `-1`.
- [ ] Тесты быстрые, на синтетике, без БД и без чтения реального `dataset_train.csv`.

Если хоть один пункт §8 отсутствует — **request changes**.

---

## 9. Совместимость с этапами 5 и 7

- [ ] Bootstrap-ДИ и метрики `train_lgbm.py` **не** реализует своими формулами — для отчёта используются `modeling/metrics.py` (этап 5) и модуль bootstrap (этап 6). Прямого расчёта log loss/Brier в `train_lgbm.py` нет (дублирование — **блокер**).
- [ ] Формат результата совместим с `train_logreg.py` (этап 7) для сведения в **одну** сравнительную таблицу на тех же сплитах. Саму таблицу `train_lgbm.py` не строит.

---

## 10. Сквозные требования UPDATE-плана (чеклист)

- [ ] **Источник истины фич**: порядок/состав X и `monotone_constraints` — из `feature_manifest`; расхождение YAML↔метадата ловит контракт этапа 1 (не дублируется здесь).
- [ ] **Воспроизводимость**: все seed LGBM = `random_seed`, `deterministic=True`; версии библиотек отдаются наверх для `metadata.json`.
- [ ] **Лимит потоков**: `num_threads = compute.num_threads`.
- [ ] **Логи**: `train_lgbm.py` отдаёт структуру результата (выбранная сетка, `best_iteration`, метрики по блокам) для `run.log`/`metrics.json` CLI этапа 10.
- [ ] **`<run_id>`**: модуль не зависит от времени и не генерирует id.
- [ ] **Никакого доступа к PostgreSQL**: проверено grep'ом / AST-тестом.

---

## 11. Документация

- [ ] Если `docs/modeling_training.md` уже создан — добавлен раздел про LGBM: параметры, детерминизм, формат сетки, построение `monotone_constraints` из имён манифеста, дефолты.
- [ ] Если файла ещё нет — раздел в docstring модуля, в описании MR отмечено: «полная страница появится на этапе 10/13».
- [ ] Битых ссылок и якорей нет.

---

## 12. Вердикт

**approve / approve with nits / request changes.**

**Блокеры:**

- производные/независимые seeds LightGBM (`random_seed + fold`, `* grid_idx` и т.п.) либо отсутствие части seed-полей / `deterministic=True`;
- глобальный `np.random.seed(...)` / `random.seed(...)`;
- early stopping/выбор сетки по test/holdout/calibration вместо `inner_val_k` (утечка);
- обучение модели не только на `train_k` (использование eval-блоков в fit);
- тихое игнорирование несматченного `monotone`-паттерна; неверная позиционная привязка `monotone_constraints`; неверные знаки;
- `import optuna` / Bayes-поиск / CatBoost / time-decay / `class_weight='balanced'` (этап 15);
- `CalibratedClassifierCV` или встроенная калибровка (этап 9);
- `num_threads=-1`/`os.cpu_count()`/env-переменные потоков;
- импорт `psycopg2` / `modeling.dataset_builder.*` в `modeling/train_lgbm.py`;
- дублирование формул log loss/Brier вместо вызова `modeling/metrics.py`;
- посторонняя работа из этапов 5 (сверх совместимого формата), 9, 10, 15.

**Ниты:**

- стиль docstring и расположение раздела документации (`docs/modeling_training.md` vs docstring);
- имена внутренних переменных и формат структуры результата (`dataclass` vs `TypedDict`);
- формат задания сетки в YAML (полный grid vs плоский список конфигов) — оба допустимы при детерминированном переборе;
- выбор `force_row_wise` vs `force_col_wise`.
