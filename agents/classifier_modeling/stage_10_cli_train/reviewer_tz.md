# ТЗ для ревьюера: этап 10 — Единая точка входа CLI и production-артефакт

**Роль:** независимый reviewer.  
**Источник:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 10. Единая точка входа CLI и production-артефакт», с учётом «## Сквозные требования (читать перед каждым этапом)» и Definition of Done «### 12. Диагностика и критерии приёмки».  
**Пара ТЗ для исполнителя:** [`executor_tz.md`](executor_tz.md).

Пути — от корня репозитория.

---

## 1. Принцип ревью

Приоритет:

1. **Отсутствие утечки holdout** — finальное переобучение и отчёт строятся строго `model_final (train_full) → calibrator_final (calibration_final) → holdout`; holdout нигде не участвует в обучении/калибровке/выборе гиперпараметров.
2. **Воспроизводимость** — единый `random_seed` раздаётся вниз, `<run_id>` в каноническом формате, версии библиотек логируются.
3. **Изоляция train-пути от PostgreSQL** — `train` не тянет `psycopg2`/`modeling.dataset_builder.*`.
4. **Корректность production-артефакта и `latest`** — фиксированная схема каталогов, полный `metadata.json`, атомарный и безопасный симлинк, baseline-гейт.
5. **CLI оркестрирует, а не дублирует** — обучение/калибровка/метрики/bootstrap/сплиты не переписаны в CLI.

Если хоть один блокер из §11 нарушен — **request changes** с указанием конкретного места в коде/тесте.

---

## 2. Точка входа и флаги

- [ ] `python -m modeling.cli train --config configs/modeling_default.yaml` запускается; subparser `train` не ломает существующую `build-dataset` (если была).
- [ ] CLI-слой тонкий: парсинг → вызов тестируемой функции оркестрации (`run_training`/`train_runner`) → код возврата.
- [ ] Поддержаны флаги: `--config`, `--set`, `--task {home_win,over_5_5,both}`, `--model {logreg,lgbm,both}`, `--run-id`, `--print-resolved-config`, `--dry-run`.
- [ ] `--print-resolved-config` печатает итоговый конфиг и завершается кодом 0 **без** обучения и без создания артефактов.
- [ ] `--dry-run` печатает resolved-config + размеры **всех** блоков (`train/inner_val/calibration/test/holdout`) по каждому фолду и для финального переобучения, завершается кодом 0, ничего не обучает и не пишет артефакты/симлинки.
- [ ] `--task X` при `tasks.X.enabled=false` → `ConfigError`/ненулевой код (не тихий no-op).
- [ ] Дефолты `--task`/`--model` = `both`, но фактический набор уважает `tasks.*.enabled` из YAML.

---

## 3. `<run_id>` и воспроизводимость

- [ ] `<run_id>` строго в формате `<task>_<model>_<features_hash[:8]>_<utc_timestamp_YYYYmmddTHHMMSSZ>`. `features_hash` берётся из `metadata_train.json`, **не** из YAML.
- [ ] `--run-id` переопределяет автогенерацию (есть тест на резолвер).
- [ ] `random_seed` читается из YAML и раздаётся во все подсистемы (сплиты, logreg, lgbm, bootstrap). В CLI **нет** производных seeds (`seed+k`, `seed*...`, отдельный seed на task/model) — иначе блокер.
- [ ] Версии библиотек (`sklearn`, `lightgbm`, `pandas`, `numpy`) фактически попадают в каждый `metadata.json`.

---

## 4. Изоляция от PostgreSQL (сквозное требование)

- [ ] Train-путь CLI (`modeling/cli.py` команда `train` и/или `modeling/train_runner.py`) **не** импортирует `psycopg2`, `modeling.dataset_builder.*` на уровне модуля.
- [ ] Если `build-dataset` живёт в том же `cli.py` — её БД-импорты **ленивые** (внутри обработчика), либо train вынесен в отдельный модуль; импорт CLI-модуля не тянет БД-зависимости. Способ зафиксирован в MR.
- [ ] Вход — только через `load_training_table_split(...)` (этап 1). Нет прямого `pd.read_csv` датасета в обход контракта, нет обращения к БД.

