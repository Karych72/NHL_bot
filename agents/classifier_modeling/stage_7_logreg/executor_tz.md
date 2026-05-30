# ТЗ для исполнителя: этап 7 — Baseline: логистическая регрессия (две модели)

**Роль:** инженер-исполнитель.  
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 7. Baseline: логистическая регрессия (две модели)» **с обязательным учётом** «## Сквозные требования (читать перед каждым этапом)» того же документа.  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Связанные ТЗ темы:**
- [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) — этап 1 (контракт входа). Единственный источник `(X, keys, labels, service, metadata)`; новый загрузчик не пишется.
- [`../stage_2_config/executor_tz.md`](../stage_2_config/executor_tz.md) — этап 2 (конфигурация). Источник сетки `C`, `random_seed`, `compute.num_threads`.
- [`../stage_4_splits/executor_tz.md`](../stage_4_splits/executor_tz.md) — этап 4 (сплиты). Источник блоков `train_k`/`inner_val_k`/`calibration_k`/`test_k`/`holdout`.
- [`../stage_5_metrics/executor_tz.md`](../stage_5_metrics/executor_tz.md) — этап 5 (метрики). `log loss` для выбора `C` берётся из `modeling/metrics.py`, формула не дублируется.
- [`../stage_6_bootstrap/executor_tz.md`](../stage_6_bootstrap/executor_tz.md) — этап 6 (bootstrap). Не вызывается в этом этапе напрямую; ДИ строит CLI этапа 10.

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать **baseline-классификатор на логистической регрессии** для двух задач (`home_win`, `over_5_5`) в файле `modeling/train_logreg.py` и слой сохранения артефактов `modeling/artifacts.py`. Модуль обязан:

1. Строить **отдельный sklearn-`Pipeline`** для каждой из двух задач, общий код — через утилиты (без копипасты двух почти одинаковых пайплайнов).
2. **Обучать пайплайн строго на `train_k`** и применять (`transform`/`predict_proba`) на `inner_val_k`, `calibration_k`, `test_k`, `holdout` — без повторного `fit` на этих блоках.
3. Перебирать сетку гиперпараметра `C` из YAML и **выбирать `C` по log loss на `inner_val_k`**.
4. Соблюдать политику `class_weight=None` по умолчанию.
5. **Запрещать** колонки `home_team_id` / `away_team_id` во входной матрице `X` для logreg v1 (assert на список колонок).
6. Сохранять обученную модель и метаданные через `modeling/artifacts.py`.

Этап **не реализует**: калибровку (этап 9), CLI `train` с walk-forward оркестрацией и финальным переобучением (этап 10), bootstrap-ДИ (этап 6 уже сделан, вызывается на этапе 10), LightGBM (этап 8), полный отчёт `summary.md`/reliability PNG (этап 5/10).

---

## 2. Сквозные требования (обязательны к применению на этом этапе)

Из раздела «## Сквозные требования (читать перед каждым этапом)» UPDATE-плана для этого этапа действуют:

- **Источник истины фич — `metadata_train.json`.** Список и порядок колонок `X` приходят из контракта входа этапа 1 (`feature_manifest`). `train_logreg.py` **не** переопределяет набор фич и **не** читает YAML-копию манифеста как источник истины. Если контракт входа уже валидирует `features_hash` / `feature_manifest` / `feature_set_version` — этот этап проверку **не дублирует**, но обязан прокинуть `features_hash` в метаданные артефакта.
- **Воспроизводимость и единый `random_seed`.** `LogisticRegression(random_state=random_seed)` (даже для `lbfgs`, где влияние минимально — фиксируем явно для парити с остальными подсистемами). Никаких независимых seed-ов по задаче/фолду/значению `C`. Версии библиотек (`scikit-learn`, `numpy`, `pandas`, `joblib`) логируются в `metadata.json` артефакта.
- **Лимит потоков (`compute.num_threads`).** Пробрасывается в `n_jobs` там, где sklearn его принимает (в самом `LogisticRegression(solver='lbfgs')` `n_jobs` неэффективен — это нормально; важно **не** ставить `n_jobs=-1` нигде). Запрещено «использовать все ядра» без явного значения из YAML.
- **Логи.** `train_logreg.py` сам по себе `run.log` **не создаёт** — это делает CLI этапа 10. Но обязан использовать стандартный `logging` (логгер модуля, без `print`), чтобы вызывающий код мог привязать вывод к `artifacts/reports/<run_id>/run.log`.
- **`<run_id>`.** `train_logreg.py` **не** генерирует `<run_id>` и **не** зависит от текущего времени. `<run_id>`/пути артефактов приходят сверху (CLI этапа 10) либо передаются параметром в функцию сохранения.
- **Никакого доступа к PostgreSQL.** В `modeling/train_logreg.py` и `modeling/artifacts.py` **запрещены** импорты `psycopg2`, `modeling.dataset_builder.*` и любых модулей, тянущих БД. Проверяется AST-тестом этапа 11 (`tests/test_modeling_no_db_access.py`); на этом этапе **минимум** — не вводить таких импортов и пройти существующий тест, если он уже создан.

