"""Клавиатуры пагинации и callback-хендлеры статистики: меню и таблицы `/stats`,
`/advanced` и `/team_stats`, дайджест дня, `/countries`, кнопка матча `/tonight`.

Часть `telegram_bot/`, который читает БД (заполненную `pipeline/`) и рисует меню поверх
неё: здесь — листание длинных таблиц статистики через inline-кнопки и отправка карточек
матчей и дайджестов, не помещающихся в одно сообщение целиком.
"""

import asyncio
import html
import logging
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from bot_messages import (
    PLAYER_GROUPS,
    COUNTRY_LABELS,
    DIGEST_GAME_SEPARATOR,
    LEADERBOARD_PAGE_SIZE,
    country_game_block,
    country_page,
    country_rankings,
    day_digest_summary_body,
    digest_game_button_labels,
    digest_shown_match_count,
    game_exists,
    game_message,
    matchup_season_preview,
    player_stat_leaderboard_page,
    rich_footer,
    rich_message,
    team_stat_leaderboard_page,
    truncate_telegram_text,
    truncation_marker,
)
from keyboards import build_menu, callback_message
from help_text import (
    ADVANCED_COMMAND_INTRO,
    STATS_COMMAND_INTRO,
    SUBSCRIPTIONS_FOOTER,
    TEAM_STATS_COMMAND_INTRO,
)
from subscription_repo import get_chat_subscriptions
from leaderboard_specs import (
    ADVANCED_METRICS,
    ADVANCED_ROWS,
    MAIN_STAT_ROWS,
    PLAYER_STAT_TITLES,
    TEAM_STAT_ROWS,
    TEAM_STAT_SPECS,
)
from video_replay import cached_goal_clip, download_goal_video, save_goal_clip

logger = logging.getLogger(__name__)

DIGEST_EXPAND_PREFIX = "dg:"
DIGEST_EXPAND_CALLBACK_PATTERN = rf"^{DIGEST_EXPAND_PREFIX}(\d+)$"
# /tonight: кнопка матча tn:<game_id>:<away>:<home>
TONIGHT_GAME_CALLBACK_PATTERN = r"^tn:(\d+):([^:]+):([^:]+)$"

# Статистика по странам (/countries, Задача 49): cn:<CODE>:<F|D|G>:<offset> —
# страница группы игроков страны, cn:list — назад к рейтингу стран
COUNTRY_CALLBACK_PATTERN = r"^cn:(?:list|([A-Z]{3}):([FDG]):(\d+))$"

# Таблицы /stats (st), /advanced (sa) и /team_stats (tm): <префикс>:<ключ>:<offset> — страница,
# <префикс>:menu — меню команды, <префикс>:save — копия страницы ниже. Ключ у st и sa —
# <таблица>:<колонка> из PLAYER_STAT_TITLES, у tm — ключ TEAM_STAT_SPECS (lookbehind по префиксу).
_PLAYER_STAT_KEYS = "|".join(f"{table}:{column}" for table, column in PLAYER_STAT_TITLES)
_TEAM_STAT_KEYS = "|".join(TEAM_STAT_SPECS)
STAT_SCREEN_CALLBACK_PATTERN = (
    rf"^(st|sa|tm):(?:(menu|save)|((?<=st:|sa:)(?:{_PLAYER_STAT_KEYS})|(?<=tm:)(?:{_TEAM_STAT_KEYS})):(\d+))$"
)

# Меню /advanced: adv:<ключ ADVANCED_METRICS> открывает статистику, adv:close снимает кнопки
ADVANCED_CALLBACK_PATTERN = rf"^adv:({'|'.join(ADVANCED_METRICS)}|close)$"

# Кнопка видео гола карточки матча: gv:<game_id>:<event_id>
GOAL_VIDEO_CALLBACK_PATTERN = r"^gv:(\d+):(\d+)$"


