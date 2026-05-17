# UPDATE: план доработки прематч-классификаторов NHL

**Статус документа:** дополнение к [nhl_classifier_modeling_plan.md](nhl_classifier_modeling_plan.md).  
**Назначение:** закрыть всё, что в базовом плане ещё не сведено к коду, в **одном упорядоченном** списке этапов.

**Уже реализовано (не дублировать работу):**

- Сборка датасета train/predict из PostgreSQL: `modeling/dataset_builder/`, CLI `python -m modeling.cli build-dataset`.
- Метки `y_home_win`, `y_over_5_5`, rolling-фичи с `shift(1)`, as-of по дате, контекст расписания, `features_hash`, манифест, валидация и тесты: см. [docs/modeling_dataset_builder.md](../../docs/modeling_dataset_builder.md).

Ниже — **оставшийся** путь до выполнения §5–§8, §12 и чеклиста §15 базового плана.

---

## Этапы (порядок логического выполнения)

### 1. Зафиксировать контракт входа для обучения

- Описать в коде/доке: путь к `dataset_train.csv` (или parquet, если добавите) + обязательный `metadata_train.json` (манифест фич, `features_hash`, `feature_set_version`).
- Функция загрузки: читает датасет, отделяет колонки ключей (`game_id`, `day`, `season_id`, `home_team_id`, `away_team_id`), меток (`y_home_win`, `y_over_5_5`), служебных (`feature_set_version`, `dataset_built_at`, …) от матрицы X по манифесту (как в `schema.py`), без «угадывания» колонок по префиксам где возможно.

### 2. Конфигурация обучения

- Добавить `modeling/config.py` (dataclass / Pydantic) и `configs/modeling_default.yaml` с полями из §11 базового плана: окна (для справки/версии — основной источник истины уже в метадате билда), `min_prior_games`, включение задач, метод сплита, сетки гиперпараметров, калибровка, bootstrap, seeds, пути `artifacts/`.
- CLI: возможность передать `--config path.yaml` с переопределением отдельных ключей.

### 3. Зависимости для моделирования

- Добавить `requirements-modeling.txt` (или optional extra): `numpy`, `pandas` (уже в проекте), `scikit-learn`, `pyyaml`, `joblib`; кандидат на primary — `lightgbm` или `catboost` (один на v1 primary, второй опционально).
- В `Makefile` опциональная цель `make modeling-dev` → `pip install -r requirements-modeling.txt` (не раздувать основной образ бота без необходимости).

### 4. Временные сплиты без утечки

- Реализовать `modeling/splits.py`:
  - сортировка по `day`, затем `game_id`;
  - **walk-forward**: блоки по календарю (например месяц) или по числу строк — метод из YAML;
  - для каждого внешнего train-блока: выделить **хвост** как inner `val` (доля или фиксированное число последних игр) для early stopping и выбора гиперпараметров;
  - **финальный holdout**: последние 15–20% времени (или явный диапазон дат в конфиге) — ни разу не в train/val/cal при тюнинге.
- Явно запретить в коде/тестах shuffle-кросс-валидацию по матчам.

### 5. Метрики и отчётность

- Реализовать `modeling/metrics.py`: log loss (клип p ∈ [ε, 1−ε]), Brier, ECE по бинам, опционально ROC-AUC.
- Функции для reliability table (бин → mean p, частота y=1, вес бина).
- Сохранение отчёта: `artifacts/reports/<run_id>/metrics.json` + краткий `summary.md`; при наличии matplotlib/seaborn — PNG reliability diagram.

### 6. Bootstrap доверительные интервалы

- На **каждом тестовом окне** и на **holdout**: resampling матчей с возвратом, N из конфига (например 1000), ДИ для log loss и Brier.
- Логировать seed и параметр N в метаданные прогона.

### 7. Baseline: логистическая регрессия (две модели)

- `modeling/train_logreg.py`:
  - отдельные пайплайны для `home_win` и `over_5_5`;
  - `StandardScaler` только на train-фолде внутри каждого walk-forward окна (через sklearn Pipeline или ручной fit на train, transform на val/test);
  - сетка `C` из конфига, выбор по log loss на inner val;
  - `class_weight=None` по умолчанию (политика §1.1).
