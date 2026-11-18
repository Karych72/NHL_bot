# ТЗ для исполнителя: этап 6 — Bootstrap доверительные интервалы

**Роль:** инженер-исполнитель.  
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 6. Bootstrap доверительные интервалы» **с обязательным учётом** «## Сквозные требования (читать перед каждым этапом)» того же документа.  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Связанные ТЗ темы:**
- [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) — этап 1 (контракт входа). Используется как источник `keys` (`day`, `game_id`) для block bootstrap.
- [`../stage_4_splits/executor_tz.md`](../stage_4_splits/executor_tz.md) — этап 4 (сплиты). Bootstrap применяется к структурам `test_k` и `holdout`, которые приходят из сплиттера.
- Этап 5 (метрики) — `modeling/metrics.py`. Bootstrap вызывает уже реализованные `log_loss` и `brier`; если этап 5 ещё не завершён, см. §3.6 ниже.

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать модуль расчёта **bootstrap-доверительных интервалов** для `log loss` и `Brier` на каждом `test_k` и на `holdout`. Модуль обязан:

1. На каждом `test_k` — **i.i.d. resampling матчей** с возвратом, `N = evaluation.bootstrap_samples` (по умолчанию 1000), 95% ДИ.
2. На `holdout` — **block bootstrap по `day`** (ресемплятся целые игровые дни), управляется флагом `evaluation.bootstrap_block_by_day=true`. Уважает временную зависимость и не сжимает ДИ.
3. Быть **полностью детерминированным** при фиксированном `random_seed`: `bootstrap_seed = random_seed`, независимые seeds запрещены.
4. Записывать в метаданные прогона: `bootstrap.N`, `bootstrap.block_by_day`, `bootstrap.seed`.

Этап **не реализует** само обучение, калибровку, CLI `train` и финальный отчёт — только функции расчёта ДИ, их интеграцию в `modeling/metrics.py` (или соседний модуль) и тесты.

---

## 2. Сквозные требования (обязательны к применению на этом этапе)

Из раздела «## Сквозные требования (читать перед каждым этапом)» UPDATE-плана для bootstrap-модуля действуют:

- **Источник истины фич — `metadata_train.json`.** Bootstrap напрямую с манифестом не работает, но если получает на вход метаданные (например, из вышестоящего CLI) — расхождение `feature_manifest` / `features_hash` / `feature_set_version` между YAML и метадатой считается `ConfigError` с диффом и пробрасывается выше. Сам bootstrap-модуль такую проверку **не дублирует**, если её делает контракт входа этапа 1.
- **Воспроизводимость и единый `random_seed`.** В bootstrap **все** случайные шаги выводятся из `random_seed` через `numpy.random.default_rng(random_seed)`. Любые «свои» seed-параметры (по фолду, по метрике, по таску) **запрещены** — это явное требование UPDATE-плана («Seed: `bootstrap_seed = random_seed`; независимые seeds запрещены»). Версии библиотек (`numpy`, `pandas`) логируются в `metadata.json` артефакта прогона (фактический лог пишет CLI на этапе 10; bootstrap отдаёт нужные поля наверх).
- **Лимит потоков (`compute.num_threads`).** Bootstrap с `N = 1000` × число фолдов × число метрик легко выжигает CPU. Реализация:
  - **по умолчанию однопоточна** (последовательный цикл по ресемплам);
  - если используется параллелизация (`joblib.Parallel` и т.п.), число воркеров **обязано** браться из `compute.num_threads` (передавать параметром в функцию, не читать `os.cpu_count()` и не использовать переменные окружения `OMP_NUM_THREADS`/`MKL_NUM_THREADS` напрямую);
  - запрещено любое автоматическое «использовать все ядра» без явного флага из YAML.
