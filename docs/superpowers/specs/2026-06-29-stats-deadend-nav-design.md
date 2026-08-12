# Дизайн: закрытие краевых «тупиков» навигации в `/stats` (задача 7.1)

**Дата:** 2026-06-29
**Источник задачи:** `plan/engineering/work_plan_2026-06-29.md` (строка 1, задача **7.1**, P0) и
`plan/engineering/refactoring_plan_3.md` (критерий готовности 3.1).
**Статус scope:** подтверждён пользователем — только краевые ветки внутри conversation `/stats`;
standalone-карточки (`/game`, `/tonight`, `/day_games`, `/today`, `/start game_…`) **вне scope**.

## Результат верификации (перед работой)

План помечал 7.1 как открытый P0, но требовал перепроверки в коде (датирован 2026-05-15).
Проверка `telegram_bot/stats_handlers.py`, `script_bot.py`, `bot.py` показала: **глобальный «тупик»
уже закрыт**. Навигацию имеют все основные ветки:

- меню (root / players / field / advanced / goalie / team / digest-date) — кнопка «Назад» → `CHOOSE_STATS`;
- лидерборды игроков/команд и пагинация (`st:` / `tm:`) — `_stats_menu_nav_row()` («В начало» / «Готово»);
- турнирная таблица (`bot_league_standings`) — «Главное меню»;
- дайджест дня (today / yesterday / custom) — `dispatch_day_digest_messages(attach_conv_nav_on_last=True)`;
- завершение/возврат (`end`, `stats_over`).

Остались **узкие краевые дыры**:

1. **Карточка матча из «Матч N» сводки дайджеста** (`callback_expand_digest_game` → `send_game_card_message`):
   когда у матча нет кнопок видео голов — `markup=None` (экран «только текст»); даже с gv-кнопками нет
   возврата в меню `/stats`.
2. **Ветка «Такого матча нет в базе бота.»** в `send_game_card_message` — отправляется голым текстом,
   параметр `reply_markup` в этой ветке игнорируется.
3. (Сопутствующее) Ветка «Некорректная ссылка на матч.» в обработчике разворота — тоже текст без клавиатуры.

## Ключевое ограничение

Кнопки «Матч N» (callback `dg:<id>`) генерит `dispatch_day_digest_messages`, вызываемая из **двух
контекстов**:

- **внутри `/stats`** — `attach_conv_nav_on_last=True`, conversation жив в состоянии `SECOND`;
- **standalone** — `/day_games`, `/today`, push-джобы, `attach_conv_nav_on_last=False`, conversation нет.

Обработчик `callback_expand_digest_game` (`dg:`) **один на оба** и зарегистрирован в standalone-группе
(`group=-1`). Навигация использует callback'и `CHOOSE_STATS` / `END_CONVERSATION`, которые ловятся
**только в состоянии `SECOND`** conversation. Повесить их на standalone-карточку нельзя — кнопки молча
не сработают (новый псевдо-тупик), и в PTB v13 общий `dg:` мог бы быть пойман и standalone-, и
conversation-обработчиком одновременно (двойные сообщения). Поэтому пути нужно **развести по разным
callback-префиксам**.

## Принятый подход

Развести conversation-дайджест и standalone-дайджест на разные callback-префиксы.

- В `/stats`-контексте сводка эмитит кнопки «Матч N» с новым префиксом **`dgc:<id>`**.
- Standalone остаётся на `dg:<id>` (там уже есть текстовая подсказка «Ещё: /stats — меню…»).
- Новый conversation-обработчик `dgc:` в состоянии `SECOND` шлёт карточку матча **с навигационным рядом**.

*Отвергнутый вариант:* вешать nav в общий `dg:`-обработчик — проще на одну функцию, но даёт неработающие
кнопки в standalone и риск двойной обработки. Не подходит.

## Компоненты (что меняется)

### `telegram_bot/stats_handlers.py`