- Сохранение: `modeling/artifacts.py` — joblib + JSON метаданные (`features_hash`, диапазон дат train, число строк, версии библиотек, git commit при наличии `.git`).

### 8. Primary: градиентный бустинг

- `modeling/train_lgbm.py` (или CatBoost): binary logloss, early stopping по inner val, сетка/Optuna — минимум фиксированная сетка из YAML.
- Две независимые модели; сравнение с logreg на тех же сплитах (таблица метрик в отчёте).

### 9. Калибровка по протоколу без утечки

- Реализовать `modeling/calibrate.py`:
  - сырой классификатор обучен только на `train`;
  - на **отдельном временном срезе** `calibration` (следующий блок после train, не пересекающийся с holdout) — предсказания сырой модели + истинные y → обучение Platt или Isotonic (из конфига);
  - итоговая оценка на `test` и на **holdout**: цепочка raw → calibrator.
- Артефакт: сохранять и сырую модель, и калибратор (или sklearn `CalibratedClassifierCV` только если срезы строго соблюдены вручную — предпочтительнее явный двухшаговый пайплайн для прозрачности).

### 10. Единая точка входа CLI

- Расширить `modeling/cli.py`:
  - `train` — полный цикл: загрузка датасета, walk-forward оценка, опционально переобучение на «всём до holdout» + калибровка + финальный holdout-отчёт;
  - опции: `--task {home_win,over_5_5,both}`, `--model {logreg,lgbm,both}`, `--dry-run` (только сплиты и размеры).
- Опционально: `make modeling-train` в Makefile, вызывающий `python -m modeling.cli train --config configs/modeling_default.yaml`.

### 11. Тесты

- `tests/test_modeling_splits.py`: монотонность индексов, отсутствие пересечения holdout с train, наличие inner val внутри train-блока.
- `tests/test_modeling_metrics.py`: эталонные значения logloss/Brier на ручных мини-векторах.
- `tests/test_modeling_calibration.py` (лёгкий): на синтетических вероятностях проверить, что calibrator не смотрит на «будущее» (мок срезов).
- При необходимости переименовать или добавить `test_modeling_no_leakage.py` как тонкую обёртку над уже существующими тестами билдера — без дублирования сценариев.

### 12. Диагностика и критерии приёмки (Definition of Done)

- Выполнить чеклист §12 базового плана:
  1. Одна команда от пустых артефактов до обученных моделей + отчётов.
  2. Автотест на утечку: уже частично покрыт билдером; дополнить при появлении нового кода в train (что сплиты не смешивают времена).
  3. Holdout-отчёт: log loss, Brier, ECE до/после калибровки + reliability PNG.
  4. JSON метаданные с `features_hash`, датами, размерами выборок, git hash.
  5. Обе задачи — отдельные артефакты (или явная multitask только если сознательно выбрана).

### 13. Синхронизация документации

- Обновить строку статуса в [nhl_classifier_modeling_plan.md](nhl_classifier_modeling_plan.md): датасет-билдер реализован; обучение — по этому UPDATE-документу.
- Короткий раздел в `docs/modeling_dataset_builder.md` или отдельный `docs/modeling_training.md` со ссылкой на `configs/modeling_default.yaml` и пример полного прогона.

### 14. Фаза 2 (интеграция в бота) — после стабилизации модели

- Загрузка joblib в сервисе бота, режим predict, `/tonight` только при наличии истории в PG, дисклеймер в UX — по §13 базового плана; не смешивать обучение с live API.

### 15. Опционально (v2)

- Stacking (OOF + метауровень) с дополнительным внешним тестом.
- CatBoost с `home_team_id` / `away_team_id` и ordered boosting.
- Эксперименты с `class_weight='balanced'` на home_win с отчётом по калибровке до/после.

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
  dataset_builder/          # уже есть
configs/
  modeling_default.yaml
requirements-modeling.txt
tests/
  test_modeling_splits.py
  test_modeling_metrics.py
  test_modeling_calibration.py
artifacts/
  models/
  reports/
```

---

*Версию этого UPDATE-документа повышать раздельно от `feature_set_version`: здесь меняется только дорожная карта реализации.*
