# ТЗ для исполнителя: этап 10 — Единая точка входа CLI и production-артефакт

**Роль:** инженер-исполнитель.  
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 10. Единая точка входа CLI и production-артефакт» **с обязательным учётом** «## Сквозные требования (читать перед каждым этапом)» того же документа.  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Связанные ТЗ темы:**
- [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) — этап 1 (контракт входа). Единственная точка загрузки датасета — `load_training_table_split(...)`.
- [`../stage_2_config/executor_tz.md`](../stage_2_config/executor_tz.md) — этап 2 (конфиг). CLI читает `configs/modeling_default.yaml` через `modeling/config.py`, поддерживает `--set` и `--print-resolved-config`.
- [`../stage_4_splits/executor_tz.md`](../stage_4_splits/executor_tz.md) — этап 4 (сплиты). Walk-forward блоки `train_k/inner_val_k/calibration_k/test_k` и финальный `holdout` приходят из сплиттера.
- [`../stage_5_metrics/executor_tz.md`](../stage_5_metrics/executor_tz.md) — этап 5 (метрики и отчётность): `metrics.json`, `summary.md`, `reliability_<task>.png`, breakdown по командам, тривиальный baseline.
- [`../stage_6_bootstrap/executor_tz.md`](../stage_6_bootstrap/executor_tz.md) — этап 6 (bootstrap-ДИ): block bootstrap по `day` на holdout, i.i.d. на test_k.
- Этапы 7–9 (`modeling/train_logreg.py`, `modeling/train_lgbm.py`, `modeling/calibrate.py`, `modeling/artifacts.py`) — CLI **оркестрирует** уже реализованные обучение и калибровку, **не** дублирует их логику.

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать **единую точку входа** `modeling/cli.py` с командой `train`, которая склеивает уже готовые подсистемы (вход → сплиты → обучение → калибровка → метрики → bootstrap → артефакты → отчёт) в один воспроизводимый прогон. Команда обязана:

1. Загрузить датасет **только** через `load_training_table_split(...)` (этап 1) — никакого собственного чтения CSV/БД.
2. Прогнать **walk-forward** оценку по `n_test_windows` (этап 4) с обучением (этапы 7–8), калибровкой (этап 9), метриками (этап 5) и bootstrap-ДИ (этап 6) на каждом фолде.
3. Выполнить **обязательное финальное переобучение** перед holdout и собрать **production-артефакт**.
4. Записать артефакты прогона по фиксированной схеме каталогов и обновить симлинк `latest`.
5. Поддержать флаги CLI (`--task`, `--model`, `--dry-run`, `--run-id`, `--print-resolved-config`, `--config`, `--set`).
6. Добавить цель `make modeling-train`.

Этап **не реализует заново** обучение, калибровку, метрики, bootstrap и сплиты — он их **вызывает**. Если какой-то из этих модулей ещё не готов — см. §6 (порядок и зависимости).

---

## 2. Сквозные требования (обязательны к применению на этом этапе)

Из раздела «## Сквозные требования (читать перед каждым этапом)» UPDATE-плана для CLI действуют **все шесть** пунктов — этап 10 является их главным потребителем:

- **Источник истины фич — `metadata_train.json`.** CLI **обязан** проверить парити `feature_manifest` / `features_hash` / `feature_set_version` / `rolling_windows` / `cold_start_policy_predict` между YAML-конфигом и `metadata_train.json`. При расхождении — `ConfigError` с **диффом конкретных полей**, прогон не стартует. Если проверку уже делает контракт входа (этап 1) и/или конфиг (этап 2) — CLI её **не дублирует**, а полагается на них и пробрасывает ошибку наверх с понятным сообщением.
- **Воспроизводимость и единый `random_seed`.** CLI читает `random_seed` из YAML и **раздаёт** его во все подсистемы (сплиты, logreg, lgbm, bootstrap). Никаких независимых/производных seeds на стороне CLI. Версии библиотек (`sklearn`, `lightgbm`, `pandas`, `numpy`) логируются в каждый `metadata.json` артефакта — это **ответственность этого этапа** (через `modeling/artifacts.py` этапа 7, если уже есть, иначе см. §3.6).
- **Лимит потоков (`compute.num_threads`).** CLI пробрасывает `compute.num_threads` в `n_jobs` sklearn, `num_threads` LightGBM и в `num_threads` bootstrap. Запрещено любое «использовать все ядра» (`n_jobs=-1`, `os.cpu_count()`, `OMP_NUM_THREADS` напрямую).
- **Логи.** Каждый прогон пишет `artifacts/reports/<run_id>/run.log` (уровень INFO; конкретный уровень — из YAML). Это **главный артефакт логирования этапа**. Лог содержит: resolved-config (без секретов), размеры всех блоков на каждом фолде, прогресс по фолдам, итоговые метрики, путь к артефактам, версии библиотек.
- **`<run_id>` — единый формат**: `<task>_<model>_<features_hash[:8]>_<utc_timestamp_YYYYmmddTHHMMSSZ>`. Генерация `<run_id>` — **ответственность этого этапа**. `--run-id` позволяет переопределить автогенерацию (для воспроизводимых прогонов/тестов). UTC-таймстемп берётся в момент старта прогона.
- **Никакого доступа к PostgreSQL.** `modeling/cli.py` (в части train) **запрещено** импортировать `psycopg2`, `modeling.dataset_builder.*`. Вход — только `dataset_train.csv` + `metadata_train.json` через контракт этапа 1. Проверяется AST-тестом этапа 11; на этом этапе минимум — не вводить таких импортов в train-путь CLI.