def _page_nav_rows(
    prefix: str, offset: int, has_prev: bool, has_next: bool
) -> List[List[InlineKeyboardButton]]:
    """Ряд листания таблиц: «« 1–10» (с третьей страницы и дальше), «← 11–20»,
    «31–40 →» — подпись называет места, на которые ведёт кнопка. Пустой
    список, если соседних страниц нет.

    Args:
        prefix: callback_data без смещения — кнопки несут `{prefix}:{offset}`.
        offset: смещение текущей страницы (шаг — `LEADERBOARD_PAGE_SIZE`).
        has_prev, has_next: есть ли соседняя страница в эту сторону.
    """
    size = LEADERBOARD_PAGE_SIZE

    def span(off: int) -> str:
        return f"{off + 1}–{off + size}"

    nav: List[InlineKeyboardButton] = []
    # Со второй страницы «назад» и так ведёт на 1–10 — отдельная кнопка не нужна.
    if offset >= 2 * size:
        nav.append(InlineKeyboardButton(text=f"« {span(0)}", callback_data=f"{prefix}:0"))
    if has_prev:
        prev_off = max(0, offset - size)
        nav.append(InlineKeyboardButton(text=f"← {span(prev_off)}", callback_data=f"{prefix}:{prev_off}"))
    if has_next:
        next_off = offset + size
        nav.append(InlineKeyboardButton(text=f"{span(next_off)} →", callback_data=f"{prefix}:{next_off}"))
    return [nav] if nav else []


async def _edit_page(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовывает листаемую страницу (rich-сообщение) на месте сообщения с нажатой кнопкой.

    Зачем: повторное нажатие кнопки текущей страницы Telegram отвергает «message is
    not modified» — это штатный случай, он глушится; любая другая ошибка всплывает.
    Общая для всех листаемых экранов: таблиц `/stats`, `/advanced`, `/team_stats`, `/countries`.

    Аргументы: `callback` — нажатие, `text` — HTML страницы, `markup` — её клавиатура.
    """
    try:
        await callback_message(callback).edit_text(rich_message=rich_message(text), reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


def stats_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню `/stats` одним экраном: основные метрики полевых и вратарей, затем
    расширенные и голы по типу броска (`MAIN_STAT_ROWS`, `ADVANCED_ROWS`). Кнопка —
    `st:<таблица>:<колонка>:0`; первый ряд (очки, голы, передачи) — самые частые
    вопросы, он выделен цветом — единственный акцент меню."""
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"st:{table}:{column}:0",
                style="primary" if i == 0 else None,
            )
            for label, (table, column) in row
        ]
        for i, row in enumerate(MAIN_STAT_ROWS)
    ]
    rows += [
        [
            InlineKeyboardButton(text=label, callback_data="st:{}:{}:0".format(*ADVANCED_METRICS[key]))
            for label, key in row
        ]
        for row in ADVANCED_ROWS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def advanced_standalone_keyboard() -> InlineKeyboardMarkup:
    """Меню `/advanced`: расширенные метрики и голы по типам бросков (`ADVANCED_ROWS`).
    Каждая кнопка — `adv:<key>`, который разбирает `callback_standalone_adv`."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"adv:{key}") for label, key in row]
        for row in ADVANCED_ROWS
    ])


def team_stats_keyboard() -> InlineKeyboardMarkup:
    """Меню `/team_stats` (`TEAM_STAT_ROWS`): кнопка — `tm:<ключ TEAM_STAT_SPECS>:0`."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"tm:{key}:0") for label, key in row]
        for row in TEAM_STAT_ROWS
    ])


# Префикс callback_data таблицы → текст и клавиатура меню её команды («« К списку»).
_STAT_MENUS: Dict[str, Tuple[str, Callable[[], InlineKeyboardMarkup]]] = {
    "st": (STATS_COMMAND_INTRO, stats_menu_keyboard),
    "sa": (ADVANCED_COMMAND_INTRO, advanced_standalone_keyboard),
    "tm": (TEAM_STATS_COMMAND_INTRO, team_stats_keyboard),
}


