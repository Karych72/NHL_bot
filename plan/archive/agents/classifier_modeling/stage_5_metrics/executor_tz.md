# ТЗ для исполнителя: этап 5 — метрики и отчётность

**Роль:** инженер-исполнитель.  
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 5. Метрики и отчётность», с учётом «## Сквозные требования (читать перед каждым этапом)» того же документа.  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Соседние этапы (не дублировать):** [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) (этап 1 — контракт входа), [`../stage_3_dependencies/executor_tz.md`](../stage_3_dependencies/executor_tz.md) (этап 3 — зависимости).

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать **офлайн-библиотеку метрик и отчётности** для прематч-классификаторов (`y_home_win`, `y_over_5_5`), которая:

- считает все основные метрики качества и калибровки **детерминированно** и без обращения к PostgreSQL и к билдеру датасетов;
- собирает табличные срезы для отчёта прогона (walk-forward + holdout) — включая **breakdown по командам** и **тривиальный baseline** на базе train-base-rate;
- умеет сериализовать результат в принятую структуру `artifacts/reports/<run_id>/{metrics.json, summary.md, reliability_<task>.png, run.log}` и не более того.

Никаких сплитов, бутстрапа, обучения моделей, калибровки и CLI `train` в этом этапе не добавляется — это этапы 4, 6, 7–9 и 10 UPDATE-плана соответственно. Этап 5 предоставляет **строительные блоки**, которые этап 10 (CLI) будет вызывать.

---

## 2. Объём работ

### 2.1 Модуль `modeling/metrics.py`

Создать публичный модуль `modeling/metrics.py`. Допускается выделить сериализацию отчёта в `modeling/report.py` (тогда `metrics.py` остаётся чисто вычислительным) — выбор на усмотрение исполнителя, но **граница «чистые метрики vs. сериализация» должна быть явной** и согласованной с тестами.

Реализовать следующие чистые функции (имена-ориентиры; точные сигнатуры — на усмотрение, но публичный API стабильный):

- `log_loss(y_true, p, *, epsilon)` — клип `p ∈ [ε, 1−ε]`, далее стандартная формула `−mean(y·log p + (1−y)·log(1−p))`. По умолчанию `epsilon = 1e-15` (поле `evaluation.epsilon_clip`).
- `brier(y_true, p)` — `mean((p − y)²)`, без клипа.
- `ece(y_true, p, *, n_bins)` — фиксированные `n_bins` бинов в `[0, 1]` равной ширины (по умолчанию `10` из `evaluation.ece_bins`). Формула: `Σ_bin w_bin · |conf_bin − acc_bin|`, где `w_bin = доля точек в бине`, `conf_bin = mean(p_in_bin)`, `acc_bin = mean(y_in_bin)`. Пустые бины не дают вклад (и не делятся на ноль).
- `reliability_table(y_true, p, *, n_bins)` — табличный аналог ECE: строки = бины, столбцы = `bin_lower`, `bin_upper`, `count`, `weight = count / N`, `mean_pred`, `frac_positive`. Пустые бины присутствуют со счётчиком `0` и `NaN` в `mean_pred`/`frac_positive`.
- `team_breakdown(y_true, p, *, team_ids, by)` — средний `log_loss` по группам команд (`by ∈ {"home_team_id", "away_team_id"}`). Возвращает таблицу: `team_id`, `n_games`, `log_loss`, `log_loss_minus_overall` (разница со средним по всему срезу — диагностика «редких команд»).
- `trivial_baseline(y_train, y_test, *, epsilon)` — log loss и Brier предиктора-константы `p ≡ mean(y_train)` на `y_test`. **Базовая частота считается на train_k, не на test_k и не на holdout** — это страховка от подсматривания.
- (опционально) утилиты валидации входа: проверка длин, отсутствие `NaN` в `p`, диапазон `p ∈ [0, 1]` (за пределами — `MetricsInputError`), все `y_true ∈ {0, 1}`.

Требования к чистым функциям:

- Только `numpy`/`pandas`; без обращений к файловой системе, БД, сетевым ресурсам.
- Без скрытых глобальных параметров — все клипы/бины передаются явно.
- Детерминизм: одинаковый вход → одинаковый выход побитово (никаких `random`/`np.random` внутри).

### 2.2 Reliability-плот

Реализовать функцию построения reliability PNG (например, `plot_reliability(reliability_df, *, title, out_path)`):

