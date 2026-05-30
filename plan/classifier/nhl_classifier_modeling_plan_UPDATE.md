# UPDATE: план доработки прематч-классификаторов NHL

**Статус документа:** дополнение к [nhl_classifier_modeling_plan.md](nhl_classifier_modeling_plan.md).
**Назначение:** закрыть всё, что в базовом плане ещё не сведено к коду, в **одном упорядоченном** списке этапов.

**Уже реализовано (не дублировать работу):**

- Сборка датасета train/predict из PostgreSQL: `modeling/dataset_builder/`, CLI `python -m modeling.cli build-dataset`.
- Метки `y_home_win`, `y_over_5_5`, rolling-фичи с `shift(1)`, as-of по дате, контекст расписания, `features_hash`, манифест, валидация и тесты: см. [docs/modeling_dataset_builder.md](../../docs/modeling_dataset_builder.md).
- **Контракт загрузки train-датасета** — `modeling/train_input.py`: `load_training_table`, `split_training_frame`, `load_training_table_split` (fail-fast на лишние колонки, парити по `feature_manifest`, перепроверка `features_hash`). Тесты — `tests/test_modeling_train_input.py`. **Этап 1 ниже использует эти функции; новый загрузчик не пишется.**

Ниже — **оставшийся** путь до выполнения §5–§8, §12 и чеклиста §15 базового плана.

---

## Сквозные требования (читать перед каждым этапом)

- **Источник истины фич**: `metadata_train.json` (поля `feature_manifest`, `features_hash`, `feature_set_version`, `rolling_windows`, `cold_start_policy_predict`). YAML — только human-readable справка; при расхождении с метадатой — `ConfigError` с диффом полей.
- **Воспроизводимость**: единый `random_seed` из YAML. **Все** seed-параметры подсистем выводятся из него (см. этапы 6, 8). Версии библиотек (`sklearn`, `lightgbm`, `pandas`, `numpy`) логируются в `metadata.json` артефакта.
- **Лимит потоков**: `compute.num_threads` (YAML), пробрасывается в `n_jobs` sklearn и `num_threads` LightGBM. Без этого walk-forward × сетка × bootstrap легко выжигают CPU.
- **Логи**: каждый прогон пишет `artifacts/reports/<run_id>/run.log` (INFO; уровень из YAML).
- **`<run_id>` — единый формат**: `<task>_<model>_<features_hash[:8]>_<utc_timestamp_YYYYmmddTHHMMSSZ>`. Все артефакты и отчёты прогона лежат под этим id.
- **Никакого доступа к PostgreSQL** в train-коде: вход — только `dataset_train.csv` + `metadata_train.json`. Это правило проверяется тестом (запрет импорта `psycopg2`, `modeling.dataset_builder.*` в `modeling/train_*.py`, `modeling/calibrate.py`, `modeling/splits.py`, `modeling/metrics.py`).

---

## Этапы (порядок логического выполнения)

### 1. Использование уже готового контракта входа

- `modeling/train_input.py` уже даёт `(X, keys, labels, service, metadata)` строго по `feature_manifest`. Новый загрузчик не писать.
- Во всех новых train-скриптах **единственный** вход — `load_training_table_split(...)`.
- В `docs/modeling_dataset_builder.md` дописать короткий пример загрузки train-датасета двумя строками — этап 1 на этом закрыт.

### 2. Конфигурация обучения

- `modeling/config.py` (dataclass / Pydantic) + `configs/modeling_default.yaml`.
- Обязательные поля YAML (минимум):
  - `random_seed`;
  - `compute.num_threads`;
  - `tasks.{home_win,over_5_5}.enabled`;
  - `split.{method, n_test_windows, inner_val_games, calibration_games, holdout_fraction_or_date_range}`;
  - `models.{logreg,lgbm}.grids`;
  - `models.lgbm.monotone` (см. этап 8);
  - `calibration.{method, min_samples}`;
  - `evaluation.{ece_bins, bootstrap_samples, bootstrap_block_by_day, epsilon_clip}`.
