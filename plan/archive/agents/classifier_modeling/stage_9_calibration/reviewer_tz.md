# ТЗ для ревьюера: этап 9 — Калибровка по протоколу без утечки

**Роль:** независимый reviewer.  
**Источник:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 9. Калибровка по протоколу без утечки», с учётом «## Сквозные требования (читать перед каждым этапом)».  
**Пара ТЗ для исполнителя:** [`executor_tz.md`](executor_tz.md).

Пути — от корня репозитория.

---

## 1. Принцип ревью

Приоритет:

1. **Отсутствие утечки** — калибратор обучается **только** на `calibration_k`, никогда не видит `test_k`/`holdout`/`inner_val_k`; сырая модель калибровкой не переобучается. Это центральное требование этапа.
2. **Запрет `CalibratedClassifierCV`** — его внутренний CV не уважает временной порядок; в проекте он запрещён.
3. **Корректность протокола малого среза** — `calibration_skipped=true` при `|calibration_k| < min_samples`, без тихой деградации.
4. **Воспроизводимость и изоляция от БД** — единый `random_seed`, отсутствие глобального state, нет PostgreSQL/`dataset_builder`.
5. **Минимальный скоуп MR** — этап 9 не реализует этапы 5/6/7/8/10.

Если хоть один из пунктов §2–§8 ниже нарушен — **request changes** с указанием конкретного места в коде/тесте.

---

## 2. Протокол без утечки (блокеры)

- [ ] Калибратор обучается **строго на `calibration_k`** (`raw_p_cal`, `y_cal`). Нет доступа к `test_k`/`holdout`/`inner_val_k` — есть тест.
- [ ] Сырая модель **не переобучается и не дообучается** внутри калибровки — она приходит готовой с этапов 7–8. Любой `.fit(...)` сырой модели в `modeling/calibrate.py` — блокер.
- [ ] Калибровка **от семейства модели не зависит**: на вход — только вероятности сырой модели, один код для logreg и lgbm.
- [ ] Итоговая оценка `test_k`/`holdout` строится **цепочкой** raw → calibrator (`apply_calibrator`), а не повторным обучением.

---

## 3. Запрет CalibratedClassifierCV (блокер)

- [ ] `modeling/calibrate.py` **не** импортирует и **не** использует `sklearn.calibration.CalibratedClassifierCV`. Проверяется grep/AST + тестом. Любое использование — блокер (UPDATE-план: «`CalibratedClassifierCV` запрещён в проекте»).
- [ ] Реализован **явный двухшаговый** пайплайн (raw-модель → отдельный калибратор), а не обёртка sklearn с внутренним CV.

---

## 4. Метод калибровки

- [ ] Метод берётся из YAML (`calibration.method`): поддержаны `isotonic` и `platt`.
- [ ] `isotonic` — `IsotonicRegression(out_of_bounds='clip')` (монотонна, клиппит вне диапазона обучения).
- [ ] `platt` — логистическая регрессия на одном признаке (сырая вероятность / её logit); в docstring явно сказано, что подаётся на вход.
- [ ] Неизвестный `method` → понятная ошибка (`CalibrationError`/`ValueError`), **без** тихого фолбэка на дефолт.
- [ ] Выход калибратора — в `[0, 1]`, без NaN.

---

## 5. Поведение при малом `calibration_k`

- [ ] При `n_cal < min_samples` (дефолт 500) — `calibration_skipped=True`.
- [ ] В этом режиме `apply_calibrator` возвращает сырую вероятность **побитово без изменений** (есть тест `assert_array_equal`).
- [ ] Флаг `calibration_skipped` честно отдан в метаданные среза наверх (для пометки в отчёте на этапе 5/10).
- [ ] **Тихая калибровка на недостаточном срезе (без флага) — блокер.**

---

## 6. Артефакт

- [ ] Сохраняются **обе** компоненты: `model_raw.joblib` и `calibrator.joblib` (при `calibration_skipped` — маркер identity), плюс `metadata.json`.
- [ ] `metadata.json` содержит: `method`, `calibration_skipped`, `n_calibration`, диапазоны дат `train`/`calibration`, размеры срезов, `seed`, версию `sklearn`, `features_hash`, `git_commit` (или `null`). Никаких рантайм-полей (`wall_time`, `cpu_id`).
- [ ] Структура каталога совместима с этапом 10 (`fold_<k>/`, `final/{model.joblib, calibrator.joblib, metadata.json}`).
- [ ] Round-trip `save → load` восстанавливает калибратор и метаданные — есть тест.

---

## 7. Контракт ошибок

- [ ] неизвестный `method` → ошибка;
- [ ] `len(raw_p_cal) != len(y_cal)` → ошибка;
- [ ] `y_cal ∉ {0, 1}` → ошибка;
- [ ] `raw_p_cal` вне `[0, 1]` / NaN → ошибка;
- [ ] `min_samples ≤ 0` → ошибка;
- [ ] все исключения — с понятным сообщением (`CalibrationError`/явный `ValueError`, не голый `assert`).

---

## 8. Воспроизводимость, изоляция и потоки (сквозные требования)

