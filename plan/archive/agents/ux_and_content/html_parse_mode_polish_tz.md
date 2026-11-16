# ТЗ исполнителю: HTML-разметка до «чистого approve» Фазы 7

**Роль:** исполнитель (дополнительное ТЗ к паре `executor_tz.md` / `reviewer_tz.md`).  
**Цель:** закрыть nit **7.7** — пользовательские ответы бота с `parse_mode` согласовать на **HTML**, с `html.escape()` для любых строк из API/БД/пользователя.  
**Уже сделано:** меню `/stats` и выбор даты дайджеста в [`telegram_bot/script_bot.py`](../../telegram_bot/script_bot.py) переведены на `parse_mode="HTML"`.

---

## 1. Объём работ

| Область | Файлы | Действие |
|---------|--------|---------|
| `/start`, `/help` | [`telegram_bot/help_text.py`](../../telegram_bot/help_text.py), [`telegram_bot/bot.py`](../../telegram_bot/bot.py) | Переписать константы сообщений на HTML (`<b>`, `<i>`, `<code>`). Убрать markdown-экранирования вида `\/day\_games` — в HTML команды давать в `<code>/day_games</code>`. В `bot.py` заменить `parse_mode="MARKDOWN"` на `"HTML"` для этих ответов. |
| `/advanced` интро | `help_text.ADVANCED_COMMAND_INTRO`, `ADVANCED_STATS_EXPLAINED` | То же: HTML, без сырого `*` из Markdown. |
| `/team` — список аббревиатур | [`telegram_bot/bot_messages.py`](../../telegram_bot/bot_messages.py) `season_team_abbrev_help_text()` | Вернуть HTML: заголовок `<b>`, список команд — в `<code>` с **поэлементным** `html.escape` для аббревиатур из БД (на случай спецсимволов). Footer без `day\_games`. |
| `/game` без аргументов | [`telegram_bot/bot.py`](../../telegram_bot/bot.py) `cmd_game` | Одна строка-подсказка в HTML + `<code>/day_games</code>`. |
| `/tonight` | [`telegram_bot/nhl_scoreboard.py`](../../telegram_bot/nhl_scoreboard.py) `tonight_reply_intro`, [`telegram_bot/bot.py`](../../telegram_bot/bot.py) `cmd_tonight` | Заменить Markdown на HTML; дату лиги `current` и числа — подставлять через `html.escape(str(...))`. Обновить docstring функции. |

**Вне объёма (по желанию следующей итерации):** второе сообщение после турнирной таблицы из меню [`stats_handlers.bot_league_standings`](../../telegram_bot/stats_handlers.py) — UX-nit из `reviewer_tz.md`.

---

## 2. Приёмка

- [ ] По проекту не осталось `parse_mode="Markdown"` / `"MARKDOWN"` в путях, где текст строится из БД или NHL API (grep по `telegram_bot/`).
- [ ] Статический HTML в `help_text` валиден для Telegram: нет неэкранированных `&`, `<`, `>` от динамики; сезон/`CURRENT_SEASON` при подстановке в строку — через `html.escape`, если добавляете интерполяцию.
- [ ] Тесты: обновить [`tests/test_skater_reports_bot.py`](../../tests/test_skater_reports_bot.py) (или добавить кейсы), чтобы проверяемые подстроки в `HELP_MESSAGE` соответствовали HTML (`/day_games` без бэкслешей и т.д.).
- [ ] Ручная проверка: `/help`, `/tonight`, `/team`, `/stats`, `/advanced` открываются без ошибки парсинга и без «сломанного» форматирования.

---

## 3. Ревью

После мержа — повторная точка **`reviewer_tz.md`**, п. 3 (таблицы/HTML) и nit **6.3.6**; целевой вердикт: **approve** без оговорки по фрагментарному 7.7.