> Важно: `modeling/cli.py` может уже содержать команду `build-dataset` (она по природе тянет БД через `modeling.dataset_builder`). Команда `train` обязана быть изолирована так, чтобы **train-путь** не импортировал БД-зависимости на уровне модуля. Решение (ленивые импорты `build-dataset` внутри его обработчика, либо вынос train-логики в отдельный модуль `modeling/train_runner.py`, импортируемый из CLI) — на усмотрение исполнителя, но **зафиксировать в MR** и обеспечить прохождение `tests/test_modeling_no_db_access.py`.

---

## 3. Объём работ

### 3.1 Точка входа и подкоманда `train`

- Расширить `modeling/cli.py` подкомандой `train` (argparse subparsers; если уже есть subparser для `build-dataset` — добавить рядом, не ломая существующее).
- Запуск: `python -m modeling.cli train --config configs/modeling_default.yaml [флаги]`.
- Оркестрация train выносится в тестируемую функцию (например `modeling/train_runner.py::run_training(config, *, task, model, run_id, dry_run) -> RunResult`), чтобы CLI-слой остался тонким (парсинг аргументов → вызов функции → код возврата). Это упрощает smoke-тест этапа 11.

### 3.2 Флаги CLI (обязательный минимум)

- `--config PATH` — путь к YAML (по умолчанию `configs/modeling_default.yaml`).
- `--set key=value` — точечные оверрайды конфига (повторяемый; семантика — из этапа 2; CLI лишь прокидывает в загрузчик конфига).
- `--task {home_win,over_5_5,both}` — какие задачи обучать (по умолчанию `both`, но уважать `tasks.{...}.enabled` из YAML; см. §3.3).
- `--model {logreg,lgbm,both}` — какие семейства моделей обучать (по умолчанию `both`).
- `--run-id RUN_ID` — переопределить автогенерацию `<run_id>` (для воспроизводимых прогонов и тестов).
- `--print-resolved-config` — напечатать итоговый resolved-config и **завершиться без обучения** (код 0).
- `--dry-run` — напечатать resolved-config + **размеры всех блоков** `train/inner_val/calibration/test/holdout` для каждого фолда и финального переобучения, **без обучения**; завершиться кодом 0.

Конфликты/приоритет: явные флаги (`--task`, `--model`) сужают набор относительно YAML; `--task home_win` при `tasks.home_win.enabled=false` — `ConfigError` с понятным сообщением (нельзя обучать выключенную задачу), а не тихий no-op.

### 3.3 Матрица прогонов (task × model)

- Декартово произведение выбранных задач (`home_win`, `over_5_5`) и семейств (`logreg`, `lgbm`).
- Для каждой пары `(task, model)` — отдельный полный прогон walk-forward + финальное переобучение + production-артефакт.
- Все пары одного запуска делят **один** `<utc_timestamp>` (момент старта) — но `<run_id>` per-(task,model) различается по `<task>_<model>_...` префиксу (см. формат). Зафиксировать в коде, чтобы артефакты разных пар не перетирали друг друга.
- Отчёт сравнения logreg vs lgbm на одних сплитах (общая таблица) — формирует этап 5; CLI лишь передаёт результаты обеих моделей в репортер (если этап 5 это поддерживает).

### 3.4 Walk-forward оценка (ядро `train`)

Для каждой пары `(task, model)`:

