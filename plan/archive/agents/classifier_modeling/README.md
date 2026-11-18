# Agents — моделирование прематч-классификаторов NHL

Источник этапов: [`plan/classifier/nhl_classifier_modeling_plan_UPDATE.md`](../../plan/classifier/nhl_classifier_modeling_plan_UPDATE.md).

В каждой **папке этапа** лежат два файла:

| Файл | Роль |
|------|------|
| `executor_tz.md` | исполнитель |
| `reviewer_tz.md` | ревьюер |

Пути в шапках ТЗ (`plan/...`, `modeling/...`) заданы **от корня репозитория**.

## Этапы (папки)

| Папка | Этап UPDATE-плана |
|--------|-------------------|
| [`stage_1_train_input/`](stage_1_train_input/) | 1 — контракт входа для обучения |
| [`stage_2_config/`](stage_2_config/) | 2 — конфигурация обучения |
| [`stage_3_dependencies/`](stage_3_dependencies/) | 3 — зависимости для моделирования |
| [`stage_4_splits/`](stage_4_splits/) | 4 — временные сплиты без утечки |
| [`stage_5_metrics/`](stage_5_metrics/) | 5 — метрики и отчётность |
| [`stage_6_bootstrap/`](stage_6_bootstrap/) | 6 — bootstrap доверительные интервалы |
| [`stage_7_logreg/`](stage_7_logreg/) | 7 — baseline: логистическая регрессия (две модели) |
| [`stage_8_lgbm/`](stage_8_lgbm/) | 8 — primary-модель LightGBM (монотонные ограничения, детерминизм) |
| [`stage_9_calibration/`](stage_9_calibration/) | 9 — калибровка по протоколу без утечки |
| [`stage_10_cli_train/`](stage_10_cli_train/) | 10 — единая точка входа CLI и production-артефакт |
| [`stage_11_tests/`](stage_11_tests/) | 11 — тесты подсистемы моделирования |
| [`stage_12_diagnostics_dod/`](stage_12_diagnostics_dod/) | 12 — диагностика и критерии приёмки (Definition of Done) |
| [`stage_13_docs_sync/`](stage_13_docs_sync/) | 13 — синхронизация документации и ТЗ |

Этапы 14–15 UPDATE-плана — ТЗ добавляются по мере появления (по одной паре `executor_tz.md` / `reviewer_tz.md` на этап).

## Workflow

1. Менеджер/тимлид назначает этап (папку) и MR.
2. Исполнитель ведёт работу по **`executor_tz.md`** в этой папке.
3. Ревьюер проверяет по **`reviewer_tz.md`** той же папки и выносит вердикт.
