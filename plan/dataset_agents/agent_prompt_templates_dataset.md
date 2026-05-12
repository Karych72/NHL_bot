# Шаблоны запуска агентов: dataset builder (executor + reviewer)

Этот документ содержит готовые промпты и практический workflow запуска агентов в Cursor для реализации и ревью датасетного контура.

Связанные документы:
- План реализации: `plan/deprecated_plan/nhl_dataset_build_plan.md`
- ТЗ исполнителя: `plan/dataset_agents/agent_executor_dataset_tz.md`
- ТЗ ревьюера: `plan/dataset_agents/agent_reviewer_dataset_tz.md`

---

## 1. Что запускать и в каком порядке

Рекомендуемый порядок:

1. Запустить **агента-исполнителя** (реализация).
2. Проверить, что исполнитель завершил задачу и приложил отчёт.
3. Запустить **агента-ревьюера** (аудит результата исполнителя).
4. Если ревьюер дал `REQUEST_CHANGES`:
   - снова исполнитель на фикс;
   - снова ревьюер.
5. Остановиться только при ревью-вердикте `APPROVE`.

---

## 2. Prompt template для агента-исполнителя

Скопируй и отправь в чат агента:

```text
Задача: реализовать dataset builder для NHL строго по ТЗ.

Обязательно прочитай и выполни:
1) plan/deprecated_plan/nhl_dataset_build_plan.md
2) plan/dataset_agents/agent_executor_dataset_tz.md

Требования:
- Реализуй полный контур сборки датасета для режимов train и predict.
- Строго соблюдай anti-leakage контракт (hist_day < target_day, без текущего game_id).
- Обеспечь schema parity train/predict (кроме y-меток), feature manifest/hash.
- Добавь fail-fast валидации качества и data_quality_report.
- Добавь/обнови автотесты согласно ТЗ.
- Не делай «тихих» автокоррекций схемы перед предиктом.

Формат результата:
1) список изменённых файлов;
2) что реализовано по каждому файлу;
3) какие тесты запускал и их результат;
4) какие проверки качества реализованы;
5) статус: DONE или PARTIAL (с причинами).

Если находишь неоднозначность — выбирай наиболее безопасный вариант и явно документируй решение.
```

---

## 3. Prompt template для агента-ревьюера

Запускай после завершения исполнителя.  
Скопируй и отправь:

```text
Задача: провести независимый аудит реализации dataset builder.

Обязательно прочитай:
1) plan/deprecated_plan/nhl_dataset_build_plan.md
2) plan/dataset_agents/agent_executor_dataset_tz.md
3) plan/dataset_agents/agent_reviewer_dataset_tz.md

Что проверить:
- строгий anti-leakage контракт;
- совпадение train/predict схемы фич;
- корректность fail-fast quality checks;
- полноту и реальную полезность тестов;
- наличие metadata и quality report артефактов.

Требуется отчёт в формате:
1) Findings (HIGH/MEDIUM/LOW, по убыванию критичности)
2) Open questions / assumptions
3) Пройденные проверки
4) Непокрытые риски
5) Вердикт: APPROVE или REQUEST_CHANGES

Важно:
- Если есть хотя бы один блокирующий дефект из ТЗ ревьюера, APPROVE ставить нельзя.
- Для каждого дефекта дай конкретный способ исправления и способ проверки фикса.
```

---

## 4. Быстрый workflow в Cursor (практика)

## 4.1 Запуск исполнителя

1. Открой новый чат.
2. Прикрепи контекст через `@`:
   - `@plan/deprecated_plan/nhl_dataset_build_plan.md`
   - `@plan/dataset_agents/agent_executor_dataset_tz.md`
3. Вставь prompt template исполнителя.
4. Дождись полного отчёта с результатами тестов.

## 4.2 Запуск ревьюера

1. Открой второй чат (или новый тред).
2. Прикрепи:
   - `@plan/deprecated_plan/nhl_dataset_build_plan.md`
   - `@plan/dataset_agents/agent_executor_dataset_tz.md`
   - `@plan/dataset_agents/agent_reviewer_dataset_tz.md`
3. Вставь prompt template ревьюера.
4. Получи вердикт `APPROVE`/`REQUEST_CHANGES`.

## 4.3 Цикл фиксов

Если `REQUEST_CHANGES`:
1. Передай findings исполнителю.
2. Попроси внести только необходимые фиксы + добавить тесты на регрессию.
3. Повтори ревью.

---

## 5. Как принимать результат (acceptance checklist)

Считай задачу завершённой, только если выполнено всё:

1. Есть рабочие команды сборки `train` и `predict`.
2. Anti-leakage проверки подтверждены кодом и тестами.
3. Train/predict schema parity автоматизирована.
4. Есть `metadata` и `data_quality_report`.
5. Ревьюер выдал `APPROVE` без блокирующих замечаний.

---

## 6. Типичные ошибки при работе с агентами

1. Давать исполнителю слишком короткий промпт без явного контракта.
2. Делать ревью тем же агентом в том же контексте без независимой проверки.
3. Принимать «зелёные тесты», которые не проверяют leakage.
4. Пропускать проверку schema parity перед инференсом.
5. Смешивать новые фичи и отладку моделей до стабилизации dataset builder.

---

## 7. Мини-шаблон для твоего короткого запуска (если спешишь)

```text
Реализуй dataset builder по plan/deprecated_plan/nhl_dataset_build_plan.md и plan/dataset_agents/agent_executor_dataset_tz.md.
Критично: anti-leakage, schema parity train/predict, fail-fast validate, обязательные тесты.
В конце дай отчёт по файлам, тестам и статус DONE/PARTIAL.
```