- **Правило приоритета**: `feature_set_version`, `rolling_windows`, `features_hash` берутся из `metadata_train.json`. YAML-копии этих полей допустимы как справка, расхождение — fail-fast.
- CLI: `--config path.yaml`, `--set key=value` для точечных оверрайдов, `--print-resolved-config` (печатает итоговый конфиг и завершается без обучения).

### 3. Зависимости для моделирования

- `requirements-modeling.txt`: `numpy`, `pandas` (уже в проекте), `scikit-learn`, `pyyaml`, `joblib`, **`lightgbm`** (единственный primary в v1), `matplotlib` (reliability PNG).
- **CatBoost в v1 не добавлять** — переезжает в этап 15.
- `Makefile`: цель `make modeling-dev` → `$(PIP) install -r requirements-modeling.txt`. Эти зависимости не попадают в `requirements.txt` и в Docker-образ бота.

### 4. Временные сплиты без утечки

- `modeling/splits.py`:
  - стабильная сортировка по `(day, game_id)`;
  - **walk-forward**: внешние блоки по календарному месяцу (или по фиксированному числу игр), метод из YAML;
  - `n_test_windows` усредняющих окон — **минимум 5** (зафиксировать в YAML), иначе bootstrap-ДИ не информативны;
  - внутри каждого внешнего train-блока — последний хвост размером `split.inner_val_games` (≥ 300 игр) как **inner_val** для early stopping и выбора гиперпараметров;
  - следом — **calibration** блок `split.calibration_games` (≥ 300 игр), не пересекающийся с train/val/test/holdout;
  - **финальный holdout**: последние 15–20% времени или явный диапазон дат из конфига; ни разу не в train/val/cal walk-forward.
- **Embargo не используется**: rolling-фичи построены с `shift(1)` и не используют признаки «текущего» матча — утечка через границу невозможна. Зафиксировать решение комментарием в `splits.py`.
- Временная раскладка одного outer-блока (`k ∈ 1..n_test_windows`):

  ```
  [ train_k ... | inner_val_k | calibration_k | test_k ]    ...    [ holdout ]
  ```

  `inner_val_k`, `calibration_k`, `test_k` — три **последовательных** непересекающихся блока сразу после `train_k`; `holdout` — после всех `test_k`.
- Тест запрещает `KFold(shuffle=True)`, `StratifiedKFold(shuffle=True)`, `ShuffleSplit` по матчам в `modeling/` (grep + AST, см. этап 11).

### 5. Метрики и отчётность

- `modeling/metrics.py`:
  - **log loss**: клип `p ∈ [ε, 1−ε]`, `ε = evaluation.epsilon_clip` (по умолчанию `1e-15`);
  - **Brier**;
  - **ECE**: фиксированные 10 бинов в `[0, 1]` равной ширины; параметр `evaluation.ece_bins` для override;
  - reliability table (бин → mean p, доля y=1, вес бина);
  - **breakdown по командам** (часть §8.3 базового плана): средний log loss по `home_team_id` и по `away_team_id` — выявляет переобучение под редкие команды;
  - **тривиальный baseline**: для каждого test_k и holdout — log loss/Brier предиктора-константы, равного base rate `y_home_win`/`y_over_5_5` на `train_k`. Строка `trivial_base_rate` в таблице метрик — защита от «модель не лучше константы».
- Отчёт: `artifacts/reports/<run_id>/{metrics.json, summary.md, reliability_<task>.png, run.log}`.

### 6. Bootstrap доверительные интервалы