- **Логи.** Bootstrap **сам по себе** `run.log` не создаёт — это делает CLI. Но **обязан** возвращать структурированный результат (`dataclass` / `dict`) с полями `bootstrap.N`, `bootstrap.block_by_day`, `bootstrap.seed`, пригодный для сериализации в `metrics.json` и логирования INFO-строкой на этапе 10.
- **`<run_id>`.** Bootstrap не генерирует `<run_id>` и не зависит от текущего времени.
- **Никакого доступа к PostgreSQL.** Файл, в котором реализован bootstrap (`modeling/metrics.py` или соседний), **запрещено** импортировать `psycopg2`, `modeling.dataset_builder.*` и любые модули, тянущие БД-зависимости. Это проверяется AST-тестом на этапе 11; на этом этапе **минимум** — не вводить таких импортов и пройти существующий `tests/test_modeling_no_db_access.py`, если он уже создан.

---

## 3. Объём работ

### 3.1 Размещение кода

Реализовать одно из двух (выбрать **одно** и зафиксировать в MR):

- Расширить `modeling/metrics.py` функциями bootstrap-ДИ (рекомендуется, если этап 5 уже в работе/готов).
- Либо отдельный модуль `modeling/bootstrap.py`, который импортирует метрики из `modeling/metrics.py`. Тогда `modeling/metrics.py` сам bootstrap не реализует, чтобы не дублировать.

В любом случае публичный API должен быть стабилен и не зависеть от внутренних деталей расчёта метрик: bootstrap принимает уже вычислительно совместимые `y_true`, `y_pred` (+ `day` для блочного режима) и функции метрик.

### 3.2 Публичный API

Минимум следующего вида (имена допустимо уточнять, семантика — фикс):

- Датакласс/`TypedDict` результата:
  - `metric_name: str` (`"log_loss"` | `"brier"`);
  - `point: float` — значение метрики на исходных `y_true`, `y_pred`, **рассчитанное теми же `metrics.py`-функциями**, что и финальные метрики этапа 5 (без дублирования формул);
  - `ci_low: float`, `ci_high: float` — 2.5% и 97.5% перцентили распределения ресемплов;
  - `n_resamples: int` — фактическое число ресемплов (= `N`);
  - `block_by_day: bool` — режим, в котором считался ДИ;
  - `seed: int` — `bootstrap_seed`.
- Функция bootstrap для **одного фолда** на входе:
  - `y_true: np.ndarray` (shape `(n,)`, значения `{0, 1}`);
  - `y_pred: np.ndarray` (shape `(n,)`, вероятности в `[0, 1]`, **уже клипнутые** по `evaluation.epsilon_clip` на стороне `modeling.metrics`; bootstrap **не** клипит сам — это ответственность функций метрик);
  - `day: pd.Series | np.ndarray | None` (shape `(n,)`, обязателен при `block_by_day=True`, иначе игнорируется);
  - `n_resamples: int` (= `evaluation.bootstrap_samples`, по умолчанию 1000);
  - `block_by_day: bool`;
  - `seed: int` (= `random_seed` из YAML);
  - `metric_fns: Mapping[str, Callable[[ndarray, ndarray], float]]` — словарь имён метрик в функции (как минимум `"log_loss"` и `"brier"`); чтобы не дублировать формулы и чтобы клиппинг `ε` шёл через `modeling.metrics`;
  - `num_threads: int = 1` — лимит из `compute.num_threads`, по умолчанию однопоточно.
  Возвращает `Mapping[str, BootstrapResult]` (по метрике).

### 3.3 Алгоритм

#### 3.3.1 i.i.d. resampling (для `test_k`)

- Один `numpy.random.Generator`, инициализированный из `seed` (`np.random.default_rng(seed)`); никаких `np.random.seed(...)`-глобалов.
- Для `b = 1..N`:
  - Сгенерировать `idx = rng.integers(0, n, size=n)` — индексы с возвратом.
  - Для каждой метрики: `m_b = metric_fns[name](y_true[idx], y_pred[idx])`.
- Точечная оценка `point` — `metric_fns[name](y_true, y_pred)` на исходных массивах (без ресемпла).
- ДИ — **перцентильный метод**: `ci_low = np.quantile(m_resamples, 0.025)`, `ci_high = np.quantile(m_resamples, 0.975)`. BCa/обычный t-bootstrap **в v1 не использовать** — UPDATE-план явно фиксирует «95% ДИ для log loss и Brier», без указания BCa.
- Запрещено: разный seed на каждую метрику; разный seed на каждый ресемпл сверх единого `Generator`.