def stat_page_reply(prefix: str, key: str, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Страница таблицы `/stats`, `/advanced` или `/team_stats` и её клавиатура.

    Зачем одна на три команды: страницы различаются только данными и меню, куда ведёт
    «« К списку»; листание (`_page_nav_rows`) и «Сохранить» у них общие.

    Аргументы:
        prefix: `st`, `sa` или `tm` — команда, из меню которой открыта таблица.
        key: у `st`/`sa` — `<таблица>:<колонка>` (ключ `PLAYER_STAT_TITLES`), у `tm` —
            ключ `TEAM_STAT_SPECS`.
        offset: сдвиг страницы.
    """
    if prefix == "tm":
        title, columns = TEAM_STAT_SPECS[key]
        text, has_prev, has_next = team_stat_leaderboard_page(title, columns, offset)
    else:
        table, column = key.split(":")
        text, has_prev, has_next = player_stat_leaderboard_page(
            PLAYER_STAT_TITLES[(table, column)], table, column, offset
        )
    rows = _page_nav_rows(f"{prefix}:{key}", offset, has_prev, has_next)
    rows.append([
        InlineKeyboardButton(text="« К списку", callback_data=f"{prefix}:menu"),
        InlineKeyboardButton(text="Сохранить", callback_data=f"{prefix}:save"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_stat_screen(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Кнопки таблиц `/stats`, `/advanced` и `/team_stats` (`match` — совпадение
    `STAT_SCREEN_CALLBACK_PATTERN`): `<префикс>:menu` — меню команды на месте страницы;
    `<префикс>:save` — страница остаётся в чате без кнопок, её копия с теми же кнопками
    уходит ниже; `<префикс>:<ключ>:<offset>` — страница таблицы (ключ уже проверен паттерном).
    """
    prefix, action, key, offset = match.groups()
    message = callback_message(callback)
    if action == "save":
        await callback.answer("Сохранено")
        # Сначала копия, потом снятие кнопок: упади отправка — страница останется живой.
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=message.reply_markup,
        )
        await message.edit_reply_markup(reply_markup=None)
        return
    await callback.answer()
    if action == "menu":
        intro, keyboard = _STAT_MENUS[prefix]
        await _edit_page(callback, intro, keyboard())
        return
    text, markup = stat_page_reply(prefix, key, int(offset))
    await _edit_page(callback, text, markup)


async def callback_standalone_adv(callback: CallbackQuery, match: re.Match) -> None:
    """Обрабатывает нажатие кнопки меню `/advanced` (`adv:<key>`, `match` — совпадение
    `ADVANCED_CALLBACK_PATTERN`): `adv:close` снимает клавиатуру, иначе по ключу из
    `ADVANCED_METRICS` открывается первая страница этой статистики — дальше она
    листается уже как `sa:...` (см. `stat_page_reply`).
    """
    await callback.answer()
    key = match.group(1)
    if key == "close":
        await callback_message(callback).edit_reply_markup(reply_markup=None)
        return
    text, kb = stat_page_reply("sa", "{}:{}".format(*ADVANCED_METRICS[key]), 0)
    await callback_message(callback).edit_text(rich_message=rich_message(text), reply_markup=kb)


def country_rankings_reply() -> Tuple[str, InlineKeyboardMarkup]:
    """Рейтинг стран сезона и сетка кнопок `cn:<CODE>:F:0` — по одной на страну,
    прошедшую порог (Задача 49, команда /countries). Пустой сезон — текст без
    кнопок (Задача 36)."""
    text, codes = country_rankings()
    buttons = [
        InlineKeyboardButton(text=COUNTRY_LABELS.get(code, code), callback_data=f"cn:{code}:F:0")
        for code in codes
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=build_menu(buttons, n_cols=2))


