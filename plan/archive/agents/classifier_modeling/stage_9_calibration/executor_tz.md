# ТЗ для исполнителя: этап 9 — Калибровка по протоколу без утечки

**Роль:** инженер-исполнитель.  
**Источник требований:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 9. Калибровка по протоколу без утечки» **с обязательным учётом** «## Сквозные требования (читать перед каждым этапом)» того же документа.  
**Пара ТЗ для ревью:** [`reviewer_tz.md`](reviewer_tz.md).  
**Связанные ТЗ темы:**
- [`../stage_4_splits/executor_tz.md`](../stage_4_splits/executor_tz.md) — этап 4 (сплиты). Калибратор обучается на блоке `calibration_k` / `calibration_final`, который приходит из сплиттера и **не пересекается** с train/val/test/holdout.
- [`../stage_5_metrics/executor_tz.md`](../stage_5_metrics/executor_tz.md) — этап 5 (метрики). Итоговая оценка `test_k`/`holdout` после калибровки считается метриками этапа 5 на вероятностях из цепочки raw → calibrator.
- [`../stage_6_bootstrap/executor_tz.md`](../stage_6_bootstrap/executor_tz.md) — этап 6 (bootstrap). ДИ метрик считаются на тех же калиброванных вероятностях.
- Этапы 7–8 (`modeling/train_logreg.py`, `modeling/train_lgbm.py`) — источник **сырых** моделей. Калибровка от семейства модели **не зависит**: на вход идут только сырые вероятности.

Пути в этом документе заданы **от корня репозитория**.

---

## 1. Цель этапа

Реализовать модуль `modeling/calibrate.py` — **двухшаговый калибровочный пайплайн без утечки**:

1. **Сырой классификатор** (logreg / lgbm) обучен **только на `train_k`** (этапы 7–8) — этот этап его **не переобучает**.
2. **Калибратор** обучается **только на `calibration_k`** — отдельном временном блоке (этап 4), по парам (предсказание сырой модели на `calibration_k`, истинный `y` на `calibration_k`).
3. **Метод** калибровки — из YAML: `isotonic` | `platt`.
4. При `|calibration_k| < calibration.min_samples` (по умолчанию **500**) фолд помечается `calibration_skipped=true`, в отчёт идёт **сырая** вероятность с явной пометкой.
5. Итоговая оценка на `test_k` и `holdout` — **цепочка** raw → calibrator.
6. Артефакт прогона сохраняет **обе** компоненты: `model_raw.joblib`, `calibrator.joblib` + метаданные срезов.

Этап **не реализует** обучение сырых моделей (этапы 7–8), CLI `train` и финальное переобучение перед holdout (этап 10), сам отчёт/метрики (этап 5), bootstrap (этап 6) — только калибровочный модуль, его API и тесты.

---

## 2. Сквозные требования (обязательны к применению на этом этапе)

Из раздела «## Сквозные требования (читать перед каждым этапом)» UPDATE-плана для калибровочного модуля действуют:

- **Источник истины фич — `metadata_train.json`.** Калибратор работает с **одномерными вероятностями** сырой модели, а не с матрицей фич, поэтому с `feature_manifest` напрямую не сверяется. Любую проверку парити фич делает контракт входа этапа 1 / обучение этапов 7–8; `modeling/calibrate.py` её **не дублирует**. Если в калибровку приходят метаданные среза — `features_hash` пробрасывается в артефакт без изменения.
- **Воспроизводимость и единый `random_seed`.** Все стохастические шаги (если они вообще есть — для `isotonic`/`platt` обычно детерминированы) выводятся из `random_seed` из YAML. Любые «свои» seed-параметры (по фолду, по таску) **запрещены**. Версии библиотек (`sklearn`) логируются в `metadata.json` артефакта (фактический лог пишет CLI этапа 10; калибровка отдаёт нужные поля наверх).
- **Лимит потоков (`compute.num_threads`).** Если калибратор поддерживает `n_jobs` — он берётся **только** из `compute.num_threads`, передаётся параметром. Запрещено `n_jobs=-1`, `os.cpu_count()`, чтение `OMP_NUM_THREADS`/`MKL_NUM_THREADS`.
- **Логи.** Калибровка **сама** `run.log` не создаёт — это CLI этапа 10. Но **обязана** возвращать сериализуемый результат с полями метаданных среза (`method`, `calibration_skipped`, размеры срезов, диапазоны дат) для записи в `metrics.json` / `metadata.json`.
- **`<run_id>`.** Калибровка не генерирует `<run_id>` и не зависит от текущего времени.
- **Никакого доступа к PostgreSQL.** `modeling/calibrate.py` **запрещено** импортировать `psycopg2`, `modeling.dataset_builder.*` и любые модули, тянущие БД-зависимости (это явно перечислено в сквозном требовании UPDATE-плана и проверяется AST-тестом `tests/test_modeling_no_db_access.py`, этап 11).