- бины на оси X — центры (или левые границы) бинов из reliability table;
- две линии: `mean_pred` (модель) и `frac_positive` (наблюдаемая частота); диагональ `y = x` пунктиром;
- ширина/высота `figure` — фиксированные, чтобы PNG воспроизводился побитово (или хотя бы по содержимому близко) между прогонами; шрифты — дефолтные, без локальных fonts;
- размер бара/маркера учитывает `weight` (опционально: размер маркера ∝ `count`);
- сохранение по `out_path` (Path-like) через `matplotlib.pyplot.savefig(..., dpi=…)`; `plt.close()` обязателен после сохранения, чтобы не утекали `figure` при много-таск прогоне;
- использовать **non-interactive backend** matplotlib (`matplotlib.use("Agg")` до первого импорта `pyplot` — либо в модуле, либо проверкой через `matplotlib.get_backend()`).

### 2.3 Сборка отчёта прогона

Реализовать функции сборки артефактов отчёта согласно UPDATE-плану:

- `compose_metrics_json(...)` — собрать словарь, который пойдёт в `artifacts/reports/<run_id>/metrics.json`. Минимальная структура (ключи фиксированы):

  ```
  {
    "run_id": "<task>_<model>_<features_hash[:8]>_<utc_timestamp_YYYYmmddTHHMMSSZ>",
    "task": "home_win" | "over_5_5",
    "model": "logreg" | "lgbm",
    "features_hash": "<sha256>",
    "evaluation": {"epsilon_clip": 1e-15, "ece_bins": 10},
    "folds": [
      {
        "k": <int>,
        "train_range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
        "test_range":  {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
        "n_train": <int>, "n_test": <int>,
        "raw":  {"log_loss": ..., "brier": ..., "ece": ...},
        "calibrated": {"log_loss": ..., "brier": ..., "ece": ...} | null,
        "trivial_base_rate": {"log_loss": ..., "brier": ..., "p": ...}
      }, ...
    ],
    "holdout": { ... тот же blok что и для fold, плюс reliability_path ... },
    "team_breakdown": { "home_team_id": [...], "away_team_id": [...] }
  }
  ```

  Конкретное наполнение блоков — на усмотрение, **но**: ключи `log_loss`, `brier`, `ece`, `trivial_base_rate`, `reliability_path`, `holdout`, `folds`, `task`, `model`, `features_hash`, `run_id` должны присутствовать в фиксированном написании — на них будет смотреть этап 10 и автотест.
- `compose_summary_md(metrics_json: dict) -> str` — человекочитаемый `summary.md`:
  - заголовок с `<run_id>`, задачей и моделью;
  - таблица по фолдам: `k | n_test | log_loss_raw | brier_raw | ece_raw | log_loss_cal | trivial_base_rate.log_loss`;
  - блок holdout с теми же столбцами + ссылка на `reliability_<task>.png`;
  - топ-5 худших/лучших команд из `team_breakdown` (по `log_loss_minus_overall`);
  - явная строка про **тривиальный baseline** для каждого блока (см. §12.6 UPDATE-плана — это критерий приёмки прогона).