def country_page_reply(code: str, group: str, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Экран страны `/countries`: страница группы игроков (`country_page`), листание,
    переключатель групп (нападающие / защитники / вратари) и «« Страны».

    Аргументы: `code` — код страны, `group` — ключ `PLAYER_GROUPS`, `offset` — сдвиг страницы.
    """
    text, has_prev, has_next = country_page(code, group, offset)
    rows = _page_nav_rows(f"cn:{code}:{group}", offset, has_prev, has_next)
    rows.append([
        InlineKeyboardButton(
            text=("• " if key == group else "") + label, callback_data=f"cn:{code}:{key}:0"
        )
        for key, (label, _cond) in PLAYER_GROUPS.items()
    ])
    rows.append([InlineKeyboardButton(text="« Страны", callback_data="cn:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_country(callback: CallbackQuery, match: re.Match) -> None:
    """Кнопки /countries: `cn:list` — снова рейтинг стран, `cn:<CODE>:<G>:<off>` —
    страница группы игроков страны с листанием, переключателем групп
    (нападающие / защитники / вратари) и «« Страны». Код вне рейтинга сезона
    `country_page()` сама превращает в текст «Страна не найдена…».
    `match` — совпадение `COUNTRY_CALLBACK_PATTERN`."""
    await callback.answer()
    if match.group(1) is None:
        text, markup = country_rankings_reply()
    else:
        text, markup = country_page_reply(match.group(1), match.group(2), int(match.group(3)))
    await _edit_page(callback, text, markup)


# --- Day digest: один матч — полная карточка; несколько — сводка + кнопки матчей ---

# Подсказка в подвале последнего сообщения дайджеста (из /today, ИИ и утренней рассылки).
_DIGEST_MORE_HINT = (
    "Ещё: /tonight — расписание NHL, /standings — таблица, /stats — лидеры игроков, "
    "/team_stats — статистика команд, /team — команды, /help — справка.\n"
    + SUBSCRIPTIONS_FOOTER
)

# Кнопки видео голов («▶ 1:0 Nelson 1п 06:33») в два столбца: столбик из десятка
# кнопок на всю ширину визуально сливался со следующей карточкой матча.
_GOAL_VIDEO_COLUMNS = 2


def _goal_video_buttons(goals_meta: List[Dict]) -> List[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=g['label'],
            callback_data=f"gv:{g['game_id']}:{g['event_id']}",
        )
        for g in goals_meta
    ]


def goal_video_markup(goals_meta: List[Dict]) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура карточки матча: кнопки видео голов в `_GOAL_VIDEO_COLUMNS` столбца.

    Зачем: одна раскладка для карточки из меню и из рассылки подписки на команду.

    Аргументы: ``goals_meta`` — вторая часть ответа ``game_message()``.
    Без голов — ``None`` (сообщение без клавиатуры).
    """
    gbtn = _goal_video_buttons(goals_meta)
    return InlineKeyboardMarkup(inline_keyboard=build_menu(gbtn, n_cols=_GOAL_VIDEO_COLUMNS)) if gbtn else None


async def send_game_card_message(bot: Bot, chat_id: int, game_id: int) -> Message:
    """Полная карточка матча с кнопками видео голов; возвращает отправленное
    сообщение. Подписчику страны под карточкой — блок игроков каждой его страны,
    сыгравших в матче (Задача 76)."""
    if not game_exists(game_id):
        return await bot.send_rich_message(
            chat_id=chat_id, rich_message=rich_message("Такого матча нет в базе бота.")
        )
    text, goals_meta = game_message(game_id)
    for country in get_chat_subscriptions(chat_id).countries:
        block = country_game_block(country, game_id)
        if block is not None:
            text += f"\n\n{block}"
    return await bot.send_rich_message(
        chat_id=chat_id, rich_message=rich_message(text), reply_markup=goal_video_markup(goals_meta),
    )


async def dispatch_day_digest_messages(
    bot: Bot,
    chat_id: int,
    day_label: Optional[str],
    games: List[Tuple[int, str, List[Dict]]],
    *,
    inter_message_sleep_sec: Optional[float] = None,
    heading: str = "Матчи ночи",
) -> None:
    """Дайджест дня: один матч — одна карточка; несколько — сводка + кнопки разворота.
    Подсказка «Ещё: …» и ссылка на подписки — подвал этого же сообщения, а не отдельное
    сообщение: служебное тише содержания (`docs/telegram_bot.md` §«Стиль сообщений»).

    Аргументы:
        bot, chat_id: куда слать.
        day_label, games: ответ `day_digest()`.
        heading: заголовок сводки (дата и число матчей — в подвале); рассылка без уже
            присланных чату матчей — «Остальные матчи ночи» (Задача 85).
        inter_message_sleep_sec: пауза после каждого сообщения (рассылка).
    """

    async def _pause() -> None:
        if inter_message_sleep_sec is not None and inter_message_sleep_sec > 0:
            await asyncio.sleep(inter_message_sleep_sec)

    hint = rich_footer(_DIGEST_MORE_HINT)
    real_games = [(gid, text, meta) for gid, text, meta in games if gid != 0]

    if not real_games:
        _, text, _ = games[0]
        safe_text = html.escape(text) if text else ""
        await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message(f"{safe_text}\n{hint}"))
        await _pause()
        return

    if len(real_games) == 1:
        _gid, text, goals_meta = real_games[0]
        await bot.send_rich_message(
            chat_id=chat_id, rich_message=rich_message(f"{text}\n{hint}"),
            reply_markup=goal_video_markup(goals_meta),
        )
        await _pause()
        return

    day_str = day_label or "—"
    game_ids = [gid for gid, _t, _m in real_games]
    lines = day_digest_summary_body(game_ids)
    header = f"<b>{html.escape(heading)}</b>\n\n"
    footer = (
        f"Игровой день {html.escape(day_str)} · матчей: {len(real_games)}\n"
        f"Кнопка матча — полная карточка и видео голов.\n{_DIGEST_MORE_HINT}"
    )
    # Матч сводки несёт HTML-теги: резать его по символам нельзя — Telegram
    # отвергнет незакрытый тег. Не влезло — оставляем целые матчи, сколько
    # уместилось, и маркер «показаны N из M» первой строкой подвала.
    total_games = len(real_games)
    shown_games = digest_shown_match_count(header, lines, total_games, tail=f"\n{rich_footer(footer)}")
    if shown_games < total_games:
        footer = truncation_marker(shown_games, total_games, item_word="матчей") + f"\n{footer}"
    summary_text = header + DIGEST_GAME_SEPARATOR.join(lines[:shown_games]) + f"\n{rich_footer(footer)}"

    expand_buttons = [
        InlineKeyboardButton(text=label, callback_data=f"{DIGEST_EXPAND_PREFIX}{gid}")
        for label, gid in zip(digest_game_button_labels(game_ids), game_ids)
    ]
    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=rich_message(summary_text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=build_menu(expand_buttons, n_cols=3)),
    )
    await _pause()