---

## 3. Объём работ

### 3.1 Размещение кода

- Модуль `modeling/calibrate.py`.
- Сериализация артефактов (joblib + JSON) допустимо делать через `modeling/artifacts.py` (этап 7), если он уже существует; иначе — локальная функция сохранения в этом модуле, совместимая по структуре с тем, что ждёт этап 10. Не создавать конкурирующий формат артефакта.

### 3.2 Публичный API

Минимум следующего вида (имена допустимо уточнять, семантика — фикс):

- **`fit_calibrator(raw_p_cal, y_cal, *, method, min_samples=500, seed, num_threads=1) -> CalibratorFit`**
  - `raw_p_cal: np.ndarray` (shape `(n_cal,)`) — вероятности **сырой** модели на `calibration_k` (в `[0, 1]`);
  - `y_cal: np.ndarray` (shape `(n_cal,)`, значения `{0, 1}`) — истинные метки на `calibration_k`;
  - `method: str` — `"isotonic"` | `"platt"` (из YAML `calibration.method`);
  - `min_samples: int` — порог пропуска калибровки (из YAML `calibration.min_samples`, дефолт 500);
  - возвращает объект/структуру, содержащую обученный калибратор **либо** флаг `calibration_skipped=True` (тогда калибратор — identity).
- **`apply_calibrator(calibrator_fit, raw_p) -> np.ndarray`**
  - применяет цепочку: если `calibration_skipped=True` — возвращает `raw_p` **без изменений**; иначе — калиброванные вероятности.
- **Структура результата `CalibratorFit`** (`dataclass`), сериализуемая в JSON-метаданные:
  - `method: str`;
  - `calibration_skipped: bool`;
  - `n_calibration: int` — `|calibration_k|`;
  - `seed: int`;
  - (опционально) минимальные сведения для отчёта; **никаких** рантайм-полей (`wall_time`, `cpu_id`).
- **`save_calibration_artifact(...)` / `load_calibration_artifact(...)`** — сохранение/загрузка `model_raw.joblib`, `calibrator.joblib` + `metadata.json` (см. §3.5). Допустимо переиспользовать `modeling/artifacts.py`.

Калибровка **от семейства сырой модели не зависит**: на вход идут только её вероятности. Один и тот же код калибрует и logreg, и lgbm.

### 3.3 Метод калибровки

- **`isotonic`**: `sklearn.isotonic.IsotonicRegression(out_of_bounds='clip')`. Монотонно неубывающая, клиппит вне диапазона обучения.
- **`platt`**: логистическая регрессия на одном признаке — сырой вероятности (или её logit). Реализовать через `sklearn.linear_model.LogisticRegression` на `raw_p.reshape(-1, 1)` (Platt scaling). Зафиксировать в docstring, что именно подаётся на вход (вероятность vs logit) и почему.
- Неизвестный `method` → `CalibrationError` (или `ValueError`) с понятным сообщением. Тихий фолбэк на дефолт **запрещён**.

### 3.4 Протокол без утечки (ключевое)

- Калибратор **обучается строго на `calibration_k`** и **никогда** не видит `test_k`/`holdout`/`inner_val_k`. Это центральное требование этапа — нарушение блокер.
- Сырая модель калибровкой **не дообучается и не переобучается** — она приходит готовой (этапы 7–8). Этап 9 берёт уже обученную модель и только строит поверх неё калибратор.
- **`sklearn.calibration.CalibratedClassifierCV` запрещён** в проекте: его внутренний CV не уважает временной порядок и нет публичного API подсунуть `TimeSeriesSplit` корректно для `method='isotonic'`. Использовать **только** явный двухшаговый пайплайн. Любой импорт/использование `CalibratedClassifierCV` в `modeling/calibrate.py` — блокер.

### 3.5 Поведение при малом `calibration_k`

