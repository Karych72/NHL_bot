# ТЗ для ревьюера: этап 2 — конфигурация обучения

**Роль:** независимый reviewer.
**Источник:** [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md), раздел «### 2. Конфигурация обучения» **и** «## Сквозные требования (читать перед каждым этапом)». Соблюдение сквозных требований проверяется отдельным блоком ниже.
**Пара ТЗ для исполнителя:** [`executor_tz.md`](executor_tz.md).

Пути — от корня репозитория.

---

## 1. Принцип ревью

Приоритеты в порядке убывания:

1. **Fail-fast по конфликту с `metadata_train.json`** — нет «тихих» оверрайдов источника истины.
2. **Полнота обязательных полей YAML** и валидация типов/диапазонов.
3. **Запрет лишних ключей** (`extra = "forbid"` / эквивалент) — опечатка не должна молча превращаться в дефолт.
4. **Единый `random_seed`** и наличие детерминированного `derive_seed`.
5. **Воспроизводимый формат `<run_id>`** и наличие `configure_run_logger` для `artifacts/reports/<run_id>/run.log`.
6. **Изоляция от PostgreSQL и dataset_builder**: `modeling/config.py` не тянет тяжёлые цепочки.
7. **Тестируемость**: позитив + ключевые негативные сценарии покрыты.

---

## 2. Файлы и расположение

- [ ] `modeling/config.py` существует, экспортирует типизированную модель конфига, `ConfigError`, функцию `load_config(...)` (или `resolve_config(...)`), хелперы `derive_seed`, `build_run_id`, `configure_run_logger`.
- [ ] `configs/modeling_default.yaml` существует, парсится моделью «как есть», без оверрайдов.
- [ ] Документация описывает: обязательные поля, правило приоритета YAML ↔ метаданные, CLI-флаги, формат `<run_id>`, путь `run.log`. Достаточно подраздела в `docs/modeling_dataset_builder.md` или нового `docs/modeling_training.md`.

---

## 3. Модель конфига и валидация

- [ ] Используется `pydantic` v2 (предпочтительно) **или** dataclass + явная ручная валидация. Если выбран dataclass — проверки сравнимы по строгости с `pydantic` (типы, диапазоны, enum).
- [ ] **Лишние ключи запрещены** на верхнем уровне и во вложенных структурах (`extra = "forbid"` или эквивалент).
- [ ] Обязательные поля присутствуют и валидируются (см. раздел 2.2 исполнительского ТЗ):
  - [ ] `random_seed: int`;
  - [ ] `compute.num_threads: int >= 1`, `compute.log_level` валидируется как logging-уровень;
  - [ ] `tasks.{home_win,over_5_5}.enabled: bool`, хотя бы одна задача `enabled=true`;
  - [ ] `split.method ∈ {"month","fixed_games"}`, `n_test_windows >= 5`, `inner_val_games >= 300`, `calibration_games >= 300`;
  - [ ] `split.holdout`: ровно один из `fraction` (0 < x < 0.5) или `date_range`;
  - [ ] `models.logreg.grids.C`: непустой список положительных float;
  - [ ] `models.lgbm.grids.*`: все списки непустые, типы согласованы (целые/дробные где ожидается);
  - [ ] `models.lgbm.monotone.{home_win,over_5_5}`: значения ∈ {-1, 0, +1}, ключи — строки; **сверка с манифестом тут не проводится** (это этап 8);
  - [ ] `calibration.method ∈ {"isotonic","platt"}`, `calibration.min_samples >= 1`;
  - [ ] `evaluation.ece_bins >= 2`, `bootstrap_samples >= 1`, `bootstrap_block_by_day: bool`, `0 < epsilon_clip < 1e-3`.
- [ ] Понятные сообщения ошибок (включая полный путь к полю, ожидаемый/полученный тип/значение).

---

## 4. Приоритет YAML ↔ `metadata_train.json` (fail-fast)

- [ ] Источником истины для `feature_set_version`, `rolling_windows`, `features_hash`, `feature_manifest`, `cold_start_policy_predict` является **`metadata_train.json`**.
- [ ] Если соответствующее поле есть в YAML и расходится с метаданными — поднимается **`ConfigError`** с **полным диффом** (поле, значение в YAML, значение в метаданных). Без «тихих» оверрайдов.
- [ ] Если поле отсутствует в YAML — берётся из метаданных и попадает в **resolved-конфиг** (виден через `--print-resolved-config`).
- [ ] Резолв вынесен в чистую функцию без побочных эффектов, удобную для тестов.

---

## 5. CLI