- На каждом **test_k** и на **holdout**: resampling с возвратом, `N = evaluation.bootstrap_samples` (по умолчанию 1000), 95% ДИ для log loss и Brier.
- **Holdout** — **block bootstrap по `day`** (ресемплятся целые игровые дни), `evaluation.bootstrap_block_by_day=true`. Уважает временную зависимость и не сжимает ДИ. Для test_k — i.i.d. resampling матчей допустим (окна короткие).
- Seed: `bootstrap_seed = random_seed`; независимые seeds запрещены.
- В метадате прогона: `bootstrap.N`, `bootstrap.block_by_day`, `bootstrap.seed`.

### 7. Baseline: логистическая регрессия (две модели)

- `modeling/train_logreg.py`:
  - отдельные пайплайны для `home_win` и `over_5_5`, общий код через утилиты;
  - `sklearn.Pipeline`: `SimpleImputer(strategy='median') → StandardScaler → LogisticRegression(penalty='l2', solver='lbfgs', max_iter=5000)`;
  - **fit pipeline только на `train_k`**, transform на `inner_val_k`/`calibration_k`/`test_k`/`holdout`;
  - сетка `C` из YAML, выбор по log loss на `inner_val_k`;
  - `class_weight=None` по умолчанию (политика §1.1 базового плана);
  - **`home_team_id`/`away_team_id` запрещены** в v1 logreg (§3.7 базового плана) — assert на список колонок X.
- Сохранение через `modeling/artifacts.py` — joblib + JSON метаданные (`features_hash`, диапазон дат train/val/cal, число строк, версии библиотек, `git_commit` если `.git` есть, иначе `null`).

### 8. Primary: LightGBM (без альтернатив в v1)

- `modeling/train_lgbm.py`:
  - `objective='binary'`, `metric='binary_logloss'`, early stopping по `inner_val_k`;
  - **фиксированная сетка** из YAML: `num_leaves`, `min_data_in_leaf`, `feature_fraction`, `bagging_fraction`, `lambda_l1`, `lambda_l2`, `learning_rate`. Optuna в v1 **не используется** (переезжает в этап 15);
  - **детерминизм**: `random_state`, `feature_fraction_seed`, `bagging_seed`, `data_random_seed`, `deterministic=True` — все из `random_seed`; `num_threads = compute.num_threads`;
  - **монотонные ограничения** (`monotone_constraints`) на физически интерпретируемые фичи — улучшают калибровку и обобщение:
    - `home_win`: положительный знак на `diff_goal_diff_roll_mean_*`, `diff_gf_roll_mean_*`; отрицательный — на `diff_ga_roll_mean_*`;
    - `over_5_5`: положительный знак на `sum_gf_roll_mean_*`, `sum_ga_roll_mean_*`;
  - точные имена/знаки берутся из манифеста после сборки датасета и фиксируются в YAML (`models.lgbm.monotone.{home_win,over_5_5}`).
- Две независимые модели; сравнение с logreg на тех же сплитах — общая таблица в отчёте.

### 9. Калибровка по протоколу без утечки

- `modeling/calibrate.py`:
  - сырой классификатор обучен только на `train_k`;
  - калибратор обучается на `calibration_k` (отдельный временной блок, см. этап 4) по предсказаниям сырой модели и истинным y;
  - метод из YAML (`isotonic` | `platt`); при `|calibration_k| < calibration.min_samples` (по умолчанию 500) фолд помечается `calibration_skipped=true`, в отчёт идёт сырая вероятность с явной пометкой;
  - итоговая оценка на test_k и holdout — цепочка raw → calibrator.
- **`sklearn.calibration.CalibratedClassifierCV` запрещён в проекте**: его внутренний CV не уважает временной порядок, нет публичного API подсунуть `TimeSeriesSplit` корректно для `method='isotonic'`. Использовать **только** явный двухшаговый пайплайн.
- Артефакт: сохранять обе компоненты (`model_raw.joblib`, `calibrator.joblib`) + метаданные срезов.

### 10. Единая точка входа CLI и production-артефакт