# Ответы на кнопки матчей /tonight по чатам: chat_id → (game_id, карточка ли это) →
# [id ответа, id последней ссылки на него или None]. Превью и карточка — разные ответы:
# матч, сыгранный после превью, открывается карточкой. Только в памяти процесса: после
# рестарта бота повторное нажатие шлёт ответ заново.
_tonight_sent: Dict[int, Dict[Tuple[int, bool], List[Optional[int]]]] = {}
_TONIGHT_ALREADY_SENT = "↑ Этот матч уже открыт — нажмите на цитату, чтобы перейти к нему."


async def _delete_tonight_pointer(bot: Bot, chat_id: int, pointer_id: Optional[int]) -> None:
    """Удаляет прошлую ссылку-reply на ответ `/tonight` (`pointer_id`, None —
    ссылки не было) в чате `chat_id`."""
    if pointer_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=pointer_id)
    except TelegramBadRequest as exc:
        # «Message to delete not found» / «message can't be deleted» (старше 48 ч):
        # убирать нечего; остальные ошибки — наверх.
        text = exc.message.lower()
        if "to delete not found" not in text and "can't be deleted" not in text:
            raise


async def _point_to_tonight_answer(bot: Bot, chat_id: int, entry: List[Optional[int]]) -> bool:
    """Повторное нажатие кнопки матча `/tonight`: вместо новой копии ответа —
    короткое сообщение-ответ (reply) на уже отправленный; тап по цитате
    прокручивает чат к нему (отзыв 2026-10-03).

    Зачем удалять прошлую ссылку: иначе каждое нажатие всё равно добавляет
    сообщение в чат — так на матч остаётся не больше одной ссылки.

    Аргументы:
        bot, chat_id: как у `callback_tonight_game`.
        entry: значение `_tonight_sent[chat_id]` для матча — обновляется
            на месте id новой ссылки.

    Возвращает: False, если исходного ответа в чате уже нет (пользователь его
    удалил) — тогда ответ нужно отправить заново; ссылка на удалённый ответ
    при этом тоже удаляется.
    """
    answer_id, pointer_id = entry
    assert answer_id is not None
    try:
        pointer = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message(_TONIGHT_ALREADY_SENT),
            reply_parameters=ReplyParameters(message_id=answer_id),
        )
    except TelegramBadRequest as exc:
        # «Message to be replied not found»: ответ удалён из чата.
        if "replied not found" not in exc.message.lower():
            raise
        await _delete_tonight_pointer(bot, chat_id, pointer_id)
        return False
    await _delete_tonight_pointer(bot, chat_id, pointer_id)
    entry[1] = pointer.message_id
    return True