1. Получить из этапа 4 список outer-блоков `k ∈ 1..n_test_windows`, каждый с непересекающимися `train_k / inner_val_k / calibration_k / test_k`, и общий `holdout`.
2. Для каждого `k`:
   - обучить **сырую** модель на `train_k` (этап 7 для logreg / этап 8 для lgbm), early stopping / выбор гиперпараметров — на `inner_val_k`;
   - обучить **калибратор** на `calibration_k` по предсказаниям сырой модели (этап 9); при `|calibration_k| < calibration.min_samples` — пометить `calibration_skipped=true` и идти с сырой вероятностью (логировать предупреждение);
   - предсказать на `test_k` цепочкой `raw → calibrator`;
   - посчитать метрики (этап 5) и bootstrap-ДИ (этап 6, i.i.d. на test_k);
   - сохранить фолд-артефакты в `artifacts/models/<task>/<model>/<run_id>/fold_<k>/` (содержимое — по контракту этапа 9/7: `model_raw.joblib`, `calibrator.joblib` (или пометка skip), `metadata.json`).
3. Агрегировать метрики по test_k (среднее ± ДИ) для отчёта.

CLI **не реализует** обучение/калибровку/метрики — только вызывает их в корректном порядке на корректных срезах.

### 3.5 Обязательное финальное переобучение перед holdout

Это **критическая** часть этапа (явное требование UPDATE-плана):

- `model_final` — **сырая** модель, обученная на `train_full` = **всё до `calibration_final`** (т.е. вся история минус последний пред-holdout калибровочный блок и минус holdout). Гиперпараметры — те же, что зафиксированы конфигом/выбраны протоколом этапов 7–8 (без подглядывания в holdout).
- `calibration_final` — калибратор, обученный на **`calibration_final`** (последний пред-holdout блок, не пересекается с holdout). Срез `calibration_final` приходит из этапа 4.
- Holdout-отчёт строится **строго цепочкой** `model_final → calibrator_final` (а не усреднением фолдов и не отдельной моделью «на всём»). holdout **ни разу** не участвует в обучении/калибровке/выборе гиперпараметров.
- Метрики на holdout (этап 5) + bootstrap-ДИ **block bootstrap по `day`** (этап 6) + reliability PNG + breakdown по командам + строка `trivial_base_rate` (base rate считается из `train_full`, не из holdout).

### 3.6 Production-артефакт

- Сохранить в `artifacts/models/<task>/<model>/<run_id>/final/`:
  - `model.joblib` — `model_final` (сырая модель);
  - `calibrator.joblib` — `calibrator_final`;
  - `metadata.json` — метаданные (см. ниже).
- `metadata.json` финального артефакта **обязан** содержать (это пункт 4 Definition of Done этапа 12):
  - `features_hash` (из `metadata_train.json`);
  - диапазоны дат `train/inner_val/calibration/test/holdout` (мин/макс `day` по каждому блоку; для walk-forward — агрегированно или по финальному переобучению — зафиксировать формат);
  - размеры выборок (число игр) по каждому блоку;
  - версии библиотек (`sklearn`, `lightgbm`, `pandas`, `numpy`);
  - `git_commit` (если `.git` есть, иначе `null`);
  - `random_seed`;
  - `<run_id>`;
  - `bootstrap.{N, block_by_day, seed}` (из этапа 6);
  - `status` (`ok` | `failed_baseline_check` — см. §3.8).
- Использовать `modeling/artifacts.py` (этап 7) как единый сериализатор joblib + JSON. Если `artifacts.py` ещё нет — см. §6: допустимо реализовать минимальный сериализатор в этом MR строго по контракту метаданных, но это должно быть согласовано.

### 3.7 Симлинк `latest`

- После успешного прогона обновить симлинк `artifacts/models/<task>/<model>/latest` → `<run_id>/final/` (относительный симлинк внутри `artifacts/models/<task>/<model>/`).
- Обновление **атомарно**: создать временный симлинк и `os.replace`, либо удалить-создать с обработкой ошибок (не оставлять каталог без `latest`, если предыдущий существовал). Зафиксировать выбранный подход.
- На ФС без поддержки симлинков (редкий случай) — лог-предупреждение и запись файла-указателя `latest.txt` с относительным путём как fallback (не падать). Решение зафиксировать в docstring.
- При `status=failed_baseline_check` (см. §3.8) — `latest` **не** переключать на провальный прогон (production не должен указывать на модель, не побившую baseline). Зафиксировать это поведение явно.