- `modeling/cli.py`, команда `train`:
  - загрузка датасета через `load_training_table_split`;
  - walk-forward оценка по `n_test_windows`;
  - **обязательное** финальное переобучение перед holdout:
    - `model_final` — сырая модель на `train_full` = всё до `calibration_final`;
    - `calibrator_final` — на `calibration_final` (последний пред-holdout блок);
    - holdout-отчёт строится цепочкой `model_final → calibrator_final`;
  - артефакты прогона:
    - фолды walk-forward: `artifacts/models/<task>/<model>/<run_id>/fold_<k>/`;
    - **production**: `artifacts/models/<task>/<model>/<run_id>/final/{model.joblib, calibrator.joblib, metadata.json}`;
    - симлинк `artifacts/models/<task>/<model>/latest` → `<run_id>/final/` — точка подгрузки для бота (фаза 2).
  - флаги: `--task {home_win,over_5_5,both}`, `--model {logreg,lgbm,both}`, `--dry-run` (печатает resolved config + размеры всех блоков train/val/cal/test/holdout, без обучения), `--run-id` (override автогенерации), `--print-resolved-config`.
- `Makefile`: `make modeling-train` → `python -m modeling.cli train --config configs/modeling_default.yaml`.

### 11. Тесты

- `tests/test_modeling_splits.py`:
  - монотонность по времени для каждого outer-блока: `max(day(train_k)) < min(day(inner_val_k)) < min(day(calibration_k)) < min(day(test_k)) < min(day(holdout))`;
  - отсутствие пересечения holdout с train/val/cal по `game_id`;
  - `|inner_val_k| ≥ split.inner_val_games`, `|calibration_k| ≥ split.calibration_games`.
- `tests/test_modeling_metrics.py`: эталонные logloss/Brier/ECE на ручных мини-векторах; ECE на идеально откалиброванном предикторе ≈ 0; тривиальный baseline считается из train base rate, а не test.
- `tests/test_modeling_calibration.py`: на синтетических вероятностях — calibrator обучается только на `calibration_k`, не видит test/holdout (мок срезов); проверка пути `calibration_skipped=true` при малом срезе.
- `tests/test_modeling_no_db_access.py`: AST-проверка, что `modeling/train_*.py`, `modeling/calibrate.py`, `modeling/splits.py`, `modeling/metrics.py` не импортируют `psycopg2`, `modeling.dataset_builder.*`.
- `tests/test_modeling_no_shuffle_cv.py`: AST-запрет `KFold(shuffle=True)`, `StratifiedKFold(shuffle=True)`, `ShuffleSplit` в `modeling/`.
- `tests/test_modeling_cli_smoke.py`: на синтетическом мини-датасете `python -m modeling.cli train --dry-run --config ...` отрабатывает за ≤ 5 c и печатает все размеры блоков.
- `tests/test_modeling_lgbm_monotone.py`: на синтетическом датасете с заведомо монотонной зависимостью — обученный LGBM не нарушает знак `monotone_constraints` на тестовом скане признака.

### 12. Диагностика и критерии приёмки (Definition of Done)

Команда для проверки: `python -m modeling.cli train --config configs/modeling_default.yaml`.

1. Команда выше: от пустых артефактов до обученных финальных моделей + walk-forward отчётов + holdout-отчёта с reliability PNG.
2. Автотесты этапа 11 проходят целиком.
3. Holdout-отчёт содержит для каждой задачи: log loss, Brier, ECE до и после калибровки, ДИ из block bootstrap по дню, строку тривиального baseline, reliability PNG, breakdown ошибок по командам.
4. `metadata.json` рядом с каждым артефактом: `features_hash`, диапазон дат train/val/cal/test/holdout, размеры выборок, версии библиотек, `git_commit` (или `null` при отсутствии `.git`), `random_seed`, `<run_id>`.
5. Для обеих задач — отдельные финальные артефакты `artifacts/models/<task>/lgbm/<run_id>/final/` и `artifacts/models/<task>/logreg/<run_id>/final/`; симлинк `latest` указывает на актуальный `<run_id>/final/`.
6. На holdout `model_final` лучшего семейства **обязана** строго улучшать log loss относительно `trivial_base_rate`; иначе прогон считается неуспешным и `summary.md` помечает его `status: failed_baseline_check`.