---

## 3. Объём работ

### 3.1 `modeling/train_logreg.py`

#### 3.1.1 Конструктор пайплайна

Единая утилита-фабрика (например `build_logreg_pipeline(C: float, class_weight=None, random_seed: int) -> Pipeline`), возвращающая **ровно** этот `sklearn.Pipeline`:

```
SimpleImputer(strategy="median")
  → StandardScaler()
  → LogisticRegression(penalty="l2", solver="lbfgs", max_iter=5000,
                       C=C, class_weight=class_weight, random_state=random_seed)
```

- Состав и порядок шагов фиксированы UPDATE-планом — **не менять** (не добавлять `PolynomialFeatures`, `SelectKBest`, `PCA` и т.п.).
- `class_weight=None` — **дефолт**; параметр оставить настраиваемым, но дефолтное значение строго `None` (политика §1.1 базового плана). `class_weight='balanced'` — эксперимент этапа 15, **не** включать здесь.
- `max_iter=5000`, `solver='lbfgs'`, `penalty='l2'` — фиксированы.

#### 3.1.2 Запрет ID-колонок

- Перед обучением — **assert** (или явный `raise ValueError`/`FeatureError`), что в списке колонок `X` **нет** `home_team_id` и `away_team_id` (а также любых их вариаций, если контракт входа их так называет — сверить с `feature_manifest`).
- Сообщение об ошибке должно явно называть нарушающие колонки. Это защита от утечки идентичности команды в линейную модель v1 (§3.7 базового плана). Голый `assert` без сообщения — нежелателен (см. ревью).
- Проверка делается по **именам колонок `X`**, пришедшим из контракта входа этапа 1, а не по позиции.

#### 3.1.3 Fit / transform по протоколу без утечки

- `pipeline.fit(X_train_k, y_train_k)` — **только** на `train_k`. Импьютер (медианы) и скейлер (mean/std) обучаются исключительно на `train_k`.
- На `inner_val_k`, `calibration_k`, `test_k`, `holdout` — **только** `predict_proba` (внутри пайплайна уже идёт `transform` импьютером/скейлером, обученными на train). **Запрещён** повторный `fit`/`fit_transform` на этих блоках — это утечка статистик.
- Предсказание для бинарной задачи — вероятность класса `1`: `pipeline.predict_proba(X)[:, 1]`.

#### 3.1.4 Перебор сетки `C` и выбор по `inner_val_k`

- Сетка `C` приходит из YAML (`models.logreg.grids`, этап 2). Если этап 2 ещё не закрыт — принимать сетку `C` параметром функции (список `float`), дефолт зафиксировать в docstring, но **не** хардкодить «магическую» сетку внутри логики выбора.
- Для каждого `C`:
  1. `fit` пайплайна на `train_k`;
  2. `predict_proba` на `inner_val_k`;
  3. `log loss` на `inner_val_k` через `modeling.metrics` (этап 5) — **не** через `sklearn.metrics.log_loss` напрямую (чтобы `ε`-клиппинг совпадал с финальными метриками; см. §3.4).
