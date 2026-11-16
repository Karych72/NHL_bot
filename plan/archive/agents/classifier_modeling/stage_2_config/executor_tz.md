# ТЗ для исполнителя: этап 2 — конфигурация обучения

**Роль:** инженер-исполнитель.
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 2. Конфигурация обучения». Сквозные требования — раздел «## Сквозные требования (читать перед каждым этапом)» того же документа: их соблюдение **обязательно**.
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).
**Предыдущий этап (контракт входа):** [`../stage_1_train_input/executor_tz.md`](../stage_1_train_input/executor_tz.md) — закрыт, переиспользовать `modeling/train_input.py` без изменений.

Пути в документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Зафиксировать **единый, машиночитаемый и валидируемый** конфиг обучения: один YAML-файл по умолчанию + типизированная модель в коде. Все последующие этапы (сплиты, метрики, calibrate, train_logreg, train_lgbm, CLI `train`) обязаны получать параметры **только** через эту структуру, без чтения YAML напрямую и без «магических» констант в модулях обучения.

Конфиг отвечает за:

- воспроизводимость прогона (`random_seed`, лимиты потоков, версии библиотек);
- состав задач (`home_win`, `over_5_5`) и моделей (`logreg`, `lgbm`);
- параметры сплитов walk-forward и финального holdout;
- сетки гиперпараметров, монотонные ограничения LGBM (как **справочное** место хранения; реальные имена фич приходят из манифеста);
- параметры калибровки и метрик/bootstrap;
- разрешение конфликтов с `metadata_train.json` по принципу **fail-fast**.

---

## 2. Объём работ (делать только этап 2)

### 2.1 Файлы и расположение

- `modeling/config.py` — типизированная модель конфига (`pydantic` v2 предпочтительнее; допустим `dataclass`-набор с ручной валидацией, если `pydantic` ещё не в `requirements-modeling.txt` — тогда **добавить его в этап 3** отдельной строкой и в этом ТЗ оставить TODO-комментарий с импортом-заглушкой).
- `configs/modeling_default.yaml` — единственный дефолтный конфиг v1. Содержит **все** обязательные поля из раздела 2.2 ниже, с разумными значениями.
- Документация: короткий подраздел «Конфиг обучения» в `docs/modeling_dataset_builder.md` **или** новый `docs/modeling_training.md` (последнее предпочтительнее — он всё равно нужен по этапу 13). Минимум: ссылка на `configs/modeling_default.yaml`, описание правила приоритета YAML ↔ `metadata_train.json`, описание CLI-флагов из 2.5.

### 2.2 Обязательные поля YAML

Зафиксировать в коде проверку наличия и типа **минимум** этих ключей. Лишние ключи (не описанные в модели) — **ошибка** (`extra = "forbid"` в pydantic / явная проверка), чтобы опечатка не превращалась в «тихий дефолт».

```
random_seed: int                     # единый источник всех seed подсистем (этапы 6, 7, 8)

compute:
  num_threads: int                   # >= 1; пробрасывается в sklearn n_jobs и lightgbm num_threads
  log_level: str                     # "INFO" по умолчанию, валидировать против logging-уровней

tasks:
  home_win:
    enabled: bool
  over_5_5:
    enabled: bool                    # хотя бы одна задача должна быть enabled=true

split:
  method: str                        # "month" | "fixed_games" (валидируемый enum)
  n_test_windows: int                # >= 5 (см. этап 4)
  inner_val_games: int               # >= 300
  calibration_games: int             # >= 300
  holdout:                           # ровно один из двух способов:
    fraction: float | null           # 0 < x < 0.5, при null используется date_range
    date_range:                      # at-least-one — fraction ИЛИ date_range
      from: str | null               # ISO-дата
      to: str | null

models:
  logreg:
    grids:
      C: list[float]                 # непустой, все > 0
  lgbm:
    grids:                           # все списки непустые, типы согласованы
      num_leaves: list[int]
      min_data_in_leaf: list[int]
      feature_fraction: list[float]
      bagging_fraction: list[float]
      lambda_l1: list[float]
      lambda_l2: list[float]
      learning_rate: list[float]
    monotone:
      home_win: dict[str, int]       # имя_фичи -> {-1, 0, +1}
      over_5_5: dict[str, int]       # см. раздел 2.4

calibration:
  method: str                        # "isotonic" | "platt"
  min_samples: int                   # >= 1, рекомендованный дефолт 500

evaluation:
  ece_bins: int                      # >= 2, дефолт 10
  bootstrap_samples: int             # >= 1, дефолт 1000
  bootstrap_block_by_day: bool       # true для holdout (этап 6)
  epsilon_clip: float                # 0 < eps < 1e-3, дефолт 1e-15
```

