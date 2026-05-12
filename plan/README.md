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
| [`engineering/refactoring_plan_2.md`](./engineering/refactoring_plan_2.md) | **В процессе** | Срез незакрытых задач рефакторинга. |
| [`engineering/refactoring_plan.md`](./engineering/refactoring_plan.md) | **Частично / история** | Полный чеклист по фазам; для остатка см. [`engineering/refactoring_plan_2.md`](./engineering/refactoring_plan_2.md). |
| [`engineering/db_tests_remediation_plan.md`](./engineering/db_tests_remediation_plan.md) | **Частично выполнено** | Этап A закрыт; CI и прочее — по чеклисту внутри. |
| [`engineering/dead_code_cleanup_candidates.md`](./engineering/dead_code_cleanup_candidates.md) | **Не выполнено** | Найденный, но не удалённый dead code (eager-helpers в loader, `close_pool`, `get_goal_video_url`). Чеклист на снос. |

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

## Связь с `docs/`

- [`docs/architecture.md`](../docs/architecture.md) — дерево проекта.
- Ссылки из планов на архитектуру: `../docs/...`.