- Выбрать `C*` с **минимальным** log loss на `inner_val_k`. При равенстве — детерминированный tie-break (например меньший `C*` → более сильная регуляризация; зафиксировать правило в коде и docstring).
- После выбора `C*` — это и есть «сырой» классификатор для данного `train_k`. (Калибровку поверх него делает этап 9; здесь возвращается raw-пайплайн и выбранный `C*`.)

#### 3.1.5 Две задачи, общий код

- `home_win` и `over_5_5` — **две независимые** модели (отдельный выбор `C*`, отдельные пайплайны, отдельные артефакты).
- Общую логику (фабрика пайплайна, перебор сетки, выбор по log loss, assert ID-колонок) вынести в утилиты, чтобы две задачи не были скопированы. Различие между задачами — только в выбранном label-векторе (`y_home_win` vs `y_over_5_5`).
- Метки берутся из `labels`, пришедших из контракта входа этапа 1, по имени задачи. Маппинг «имя задачи → имя label-колонки» — единая константа/функция.

#### 3.1.6 Публичный API (минимум)

Имена допустимо уточнять, семантика — фикс:

- `build_logreg_pipeline(C, class_weight=None, random_seed) -> Pipeline`.
- `assert_no_team_id_columns(columns: Iterable[str]) -> None` (бросает при наличии запрещённых колонок).
- `select_C_by_inner_val(X_train, y_train, X_val, y_val, C_grid, *, class_weight, random_seed, log_loss_fn) -> tuple[float, Pipeline, dict]` — возвращает выбранный `C*`, обученный на `train_k` пайплайн и таблицу `{C: inner_val_log_loss}` (для отчёта/лога).
- `train_logreg_for_task(task, X_train, y_train, X_val, y_val, *, C_grid, class_weight, random_seed, log_loss_fn) -> FitResult` — обёртка, включающая assert ID-колонок и выбор `C*`.

`FitResult` (dataclass) минимум: `task`, `pipeline`, `chosen_C`, `inner_val_log_loss_by_C: dict[float, float]`, `chosen_inner_val_log_loss`, `n_rows_train`, `n_rows_inner_val`.

Функция **не** делает predict на `test_k`/`holdout` и **не** калибрует — это оркестрация этапа 10 и калибровка этапа 9. Этот этап даёт переиспользуемые кирпичики + сохранение.

### 3.2 `modeling/artifacts.py`

Слой сохранения/загрузки артефактов модели (общий для logreg и будущего LightGBM этапа 8 — спроектировать так, чтобы этап 8 переиспользовал, не переписывая):

- `save_model_artifact(path_dir, *, model, metadata: dict) -> None`:
  - сохраняет `model` через **`joblib.dump`** в `model.joblib` (имя файла — фикс; калибратор сохраняет этап 9 отдельным файлом `calibrator.joblib`, здесь его нет);
  - сохраняет `metadata` в `metadata.json` (UTF-8, `ensure_ascii=False`, отсортированные ключи, отступ 2);
  - создаёт каталог при необходимости; путь приходит сверху (CLI этапа 10), функция сама `<run_id>` не выдумывает.
- `load_model_artifact(path_dir) -> tuple[model, dict]` — обратная операция (нужна боту/тестам).
- **Состав `metadata.json`** (минимум, из UPDATE-плана §7 и §12.4):
  - `features_hash` (из `metadata_train.json` контракта входа);
  - `feature_set_version`, `feature_manifest` (список колонок, на которых обучена модель — для парити при инференсе);
  - диапазон дат `train` / `inner_val` / `calibration` (min/max `day`), если соответствующие блоки переданы; на этом этапе минимум — `train` и `inner_val`;
  - число строк по каждому переданному блоку;
  - `model_family` (`"logreg"`), `task` (`"home_win"`/`"over_5_5"`), `chosen_C`, `class_weight`;
  - `random_seed`;
  - версии библиотек: `scikit-learn`, `numpy`, `pandas`, `joblib` (фактические `__version__`);
  - `git_commit`: реальный commit hash, если в корне репозитория есть `.git`; иначе `null`. **Не** падать при отсутствии `.git`.
  - `run_id` — если передан сверху; иначе допускается отсутствие/`null` (генерит CLI этапа 10).
- Получение `git_commit` — без жёсткой зависимости от наличия `git` в PATH-исключениях: обернуть в try/except, при любой ошибке → `null`.