- [ ] Все стохастические шаги (если есть) — из единого `random_seed`; нет фолд-специфичных seed'ов (`seed + k` и т.п.).
- [ ] Нет глобального `np.random.seed(...)`; запуск не мутирует `np.random.get_state()`.
- [ ] Потоки — только через `num_threads` (= `compute.num_threads`). Нет `n_jobs=-1`, `os.cpu_count()`, `multiprocessing.cpu_count()`, чтения `OMP_NUM_THREADS`/`MKL_NUM_THREADS`.
- [ ] `modeling/calibrate.py` **не** импортирует `psycopg2`, `modeling.dataset_builder.*` (grep/AST; покрытие `tests/test_modeling_no_db_access.py` на этапе 11).
- [ ] Калибровка не генерирует `<run_id>` и не зависит от текущего времени.

---

## 9. Тесты

В `tests/test_modeling_calibration.py`:

- [ ] **Калибратор только на `calibration_k`:** мок-срезы; нет доступа к `test_k`/`holdout`.
- [ ] **`calibration_skipped=true`:** при малом срезе `apply_calibrator` отдаёт сырую вероятность побитово (`assert_array_equal`).
- [ ] **`isotonic` не ухудшает калибровку:** на синтетике с плохо откалиброванными сырыми p — ECE/Brier после `isotonic` не хуже сырого (порог в комментарии).
- [ ] **`platt`:** монотонность, выход в `[0,1]`, без NaN.
- [ ] **Запрет `CalibratedClassifierCV`:** AST/grep-тест.
- [ ] **Детерминизм:** одинаковый `seed` → идентичный результат.
- [ ] **Контракт ошибок:** неизвестный method, len mismatch, `y ∉ {0,1}`, `raw_p` вне `[0,1]`/NaN, `min_samples ≤ 0`.
- [ ] **Сериализация артефакта:** round-trip; `metadata.json` проходит `json.dumps`; обе компоненты на месте.
- [ ] **Цепочка raw → calibrator:** `apply_calibrator` совпадает с прямым применением калибратора; identity при skip.

Если хоть один из пунктов выше отсутствует — **request changes**.

---

## 10. Скоуп MR

- [ ] В MR **нет** обучения сырых моделей (никаких новых `train_logreg.py`/`train_lgbm.py`), нет CLI `train`, нет walk-forward оркестрации, нет финального переобучения перед holdout, нет симлинка `latest` — это этапы 7/8/10.
- [ ] **Нет** метрик/ECE/reliability/breakdown/тривиального baseline (этап 5) и bootstrap (этап 6) — калибровка лишь отдаёт калиброванные вероятности.
- [ ] `requirements.txt`, `requirements-dev.txt`, `Dockerfile`, `docker-compose.yml` не меняются (или косметически с явным обоснованием — тогда не блокер, но проговорено). Зависимости калибровки уже в `requirements-modeling.txt` (этап 3).
- [ ] Никаких изменений в `modeling/dataset_builder/*`, `modeling/train_input.py`, `modeling/splits.py`.
- [ ] В описании MR явно сказано: «реализован этап 9 UPDATE-плана».

---

## 11. Документация

- [ ] Если `docs/modeling_training.md` уже создан — добавлен раздел про калибровку: двухшаговый протокол без утечки, методы `isotonic`/`platt`, поведение `calibration_skipped`, явный запрет `CalibratedClassifierCV` и причина.
- [ ] Если файла ещё нет — раздел может быть в docstring публичных функций, но в описании MR отмечено: «полная страница появится на этапе 10/13».
- [ ] Битых ссылок и якорей нет.

---

## 12. Сквозные требования UPDATE-плана (чеклист)

- [ ] **Источник истины фич**: калибровка работает с вероятностями, не с манифестом; проверка не дублируется.
- [ ] **Воспроизводимость**: единый `random_seed`, независимые seeds запрещены.
- [ ] **Лимит потоков**: `num_threads` — параметр, по умолчанию 1.
- [ ] **Логи**: калибровка отдаёт `method`, `calibration_skipped`, размеры/диапазоны срезов наверх.
- [ ] **`<run_id>`**: калибровка не зависит от времени и не генерирует id.
- [ ] **Никакого доступа к PostgreSQL**: проверено grep'ом / AST-тестом.

---

## 13. Вердикт

**approve / approve with nits / request changes.**

**Блокеры:**

- утечка: калибратор видит `test_k`/`holdout`/`inner_val_k` или обучается не на `calibration_k`;
- переобучение/дообучение сырой модели внутри калибровки;
- использование `sklearn.calibration.CalibratedClassifierCV` в любом виде;
- тихая деградация при малом срезе (нет `calibration_skipped`, но калибровка применена), либо `apply_calibrator` при skip меняет сырые вероятности;
- тихий фолбэк на дефолтный метод при неизвестном `method`;
- глобальный `np.random.seed(...)` / мутация `np.random.get_state()` / фолд-специфичные seeds;
- `os.cpu_count()` / `n_jobs=-1` / `OMP_NUM_THREADS` без явного `num_threads`;
- импорт `psycopg2` или `modeling.dataset_builder.*` в `modeling/calibrate.py`;
- артефакт не сохраняет обе компоненты (`model_raw.joblib`, `calibrator.joblib`) или метаданные срезов;
- в MR есть посторонняя работа из этапов 5/6/7/8/10.

**Ниты:**

- стиль docstring и расположение раздела документации;
- имена внутренних переменных и формат сообщений `CalibrationError`;
- выбор между `dataclass` и `TypedDict` для результата (оба допустимы, важна JSON-сериализуемость);
- что именно подаётся на вход Platt (вероятность vs logit) — допустимы оба, важно зафиксировать в docstring.