async def callback_tonight_game(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Обрабатывает нажатие кнопки матча `/tonight` (`tn:<game_id>:<away>:<home>`,
    `match` — совпадение `TONIGHT_GAME_CALLBACK_PATTERN`).

    Если матч уже есть в базе — шлёт полную карточку (`send_game_card_message`);
    иначе шлёт текстовое превью сезонных встреч команд (`matchup_season_preview`)
    — матч из расписания NHL API мог ещё не попасть в БД. Повторное нажатие
    не шлёт копию, а ссылается на уже отправленный ответ
    (`_point_to_tonight_answer`).
    """
    assert callback.message is not None
    chat_id = callback.message.chat.id
    await callback.answer()
    game_id, away, home = int(match.group(1)), match.group(2), match.group(3)
    is_card = game_exists(game_id)
    sent = _tonight_sent.setdefault(chat_id, {})
    key = (game_id, is_card)
    if key in sent and await _point_to_tonight_answer(bot, chat_id, sent[key]):
        return
    if is_card:
        answer = await send_game_card_message(bot, chat_id, game_id)
    else:
        # Проза без счётных элементов — сноска без чисел, но текст берётся из
        # того же единственного хелпера (truncate_telegram_text по умолчанию),
        # а не из литерала, продублированного здесь и в bot.py.
        body, footer = matchup_season_preview(game_id, away, home)
        text = f"{truncate_telegram_text(body)}\n{rich_footer(footer)}"
        answer = await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message(text))
    sent[key] = [answer.message_id, None]


async def callback_expand_digest_game(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Обрабатывает кнопку матча сжатой сводки дайджеста (`dg:<game_id>`, `match` —
    совпадение `DIGEST_EXPAND_CALLBACK_PATTERN`) — досылает полную карточку
    конкретного матча отдельным сообщением.
    """
    assert callback.message is not None
    await callback.answer()
    await send_game_card_message(bot, callback.message.chat.id, int(match.group(1)))


async def handle_goal_video(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Шлёт клип гола по кнопке `gv:<game_id>:<event_id>` (`match` — совпадение
    `GOAL_VIDEO_CALLBACK_PATTERN`) с подписью ``goal_caption`` (кто забил, с чьих
    передач, когда).

    Клип из кэша (``cached_goal_clip``) уходит по ``file_id`` сразу; иначе он скачивается
    с NHL, грузится в Telegram, и его ``file_id`` запоминается (Задача 87). Протухший
    ``file_id`` (Telegram отверг) — ошибка в лог и одна повторная загрузка.
    """
    assert callback.message is not None
    game_id, event_id = int(match.group(1)), int(match.group(2))
    chat_id = callback.message.chat.id

    cached = cached_goal_clip(game_id, event_id)
    if cached is not None:
        await callback.answer()
        file_id, caption = cached
        try:
            await bot.send_video(
                chat_id=chat_id, video=file_id, caption=caption, parse_mode="HTML",
                supports_streaming=True,
            )
            return
        except TelegramBadRequest as exc:
            logger.error(
                "Telegram rejected cached clip game=%s event=%s, re-uploading: %s",
                game_id, event_id, exc,
            )
    else:
        await callback.answer("Загружаю видео гола...")

    # В отдельном потоке: скачивание MP4 и два прохода ffmpeg блокируют
    # единственный event loop бота до ~2 минут (см. video_replay).
    delivery = await asyncio.to_thread(download_goal_video, game_id, event_id)
    if delivery is None:
        await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message("Видео пока недоступно."))
        return

    try:
        sent = await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(delivery.path),
            thumbnail=FSInputFile(delivery.thumb_path) if delivery.thumb_path else None,
            width=delivery.width,
            height=delivery.height,
            duration=delivery.duration,
            supports_streaming=True,
            caption=delivery.caption,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to send goal video")
        await bot.send_rich_message(
            chat_id=chat_id, rich_message=rich_message("Ошибка при отправке видео.")
        )
    else:
        assert sent.video is not None  # ответ на отправку видео всегда содержит video
        save_goal_clip(game_id, event_id, sent.video.file_id, delivery.caption)
    finally:
        try:
            os.unlink(delivery.path)
        except OSError:
            pass
        if delivery.thumb_path:
            try:
                os.unlink(delivery.thumb_path)
            except OSError:
                pass