- Если `n_cal = |calibration_k| < min_samples` (дефолт 500):
  - `calibration_skipped = True`;
  - калибратор — **identity** (`apply_calibrator` возвращает `raw_p` как есть);
  - в метаданные среза идёт `calibration_skipped=true` и `n_calibration`;
  - **в отчёт идёт сырая вероятность с явной пометкой** (пометку проставляет вызывающий код этапа 5/10 на основе этого флага; калибровка обязана честно вернуть флаг).
- Тихая калибровка на недостаточном срезе (без флага) — запрещена.

### 3.6 Артефакт

Сохранять **обе** компоненты + метаданные срезов:

- `model_raw.joblib` — сырая модель (как пришла с этапов 7–8);
- `calibrator.joblib` — обученный калибратор **или** маркер identity при `calibration_skipped=true`;
- `metadata.json` — `method`, `calibration_skipped`, `n_calibration`, диапазоны дат `train`/`calibration`, размеры срезов, `seed`, версия `sklearn`, `features_hash` (проброшен из метаданных среза), `git_commit` (или `null` при отсутствии `.git`).

Структура каталога должна совпадать с тем, что ждёт этап 10:
- фолды: `artifacts/models/<task>/<model>/<run_id>/fold_<k>/` (см. карту файлов UPDATE-плана);
- production: `.../final/{model.joblib, calibrator.joblib, metadata.json}`.

На этом этапе достаточно дать функции сохранения/загрузки с этой структурой; раскладку по `<run_id>` и `final/` оркестрирует CLI этапа 10.

### 3.7 Контракт ошибок

`modeling/calibrate.py` обязан бросать понятные исключения (свой класс `CalibrationError` или `ValueError`, не голый `assert`):

- неизвестный `method` (не `isotonic`/`platt`);
- `len(raw_p_cal) != len(y_cal)`;
- `y_cal` содержит значения вне `{0, 1}`;
- `raw_p_cal` содержит значения вне `[0, 1]` / NaN;
- `min_samples ≤ 0` или не int.

### 3.8 Запреты

- **`sklearn.calibration.CalibratedClassifierCV`** в любом виде — запрещён.
- **Переобучение/дообучение сырой модели** внутри калибровки — запрещено.
- Обучение калибратора на чём-либо, кроме `calibration_k` (особенно на `test_k`/`holdout`) — запрещено.
- Тихая деградация при малом срезе (без `calibration_skipped`) — запрещена.
- Тихий фолбэк на дефолтный метод при неизвестном `method` — запрещён.
- `n_jobs=-1`, `os.cpu_count()`, чтение `OMP_NUM_THREADS`/`MKL_NUM_THREADS` — запрещены; потоки только через `num_threads`.
- Глобальный `np.random.seed(...)` — запрещён.
- Импорт `psycopg2`, `modeling.dataset_builder.*` — запрещён.

---

## 4. Тесты

Создать `tests/test_modeling_calibration.py`. Минимум (в т.ч. требования этапа 11 §149 UPDATE-плана):

1. **Калибратор обучается только на `calibration_k`:** на синтетике с мок-срезами проверить, что `fit_calibrator` не получает и не использует `test_k`/`holdout` (например, через фикстуру, где передача любых не-`calibration_k` данных привела бы к иному результату; либо проверка, что функция принимает ровно `raw_p_cal`/`y_cal` и не имеет доступа к остальным срезам).
2. **Путь `calibration_skipped=true`:** при `n_cal < min_samples` — `calibration_skipped=True`, `apply_calibrator` возвращает `raw_p` **побитово** без изменений (`np.testing.assert_array_equal`).
3. **`isotonic` улучшает калибровку:** на синтетике с заведомо плохо откалиброванными сырыми вероятностями (например, сжатыми/растянутыми) ECE/Brier после `isotonic` **не хуже** (а на явном примере — лучше) сырого. Порог мягкий, задокументирован в комментарии.
4. **`platt` работает:** монотонность выхода по входу, выход в `[0, 1]`, отсутствие NaN.
5. **Запрет `CalibratedClassifierCV`:** AST/grep-проверка, что `modeling/calibrate.py` не импортирует и не использует `sklearn.calibration.CalibratedClassifierCV`.
6. **Детерминизм:** два вызова `fit_calibrator` с одинаковым `seed` на одних данных дают идентичный результат.
7. **Контракт ошибок:** неизвестный `method`; `len` mismatch; `y ∉ {0,1}`; `raw_p` вне `[0,1]`/NaN; `min_samples ≤ 0` — каждый кейс падает с понятной ошибкой.
8. **Сериализация артефакта:** `save_calibration_artifact` → `load_calibration_artifact` round-trip восстанавливает калибратор и метаданные; `metadata.json` проходит `json.dumps`; обе компоненты (`model_raw.joblib`, `calibrator.joblib`) на месте.
9. **Цепочка raw → calibrator:** `apply_calibrator(fit, raw_p)` даёт то же, что прямое применение обученного калибратора; при `calibration_skipped` — identity.

