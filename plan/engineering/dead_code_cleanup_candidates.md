# Кандидаты на удаление: dead code

Список найденного, но **не удалённого** во время ревизии 2026-05-02. Каждый
пункт подтверждён `rg`-ом по всему репо, но удаление потенциально ломает
неявные контракты (публичный API, документированное поведение), поэтому
выведено в отдельный чеклист — нужен явный go перед `rm`.

См. контекст ревизии: чат «Проведи ревизию, удали ненужное и неиспользуемое»
от 2026-05-02 (доку и инфраструктуру `pipeline / dirs / run-pipeline / run-full`
вычистил, дополнительно удалил `test_goal_replay.py`, `telegram_bot/sql_query.py`
и константу `PLAYER_SHOT_TYPES_MENU`).

---

## 1. Eager-default хелперы в `pipeline/load_season_modern.py`

**Что:** 5 функций, на которые в коде нет ни одного вызова. `to_int` (тоже из
этой «семьи») активно используется и остаётся.

| Функция | Строка | Подтверждение |
|---|---|---|
| `to_float(value, default=0.0)` | `pipeline/load_season_modern.py:50` | `rg '\bto_float\b' --type py` → только декларация + ссылка из `pct_from_ratio`, других вызовов нет. |
| `pct_from_ratio(value, default=0.0)` | `pipeline/load_season_modern.py:59` | `rg '\bpct_from_ratio\b' --type py` → только декларация + комментарий в `bot_messages.py:226`. |
| `split_sv(value)` | `pipeline/load_season_modern.py:70` | `rg '\bsplit_sv\b' --type py` → только декларация. Используется `optional_split_sv`. |
| `seconds_to_mmss(value, default="00:00")` | `pipeline/load_season_modern.py:77` | `rg '\bseconds_to_mmss\b' --type py` → только декларация. Используется `optional_seconds_to_mmss`. |
| `age_from_birthdate(birth_date)` | `pipeline/load_season_modern.py:88` | `rg '\bage_from_birthdate\b' --type py` → только декларация. Используется `optional_age_from_birthdate`. |

**Почему страшно удалять без обсуждения:**

- В `docs/pipeline_nulls_and_explicit_null_tz.md` §1 они задокументированы
  как часть «семьи eager-default'ов» рядом с `to_int`. Контракт обещает: «если
  понадобится eager default — используй эти». Удаление кода = удаление контракта.
- В `bot_messages.py:226` в комментарии: `"""Доля очков в БД уже в шкале 0–100
  (см. pct_from_ratio в пайплайне)."""` — комментарий устареет.
- В `plan/deprecated_plan/skater_reports_plan*.md` есть исторические упоминания —
  трогать их не надо (это снимки старого ТЗ), но заметка для grep-аудита.

**Что нужно сделать вместе с удалением:**

1. Убрать эти 5 `def`-ов из `pipeline/load_season_modern.py` (строки 50–96 диапазона coercion-helpers).
2. В шапке coercion-блока (`pipeline/load_season_modern.py:23-38`) переписать комментарий: оставить только одну «семью» `optional_*` + `to_int` + `safe_pct`.
3. В `docs/pipeline_nulls_and_explicit_null_tz.md`:
   - §1 таблица: убрать строку про `to_float` / `pct_from_ratio` / `split_sv` / `seconds_to_mmss` / `age_from_birthdate`. Оставить только `to_int` и `safe_pct`.
   - §4 «Архив» (свёрнутая `<details>`-таблица): оставить как есть — это исторический срез до рефакторинга.
4. В `telegram_bot/bot_messages.py:226` обновить комментарий: «Доля очков в БД уже в шкале 0–100 (нормализуется в пайплайне через `optional_pct_from_ratio`).»

**Тесты:** `tests/test_pipeline_optional_helpers.py` использует только `optional_*` и `safe_pct` (см. сам файл). На удаление 5 eager-функций не повлияет.

---

## 2. `telegram_bot/database.py::close_pool()`

**Что:** строки `database.py:137-142`. Корректное закрытие
`SimpleConnectionPool` при graceful shutdown.

```bash
rg '\bclose_pool\b' --type py /Users/petrkarol/Desktop/projects/NHL_bot
```

→ только декларация в `database.py` и упоминание в `docs/architecture.md` («Функция
`close_pool()` для корректного завершения»).

**Почему страшно удалять:**

- Это публичный API модуля `database`. Если кто-то когда-нибудь добавит
  signal-handler / `atexit`-хук, `close_pool()` — естественное место.
- Размер крошечный (6 строк), польза от удаления сомнительная.

**Альтернативы:** оставить, но прикрутить `atexit.register(close_pool)` в
`bot.py` (отдельный таск, не часть этой чистки).

---

## 3. `telegram_bot/video_replay.py::get_goal_video_url()`

**Что:** строки `video_replay.py:135-140`. Возвращает прямой MP4-URL без
скачивания файла.

```bash
rg '\bget_goal_video_url\b' --type py /Users/petrkarol/Desktop/projects/NHL_bot
```

→ только декларация. Production-путь идёт через `download_goal_video()` в
`stats_handlers.py:34`.

**Почему страшно удалять:**

- Лёгкая альтернатива `download_goal_video` без записи на диск — может пригодиться, если когда-нибудь начнём отдавать `bot.send_video(url=...)` напрямую (Telegram это поддерживает для коротких клипов).
- Не зовёт `ffmpeg` / `tempfile`, поэтому это простой публичный API «дай мне ссылку».

**Если удалять — что ещё снести по цепочке:** в текущем виде ничего, функция
переиспользует `_get_brightcove_clip_id` и `_get_mp4_url`, оба нужны для
`download_goal_video`.

---

## Чеклист исполнения (после go от автора)

- [x] §1: удалить 5 eager-helper'ов из `load_season_modern.py`, обновить комментарий-шапку.
- [x] §1: обновить `docs/pipeline_nulls_and_explicit_null_tz.md` §1.
- [x] §1: поправить комментарий в `telegram_bot/bot_messages.py:226`.
- [x] §1: прогнать `make test-fast`.
- [ ] §2: удалить `close_pool()` из `database.py`, убрать упоминание из `docs/architecture.md`.
- [ ] §3: удалить `get_goal_video_url()` из `video_replay.py`.
- [ ] Прогнать `make test-fast` после каждого пункта.
- [ ] Удалить этот файл (`plan/engineering/dead_code_cleanup_candidates.md`) и ссылку в `plan/README.md`.