### 3.8 Definition of Done / baseline-гейт (из этапа 12)

- На holdout `model_final` лучшего семейства **обязана** строго улучшать log loss относительно `trivial_base_rate`. Если **ни одно** обученное семейство для задачи не побило тривиальный baseline — прогон по этой задаче помечается `status: failed_baseline_check` в `summary.md` и в `metadata.json`, а команда возвращает **ненулевой код возврата** (для CI).
- `summary.md` (этап 5) обязан содержать строку статуса; CLI передаёт этап 5 нужный флаг/значение.

### 3.9 Отчётность прогона

- `artifacts/reports/<run_id>/` содержит `metrics.json`, `summary.md`, `reliability_<task>.png`, `run.log` (всё формирует этап 5/6; CLI оркестрирует и пишет `run.log`).
- holdout-отчёт содержит для каждой задачи (DoD п.3 этапа 12): log loss, Brier, ECE **до и после** калибровки, ДИ из block bootstrap по дню, строку тривиального baseline, reliability PNG, breakdown ошибок по командам.

### 3.10 Поведение `--dry-run` и `--print-resolved-config`

- `--print-resolved-config`: вывести итоговый конфиг (после слияния YAML + `--set` + дефолтов) в читаемом виде (YAML/JSON) и выйти кодом 0. Ничего не обучать, артефакты не трогать.
- `--dry-run`: загрузить датасет (этап 1) **только ради расчёта размеров блоков**, построить сплиты (этап 4), напечатать resolved-config + размеры `train/inner_val/calibration/test/holdout` по каждому фолду и для финального переобучения, выйти кодом 0. **Без** обучения, **без** записи артефактов/симлинков. Должен укладываться в ≤ 5 c на синтетическом мини-датасете (smoke-тест этапа 11).

### 3.11 `Makefile`

- Добавить цель:
  - `make modeling-train` → `python -m modeling.cli train --config configs/modeling_default.yaml`.
- Не ломать существующие цели (`modeling-dev`, `modeling-build-dataset` и т.п., если есть).

---

## 4. Тесты

Этап 11 централизует тесты, но **smoke-тест CLI** — ответственность этого этапа (он же фигурирует в этапе 11 как `tests/test_modeling_cli_smoke.py`). Минимум:

1. **`tests/test_modeling_cli_smoke.py`:** на синтетическом мини-датасете (`dataset_train.csv` + `metadata_train.json` во временной директории) `python -m modeling.cli train --dry-run --config <tmp>` отрабатывает за **≤ 5 c**, код возврата 0, в stdout/`run.log` напечатаны размеры **всех** блоков `train/inner_val/calibration/test/holdout`.
2. **`--print-resolved-config`:** завершение кодом 0, в выводе присутствуют ключевые поля (`random_seed`, `compute.num_threads`, `split.*`), обучение не запускалось (артефакты не созданы).
3. **Изоляция от БД:** train-путь CLI/`train_runner` не импортирует `psycopg2`, `modeling.dataset_builder.*` (мини-AST-проверка; полный охват — `tests/test_modeling_no_db_access.py` этапа 11). Если выбран вариант с ленивыми импортами `build-dataset` — тест подтверждает, что импорт модуля CLI не тянет БД-зависимости.
4. **Генерация `<run_id>`:** формат `<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>`; `--run-id` переопределяет автогенерацию (юнит на функцию-генератор/резолвер).
5. **Конфликт флага и YAML:** `--task home_win` при `tasks.home_win.enabled=false` → `ConfigError`/ненулевой код, понятное сообщение.
6. **(Если этапы 7–9 готовы) мини-end-to-end:** на крошечном синтетическом датасете (с `n_test_windows` минимально допустимым) реальный прогон `train` создаёт `fold_<k>/`, `final/{model.joblib, calibrator.joblib, metadata.json}` и симлинк `latest`; `metadata.json` содержит обязательные поля §3.6. Если этапы 7–9 ещё не готовы — этот тест помечается `skip` с причиной и заводится TODO; **не** мокать обучение «лишь бы прошло».

Тесты не должны тянуть PostgreSQL и сеть.

---

## 5. Интеграция с этапами (контракты, которые CLI потребляет)

