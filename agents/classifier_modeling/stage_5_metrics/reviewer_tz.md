# ТЗ для ревьюера: этап 5 — метрики и отчётность

**Роль:** независимый reviewer.  
**Источник:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 5. Метрики и отчётность», с учётом «## Сквозные требования (читать перед каждым этапом)».  
**Пара ТЗ для исполнителя:** [`executor_tz.md`](executor_tz.md).

Пути — от корня репозитория.

---

## 1. Принцип ревью

Приоритет: **корректность формул и клиппинга**, **отсутствие подсматривания** в `trivial_baseline` (base rate берётся с train, а не с test/holdout), **детерминизм** чистых функций, **изоляция от БД и билдера**, фиксированная структура `metrics.json` для последующего этапа 10.

Блокеры — в §7. Ниты — в §8.

---

## 2. Корректность метрик (`modeling/metrics.py`)

- [ ] `log_loss` использует клип `p ∈ [ε, 1−ε]` с `ε` из аргумента (по умолчанию `1e-15`); для `p = 0` или `p = 1` функция возвращает конечное значение.
- [ ] `brier` реализован как `mean((p − y)²)` **без** клипа.
- [ ] `ece` считается на **фиксированных** `n_bins` бинах равной ширины в `[0, 1]` (по умолчанию 10); пустые бины не дают вклад и не делят на ноль; формула — `Σ_bin w_bin · |conf_bin − acc_bin|`.
- [ ] `reliability_table` возвращает по строке на бин (включая пустые), сумма `weight` ≈ `1.0` на непустом срезе; для пустого бина `count=0`, `mean_pred=NaN`, `frac_positive=NaN`.
- [ ] `team_breakdown` принимает явный параметр `by ∈ {"home_team_id", "away_team_id"}` и не путает home/away; возвращает `log_loss_minus_overall` относительно среднего по **переданному срезу** (не глобального).
- [ ] `trivial_baseline` принимает **два** массива: `y_train` (для base rate) и `y_test` (для оценки). Реализация явно использует `y_train` для `p ≡ mean(y_train)`; тест на перепутывание присутствует.
- [ ] Эталонные значения покрыты тестом на ручных мини-векторах с относительной погрешностью ≤ `1e-12`.

---

## 3. Reliability PNG

- [ ] matplotlib переведён на non-interactive backend (`Agg`) **до** первого импорта `pyplot` (или явная проверка/настройка в модуле). Импорт `pyplot` не падает на CI без X-сервера.
- [ ] После сохранения PNG вызывается `plt.close()` — нет утечки `figure` при повторных вызовах.
- [ ] Не используется системный шрифт/локальный TTF; график строится на дефолтных шрифтах matplotlib.
- [ ] Размеры `figure` и `dpi` фиксированы (детерминизм PNG между прогонами при идентичном входе — желательно, но не блокер; блокер — отсутствие воспроизводимости от run-to-run при одинаковых данных).

---

## 4. Структура отчёта