- Новая константа `DIGEST_EXPAND_CONV_PREFIX = "dgc:"`.
- `send_game_card_message(...)`: заменить **никогда не используемый** параметр `reply_markup` на
  `nav_row: Optional[List[InlineKeyboardButton]] = None`:
  - ветка «матча нет в базе» → прикрепляет `InlineKeyboardMarkup([nav_row])`, если `nav_row` задан (закрывает дыру 2);
  - ветка «матч есть» → строит `build_menu(gv-кнопки, n_cols=1)` и, если `nav_row` задан, добавляет его
    последним рядом (закрывает дыру 1, gv-кнопки сохраняются).
- Новый обработчик `callback_expand_digest_game_conv(update, context) -> int`:
  - проверяет префикс `DIGEST_EXPAND_CONV_PREFIX`, `query.answer()`;
  - парсит `dgc:<id>`; при ошибке парсинга шлёт «Некорректная ссылка на матч.» с `nav_row`;
  - иначе `send_game_card_message(..., nav_row=_stats_menu_nav_row())`;
  - всегда возвращает `SECOND`.
- `dispatch_day_digest_messages`: префикс кнопок «Матч N» выбирается по флагу —
  `DIGEST_EXPAND_CONV_PREFIX` при `attach_conv_nav_on_last=True`, иначе `DIGEST_EXPAND_PREFIX`.

### `telegram_bot/bot.py`

- Импортировать `DIGEST_EXPAND_CONV_PREFIX` и `callback_expand_digest_game_conv`.
- В состоянии `SECOND` conversation зарегистрировать
  `CallbackQueryHandler(callback_expand_digest_game_conv, pattern=r"^dgc:\d+$")`.

## Поток данных

`/stats` → «Статистика дня» → today/yesterday → `dispatch_day_digest_messages(attach=True)` → сводка с
`dgc:`-кнопками (conversation в `SECOND`) → клик «Матч N» → `callback_expand_digest_game_conv` → карточка
**с «В начало» / «Готово»** → клик ловится в `SECOND` (`stats_over` / `end`). Тупик закрыт.

## Проверки безопасности рефактора

- Параметр `reply_markup` у `send_game_card_message` сейчас **нигде не передаётся** (все 4 вызова —
  с дефолтом: `callback_tonight_game`, `callback_expand_digest_game`, `cmd_game`, `cmd_start`),
  поэтому замена на `nav_row` ничего не ломает.
- Префикс `dgc:` не коллизирует с `^dg:\d+$` (после `dg` шаблон ждёт `:`, а в `dgc:` идёт `c`).
- Standalone-путь (`dg:`, `callback_expand_digest_game`) не изменяется — нет регрессии `/day_games`,
  `/today`, push-джобов.

## Тестирование (TDD)

Инфраструктура: `tests/conftest.py` + unittest с `unittest.mock` (без БД, модули загружаются с
`os.chdir(telegram_bot)`).

Тест-кейсы:

1. Карточка существующего матча через `callback_expand_digest_game_conv` (мок `game_exists=True`,
   `game_message`) → `context.bot.send_message` вызван с `reply_markup`, содержащим ряд с
   `str(CHOOSE_STATS)` и `str(END_CONVERSATION)`.
2. gv-кнопки сохраняются вместе с nav-рядом (мок `game_message` возвращает goals_meta).
3. Ветка «матча нет в базе» (`game_exists=False`) → `reply_markup` не `None`, содержит nav-ряд.
4. `dispatch_day_digest_messages` с несколькими матчами: `attach_conv_nav_on_last=True` → кнопки с
   `dgc:`; `attach_conv_nav_on_last=False` → кнопки с `dg:` (защита от регрессии standalone).
5. Обработчик возвращает `SECOND` во всех ветках.

## Критерий готовности (из refactoring_plan_3, 3.1)

После разворота карточки матча из дайджеста внутри `/stats` пользователь видит клавиатуру с выходом
назад / в главное меню; нет экрана «только текст». Standalone-сценарии сохраняют прежнее поведение.