- **Этап 1:** `load_training_table_split(...)` → `(X, keys, labels, service, metadata)`. Никакого иного входа.
- **Этап 2:** `modeling/config.py` — загрузка/слияние/валидация конфига, `--set`, `--print-resolved-config`, парити с `metadata_train.json`.
- **Этап 4:** сплиттер отдаёт outer-блоки и `holdout` + срезы `train_full` / `calibration_final` для финального переобучения. Если в API сплиттера нет явных `train_full`/`calibration_final` — согласовать с исполнителем этапа 4 или вычислить их в CLI **строго** по определению §3.5 (всё до `calibration_final`; `calibration_final` — последний пред-holdout блок) и зафиксировать в MR.
- **Этапы 7–8:** обучение logreg/lgbm на `train_k` (и `train_full`), выбор по `inner_val`. Гиперпараметры/сетки — из YAML.
- **Этап 9:** калибратор на `calibration_k`/`calibration_final`; правило `calibration_skipped`.
- **Этап 5:** метрики, `summary.md`, `metrics.json`, reliability PNG, breakdown, тривиальный baseline, статус прогона.
- **Этап 6:** bootstrap-ДИ (i.i.d. для test_k, block-by-day для holdout) с `seed=random_seed`, `num_threads=compute.num_threads`.
- **Этап 7 (`artifacts.py`):** сериализация joblib + JSON-метаданных.

---

## 6. Порядок и зависимости (что делать, если соседние этапы не готовы)

Этап 10 — **интеграционный** и по логике идёт последним среди 1–10. Возможны два режима работы:

1. **Этапы 1–9 готовы** — реализовать полную оркестрацию и mini-end-to-end тест.
2. **Часть этапов (7–9, 5) не готова** — реализовать:
   - каркас CLI (`train` subparser, парсинг всех флагов, коды возврата);
   - генерацию/резолв `<run_id>`;
   - `--print-resolved-config` и `--dry-run` (для них достаточно этапов 1, 2, 4);
   - запись `run.log`, создание схемы каталогов, атомарный симлинк `latest`;
   - точки вызова обучения/калибровки/метрик/bootstrap — за **четко определёнными интерфейсами** (адаптеры), чтобы при готовности этапов 7–9 подключение было тривиальным;
   - mini-end-to-end тест помечается `skip` с причиной.

   Минимальные «заглушки» обучения **запрещены** в production-пути (никаких `DummyClassifier` в финальном артефакте). Допустим только пропуск (`skip`) end-to-end теста.

В описании MR явно указать, в каком режиме сделана работа и что осталось подключить.

---

## 7. Запреты и вне скоупа

- **Не** переписывать обучение, калибровку, метрики, bootstrap, сплиты — только вызывать (этапы 4–9).
- **Не** использовать `sklearn.calibration.CalibratedClassifierCV` (запрещён проектом, этап 9).
- **Не** обучать/калибровать/выбирать гиперпараметры на `holdout` ни в каком виде.
- **Не** использовать `n_jobs=-1`, `os.cpu_count()`, `OMP_NUM_THREADS`/`MKL_NUM_THREADS` напрямую — только `compute.num_threads`.
- **Не** генерировать независимые/производные seeds — только `random_seed` из YAML, раздаётся вниз.
- **Не** импортировать `psycopg2`, `modeling.dataset_builder.*` в train-пути CLI.
- **Не** читать датасет иначе, чем через `load_training_table_split` (этап 1).
- **Не** менять `requirements.txt` / `Dockerfile` бота; модельные зависимости — только `requirements-modeling.txt` (этап 3).
- **Не** добавлять Optuna, CatBoost, stacking, time-decay weights — это этап 15.

---

## 8. Что отдать в MR

- `modeling/cli.py` (расширение командой `train`) и, при выбранном разделении, `modeling/train_runner.py`.
- При отсутствии `modeling/artifacts.py` — минимальный сериализатор (с явной пометкой в MR и согласованием с этапом 7).
- `tests/test_modeling_cli_smoke.py` (+ мини-AST-проверка изоляции от БД, если ещё нет).
- Цель `make modeling-train` в `Makefile`.
- Раздел в `docs/modeling_training.md`: формат `<run_id>`, схема каталога `artifacts/models/...` и `artifacts/reports/...`, описание `--dry-run`/`--print-resolved-config`, правило финального переобучения (`train_full` → `model_final`, `calibration_final` → `calibrator_final`), baseline-гейт и поведение `latest` при `failed_baseline_check`, пример полного прогона.
- В описании MR явно: «реализован этап 10 UPDATE-плана»; в каком режиме (§6); как обеспечена изоляция train-пути от БД; как реализован атомарный симлинк `latest`.