### 3.3 Использование контракта входа (этап 1)

- Единственный вход для матриц/меток — `load_training_table_split(...)` из `modeling/train_input.py`. **Новый загрузчик не писать.**
- Если в момент работы над этапом 7 удобнее тестировать на синтетике — допустимо принимать уже подготовленные `X`/`y`/`columns` как аргументы функций (см. §3.1.6); прямого чтения CSV/PostgreSQL внутри `train_logreg.py` быть **не должно**.

### 3.4 Совместимость с метриками этапа 5

- `log loss` для выбора `C` — через `modeling/metrics.py` (функция `log_loss` с `ε`-клиппингом, `ε = evaluation.epsilon_clip`, дефолт `1e-15`). **Не** дублировать формулу и **не** звать `sklearn.metrics.log_loss` напрямую.
- Если этап 5 ещё не реализован — принимать `log_loss_fn` параметром функции (callable `(y_true, y_pred) -> float`) и в тестах передавать мини-реализацию строго по контракту этапа 5. Расширять `modeling/metrics.py` сверх минимума в этом MR **нельзя** (сорвёт скоуп этапа 5).

### 3.5 Запреты

- Менять состав/порядок шагов пайплайна (`SimpleImputer → StandardScaler → LogisticRegression`).
- `fit`/`fit_transform` на `inner_val_k`/`calibration_k`/`test_k`/`holdout`.
- `n_jobs=-1`, `os.cpu_count()`, скрытая параллелизация.
- `class_weight='balanced'` как дефолт.
- `home_team_id`/`away_team_id` (и аналоги) в `X`.
- Любой `KFold(shuffle=True)`, `StratifiedKFold(shuffle=True)`, `ShuffleSplit`, `GridSearchCV`/`RandomizedSearchCV` со встроенным CV по матчам — выбор `C` идёт **только** по временно́му `inner_val_k` из этапа 4 (внутренний shuffle-CV ломает временной порядок; проверяется тестом этапа 11).
- `CalibratedClassifierCV` — запрещён в проекте (этап 9 объясняет почему); калибровку здесь не делать вовсе.
- Прямое чтение CSV/PostgreSQL внутри `train_logreg.py`.

---

## 4. Тесты

Создать `tests/test_modeling_train_logreg.py`. Минимум:

1. **Состав пайплайна:** `build_logreg_pipeline(...)` возвращает `Pipeline` ровно из трёх шагов в порядке `SimpleImputer(strategy='median')` → `StandardScaler` → `LogisticRegression(penalty='l2', solver='lbfgs', max_iter=5000)`; `class_weight=None` по умолчанию.
2. **Запрет ID-колонок:** `assert_no_team_id_columns([... 'home_team_id' ...])` бросает понятную ошибку с именем колонки; на чистом списке — не бросает.
3. **Нет утечки статистик:** на синтетике с разными распределениями в train и val — медианы импьютера и mean/std скейлера посчитаны **только** по `train_k` (проверить, что `pipeline.named_steps['standardscaler'].mean_` равен среднему train, а не train+val). Косвенно: вызов на val не меняет `mean_`/`scale_`.
4. **Выбор `C` по inner_val:** на синтетике, где один `C` заведомо лучше, `select_C_by_inner_val` возвращает именно его; таблица `{C: log_loss}` заполнена для **всех** значений сетки.
5. **Tie-break детерминирован:** при искусственно равных log loss выбирается зафиксированный в правиле `C` (например меньший); тест документирует ожидание.
6. **`log_loss_fn` используется, а не sklearn:** подменить `log_loss_fn` на маркерную функцию и убедиться, что выбор идёт по её значениям (защита от прямого вызова `sklearn.metrics.log_loss`).
7. **Детерминизм:** два прогона `train_logreg_for_task` с одним `random_seed` дают идентичные `chosen_C` и идентичные `predict_proba` на фиксированном `X` (`np.testing.assert_allclose`).
8. **Две независимые задачи:** `home_win` и `over_5_5` обучаются независимо (разные label-векторы → потенциально разные `chosen_C`); маппинг задача→label корректен.
9. **Артефакт round-trip:** `save_model_artifact` + `load_model_artifact` восстанавливают модель, дающую идентичные `predict_proba`; `metadata.json` содержит все обязательные поля §3.2 и валиден через `json.loads`.
10. **`git_commit` без `.git`:** в окружении без `.git` (мок/временный каталог) `metadata['git_commit'] is None`, функция не падает.
11. **Версии библиотек:** `metadata` содержит непустые строки версий `scikit-learn`, `numpy`, `pandas`, `joblib`.