- [ ] `metrics.json` содержит **обязательные ключи** в фиксированном написании: `run_id`, `task`, `model`, `features_hash`, `evaluation`, `folds`, `holdout`, `team_breakdown`.
- [ ] В каждом блоке fold/holdout присутствуют `raw`, `calibrated` (`null` допустим, если калибровка пропущена), `trivial_base_rate.log_loss` и `trivial_base_rate.brier`. Без последних строк прогон не сможет пройти §12.6 UPDATE-плана.
- [ ] `summary.md` содержит таблицу по фолдам и блок holdout; явная строка про тривиальный baseline; ссылка на `reliability_<task>.png`.
- [ ] `write_report` пишет ровно три типа файлов в `<out_dir>`: `metrics.json` (UTF-8, `indent=2`, **ключи отсортированы** — критично для git-diff'ов и code review), `summary.md`, `reliability_<task>.png` для каждой задачи.
- [ ] Перезапись существующих файлов сопровождается `logger.warning`.

---

## 5. `<run_id>` и логирование

- [ ] `compose_metrics_json` валидирует `<run_id>` единой регуляркой формата `<task>_<model>_<features_hash[:8]>_<utc_timestamp_YYYYmmddTHHMMSSZ>` и поднимает `ValueError` на битом id. Регулярка — **одна** на весь модуль (не дублируется).
- [ ] `configure_run_logger` создаёт `FileHandler` на `<out_dir>/run.log` с UTC-временем (`Formatter.converter = time.gmtime`); повторный вызов **не** дублирует хендлеры; root-логер не перенастраивается агрессивно.
- [ ] В продакшен-коде нет `print()` — только `logging.getLogger(__name__)`.

---

## 6. Изоляция и зависимости

- [ ] `modeling/metrics.py` (и `modeling/report.py`, если выделен) **не** импортируют `psycopg2`, `modeling.dataset_builder.*`, `modeling.train_input`, любые модули с БД/сетью. Проверяется автотестом этапа 11 (`tests/test_modeling_no_db_access.py`) — если тест ещё не написан, в ревью убедиться импорт-блоком модуля.
- [ ] Внешние зависимости — только из `requirements-modeling.txt` (`numpy`, `pandas`, `matplotlib`, опционально `scikit-learn` для AUC). Никаких новых строк в `requirements-modeling.txt` в этом MR.
- [ ] Никаких `OMP_NUM_THREADS`/`MKL_NUM_THREADS`/`joblib.Parallel` в `metrics.py`.

---

## 7. Тесты

- [ ] `tests/test_modeling_metrics.py` существует и содержит минимум 10 сценариев §2.6 ТЗ исполнителя:
  - эталонные log loss / Brier / ECE на ручных мини-векторах;
  - клип на `p ∈ {0, 1}`;
  - ECE ≈ 0 на «идеальном» синтетическом срезе с большим `N` и фиксированным `np.random.default_rng(seed)`;
  - сумма `weight` в reliability ≈ 1, пустой бин корректен;
  - `trivial_baseline` использует **train** base rate, а не test (явно проверено);
  - team breakdown — формат и сравнение «худшая команда» vs. «средняя»;
  - `plot_reliability` сохраняет непустой PNG в `tmp_path`;
  - `compose_metrics_json` ловит битый `<run_id>`;
  - `write_report` пишет три файла;
  - `configure_run_logger` идемпотентен.
- [ ] Тесты не требуют PostgreSQL, не читают `dataset_train.csv`, не запускают обучение.
- [ ] Общее время `pytest tests/test_modeling_metrics.py -q` — секунды на ноутбуке; нет flaky из-за неконтролируемого `random`.

---

## 8. Скоуп

- [ ] В MR **нет** кода сплитов (этап 4), бутстрапа (этап 6), обучения logreg/lgbm (этапы 7–8), калибровки (этап 9), CLI `train` (этап 10). Если такие файлы появились — это блокер.
- [ ] `modeling/train_input.py`, `modeling/dataset_builder/*` не меняются.
- [ ] `requirements-modeling.txt`, `Makefile`, `Dockerfile`, `docker-compose.yml` не меняются (если изменения нужны — отдельный абзац в описании MR с обоснованием; в данном этапе ожидается, что они не нужны).
- [ ] Документ `docs/modeling_training.md` либо не создан в этом MR, либо содержит только короткий подраздел «Метрики и формат отчёта» — без забегания вперёд по другим этапам.

---

## 9. Вердикт

**approve / approve with nits / request changes**.

### Блокеры (request changes)

- `trivial_baseline` считает base rate на test/holdout вместо train — это прямое нарушение §5 UPDATE-плана и §12.6 (защита от «модель не лучше константы»).
- `log_loss` падает с `−inf` или `NaN` на `p ∈ {0, 1}` — отсутствует клиппинг ε.
- ECE: бины не равной ширины, или используется квантильная схема без override — расходится с UPDATE-планом.
- `<run_id>` не валидируется регуляркой, либо валидаторы расходятся по модулю.
- `metrics.py` импортирует `psycopg2` / `modeling.dataset_builder.*` / делает сетевые запросы.
- matplotlib без `Agg`-backend — падение на CI; либо `plt.close()` отсутствует → утечка `figure` при walk-forward.
- `metrics.json` без ключа `trivial_base_rate` в фолдах/holdout, либо без сортировки ключей — ломает code review diff'ы и приёмку §12.6.
- Отсутствует тест на «base rate с train, не с test» — приёмочный критерий не закрыт.
- Появился код этапов 4 / 6 / 7–10 в этом MR.

### Ниты (approve with nits)

- Имена публичных функций отличаются от ориентиров `executor_tz.md` — допустимо, если семантика та же и docstring явен.
- ROC-AUC реализован, но в `metrics.json` его нет — ок, не блокер.
- Reliability PNG не побитово воспроизводим (отличия в антиалиасинге между версиями matplotlib) — приемлемо, если файл создаётся и не пустой.
- `summary.md` без топ-5 худших/лучших команд — приемлемо, если есть ссылка на `team_breakdown` в `metrics.json`.
- Логгер пишет в `INFO` без возможности override — добавить параметр `level`.