- `write_report(out_dir: Path, *, metrics_json: dict, reliability_pngs: Mapping[str, np.ndarray|pd.DataFrame], summary_md: str)` (имя на усмотрение) — единственная точка, которая фактически пишет файлы; принимает уже подготовленные структуры и сохраняет:
  - `metrics.json` (UTF-8, `indent=2`, ключи отсортированы для воспроизводимости diff'ов);
  - `summary.md`;
  - `reliability_<task>.png` для каждой задачи в `reliability_pngs`.

  Каталог `out_dir` создаётся, если его нет. Перезапись допускается, но логируется (`logger.warning` с указанием существующего файла).

### 2.4 `run.log` — единый логер

`run.log` в `artifacts/reports/<run_id>/run.log` — общий для всего прогона. В рамках этого этапа:

- добавить хелпер `configure_run_logger(out_dir: Path, *, level: str) -> logging.Logger` (имя на усмотрение). Хелпер:
  - создаёт `FileHandler` на `<out_dir>/run.log`, формат `"%(asctime)sZ %(levelname)s %(name)s: %(message)s"` (время — UTC, `logging.Formatter.converter = time.gmtime`);
  - не перенастраивает root-логер агрессивно; добавляет хендлер к именованному логеру модуля и возвращает его;
  - уровень — из аргумента (значение приходит из YAML на этапе 10; в этом этапе достаточно поддержать передачу строкой `"INFO"`, `"DEBUG"`, `"WARNING"`).
- `metrics.py` / `report.py` использует этот логер (через `logging.getLogger(__name__)`) для нетривиальных событий: пустой срез команды в breakdown, пустые бины в reliability, перезапись существующих файлов.

В этом этапе не требуется поднимать `run.log` с реальным прогоном моделей — достаточно, чтобы вызов `configure_run_logger` и последующий вызов `write_report` корректно писал лог-файл в той же папке отчёта.

### 2.5 Интеграция со сквозными требованиями UPDATE-плана

Из «## Сквозные требования» обязательно учесть:

- **Источник истины фич** — `metadata_train.json`: модуль метрик **не** знает про фичи и не должен принимать `feature_manifest` на вход; ему передают только массивы `y_true`, `p` и идентификаторы команд. Это сквозное требование закрывается тем, что метрики **не** трогают `X` и `manifest`.
- **Воспроизводимость**: чистые функции не должны привносить недетерминизм. Версии библиотек (`numpy`, `pandas`, `sklearn` если используется хотя бы для `roc_auc_score`, `matplotlib`) **не** логируются здесь — это задача этапа 10 при сборке `metadata.json` артефакта. Однако функции этапа 5 не должны мешать такому логированию (например, держать version pins из `requirements-modeling.txt`, не делать монки-патчей в matplotlib).
- **Лимит потоков** (`compute.num_threads`): в `metrics.py` нет операций, которые порождают тред-пулы (мы оперируем `numpy`/`pandas` на одном фрейме). Не задавать `OMP_NUM_THREADS`/`MKL_NUM_THREADS` и не трогать `joblib.Parallel`. Если для построения reliability используется любая внешняя многопоточная либа — отказаться от неё в пользу простого `numpy`.
- **`<run_id>`**: формирование `<run_id>` — не задача `metrics.py`. Но `compose_metrics_json` принимает готовый `run_id` строкой и **валидирует** формат регуляркой `^[a-z0-9_]+_(logreg|lgbm)_[0-9a-f]{8}_\d{8}T\d{6}Z$` (поднимает `ValueError` при несовпадении). Регулярка должна быть единственной точкой истины формата — её же будет использовать этап 10.
- **Никакого доступа к PostgreSQL**: `modeling/metrics.py` (и `modeling/report.py`, если выделен) **не** импортируют `psycopg2`, `modeling.dataset_builder.*`, любые модули, делающие сетевые/БД-запросы. Это правило уже зафиксировано в этапе 11 (`tests/test_modeling_no_db_access.py`) — данный этап должен пройти этот тест из коробки.

### 2.6 Тесты

Создать `tests/test_modeling_metrics.py` со следующими сценариями:

1. **Эталонные мини-векторы** для `log_loss`, `brier`, `ece`: ручные значения на 4–8 точках, посчитанные офлайн (например, для `y = [0, 1, 1, 0]`, `p = [0.1, 0.9, 0.7, 0.2]`). Сравнение через `pytest.approx` с относительной погрешностью `1e-12`.
2. **Клиппинг log_loss**: для `p ∈ {0.0, 1.0}` функция не падает с `−inf`, а возвращает конечное значение, согласованное с `epsilon=1e-15`.
3. **ECE на идеально откалиброванном предикторе ≈ 0**: сгенерировать большой синтетический срез (например, `p ~ U[0,1]`, `y ~ Bernoulli(p)`, `N ≥ 50_000`, фиксированный `np.random.default_rng(seed=…)`); проверить `ece(...) < 0.01`. Тест помечен как «детерминированный по seed», не flaky.
4. **Reliability table**: сумма `weight` по всем бинам ≈ `1.0`; число строк = `n_bins`; пустой бин имеет `count = 0` и `NaN` в `mean_pred`.
5. **Тривиальный baseline считается на train, не на test**: на синтетике `y_train = [0]*100`, `y_test = [1]*10` — `trivial_baseline` должен дать `p = 0.0` (с клипом ε) и log loss соответствующий ε-клипу; перепутывание train/test даст другой ответ (явно проверить, что используется именно `y_train`).
6. **Team breakdown**: на синтетическом срезе с заведомо «плохой» командой — её `log_loss` ощутимо выше среднего (`log_loss_minus_overall > 0`); проверить и формат таблицы (`team_id`, `n_games`, `log_loss`, `log_loss_minus_overall`).
7. **Reliability PNG сохраняется**: вызвать `plot_reliability` на `tmp_path`, проверить, что файл существует и непустой (`stat().st_size > 0`); без сравнения по байтам.
8. **`compose_metrics_json` валидирует `<run_id>`**: корректный id проходит, битый (`"weird-id"`) — `ValueError`.
9. **`write_report` пишет три файла** в `tmp_path / "<run_id>"`: `metrics.json` (валидный JSON, ключи отсортированы), `summary.md` (непустой), `reliability_<task>.png`.
10. **`configure_run_logger`** создаёт `run.log` в нужной папке; повторный вызов не дублирует хендлеры (idempotent).

Запрет: тесты **не** требуют PostgreSQL, не читают `dataset_train.csv`, не зависят от LightGBM/sklearn beyond use of `numpy`/`pandas`/`matplotlib`. Все фикстуры — синтетические.

### 2.7 Документация

Минимум:

- docstring модулей `modeling/metrics.py` (и `modeling/report.py`, если выделен) — что считают, какие ключи в `metrics.json`, ссылка на этап 5 UPDATE-плана.
- В `docs/modeling_dataset_builder.md` — **не** трогать. Документ обучения (`docs/modeling_training.md`) **может** не существовать к моменту этого этапа; если уже создан соседним MR — дописать короткий подраздел «Метрики и формат отчёта прогона» со ссылкой на ключи `metrics.json`. Если документа ещё нет — оставить только docstring; создавать `docs/modeling_training.md` ради одного абзаца **не** требуется (он появится в этапе 13).

---

## 3. Сквозные требования (обязательно учесть)

Из раздела «Сквозные требования» UPDATE-плана на этом этапе релевантно:

- **Источник истины фич** — `metadata_train.json`: метрики не работают с `feature_manifest`, см. §2.5 выше.
- **Воспроизводимость**: все функции чистые; никакого `random` без явного `seed`.
- **Лимит потоков**: модуль метрик не порождает тред-пулы.
- **Логи**: единый `run.log` через `configure_run_logger`; никакого `print` в продакшен-коде, только в тестах при необходимости.
- **`<run_id>`** — единый формат `<task>_<model>_<features_hash[:8]>_<utc_timestamp_YYYYmmddTHHMMSSZ>`; валидация регуляркой.
- **Никакого доступа к PostgreSQL в train-коде** — `metrics.py` свободен от импортов `psycopg2`, `modeling.dataset_builder.*` (проверяется автотестом этапа 11).

---

## 4. Критерии приёмки

1. Создан `modeling/metrics.py` (и, при необходимости, `modeling/report.py`) с публичным API: `log_loss`, `brier`, `ece`, `reliability_table`, `team_breakdown`, `trivial_baseline`, `plot_reliability`, `compose_metrics_json`, `compose_summary_md`, `write_report`, `configure_run_logger`. Имена допускают вариативность, но **семантика и набор функций** соответствует §2.1–2.4.
2. Все функции в §2.1 — детерминированные и чистые: одинаковый вход → одинаковый выход; никаких сторонних эффектов в `metrics.py` (файлы пишет только `write_report`).
3. `metrics.json` имеет фиксированные ключи `run_id`, `task`, `model`, `features_hash`, `folds`, `holdout`, `team_breakdown`, `evaluation`; `trivial_base_rate` присутствует и в каждом фолде, и в holdout.
4. Reliability PNG сохраняется через non-interactive backend matplotlib; `plt.close()` после сохранения; PNG воспроизводится между прогонами при идентичном входе.
5. `<run_id>` валидируется единой регуляркой в `compose_metrics_json`; формат соответствует UPDATE-плану.
6. Тесты `tests/test_modeling_metrics.py` (минимум 10 сценариев §2.6) проходят локально командой `pytest tests/test_modeling_metrics.py -q`. Время прогона — секунды, без сети и БД.
7. `modeling/metrics.py` и `modeling/report.py` (если выделен) **не** импортируют `psycopg2`, `modeling.dataset_builder.*`, `modeling.train_input` — модуль изолирован от пайплайна загрузки и от БД.
8. `modeling/metrics.py` корректно работает после `make modeling-dev` (этап 3): зависимости `matplotlib`, `numpy`, `pandas` — единственные обязательные внешние; `scikit-learn` использовать только если действительно нужно (например, `sklearn.metrics.roc_auc_score` для опционального ROC-AUC), и тогда он уже есть в `requirements-modeling.txt`.

---

## 5. Ограничения и вне скоупа

- **Не** реализовывать сплиты (этап 4), бутстрап-ДИ (этап 6), обучение logreg/lgbm (этапы 7–8), калибровку (этап 9), CLI `train` и финальное переобучение (этап 10) — даже частично. Если для тестов нужны предсказания — собирать **синтетически** в фикстурах теста, а не запускать обучение.
- **Не** добавлять зависимости в `requirements-modeling.txt` — список зафиксирован на этапе 3 (`numpy`, `pandas`, `scikit-learn`, `lightgbm`, `matplotlib`, `pyyaml`, `joblib`).
- **Не** трогать `modeling/train_input.py`, `modeling/dataset_builder/*`, билдер датасетов.
- **Не** добавлять глобальные env-переменные (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`).
- **Не** реализовывать `<run_id>` генератор — только валидатор. Генерация — задача этапа 10.
- **Не** делать сравнения PNG по байтам в тестах — достаточно «файл создан и непустой».
- **Не** включать ROC-AUC как обязательную метрику — UPDATE-план не требует его в §5; при желании оставить как вспомогательную функцию **без** обязательной строки в `metrics.json`.
