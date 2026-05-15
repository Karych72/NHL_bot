# Agents — тематические ТЗ

Источник фаз и чеклиста: [`plan/engineering/refactoring_plan_2.md`](../plan/engineering/refactoring_plan_2.md).

В каждой **тематической папке** лежат два файла:

| Файл | Роль |
|------|------|
| `executor_tz.md` | исполнитель |
| `reviewer_tz.md` | ревьюер |

Пути в шапках ТЗ (`plan/...`, `telegram_bot/...`) заданы **от корня репозитория**.

## Темы (папки)

| Папка | Соответствие в плане |
|--------|----------------------|
| [`security_critical_bugs/`](security_critical_bugs/) | Фаза 1 — безопасность и критические баги |
| [`infrastructure/`](infrastructure/) | Фаза 2 — инфраструктура |
| [`code_quality/`](code_quality/) | Фаза 3 — качество кода |
| [`architecture/`](architecture/) | Фаза 5 — архитектурные улучшения |
| [`tests_and_documentation/`](tests_and_documentation/) | Фаза 6 — тесты и документация |
| [`ux_and_content/`](ux_and_content/) | Фаза 7 — UX и контент; см. также [`html_parse_mode_polish_tz.md`](ux_and_content/html_parse_mode_polish_tz.md) (дожим HTML после ревью). |

Фазы **4** в плане нет — отдельной папки нет.

## Workflow

1. Менеджер/тимлид назначает тему (папку) и MR.
2. Исполнитель ведёт работу по **`executor_tz.md`** в этой папке.
3. Ревьюер проверяет по **`reviewer_tz.md`** той же папки и выносит вердикт.

По необходимости добавляются **дополнительные ТЗ** в ту же тематическую папку (например целевая дороботка после ревью) — они не заменяют `executor_tz.md`, а уточняют очередную порцию работ.