---

## 5. Walk-forward оркестрация

- [ ] Сплиты приходят из этапа 4; CLI не реализует собственную нарезку времени и не использует `KFold/StratifiedKFold(shuffle=True)`/`ShuffleSplit`.
- [ ] Для каждого фолда `k`: сырая модель на `train_k`, выбор/early stopping на `inner_val_k`, калибратор на `calibration_k`, предсказание/метрики/bootstrap на `test_k`. Порядок и срезы корректны.
- [ ] `calibration_skipped=true` при `|calibration_k| < calibration.min_samples` обрабатывается (сырая вероятность + предупреждение), а не падение.
- [ ] bootstrap на `test_k` — **i.i.d.**; на holdout — **block-by-day** (этап 6), `seed=random_seed`, `num_threads=compute.num_threads`.
- [ ] `n_test_windows` уважается; CLI не «зашивает» число окон в обход YAML.

---

## 6. Финальное переобучение и отсутствие утечки (ключевой раздел)

- [ ] `model_final` обучается на `train_full` = **всё до `calibration_final`** (без holdout и без последнего калибровочного блока). Проверить по коду срезов.
- [ ] `calibrator_final` обучается на `calibration_final` (последний пред-holdout блок, **не** пересекается с holdout).
- [ ] Holdout-отчёт строится **строго** цепочкой `model_final → calibrator_final` (а не усреднением фолдов, не отдельной «моделью на всём с holdout»).
- [ ] holdout **нигде** не участвует в `fit`/калибровке/выборе гиперпараметров. Любое подглядывание в holdout (включая расчёт base rate для baseline из holdout вместо `train_full`) — **блокер**.
- [ ] Гиперпараметры финальной модели — те же, что зафиксированы протоколом этапов 7–8, без оптимизации по holdout.

---

## 7. Production-артефакт и `latest`

- [ ] Схема каталогов точно как в плане:
  - фолды: `artifacts/models/<task>/<model>/<run_id>/fold_<k>/`;
  - production: `artifacts/models/<task>/<model>/<run_id>/final/{model.joblib, calibrator.joblib, metadata.json}`;
  - симлинк: `artifacts/models/<task>/<model>/latest` → `<run_id>/final/`.
- [ ] `metadata.json` финального артефакта содержит **все** поля DoD: `features_hash`, диапазоны дат `train/inner_val/calibration/test/holdout`, размеры выборок, версии библиотек, `git_commit` (или `null` без `.git`), `random_seed`, `<run_id>`, `bootstrap.{N, block_by_day, seed}`, `status`.
- [ ] Симлинк `latest` обновляется **атомарно** (temp + `os.replace` или эквивалент); не остаётся «без latest» при сбое; относительный путь внутри каталога модели.
- [ ] При `status=failed_baseline_check` симлинк `latest` **не** переключается на провальный прогон.
- [ ] Сериализация через `modeling/artifacts.py` (или согласованный минимальный сериализатор с явной пометкой в MR).

---

## 8. Definition of Done / baseline-гейт (этап 12)

- [ ] Один запуск `python -m modeling.cli train --config configs/modeling_default.yaml` идёт от пустых артефактов до финальных моделей + walk-forward отчётов + holdout-отчёта с reliability PNG (или, если этапы 7–9 не готовы, это честно отражено в MR и end-to-end тест помечен `skip`).
- [ ] Holdout-отчёт по каждой задаче: log loss, Brier, ECE **до и после** калибровки, ДИ block bootstrap по дню, строка тривиального baseline, reliability PNG, breakdown по командам.
- [ ] Для обеих задач — отдельные финальные артефакты `.../lgbm/<run_id>/final/` и `.../logreg/<run_id>/final/`; `latest` указывает на актуальный `<run_id>/final/`.
- [ ] base rate тривиального baseline считается из `train_full`/`train_k`, **не** из holdout/test.
- [ ] Если лучшее семейство **не** улучшает log loss относительно `trivial_base_rate` — `summary.md` и `metadata.json` помечают `status: failed_baseline_check`, команда возвращает **ненулевой** код возврата.