### 13. Синхронизация документации и ТЗ

- Обновить строку статуса в [nhl_classifier_modeling_plan.md](nhl_classifier_modeling_plan.md): датасет-билдер + контракт загрузки реализованы; обучение — по этому UPDATE.
- Создать `docs/modeling_training.md` со ссылкой на `configs/modeling_default.yaml`, описанием `<run_id>`, схемой каталога `artifacts/`, и примером полного прогона.
- Обновить ТЗ исполнителя в [`agents/classifier_modeling/`](../agents/classifier_modeling/) (по одной паре `executor_tz.md` / `reviewer_tz.md` на этап UPDATE-плана) — каждое должно ссылаться на этот UPDATE.

### 14. Фаза 2 (интеграция в бота) — после стабилизации модели

- Бот загружает `artifacts/models/<task>/<model>/latest/{model.joblib, calibrator.joblib, metadata.json}`; **сверяет** `features_hash` с метадатой текущего predict-датасета — при расхождении отказывается выдавать прогноз.
- Предсказание строится цепочкой `model → calibrator`; финальная `p` клиппится в `[0.02, 0.98]` для UX (защита от чрезмерной уверенности).
- `/tonight` отдаёт прогноз только при `low_history_confidence=false` из predict-датасета. Иначе — UX-сообщение без числа.
- Дисклеймер «модельная оценка, не совет» — по §13 базового плана.

### 15. Опционально (v2) и эксперименты улучшения качества

- **Stacking**: OOF logreg + LGBM → мета-логрег + 3–5 сильных фич; внешний тест на отдельном холдауте (см. предостережение §6.7 базового плана).
- **CatBoost** с `home_team_id`/`away_team_id`, ordered boosting; отдельный `requirements-modeling-catboost.txt`.
- **Optuna** (Bayes) поверх сетки LGBM с фиксированным seed и временным CV `train_k → inner_val_k`.
- **Time-decay sample weights** для LGBM (экспоненциальный вес по `day`); сравнить holdout-метрики с базовым прогоном.
- **`class_weight='balanced'`** на `home_win` с отчётом о калибровке до/после.
- **Сравнение Platt vs Isotonic** на одних и тех же фолдах; зафиксировать победителя в YAML по умолчанию.
- **Линии 6.0/7.5** для тотала (с пушем) — после стабилизации 5.5.
- **Сезонный retrain как plan-of-record**: автоматический cron-запуск `make modeling-train` в начале каждого сезона (§14 рисков базового плана).

---

## Краткая карта файлов (целевое состояние)

```
modeling/
  config.py
  splits.py
  metrics.py
  train_logreg.py
  train_lgbm.py
  calibrate.py
  artifacts.py
  cli.py                    # расширить: train
  train_input.py            # уже есть
  dataset_builder/          # уже есть
configs/
  modeling_default.yaml
requirements-modeling.txt
tests/
  test_modeling_splits.py
  test_modeling_metrics.py
  test_modeling_calibration.py
  test_modeling_no_db_access.py
  test_modeling_no_shuffle_cv.py
  test_modeling_cli_smoke.py
  test_modeling_lgbm_monotone.py
artifacts/
  models/
    home_win/
      lgbm/
        <run_id>/
          fold_<k>/
          final/{model.joblib, calibrator.joblib, metadata.json}
        latest -> <run_id>/final/
      logreg/
        <run_id>/...
        latest -> <run_id>/final/
    over_5_5/
      ...
  reports/
    <run_id>/{metrics.json, summary.md, reliability_<task>.png, run.log}
```

---

*Версию этого UPDATE-документа повышать раздельно от `feature_set_version`: здесь меняется только дорожная карта реализации.*