Допустимо иметь в YAML **справочные** копии `feature_set_version`, `rolling_windows`, `features_hash`, но они **не используются** в коде как источник истины — только сравниваются с `metadata_train.json` (см. 2.3).

### 2.3 Правило приоритета YAML ↔ `metadata_train.json`

- Источник истины для `feature_set_version`, `rolling_windows`, `features_hash`, `feature_manifest`, `cold_start_policy_predict` — **`metadata_train.json`** (поля из загрузчика `modeling/train_input.py`).
- Если соответствующее поле присутствует в YAML и **отличается** от метаданных — поднять **`ConfigError`** (новый класс в `modeling/config.py`) с **полным диффом по полям** в сообщении (поле, значение_yaml, значение_metadata). Никаких «тихих» оверрайдов.
- Если поле есть только в метаданных — использовать значение из метаданных и записать его в **итоговый resolved-конфиг** (см. 2.5, флаг `--print-resolved-config`).
- Резолв должен происходить в одной публичной функции вроде `resolve_config(yaml_path, metadata, overrides) -> ResolvedConfig`, чтобы её можно было вызывать и из CLI, и из тестов без побочных эффектов.

### 2.4 Связь с другими сквозными требованиями

- `random_seed` — **единственный** источник всех seed-параметров подсистем (`numpy`, `sklearn`, lightgbm: `random_state`, `feature_fraction_seed`, `bagging_seed`, `data_random_seed`, bootstrap seed). В этом этапе зафиксировать только сам ключ и его тип; деривация seed выполняется в этапах 6/7/8, но в `config.py` уместно добавить хелпер `derive_seed(name: str) -> int` (детерминированный, например `hash(name)` смешанный с `random_seed`) — используется потребителями позже.
- `compute.num_threads` хранится один раз; помощников/проброса в sklearn/lightgbm в этом этапе писать **не нужно** — достаточно гарантировать, что значение доступно через `ResolvedConfig.compute.num_threads`.
- `models.lgbm.monotone` — это **справочная** структура: реальные имена фич приходят из манифеста датасета. В этапе 2 валидировать только, что:
  - значения знаков ∈ {-1, 0, +1};
  - имена-ключи являются строками;
  - **никакой** сверки с манифестом тут нет — это работа этапа 8 (там же — fail-fast, если имя не найдено в манифесте).
- `<run_id>` — в этом этапе **не генерировать**, но завести в `ResolvedConfig` пустое поле `run_id: str | None = None` и хелпер `build_run_id(task: str, model: str, features_hash: str, now_utc: datetime) -> str`, возвращающий строку формата `<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>`. Использовать его будут CLI/train модули.
- Логи: добавить вспомогательную функцию `configure_run_logger(run_id, log_level, reports_root="artifacts/reports") -> Path`, которая создаёт `artifacts/reports/<run_id>/run.log` и настраивает root-logger. В этом этапе достаточно функции и её юнит-теста; интегрировать её будет CLI на этапе 10.
- Запрет PG: `modeling/config.py` **не должен** импортировать `psycopg2`, `modeling.dataset_builder.*` (кроме типов из `schema.py` при необходимости — но лучше вообще не импортировать, если это тянет цепочки). Если используется только `feature_manifest`, его достаточно типизировать как `list[dict]` локально.

### 2.5 CLI

Расширить `modeling/cli.py` подкомандой **`train`** (полную реализацию train оставить на этап 10; в рамках этапа 2 добавить только парсинг конфига и три флага):

- `--config <path.yaml>` — обязательный;
- `--set key=value` — множественный, точечные оверрайды через dotted-path (например `--set compute.num_threads=4 --set models.lgbm.grids.learning_rate=[0.05,0.1]`). Значения парсить как YAML-литералы (`yaml.safe_load(value)`), чтобы поддерживать списки/числа без шелл-кавычек на каждый чих;
- `--print-resolved-config` — печатает итоговый YAML-документ resolved-конфига (после YAML + overrides + сверки с метаданными) **в stdout** и завершает процесс с `exit code 0` без обучения. Метадату для сверки в этом флаге читать **из того же пути, что и будущий train**: `--metadata path/to/metadata_train.json` (если флаг не передан — допустим путь по умолчанию `artifacts/datasets/metadata_train.json`).

Команда `train` без `--print-resolved-config` в рамках этапа 2 может печатать `NotImplementedError("stage 10")` или заглушку с понятным сообщением — это **ожидаемо** и не блокирует приёмку.