#### 3.3.2 Block bootstrap по `day` (для `holdout`)

- На входе обязательна колонка `day` (datetime64 или string `YYYY-MM-DD`). Преобразование к канонической форме — внутри bootstrap-модуля (через `pd.to_datetime`), но без побочных эффектов для входного объекта.
- Сгруппировать индексы по `day`: `day_to_idx: dict[day, np.ndarray]`. Порядок ключей **детерминирован** (отсортирован по возрастанию `day`).
- Пусть `D` — число уникальных дней. Для `b = 1..N`:
  - Сгенерировать `day_choice = rng.integers(0, D, size=D)` — индексы дней с возвратом, **размер выборки дней = `D`** (ресемплим столько же дней, сколько в исходном holdout, иначе ДИ исказятся).
  - Собрать индексы строк: `idx_b = concatenate([day_to_idx[sorted_days[i]] for i in day_choice])`. Никакого ресемплинга внутри дня (целый день идёт как блок).
  - Посчитать каждую метрику на `(y_true[idx_b], y_pred[idx_b])`.
- Точечная оценка и ДИ — как в §3.3.1.
- **Edge case:** если в `holdout` всего 1 уникальный день — `block_by_day` теряет смысл; модуль обязан бросить `BootstrapError` с понятным сообщением «недостаточно дней для block bootstrap (D=1)». Тихая деградация до i.i.d. **запрещена** (это исказит ДИ незаметно для пользователя).

#### 3.3.3 Поведение по умолчанию для `holdout` и `test_k`

- На `holdout` — `block_by_day=True` по умолчанию (флаг `evaluation.bootstrap_block_by_day=true` из YAML).
- На `test_k` — `block_by_day=False`. Окна короткие, i.i.d. ресемплинг матчей корректен. **Не использовать** block bootstrap на `test_k` по умолчанию даже если флаг true — это решение этапа: «для test_k — i.i.d. resampling матчей допустим (окна короткие)».
- Решение «какой режим на каком фолде» принимает вызывающий код (CLI этапа 10 / отчёт этапа 5); bootstrap-функция лишь честно выполняет тот режим, который ей передали.

### 3.4 Контракт ошибок

Bootstrap-модуль обязан бросать понятные исключения (свой класс `BootstrapError` или `ValueError` с пояснением):

- `n_resamples ≤ 0` — ошибка конфигурации.
- `len(y_true) != len(y_pred)` — несоответствие форм.
- `y_true` содержит значения вне `{0, 1}` — ошибка (метки должны быть бинарными; преобразование меток — ответственность вышестоящего кода).
- `block_by_day=True` и `day` не передан / содержит NaT / число уникальных дней < 2 — `BootstrapError`.
- `n_resamples` не int или не положителен — ошибка.

### 3.5 Совместимость с метриками этапа 5

- Bootstrap **не** реализует свою формулу `log loss` и `brier` — он вызывает функции из `modeling/metrics.py` (через `metric_fns`). Это устраняет риск рассинхронизации `ε`-клиппинга, формулы Brier, и т.п.
- Точечная оценка `point` в результате bootstrap **обязана совпадать** с тем, что вернёт обычный вызов соответствующей метрики на исходных `(y_true, y_pred)` — это легко проверить тестом (см. §4).

### 3.6 Если этап 5 ещё не реализован

Допустимы два варианта (выбрать **один**, зафиксировать в MR):

1. **Реализовать минимальные `log_loss` и `brier` в `modeling/metrics.py`** в этом же MR — но **строго по контракту** этапа 5: log loss с клипом `p ∈ [ε, 1−ε]`, `ε = evaluation.epsilon_clip` (по умолчанию `1e-15`); Brier — стандартная формула; никаких дополнительных метрик и breakdown'ов из этапа 5. Это разрешённое исключение **только** ради устранения зависимости bootstrap от ещё не написанного модуля. ECE, reliability table, breakdown по командам, тривиальный baseline — **вне скоупа этого этапа**.
2. Принимать `metric_fns` через параметр функции и не импортировать `modeling/metrics.py` напрямую. Тогда тесты этапа должны передавать собственные мини-реализации `log_loss`/`brier`, а интеграция с реальными метриками произойдёт на этапе 5 / 10.

