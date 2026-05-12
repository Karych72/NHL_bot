# Планы разработки NHL_bot

Актуальные и незавершённые планы — в этом каталоге `plan/`. **Выполненные или заархивированные как контракт** перенесены в [`deprecated_plan/`](./deprecated_plan/) (см. [`deprecated_plan/README.md`](./deprecated_plan/README.md)).

Оперативная документация по архитектуре и API — в [`docs/`](../docs/).

## Актуальные планы (`plan/`)

| Файл | Статус | Комментарий |
|------|--------|-------------|
| [`team_and_country_stats_plan.md`](./team_and_country_stats_plan.md) | **Не выполнено** | Агрегаты по стране и сравнение команд. |
| [`refactoring_plan_2.md`](./refactoring_plan_2.md) | **В процессе** | Срез незакрытых задач рефакторинга. |
| [`refactoring_plan.md`](./refactoring_plan.md) | **Частично / история** | Полный чеклист по фазам; для остатка см. [`refactoring_plan_2.md`](./refactoring_plan_2.md). |
| [`db_tests_remediation_plan.md`](./db_tests_remediation_plan.md) | **Частично выполнено** | Этап A закрыт; CI и прочее — по чеклисту внутри. |
| [`dead_code_cleanup_candidates.md`](./dead_code_cleanup_candidates.md) | **Не выполнено** | Найденный, но не удалённый dead code (eager-helpers в loader, `close_pool`, `get_goal_video_url`). Чеклист на снос. |
| [`nhl_classifier_modeling_plan.md`](./nhl_classifier_modeling_plan.md) | **Проектирование / не выполнено** | Обучение и валидация прематч-классификаторов (модели вне датасетного билдера). |
| [`agent_executor_dataset_tz.md`](./agent_executor_dataset_tz.md) | **ТЗ процесса** | Промпт для агента-исполнителя датасета; контракт — [`deprecated_plan/nhl_dataset_build_plan.md`](./deprecated_plan/nhl_dataset_build_plan.md). |
| [`agent_reviewer_dataset_tz.md`](./agent_reviewer_dataset_tz.md) | **ТЗ процесса** | Промпт для агента-ревьюера датасета. |
| [`agent_prompt_templates_dataset.md`](./agent_prompt_templates_dataset.md) | **Шпаргалка** | Готовые промпты Cursor для executor/reviewer цикла. |

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
