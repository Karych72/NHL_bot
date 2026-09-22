# Планы разработки NHL_bot

Актуальные планы сгруппированы **по темам** в подкаталогах `plan/<тема>/`. **Выполненные или заархивированные как контракт** лежат в [`deprecated_plan/`](./deprecated_plan/) (см. [`deprecated_plan/README.md`](./deprecated_plan/README.md)).

Оперативная документация по архитектуре и API — в [`docs/`](../docs/).

## Что открыто прямо сейчас

**[`open_tasks.md`](./open_tasks.md) — единый реестр невыполненных задач** по всем темам
(инженерия, трек B, продукт). Выбирать следующую задачу — оттуда; остальные файлы ниже
нужны за историей, критериями приёмки и контрактами.

## Статистика и продукт (`stats/`)

| Файл | Статус | Комментарий |
|------|--------|-------------|
| [`stats/team_and_country_stats_plan.md`](./stats/team_and_country_stats_plan.md) | **Частично** | Фаза C реализована (сезонное сравнение команд из `/tonight`), Фаза A частично; остаток — Задачи 41 и 42 в [`open_tasks.md`](./open_tasks.md). |

## Датасет и агенты Cursor (`dataset_agents/`)

| Файл | Статус | Комментарий |
|------|--------|-------------|
| [`dataset_agents/agent_executor_dataset_tz.md`](./dataset_agents/agent_executor_dataset_tz.md) | **ТЗ процесса** | Промпт для агента-исполнителя датасета; контракт — [`deprecated_plan/nhl_dataset_build_plan.md`](./deprecated_plan/nhl_dataset_build_plan.md). |
| [`dataset_agents/agent_reviewer_dataset_tz.md`](./dataset_agents/agent_reviewer_dataset_tz.md) | **ТЗ процесса** | Промпт для агента-ревьюера датасета. |
| [`dataset_agents/agent_prompt_templates_dataset.md`](./dataset_agents/agent_prompt_templates_dataset.md) | **Шпаргалка** | Готовые промпты Cursor для executor/reviewer цикла. |
| [`dataset_agents/agent_reviewer_dataset_verdict.md`](./dataset_agents/agent_reviewer_dataset_verdict.md) | **Артефакт ревью** | Вердикт и findings по датасетному контуру (2026-05-10). |

## Прематч-классификаторы (`classifier/`)

| Файл | Статус | Комментарий |
|------|--------|-------------|
| [`classifier/nhl_classifier_modeling_plan.md`](./classifier/nhl_classifier_modeling_plan.md) | **Проектирование / не выполнено** | Обучение и валидация прематч-классификаторов (модели вне датасетного билдера). |

## Инженерия кодовой базы (`engineering/`)

| Файл | Статус | Комментарий |
|------|--------|-------------|
| [`engineering/work_plan_2026-08-08.md`](./engineering/work_plan_2026-08-08.md) | **Источник истины по Задачам 1–32** | Критерии приёмки, команды проверки и основания закрытия. Открытых задач осталось три (21, 22, 26), они в [`open_tasks.md`](./open_tasks.md). |
| [`engineering/work_plan_2026-09-12.md`](./engineering/work_plan_2026-09-12.md) | **История** | Срез остатка на 2026-09-12; заменён [`open_tasks.md`](./open_tasks.md). |
| [`engineering/work_plan_2026-06-29.md`](./engineering/work_plan_2026-06-29.md) | **История / источник приоритетов** | Заменён [`engineering/work_plan_2026-08-08.md`](./engineering/work_plan_2026-08-08.md). |
| [`engineering/refactoring_plan_3.md`](./engineering/refactoring_plan_3.md) | **Исчерпан** | Мастер-план приоритетов (срез 2026-05-15); все пункты перенумерованы в `work_plan_2026-08-08.md` и закрыты, кроме B3/B5/B6 → Задачи 21, 22, 26. |
| [`engineering/refactoring_plan_2.md`](./engineering/refactoring_plan_2.md) | **История** | Срез незакрытых задач рефакторинга; остатка не осталось. |
| [`engineering/refactoring_plan.md`](./engineering/refactoring_plan.md) | **История** | Полный чеклист по фазам. |
| [`engineering/db_tests_remediation_plan.md`](./engineering/db_tests_remediation_plan.md) | **Выполнено** | Этапы A–D закрыты Задачей 1 (DB-тесты в CI) и целями `test-db`/`test-db-data`. |

## Архив выполненных планов (`plan/deprecated_plan/`)

| Файл | Комментарий |
|------|-------------|
| [`deprecated_plan/bot_ux_implementation_phases.md`](./deprecated_plan/bot_ux_implementation_phases.md) | Фазы 1–4 в коде. |
| [`deprecated_plan/bot_ux_flow_plan.md`](./deprecated_plan/bot_ux_flow_plan.md) | Исходный UX-план. |
| [`deprecated_plan/skater_reports_plan_realize.md`](./deprecated_plan/skater_reports_plan_realize.md) | Реализация skater reports. |
| [`deprecated_plan/skater_reports_plan_v2.md`](./deprecated_plan/skater_reports_plan_v2.md) | Требования v2. |
| [`deprecated_plan/skater_reports_plan.md`](./deprecated_plan/skater_reports_plan.md) | Ранняя версия ТЗ. |
| [`deprecated_plan/tonight_games_plan.md`](./deprecated_plan/tonight_games_plan.md) | `/tonight` и score API — реализовано. |
| [`deprecated_plan/nhl_dataset_build_plan.md`](./deprecated_plan/nhl_dataset_build_plan.md) | Датасет train/predict — реализовано; эталон контракта. |
| [`deprecated_plan/dead_code_cleanup_candidates.md`](./deprecated_plan/dead_code_cleanup_candidates.md) | Чеклист сноса dead code — `close_pool`, `get_goal_video_url` удалены Задачей 27. |

## Связь с `docs/`

- [`docs/architecture.md`](../docs/architecture.md) — дерево проекта.
- Ссылки из планов на архитектуру: `../docs/...`.