Параллельно (минимум, не блокер этого MR):
- Если ещё нет `tests/test_modeling_no_db_access.py` для этих файлов — добавить мини-проверку, что `modeling/train_logreg.py` и `modeling/artifacts.py` не импортируют `psycopg2`, `modeling.dataset_builder.*`. Полный охват — этап 11.

---

## 5. Критерии приёмки

1. `modeling/train_logreg.py` реализует фабрику пайплайна, assert ID-колонок, выбор `C` по `inner_val_k` через `log_loss_fn`, обучение **только на `train_k`**, для **двух** задач с общим кодом.
2. Состав/порядок шагов пайплайна и гиперпараметры (`penalty='l2'`, `solver='lbfgs'`, `max_iter=5000`, `class_weight=None` по умолчанию) строго соответствуют UPDATE-плану §7.
3. `home_team_id`/`away_team_id` в `X` приводят к понятной ошибке (есть тест).
4. Нет повторного `fit` на val/cal/test/holdout; статистики импьютера/скейлера — только из `train_k` (есть тест).
5. `modeling/artifacts.py` сохраняет/загружает модель (`joblib`) и `metadata.json` со всеми полями §3.2, включая `features_hash`, диапазоны дат, число строк, версии библиотек, `git_commit` (или `null`).
6. Детерминизм при фиксированном `random_seed` (есть тест).
7. `log loss` для выбора `C` идёт через `modeling/metrics.py` / `log_loss_fn`, формула не дублируется.
8. Нет доступа к PostgreSQL, нет `n_jobs=-1`/`os.cpu_count()`, нет shuffle-CV, нет `CalibratedClassifierCV`.
9. Тесты §4 проходят локально (`pytest tests/test_modeling_train_logreg.py`).
10. В описании MR явно сказано: «реализован этап 7 UPDATE-плана; калибровка (9), CLI train (10), LightGBM (8) не затрагиваются», и какие функции из `artifacts.py` спроектированы под переиспользование этапом 8.

---

## 6. Ограничения и вне скоупа

- **Не** реализовывать калибровку (этап 9), CLI `train` и walk-forward оркестрацию + финальное переобучение (этап 10), bootstrap-вызовы (этап 6/10), LightGBM (этап 8).
- **Не** строить reliability PNG, `summary.md`, ECE, breakdown по командам — это этапы 5/10.
- **Не** расширять `modeling/metrics.py` сверх минимума (если он ещё не готов — использовать `log_loss_fn`-параметр).
- **Не** менять `modeling/train_input.py`, `modeling/splits.py`, `modeling/dataset_builder/*`.
- **Не** добавлять `class_weight='balanced'`, time-decay веса, Optuna, отбор фич — эксперименты этапа 15.
- **Не** менять `requirements.txt` / Docker-образ бота; `scikit-learn`/`joblib` уже идут в `requirements-modeling.txt` (этап 3).

---

## 7. Что отдать в MR

- `modeling/train_logreg.py` — фабрика пайплайна, утилиты выбора `C`, обучение двух задач.
- `modeling/artifacts.py` — `save_model_artifact` / `load_model_artifact` + сбор `metadata.json` (спроектировано под переиспользование этапом 8).
- `tests/test_modeling_train_logreg.py` (и мини-проверка no-db-access, если её ещё нет).
- Краткий раздел в `docs/modeling_training.md` (если файл уже создан этапами 1/4): схема пайплайна logreg, правило выбора `C` по `inner_val_k`, запрет ID-колонок, состав `metadata.json` артефакта. Если файла ещё нет — раздел в docstring + пометка в MR «полная страница на этапе 10/13».
- В описании MR — явная отметка границ скоупа (см. §5.10).
