# ТЗ для ревьюера: этап 7 — Baseline: логистическая регрессия (две модели)

**Роль:** независимый reviewer.  
**Источник:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 7. Baseline: логистическая регрессия (две модели)», с учётом «## Сквозные требования (читать перед каждым этапом)».  
**Пара ТЗ для исполнителя:** [`executor_tz.md`](executor_tz.md).

Пути — от корня репозитория.

---

## 1. Принцип ревью

Приоритет:

1. **Отсутствие утечки** — fit пайплайна (импьютер + скейлер + logreg) **только** на `train_k`; на val/cal/test/holdout — лишь predict.
2. **Корректность выбора `C`** — по log loss на `inner_val_k`, через `modeling/metrics.py` (без дублирования формулы и без sklearn-`log_loss` напрямую).
3. **Запрет ID-колонок** `home_team_id`/`away_team_id` в `X` (assert с понятным сообщением).
4. **Воспроизводимость и изоляция** — единый `random_seed`, нет доступа к PostgreSQL, нет `n_jobs=-1`/скрытой параллелизации, нет shuffle-CV.
5. **Минимальный скоуп MR** — этап 7 не делает калибровку (9), CLI train (10), LightGBM (8), полный отчёт (5).

Если хоть один из пунктов §3–§6 ниже нарушен — **request changes** с указанием конкретного места в коде/тесте.

---

## 2. Состав пайплайна и гиперпараметры

- [ ] `Pipeline` состоит **ровно** из трёх шагов в порядке: `SimpleImputer(strategy='median')` → `StandardScaler()` → `LogisticRegression(...)`. Никаких лишних шагов (`PCA`, `SelectKBest`, `PolynomialFeatures` и т.п.).
- [ ] `LogisticRegression`: `penalty='l2'`, `solver='lbfgs'`, `max_iter=5000`, `random_state=random_seed`.
- [ ] `class_weight=None` **по умолчанию**. `class_weight='balanced'` как дефолт — **блокер** (это эксперимент этапа 15).
- [ ] `SimpleImputer(strategy='median')` — именно медиана, не mean/constant.

---

## 3. Протокол без утечки (приоритет №1)

- [ ] `pipeline.fit(...)` вызывается **только** на `train_k`. На `inner_val_k`/`calibration_k`/`test_k`/`holdout` — **только** `predict_proba`/`transform` уже обученным пайплайном.
- [ ] Нет `fit_transform`/`fit` на val/cal/test/holdout — это **блокер** (утечка статистик импьютера и скейлера).
- [ ] Статистики `SimpleImputer.statistics_` и `StandardScaler.mean_`/`scale_` посчитаны по `train_k`, не по объединению с другими блоками — есть тест.
- [ ] Вероятность берётся как `predict_proba(X)[:, 1]` (класс 1), не `decision_function` без сигмоиды и не `predict`.

---

## 4. Выбор гиперпараметра `C`

- [ ] Сетка `C` приходит из YAML (`models.logreg.grids`) либо параметром функции; «магическая» захардкоженная сетка в логике выбора — нит/блокер по обстоятельствам (если нет способа переопределить — блокер).
- [ ] Для каждого `C`: fit на `train_k` → `predict_proba` на `inner_val_k` → log loss. Выбор `C*` — по **минимальному** log loss на `inner_val_k`.
- [ ] log loss считается через `modeling/metrics.py` / переданный `log_loss_fn`. Прямой `sklearn.metrics.log_loss` или своя формула — **блокер** (рассинхрон `ε`-клиппинга с финальными метриками).
- [ ] Выбор `C` **не** использует `GridSearchCV`/`RandomizedSearchCV`/любой shuffle-CV по матчам — только временно́й `inner_val_k` из этапа 4. Встроенный CV — **блокер** (ломает временной порядок).
- [ ] Tie-break при равных log loss детерминирован и задокументирован.

---

## 5. Запрет ID-колонок

- [ ] Перед обучением проверяется отсутствие `home_team_id`/`away_team_id` (и аналогов по `feature_manifest`) в колонках `X`. Проверка по **именам**, не по позиции.
- [ ] При нарушении — понятная ошибка с именем колонки (не голый `assert` без сообщения; голый `assert` исчезает под `python -O` — нежелательно, отметить как нит/блокер по строгости).
- [ ] Есть тест и на срабатывание, и на чистый список.

---

## 6. Две задачи и общий код

- [ ] `home_win` и `over_5_5` — **две независимые** модели (отдельный `C*`, отдельные пайплайны, отдельные артефакты).
- [ ] Общая логика вынесена в утилиты; нет копипасты двух почти идентичных пайплайнов.
- [ ] Маппинг «задача → label-колонка» — единый источник, метки берутся из контракта входа этапа 1, а не пересобираются.

---

## 7. Артефакты (`modeling/artifacts.py`)

- [ ] `save_model_artifact` сохраняет модель через `joblib.dump` в `model.joblib`; `metadata.json` — UTF-8, валиден через `json.loads`.
- [ ] `metadata.json` содержит (минимум): `features_hash`, `feature_set_version`, `feature_manifest`, диапазоны дат `train`/`inner_val` (+`calibration`, если передан), число строк по блокам, `model_family='logreg'`, `task`, `chosen_C`, `class_weight`, `random_seed`, версии `scikit-learn`/`numpy`/`pandas`/`joblib`, `git_commit` (или `null`).
- [ ] `git_commit`: реальный hash при наличии `.git`; при отсутствии `.git` или ошибке — `null`, **без падения** (есть тест).
- [ ] `artifacts.py` **не** генерирует `<run_id>` сам и не зависит от текущего времени — путь приходит сверху.
- [ ] `load_model_artifact` восстанавливает модель с идентичными `predict_proba` (round-trip тест).
- [ ] Слой спроектирован под переиспользование этапом 8 (LightGBM) — нет logreg-специфичных хардкодов в общих функциях сохранения.