Тестовый MR не должен расширять `modeling/metrics.py` сверх минимума — иначе сорвётся скоуп этапа 5.

### 3.7 Запреты

- **Независимые seeds** на метрику, фолд, ресемпл — запрещены. Один `Generator` из `random_seed`, точка.
- Глобальный `np.random.seed(...)` — запрещён (загрязняет state процесса; ломает соседние тесты).
- Скрытое использование `os.cpu_count()` / `joblib` без явного `n_jobs = compute.num_threads` — запрещено.
- BCa-bootstrap, t-bootstrap, доверительные интервалы по нормальной аппроксимации — **вне скоупа v1**. Только перцентильный метод.

---

## 4. Тесты

Создать `tests/test_modeling_bootstrap.py` (или дополнить `tests/test_modeling_metrics.py`, если bootstrap живёт в `modeling/metrics.py`). Минимум:

1. **Детерминизм при фиксированном seed:** на синтетических `y_true`, `y_pred` два независимых вызова bootstrap с одинаковым `seed` дают **попарно идентичные** `ci_low`, `ci_high`, `point` (через `np.testing.assert_allclose` с `atol=0`).
2. **Зависимость от seed:** два вызова с разными `seed` дают **разные** ДИ (на нетривиальных данных). Это защита от случайно зашитого seed внутри функции.
3. **Совпадение `point` с метрикой:** на исходных `(y_true, y_pred)` `point` из bootstrap равен прямому вызову `metric_fns[name](y_true, y_pred)` — `np.testing.assert_allclose` с `atol=1e-12`.
4. **Размер ресемпла i.i.d.:** покрытие ДИ на синтетике с известным распределением (генерим `y_pred` как Bernoulli(`p`), считаем log loss / Brier, проверяем, что истинное значение попадает в 95% ДИ в ≥ 90% прогонов из 20–50 случайных seed'ов — мягкая граница, чтобы тест не был flaky; конкретный порог зафиксировать в коде теста с комментарием).
5. **Block bootstrap по day:** на синтетике с днями, где у каждого дня сильно разная база (например, half-days с `y=0`, half-days с `y=1`), block bootstrap даёт **существенно более широкий ДИ**, чем i.i.d. на том же `n` (qualitative-проверка `ci_high − ci_low` block > i.i.d.).
6. **Edge case D=1:** при `block_by_day=True` и единственном уникальном дне — `BootstrapError` (а не тихий i.i.d.).
7. **Edge case NaT в `day`:** при `block_by_day=True` и NaT в днях — `BootstrapError` с понятным сообщением.
8. **Контракт ошибок:** `n_resamples=0` → ошибка; `len(y_true) != len(y_pred)` → ошибка; `y_true ∉ {0,1}` → ошибка.
9. **Отсутствие глобального state:** после вызова bootstrap `np.random.rand()` даёт то же значение, что и до (значит, не было `np.random.seed(...)` под капотом). Тест запускается с сохранением `np.random.get_state()` до/после.
10. **Метаданные результата:** `BootstrapResult` содержит ровно `bootstrap.N`, `bootstrap.block_by_day`, `bootstrap.seed` (имена и типы), пригоден к `json.dumps` (через `dataclasses.asdict` или эквивалент).

Параллельно (минимум, не блокер для этого MR):

- Если ещё нет `tests/test_modeling_no_db_access.py` для соответствующего файла — добавить мини-проверку, что `modeling/bootstrap.py` (или релевантный кусок `modeling/metrics.py`) не импортирует `psycopg2`, `modeling.dataset_builder.*`. Полный охват — на этапе 11.

---

## 5. Интеграция с конфигом (этап 2)

Этап 2 (`modeling/config.py`) может быть ещё не закрыт к моменту работы над этим этапом. Допустимо:

- Принимать в bootstrap-функцию **обычные параметры** (`n_resamples: int`, `block_by_day: bool`, `seed: int`, `num_threads: int`) — без зависимости от dataclass'а конфига.
- Все значения по умолчанию (`n_resamples=1000`, `block_by_day=False`, `num_threads=1`) обязаны совпадать с дефолтами UPDATE-плана: `evaluation.bootstrap_samples=1000`, `evaluation.bootstrap_block_by_day=true` (но только для holdout — см. §3.3.3), `compute.num_threads=1`.
- При появлении `modeling/config.py` (этап 2) — связь делается на этапе 10 (CLI), не в этом MR.

---

## 6. Критерии приёмки

1. Bootstrap-модуль реализован (`modeling/metrics.py` либо `modeling/bootstrap.py`) с публичным API из §3.2.
2. На исходных `(y_true, y_pred)` `point` совпадает с прямым вызовом метрики — есть тест.
3. Detерминизм: одинаковый `seed` → попарно идентичные ДИ; разные `seed` → разные ДИ.
4. Block bootstrap по `day` реализован, при `D=1` бросает `BootstrapError`, тихая деградация запрещена.
5. Никаких `np.random.seed(...)`, `os.cpu_count()`, скрытых параллелизаций — потоки только через `num_threads`.
6. Результат сериализуется в `metrics.json` с полями `bootstrap.N`, `bootstrap.block_by_day`, `bootstrap.seed` (это поля метаданных прогона из UPDATE-плана).
7. Тесты §4 проходят локально (`pytest tests/test_modeling_bootstrap.py` или соответствующий файл).
8. `modeling/bootstrap.py` (или часть `modeling/metrics.py` с bootstrap) не импортирует `psycopg2`, `modeling.dataset_builder.*`.
9. Дефолты совпадают с UPDATE-планом: `n_resamples=1000`, `num_threads=1`; режим `block_by_day` по умолчанию для holdout — `True`, для test_k — `False` (это документировано в docstring).
10. В описании MR явно сказано: «реализован этап 6 UPDATE-плана; этапы 5 (метрики сверх минимума), 7–10 не затрагиваются».

---

## 7. Ограничения и вне скоупа

- **Не** реализовывать обучение моделей (этапы 7–8), калибровку (этап 9), CLI `train` (этап 10), полный `metrics.py` со всеми метриками этапа 5 (ECE, reliability, breakdown по командам, тривиальный baseline) — кроме минимума `log_loss`/`brier`, если выбран вариант §3.6.1.
- **Не** добавлять BCa / t-bootstrap / нормальные ДИ — только перцентильный метод.
- **Не** менять `modeling/train_input.py`, `modeling/splits.py`, `modeling/dataset_builder/*`.
- **Не** вводить новые YAML-поля сверх уже зафиксированных в UPDATE-плане (`evaluation.bootstrap_samples`, `evaluation.bootstrap_block_by_day`). Загрузка YAML — на этапе 2; здесь параметры приходят как аргументы функции.
- **Не** добавлять параллелизацию через `multiprocessing`/`joblib` без явного `num_threads` параметра, не использовать переменные окружения для потоков.

---

## 8. Что отдать в MR

- `modeling/bootstrap.py` **или** расширение `modeling/metrics.py` секцией bootstrap (с явным указанием в описании MR, какой вариант выбран).
- `tests/test_modeling_bootstrap.py` (или соответствующее расширение `tests/test_modeling_metrics.py`).
- Краткий раздел в `docs/modeling_training.md` (если файл уже создан этапами 1/4) с описанием:
  - формулы перцентильного ДИ;
  - режима block bootstrap по `day` и где он применяется (holdout) vs i.i.d. (test_k);
  - дефолтов (`N=1000`, `seed=random_seed`);
  - правила «независимые seeds запрещены».
- В описании MR — явная отметка: «реализован этап 6 UPDATE-плана; этапы 7–10 не затрагиваются». Также — какой вариант размещения кода (метrics.py vs bootstrap.py) выбран и почему.
