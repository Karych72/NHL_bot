# Задача 127. Окружение и гигиена репозитория: venv, алиасы Makefile, мусор в git

**Блок:** инженерия · **Надобность:** 🟠
**Область:** `Makefile`, `README.md`, `DEVELOPMENT.md`, `.gitignore`, `.dockerignore`,
индекс git (`.claude/worktrees/sdd-tasks-1-3`, `downloads/`).
**Зависит от:** ничего. Делать первой из блока: без рабочего `make ci-local` остальные задачи
не проверить локально.
**Источник:** ревизия качества кода 2026-11-16, просьба человека завести задачи; реестр —
[`../closed_tasks.md`](../closed_tasks.md).

## Кратко

Убрать то, обо что спотыкается каждая сессия: сломанный `.venv`, битую запись в индексе git,
разрушительный алиас `make db-init` и дубли целей Makefile.

## Зачем и на что влияет

- `make ci-local` на ноутбуке не запускается, а `git status` и `git diff` падают. Каждая сессия
  агента тратит шаги на диагностику этого, прежде чем начать работу.
- `make db-init` в документации описан как «применить DDL к пустой БД», а на деле сносит все
  таблицы. Makefile подхватывает `.env`, поэтому на VM это потеря продовой базы одной командой.
- Алиасы целей — две живые ветки одного и того же (Global Constraint 3).

## Степень важности

🟠: подтверждённый риск (`db-init`) и постоянная потеря времени. Задача маленькая, почти целиком
удаление.

## Подробно

**Текущее состояние (снимок 2026-11-16, `origin/develop` = `5a97128`).**
- `.venv/bin/python3.11` — ссылка на `/Library/Frameworks/Python.framework/Versions/3.11`,
  которого на машине нет (venv создан под другим пользователем). Цель `setup` (`Makefile:62`)
  проверяет только архитектуру и битый интерпретатор не замечает.
- В индексе git лежит gitlink `.claude/worktrees/sdd-tasks-1-3` (mode 160000, без
  `.gitmodules`), хотя каталог в `.gitignore`. Из-за него `git diff --stat` падает с
  `fatal: not a git repository: …/worktrees/sdd-tasks-1-3`.
- `Makefile:133` — `db-init: db-reset`, то есть DROP всех таблиц. `README.md:35` и
  `DEVELOPMENT.md:31` подают цель как безопасную.
- Алиасы: `season-load`, `season-reload-current`, `season-update`, `run-local`,
  `db-init-local`. `db-tables-local` нигде не описана, `test-skater-bot` — подмножество
  `test-fast`. `modeling-promote` нет в `.PHONY`.
- `downloads/goal_2025021079_710_A._Ovechkin.mp4` (14,5 МБ) отслеживается git, кодом не
  используется.
- `COPY . .` в `Dockerfile` при текущем `.dockerignore` кладёт в образ `tests/`, `plan/`,
  `agents/`, `docs/`.

**Что делать.**
1. Пересоздать venv: `rm -rf .venv && make setup-dev` (делает человек или агент с его
   разрешения). В цель `setup` добавить проверку, что `$(PY)` запускается; не запускается —
   пересоздать, как при смене архитектуры.
2. `git rm --cached .claude/worktrees/sdd-tasks-1-3`.
3. Удалить `db-init` и `db-init-local`; в `README.md` и `DEVELOPMENT.md` заменить на `db-sync`
   (пустая БД) и явно назвать `db-reset` разрушительной.
4. Удалить алиасы `season-load`, `season-reload-current`, `season-update`, `run-local`,
   цели `db-tables-local` и `test-skater-bot`; вычистить упоминания из документации.
   `modeling-promote` — в `.PHONY`.
5. `git rm downloads/goal_2025021079_710_A._Ovechkin.mp4`.
6. В `.dockerignore` добавить `tests/`, `plan/`, `agents/`, `docs/`, `research_notes/`,
   `reports/` — сначала убедиться, что образ ничего из них не читает.

**Критерии приёмки.**
- `make ci-local` запускается и зелёный на ноутбуке.
- `git status` и `git diff --stat` работают без `--ignore-submodules`.
- `grep -n "db-init\|season-update\|run-local" Makefile README.md DEVELOPMENT.md docs/` пуст.
- Образ собирается, dev-бот стартует после выкатки.

**Подводные камни.**
- Удалённые цели могут вызываться из `deploy/`, `scripts/*.sh`, `docker-compose.yml` и
  workflow — искать по всему репозиторию, не только в Makefile.
- Клип удаляется из дерева, но остаётся в истории; переписывать историю не нужно.

## Открытые вопросы человеку

1. **Корень репозитория:** `bot_users_*.log`, `reports/`, `research_notes/`, `UI_screenshot/`,
   PNG — добавить в `.gitignore` или это рабочие файлы, которые вы собираетесь коммитить?