---

## 8. Воспроизводимость и изоляция (сквозные требования)

- [ ] Единый `random_seed` из YAML прокинут в `LogisticRegression(random_state=...)`. Нет независимых seed по задаче/фолду/`C`.
- [ ] Детерминизм: одинаковый `random_seed` → идентичные `chosen_C` и `predict_proba` (есть тест).
- [ ] Нет `n_jobs=-1`, `os.cpu_count()`, `OMP_NUM_THREADS`-хаков, скрытой параллелизации. Лимит потоков — только из `compute.num_threads` там, где применимо.
- [ ] `modeling/train_logreg.py` и `modeling/artifacts.py` **не** импортируют `psycopg2`, `modeling.dataset_builder.*` (grep / AST-тест этапа 11).
- [ ] Нет прямого чтения CSV/PostgreSQL в `train_logreg.py`; вход — контракт этапа 1 или аргументы функций.
- [ ] Логирование через `logging` (логгер модуля), не `print`.

---

## 9. Тесты

В `tests/test_modeling_train_logreg.py`:

- [ ] Состав и порядок шагов пайплайна + дефолтные гиперпараметры.
- [ ] Запрет ID-колонок (срабатывание + чистый список).
- [ ] Нет утечки статистик: импьютер/скейлер обучены только на `train_k`.
- [ ] Выбор `C` по `inner_val_k`; таблица `{C: log_loss}` заполнена по всей сетке.
- [ ] Tie-break детерминирован.
- [ ] `log_loss_fn` действительно используется (подмена маркерной функцией), а не sklearn напрямую.
- [ ] Детерминизм при фиксированном `random_seed`.
- [ ] Две независимые задачи + корректный маппинг задача→label.
- [ ] Артефакт round-trip + полный состав `metadata.json`.
- [ ] `git_commit is None` без `.git`, без падения.
- [ ] Непустые версии библиотек в `metadata`.

Если значимый пункт отсутствует — **request changes**.

---

## 10. Скоуп MR

- [ ] В MR **нет** калибровки (`calibrate.py`, `CalibratedClassifierCV`), CLI `train`, walk-forward оркестрации, финального переобучения, bootstrap-вызовов, LightGBM (`train_lgbm.py`), reliability PNG, ECE, breakdown по командам, `summary.md`.
- [ ] `modeling/metrics.py` не расширен сверх минимума этапа 5 (если вообще трогался) — иначе пересечение со скоупом этапа 5.
- [ ] Нет изменений в `modeling/train_input.py`, `modeling/splits.py`, `modeling/dataset_builder/*`.
- [ ] `requirements.txt`/`Dockerfile`/`docker-compose.yml` не меняются (или только косметически с явным обоснованием — тогда не блокер, но проговорено).
- [ ] В описании MR явно сказано: «реализован этап 7 UPDATE-плана; этапы 8–10 и калибровка не затрагиваются».

---

## 11. Сквозные требования UPDATE-плана (чеклист)

- [ ] **Источник истины фич**: набор/порядок колонок из `feature_manifest` (этап 1), не из YAML; `features_hash` прокинут в `metadata.json`.
- [ ] **Воспроизводимость**: единый `random_seed`, версии библиотек в `metadata.json`.
- [ ] **Лимит потоков**: нет `n_jobs=-1`/авто-все-ядра.
- [ ] **Логи**: `logging`, привязка к `run.log` — ответственность CLI этапа 10 (здесь не нарушено).
- [ ] **`<run_id>`**: модуль не генерирует id и не зависит от времени.
- [ ] **Никакого доступа к PostgreSQL**: подтверждено grep/AST.

---

## 12. Вердикт

**approve / approve with nits / request changes.**

**Блокеры:**

- `fit`/`fit_transform` пайплайна на `inner_val_k`/`calibration_k`/`test_k`/`holdout` (утечка статистик);
- изменённый состав/порядок шагов пайплайна или гиперпараметры (не `l2`/`lbfgs`/`max_iter=5000`/median);
- `class_weight='balanced'` по умолчанию;
- `home_team_id`/`away_team_id` (или аналог) допущены в `X`;
- выбор `C` через `sklearn.metrics.log_loss` напрямую или собственную формулу (рассинхрон `ε`);
- выбор `C` через `GridSearchCV`/любой shuffle-CV по матчам вместо временно́го `inner_val_k`;
- использование `CalibratedClassifierCV` в любом виде;
- независимые seed по задаче/фолду/`C`;
- `n_jobs=-1`/`os.cpu_count()`/скрытая параллелизация;
- импорт `psycopg2`/`modeling.dataset_builder.*` либо прямое чтение CSV/PostgreSQL в train-коде;
- посторонняя работа из этапов 8–10 и калибровки в этом MR.

**Ниты:**

- голый `assert` вместо `raise` с сообщением для запрета ID-колонок;
- стиль docstring и расположение раздела документации (`docs/modeling_training.md` vs docstring);
- имена внутренних переменных/утилит;
- выбор `dataclass` vs `dict` для `FitResult` (оба допустимы, важна сериализуемость метаданных);
- формат tie-break при равных log loss (любой детерминированный — ок).