### 2.6 Тесты

`tests/test_modeling_config.py`:

- успех на синтетическом минимальном YAML — все обязательные поля парсятся, типы корректные;
- ошибка на каждое из:
  - отсутствие обязательного ключа (точечный сценарий для одного-двух ключей разных уровней — `random_seed`, `split.n_test_windows`, `evaluation.epsilon_clip`);
  - лишний ключ верхнего уровня (`extra = "forbid"`);
  - неверный тип (`random_seed: "abc"`);
  - `n_test_windows < 5`;
  - `inner_val_games < 300`, `calibration_games < 300`;
  - `tasks.home_win.enabled=false` И `tasks.over_5_5.enabled=false` одновременно;
  - неверный `split.method` / `calibration.method`;
  - `monotone` со значением вне `{-1, 0, +1}`;
- сверка с метаданными:
  - `features_hash` в YAML отличается от `metadata_train.json` — `ConfigError` с диффом;
  - `feature_set_version` отличается — `ConfigError`;
  - поле отсутствует в YAML, есть в метаданных — берётся из метаданных, попадает в resolved;
- оверрайды:
  - `--set compute.num_threads=8` меняет значение;
  - `--set models.lgbm.grids.learning_rate=[0.05,0.1]` корректно парсится как список;
- `--print-resolved-config` — печатает валидный YAML, парсится обратно в ту же структуру; код возврата 0;
- хелперы:
  - `build_run_id(...)` даёт строку строго формата `<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>` (regex-проверка), не зависит от локали;
  - `configure_run_logger(...)` создаёт каталог и файл `artifacts/reports/<run_id>/run.log`, пишет хотя бы одну INFO-строку через root-logger.

`tests/test_modeling_no_db_access.py` уже планируется в этапе 11 — **расширять его в рамках этапа 2 не нужно**, но `modeling/config.py` обязан проходить тот же запрет: не импортировать `psycopg2`, `modeling.dataset_builder.*`. Это можно проверить локальным мини-тестом на AST в `tests/test_modeling_config.py` или оставить полную проверку на этап 11 — на усмотрение, главное чтобы импорта не было.

---

## 3. Критерии приёмки

1. `modeling/config.py` существует, экспортирует типизированную модель (`ResolvedConfig` или эквивалент), функцию `load_config(yaml_path, overrides, metadata) -> ResolvedConfig`, класс `ConfigError`, хелперы `derive_seed`, `build_run_id`, `configure_run_logger`.
2. `configs/modeling_default.yaml` валиден этой моделью «как есть», содержит все обязательные поля из 2.2 с осмысленными значениями (включая `n_test_windows >= 5`, `inner_val_games >= 300`, `calibration_games >= 300`, `evaluation.bootstrap_samples = 1000`, `evaluation.epsilon_clip = 1e-15`, `evaluation.ece_bins = 10`, `calibration.min_samples = 500`, `calibration.method = "isotonic"`).
3. CLI поддерживает `--config`, повторяемый `--set key=value`, `--print-resolved-config`; последний завершает процесс кодом 0 без обучения. `--metadata` принят и используется в сверке.
4. Сверка YAML с `metadata_train.json` по `feature_set_version`, `rolling_windows`, `features_hash`, `feature_manifest`, `cold_start_policy_predict` работает fail-fast и выдаёт читаемый дифф.
5. Все тесты из 2.6 проходят на чистом репозитории; ни один тест не требует подключения к PostgreSQL.
6. Документация (`docs/modeling_training.md` или подраздел в `docs/modeling_dataset_builder.md`) объясняет: какие поля обязательны, как работает приоритет YAML ↔ метаданные, как пользоваться CLI-флагами, формат `<run_id>` и где лежит `run.log`.
7. `modeling/config.py` не импортирует `psycopg2`, `modeling.dataset_builder.*` (за исключением, при необходимости, только лёгких типов — но желательно вовсе без этого импорта).

---

## 4. Ограничения и вне скоупа

- Не реализовывать сплиты, метрики, обучение, калибровку, полноценный `train` CLI — это этапы 4–10.
- Не пробрасывать `compute.num_threads` в sklearn/lightgbm здесь — только хранение и доступ через resolved-конфиг.
- Не сверять `models.lgbm.monotone` с манифестом — это этап 8.
- Не добавлять Optuna/CatBoost/новые модели — это этап 15.
- Если для типизации потребуется добавить `pydantic` в зависимости — это правка `requirements-modeling.txt` из этапа 3; согласовать в том же MR коротким примечанием и не тащить туда других зависимостей.