- [ ] Подкоманда `train` принимает: `--config <path.yaml>`, повторяемый `--set key=value`, `--print-resolved-config`, `--metadata <path>` (или эквивалент для сверки с `metadata_train.json`).
- [ ] `--set key=value` поддерживает dotted-path и парсит значения как YAML-литералы (списки/числа/булевы без шелл-кавычек на каждое значение).
- [ ] `--print-resolved-config` печатает итоговый YAML resolved-конфига в stdout и завершает процесс кодом 0 **без обучения**.
- [ ] Команда `train` без флага `--print-resolved-config` либо явно бросает понятное `NotImplementedError("stage 10")`/сообщение-заглушку, либо вообще не реализована за пределами парсинга — это допустимо в рамках этапа 2.

---

## 6. Сквозные требования (раздел «## Сквозные требования» в плане)

- [ ] **Единый `random_seed`**: в коде есть детерминированный хелпер `derive_seed(name) -> int`, использующий `random_seed`. Отдельных независимых seed-ключей в YAML нет.
- [ ] **`compute.num_threads`**: значение доступно через resolved-конфиг (фактический проброс в sklearn/lightgbm — этапы 7/8, не блокирует ревью).
- [ ] **`<run_id>`-формат**: хелпер `build_run_id(task, model, features_hash, now_utc)` возвращает строку `<task>_<model>_<features_hash[:8]>_<YYYYmmddTHHMMSSZ>` (regex-проверка в тесте, независимость от локали).
- [ ] **Логи**: `configure_run_logger(run_id, log_level, reports_root="artifacts/reports")` создаёт `artifacts/reports/<run_id>/run.log`, уровень берётся из `compute.log_level`.
- [ ] **Запрет PG-доступа**: `modeling/config.py` не импортирует `psycopg2`, `modeling.dataset_builder.*` (допустимы лёгкие импорты только при крайней необходимости — обоснованно прокомментированы). Проверено мини-тестом в этом этапе **или** оставлено на полную AST-проверку этапа 11 — обоснование автора принимается.
- [ ] **Версии библиотек** не логируются здесь (это этап 10/12, в `metadata.json` артефакта) — отсутствие этого функционала в этапе 2 **не** блокер.

---

## 7. Тесты

- [ ] Позитив: минимальный YAML парсится в модель, типы корректные.
- [ ] Негативы по полям (минимум: отсутствие обязательного ключа, лишний ключ верхнего уровня, неверный тип `random_seed`, `n_test_windows < 5`, `inner_val_games < 300`, `calibration_games < 300`, обе задачи `enabled=false`, неверный `split.method` / `calibration.method`, `monotone` со значением вне `{-1, 0, +1}`).
- [ ] Сверка с метаданными: расхождение `features_hash` — `ConfigError` с диффом; расхождение `feature_set_version` — `ConfigError`; поле отсутствует в YAML, есть в метаданных — берётся из метаданных.
- [ ] Оверрайды `--set` для скаляров и списков работают корректно.
- [ ] `--print-resolved-config` — печатает валидный YAML, который парсится обратно в ту же структуру; код возврата 0.
- [ ] `build_run_id` — regex-проверка формата.
- [ ] `configure_run_logger` — создаёт каталог и файл, пишет INFO-строку.

---

## 8. Скоуп

- [ ] В MR нет реализации этапов 4–10, 15 UPDATE-плана (сплиты, метрики, train_logreg, train_lgbm, calibrate, полноценный CLI train, Optuna/CatBoost). Допустимы только заготовки, явно нужные для конфига.
- [ ] Не сверяется `models.lgbm.monotone` с манифестом (это этап 8).
- [ ] Не пробрасываются `num_threads` в sklearn/lightgbm (это этапы 7/8).

---

## 9. Вердикт

**approve / approve with nits / request changes**.

**Блокеры:**

- «Тихий» оверрайд `features_hash` / `feature_set_version` / `rolling_windows` без `ConfigError`.
- Отсутствие запрета лишних ключей (опечатка → молчаливый дефолт).
- Отсутствие обязательных полей из раздела 3 этого ТЗ или их слабая валидация.
- Несколько независимых seed-полей вместо единого `random_seed` + `derive_seed`.
- Неверный или нестабильный формат `<run_id>`.
- Импорт `psycopg2` / `modeling.dataset_builder.*` в `modeling/config.py` без обоснования.
- Отсутствие тестов на сверку с метаданными или на негативные сценарии валидации.

**Ниты:**

- Стиль сообщений `ConfigError` (читаемость диффа).
- Расположение документации (`docs/modeling_training.md` vs подраздел в `docs/modeling_dataset_builder.md`).
- Именование функций/полей (`load_config` vs `resolve_config`, `ResolvedConfig` vs `ModelingConfig`).
- Покрытие edge cases CLI-парсинга `--set` (вложенные списки/булевы).