---

## 9. Потоки и зависимости

- [ ] `compute.num_threads` пробрасывается в `n_jobs` sklearn, `num_threads` LightGBM, `num_threads` bootstrap. Нет `n_jobs=-1`, `os.cpu_count()`, прямого `OMP_NUM_THREADS`/`MKL_NUM_THREADS`.
- [ ] CLI не добавляет Optuna/CatBoost/stacking/time-decay (это этап 15). Зависимости — только из `requirements-modeling.txt`; `requirements.txt`/`Dockerfile` бота не тронуты.

---

## 10. Тесты, Makefile, документация

- [ ] `tests/test_modeling_cli_smoke.py`: `--dry-run` на синтетическом мини-датасете ≤ 5 c, код 0, печатает размеры всех блоков. Тест не тянет БД/сеть.
- [ ] Тест на `--print-resolved-config` (код 0, ключевые поля в выводе, артефакты не созданы).
- [ ] Тест/проверка изоляции train-пути от БД (мини-AST либо опора на `tests/test_modeling_no_db_access.py`).
- [ ] Тест резолва `<run_id>` (формат + override через `--run-id`).
- [ ] Если этапы 7–9 готовы — mini-end-to-end тест создаёт `fold_<k>/`, `final/{...}`, `latest`, валидный `metadata.json`. Если не готовы — `skip` с причиной (а **не** заглушка обучения в production-пути).
- [ ] `Makefile`: цель `make modeling-train` → корректная команда; существующие цели не сломаны.
- [ ] `docs/modeling_training.md` обновлён: `<run_id>`, схема `artifacts/`, `--dry-run`/`--print-resolved-config`, правило финального переобучения, baseline-гейт и поведение `latest`, пример прогона. Битых ссылок нет.

---

## 11. Вердикт

**approve / approve with nits / request changes.**

**Блокеры:**

- любое участие holdout в обучении/калибровке/выборе гиперпараметров или расчёте base rate;
- финальное переобучение не по правилу `train_full → model_final`, `calibration_final → calibrator_final` (либо holdout попадает в `train_full`);
- импорт `psycopg2` / `modeling.dataset_builder.*` в train-пути CLI; чтение датасета в обход `load_training_table_split`;
- независимые/производные seeds в CLI вместо единого `random_seed`;
- `<run_id>` не в каноническом формате или `features_hash` берётся из YAML вместо `metadata_train.json`;
- неполный `metadata.json` финального артефакта (отсутствуют поля DoD п.4 этапа 12);
- `latest` переключается на прогон со `status=failed_baseline_check`, либо обновление неатомарно и может оставить каталог без `latest`;
- отсутствие baseline-гейта (нет `failed_baseline_check`/ненулевого кода при непобитом baseline);
- `n_jobs=-1`/`os.cpu_count()`/прямой `OMP_NUM_THREADS` вместо `compute.num_threads`;
- CLI переписывает обучение/калибровку/метрики/сплиты вместо вызова этапов 4–9; использование `CalibratedClassifierCV`;
- заглушка обучения (`DummyClassifier` и т.п.) в production-артефакте;
- посторонняя работа из этапа 15 (Optuna/CatBoost/stacking) или изменения `requirements.txt`/`Dockerfile` бота.

**Ниты:**

- разделение `cli.py` vs `train_runner.py` (оба варианта допустимы при тонком CLI-слое);
- формат печати resolved-config (YAML vs JSON);
- формат записи диапазонов дат в `metadata.json` (агрегированно vs по финальному переобучению) — важно лишь, что поля присутствуют и однозначны;
- fallback `latest.txt` на ФС без симлинков;
- стиль сообщений `run.log` и порядок INFO-строк.
