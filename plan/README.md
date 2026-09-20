# Планы разработки NHL_bot

Актуальные планы сгруппированы **по темам** в подкаталогах `plan/<тема>/`. **Выполненные или заархивированные как контракт** лежат в [`deprecated_plan/`](./deprecated_plan/) (см. [`deprecated_plan/README.md`](./deprecated_plan/README.md)).

Оперативная документация по архитектуре и API — в [`docs/`](../docs/).

## Статистика и продукт (`stats/`)

| Файл | Статус | Комментарий |
|------|--------|-------------|
| [`stats/team_and_country_stats_plan.md`](./stats/team_and_country_stats_plan.md) | **Не выполнено** | Агрегаты по стране и сравнение команд. |

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
| [`engineering/work_plan_2026-08-08.md`](./engineering/work_plan_2026-08-08.md) | **В процессе** | Действующий исполняемый план; единственный источник истины по задачам, критерии приёмки и команды проверки. |
| [`engineering/work_plan_2026-09-12.md`](./engineering/work_plan_2026-09-12.md) | **В процессе** | Срез остатка; оставшиеся задачи одной таблицей, критерии приёмки — в [`engineering/work_plan_2026-08-08.md`](./engineering/work_plan_2026-08-08.md). |
| [`engineering/work_plan_2026-06-29.md`](./engineering/work_plan_2026-06-29.md) | **История / источник приоритетов** | Заменён [`engineering/work_plan_2026-08-08.md`](./engineering/work_plan_2026-08-08.md). |
| [`engineering/refactoring_plan_3.md`](./engineering/refactoring_plan_3.md) | **В процессе** | Актуальный мастер-план приоритетов (дата среза 2026-05-15), наследует `refactoring_plan_2.md` и `refactoring_plan.md`; один из двух источников приоритетов `work_plan_2026-08-08.md`. |
| [`engineering/refactoring_plan_2.md`](./engineering/refactoring_plan_2.md) | **В процессе** | Срез незакрытых задач рефакторинга. |
| [`engineering/refactoring_plan.md`](./engineering/refactoring_plan.md) | **Частично / история** | Полный чеклист по фазам; для остатка см. [`engineering/refactoring_plan_2.md`](./engineering/refactoring_plan_2.md). |
| [`engineering/db_tests_remediation_plan.md`](./engineering/db_tests_remediation_plan.md) | **Частично выполнено** | Этап A закрыт; CI и прочее — по чеклисту внутри. |

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