Параллельно (минимум, не блокер этого MR):
- Если `tests/test_modeling_no_db_access.py` уже существует — убедиться, что `modeling/calibrate.py` входит в его охват и проходит. Полный охват — на этапе 11.

---

## 5. Интеграция с конфигом (этап 2)

Этап 2 (`modeling/config.py`) может быть ещё не закрыт. Допустимо:

- Принимать в функции **обычные параметры** (`method: str`, `min_samples: int`, `seed: int`, `num_threads: int`) — без зависимости от dataclass'а конфига.
- Дефолты обязаны совпадать с UPDATE-планом: `calibration.method` — без скрытого дефолта (метод обязателен из YAML), `calibration.min_samples=500`, `compute.num_threads=1`.
- При появлении `modeling/config.py` связь делается на этапе 10 (CLI), не в этом MR.

---

## 6. Критерии приёмки

1. `modeling/calibrate.py` реализует двухшаговый пайплайн: сырая модель (с этапов 7–8) + отдельный калибратор на `calibration_k`; сырая модель не переобучается.
2. Поддержаны `isotonic` и `platt` из YAML; неизвестный метод → понятная ошибка.
3. При `|calibration_k| < min_samples` (дефолт 500) — `calibration_skipped=true`, `apply_calibrator` возвращает сырую вероятность без изменений; флаг честно отдан наверх.
4. **`CalibratedClassifierCV` не импортируется и не используется** — есть тест.
5. Артефакт содержит обе компоненты (`model_raw.joblib`, `calibrator.joblib`) + `metadata.json` с метаданными срезов; round-trip восстановления работает.
6. Калибратор обучается **только** на `calibration_k`; нет доступа к `test_k`/`holdout` — есть тест.
7. Нет `np.random.seed(...)`, `os.cpu_count()`, `n_jobs=-1`; потоки только через `num_threads`.
8. `modeling/calibrate.py` не импортирует `psycopg2`, `modeling.dataset_builder.*`.
9. Тесты §4 проходят локально (`pytest tests/test_modeling_calibration.py`).
10. В описании MR явно сказано: «реализован этап 9 UPDATE-плана; этапы 5/6/7/8/10 не затрагиваются (кроме чтения готовых артефактов сырых моделей)».

---

## 7. Ограничения и вне скоупа

- **Не** реализовывать обучение сырых моделей (этапы 7–8) — калибровка берёт их готовыми.
- **Не** реализовывать CLI `train`, walk-forward оркестрацию, финальное переобучение перед holdout, симлинк `latest` (этап 10).
- **Не** реализовывать метрики/ECE/reliability/breakdown/тривиальный baseline (этап 5) и bootstrap (этап 6) — калибровка лишь отдаёт калиброванные вероятности, которые они потребляют.
- **Не** использовать `sklearn.calibration.CalibratedClassifierCV` ни при каких условиях.
- **Не** менять `modeling/train_input.py`, `modeling/splits.py`, `modeling/dataset_builder/*`.
- **Не** менять `requirements.txt`, `Dockerfile`, `docker-compose.yml`; новые зависимости калибровки покрываются `requirements-modeling.txt` (этап 3, уже содержит `scikit-learn`, `joblib`).

---

## 8. Что отдать в MR

- `modeling/calibrate.py` (+ при необходимости минимальное расширение `modeling/artifacts.py` для сохранения двух компонент, если этап 7 ещё не дал нужную функцию).
- `tests/test_modeling_calibration.py`.
- Краткий раздел в `docs/modeling_training.md` (если файл уже создан этапами 1/4) с описанием:
  - двухшагового протокола без утечки (raw на `train_k`, калибратор на `calibration_k`);
  - методов `isotonic` / `platt` и что подаётся на вход Platt;
  - поведения `calibration_skipped` при малом срезе;
  - явного запрета `CalibratedClassifierCV` и причины.
- В описании MR — явная отметка: «реализован этап 9 UPDATE-плана; этапы 5/6/7/8/10 не затрагиваются».
