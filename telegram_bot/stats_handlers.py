"""Клавиатуры пагинации и callback-хендлеры статистики: диалог `/stats`, дайджест дня,
лидерборд `/leaders`, standalone-меню `/advanced` и `/countries`, кнопка матча `/tonight`.

Часть `telegram_bot/`, который читает БД (заполненную `pipeline/`) и рисует меню поверх
неё: здесь — листание длинных таблиц статистики через inline-кнопки и отправка карточек
матчей и дайджестов, не помещающихся в одно сообщение целиком.
"""

import asyncio
import html
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters, Update
from telegram.error import BadRequest
from telegram.ext import CallbackContext

from bot_messages import (
    PLAYER_GROUPS,
    COUNTRY_LABELS,
    DIGEST_GAME_SEPARATOR,
    LEADERBOARD_PAGE_SIZE,
    conference_summary,
    country_page,
    country_rankings,
    day_digest,
    day_digest_summary_body,
    digest_game_button_labels,
    digest_shown_match_count,
    last_night_day,
    division_summary,
    game_exists,
    game_message,
    matchup_season_preview,
    player_stat_leaderboard_page,
    season_team_abbrevs,
    stat_leaderboard_for_kind,
    team_profile,
    team_stat_leaderboard_page,
    team_table,
    truncate_telegram_text,
    truncation_marker,
)
from dialog_states import (
    CHOOSE_STATS,
    DAY_DIGEST,
    END_CONVERSATION,
    FIRST,
    LAST_MENU_MESSAGE_ID_KEY,
    PLAYER_ADVANCED_SUBMENU,
    PLAYER_FIELD,
    PLAYER_GOALIE,
    SECOND,
    TEAM_PROFILE_PICK,
    TEAM_STATS,
    THIRD,
    build_menu,
)
from help_text import ADVANCED_COMMAND_INTRO, SUBSCRIPTIONS_FOOTER
from leaderboard_specs import (
    ADV_STANDALONE_TO_STAT,
    PLAYER_STAT_TITLES,
    SHOT_STANDALONE_TO_STAT,
    TEAM_STAT_TITLES,
)
from video_replay import download_goal_video

logger = logging.getLogger(__name__)

# /leaders: выбор категории, затем pl:<kind>:<offset>
LEADERS_PICK_CALLBACK_PATTERN = r"^pl:pick:(points|goals|assists)$"
LEADERBOARD_PAGE_CALLBACK_PATTERN = r"^pl:(points|goals|assists):(\d+)$"
DIGEST_EXPAND_PREFIX = "dg:"
DIGEST_EXPAND_CALLBACK_PATTERN = rf"^{DIGEST_EXPAND_PREFIX}\d+$"
DIGEST_BACK_FROM_DATE_CALLBACK = "digest:back"
# /tonight: кнопка матча tn:<game_id>:<away>:<home>
TONIGHT_GAME_CALLBACK_PATTERN = r"^tn:\d+:[^:]+:[^:]+$"

# Пагинация в /stats (игроки / вратари / типы бросков / advanced)
STAT_PAGE_CALLBACK_PATTERN = r"^st:([\w_]+):([\w_]+):(\d+)$"
TEAM_PAGE_CALLBACK_PATTERN = r"^tm:([\w_]+):(\d+)$"

# Профиль команды (Задача 41, Фаза D): кнопка аббревиатуры на экране выбора
# команды tp:<ABBR>
TEAM_PROFILE_CALLBACK_PREFIX = "tp:"
TEAM_PROFILE_CALLBACK_PATTERN = r"^tp:([A-Za-z0-9]{2,4})$"

# Статистика по странам (/countries, Задача 49): cn:<CODE>:<F|D|G>:<offset> —
# страница группы игроков страны, cn:list — назад к рейтингу стран
COUNTRY_CALLBACK_PATTERN = r"^cn:(?:list|([A-Z]{3}):([FDG]):(\d+))$"

# Standalone: /advanced — листание sa:<table>:<col>:<offset>, sa:menu, sa:save
STANDALONE_SA_CALLBACK_PATTERN = r"^sa:"

ADV_CALLBACK_PREFIX = "adv:"


def _stats_menu_nav_row(parent_state: int) -> List[InlineKeyboardButton]:
    """Нижний ряд навигации страницы стата: родитель / корень / выход.

    `parent_state` — callback_data родительского экрана (подменю, из которого
    открыли эту страницу): «« Назад» ведёт туда, а не сразу в корень.
    Родитель для страниц статов игроков/вратарей выводится из
    (table, column) — см. `_player_stat_parent_state`.
    """
    return [
        InlineKeyboardButton("« Назад", callback_data=str(parent_state)),
        InlineKeyboardButton("В начало", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Готово", callback_data=str(END_CONVERSATION)),
    ]


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
        nav.append(InlineKeyboardButton(f"« {span(0)}", callback_data=f"{prefix}:0"))
    if has_prev:
        prev_off = max(0, offset - size)
        nav.append(InlineKeyboardButton(f"← {span(prev_off)}", callback_data=f"{prefix}:{prev_off}"))
    if has_next:
        next_off = offset + size
        nav.append(InlineKeyboardButton(f"{span(next_off)} →", callback_data=f"{prefix}:{next_off}"))
    return [nav] if nav else []


def _record_menu_message(context: CallbackContext, message_id: int) -> None:
    """Записывает id сообщения с живой FSM-клавиатурой меню в `user_data`
    под ключом `dialog_states.LAST_MENU_MESSAGE_ID_KEY` (см. его комментарий
    для канонического списка мест, которые сюда пишут и читают).

    Вызывается только там, где страница диалога `/stats` создаёт НОВОЕ
    сообщение (`send_message`/`reply_text`, а не `edit_message_text`) — эти
    места в этом модуле собраны через один вызов, чтобы не повторять guard
    на каждом из них. `context.user_data` типизирован как `Optional`, но
    гарантированно не `None` для контекста, порождённого реальным Update от
    пользователя (единственный вызывающий тут сценарий, в отличие от
    `push_digest_job.py`, который сам себе строит контекст без пользователя
    и не проходит в ветки, откуда зовётся эта функция).
    """
    assert context.user_data is not None
    context.user_data[LAST_MENU_MESSAGE_ID_KEY] = message_id


def _player_stat_parent_state(table: str, column: str) -> int:
    """Родительское подменю страницы стата игрока/вратаря — по (table, column).

    Зачем выводить, а не хранить: пагинация (`callback_stats_player_page`)
    восстанавливает клавиатуру только из `table`/`column`/`offset` в
    callback_data (лимит Telegram 64 байта не оставляет места для лишнего
    поля) — родитель должен считаться тем же правилом, каким собраны сами
    подменю в `script_bot.py`: `bot_player_advanced_menu` собирает
    `players_advanced_stats`, `players_shot_types` и `shootout_pct`
    (единственный столбец advanced-набора, физически лежащий в
    `players_season_stats`); остальные `players_season_stats` — подменю
    полевых (`bot_player_field`); `goalies_season_stats` — подменю вратарей
    (`bot_player_goalie`).
    """
    if table == "goalies_season_stats":
        return PLAYER_GOALIE
    if table in ("players_advanced_stats", "players_shot_types") or (
        table, column
    ) == ("players_season_stats", "shootout_pct"):
        return PLAYER_ADVANCED_SUBMENU
    return PLAYER_FIELD


def conversation_player_stat_keyboard(
    table: str,
    column: str,
    offset: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура страницы статистики игрока/вратаря внутри диалога `/stats`.

    Кнопки листания (`_page_nav_rows`) несут callback_data `st:{table}:{column}:{offset}`,
    которую разбирает `callback_stats_player_page`; нижний ряд — «« Назад»» на
    родительское подменю (`_player_stat_parent_state`), «В начало» и «Готово».

    Args:
        has_prev, has_next: есть ли соседняя страница — определяют, рисовать
            ли кнопку в эту сторону.
    """
    rows = _page_nav_rows(f"st:{table}:{column}", offset, has_prev, has_next)
    rows.append(_stats_menu_nav_row(_player_stat_parent_state(table, column)))
    return InlineKeyboardMarkup(rows)


def conversation_team_stat_keyboard(
    column: str,
    offset: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура страницы командной статистики внутри диалога `/stats`.

    Как `conversation_player_stat_keyboard`, но родитель всегда один —
    `TEAM_STATS` (единственное подменю команд), поэтому `table` в callback_data
    не передаётся. Кнопки листания несут `tm:{column}:{offset}`, которую
    разбирает `callback_stats_team_page`.
    """
    rows = _page_nav_rows(f"tm:{column}", offset, has_prev, has_next)
    # Единственное подменю команд (bot_team_stats/TEAM_STATS) — table в
    # callback_data «tm:...» не передаётся, других родителей у страниц
    # команд нет.
    rows.append(_stats_menu_nav_row(TEAM_STATS))
    return InlineKeyboardMarkup(rows)


def _make_paginated_player_stat_open_handler(table: str, column: str):
    title = PLAYER_STAT_TITLES[(table, column)]

    async def handler(update: Update, context: CallbackContext) -> int:
        query = update.callback_query
        assert query is not None
        await query.answer()
        text, hp, hn = player_stat_leaderboard_page(title, table, column, 0)
        markup = conversation_player_stat_keyboard(table, column, 0, hp, hn)
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
        return SECOND

    return handler


def _make_paginated_team_stat_open_handler(column: str):
    title = TEAM_STAT_TITLES[column]

    async def handler(update: Update, context: CallbackContext) -> int:
        query = update.callback_query
        assert query is not None
        await query.answer()
        text, hp, hn = team_stat_leaderboard_page(title, column, 0)
        markup = conversation_team_stat_keyboard(column, 0, hp, hn)
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
        return SECOND

    return handler


def _make_team_group_summary_handler(summary_func):
    """Фабрика для сводок по конференциям/дивизионам (Задача 41, Фаза B) —
    без пагинации (2/4 строки, отдельная страница не нужна), поэтому проще
    `_make_paginated_team_stat_open_handler`: одно сообщение, «« Назад»» на
    `TEAM_STATS` (`_stats_menu_nav_row`), как у соседних командных экранов
    (TEAM_PROCENT_WINS/TEAM_POWER_PLAY/TEAM_POWER_KILL).

    Args:
        summary_func: `conference_summary`/`division_summary` — без аргументов,
            сезон берут из `config` сами.
    """

    async def handler(update: Update, context: CallbackContext) -> int:
        query = update.callback_query
        assert query is not None
        await query.answer()
        markup = InlineKeyboardMarkup([_stats_menu_nav_row(TEAM_STATS)])
        await query.edit_message_text(
            text=summary_func(), parse_mode="HTML", reply_markup=markup
        )
        return SECOND

    return handler


async def bot_team_profile_pick(update: Update, context: CallbackContext) -> int:
    """Кнопка «Профиль команды» подменю команд: список аббревиатур сезона
    для выбора (Задача 41, Фаза D) — сетка кнопок `tp:<ABBR>` (разбирает
    `bot_team_profile_show`), футер «« Назад»» на подменю команд (`TEAM_STATS`).
    Тот же хендлер перерисовывает экран, когда «« Назад»» с профиля
    возвращает сюда (`TEAM_PROFILE_PICK` зарегистрирован и в FIRST, и в
    SECOND — см. `bot.py`).

    Пустой сезон (Задача 36): нет команд в базе — текст с причиной вместо
    клавиатуры без кнопок.
    """
    query = update.callback_query
    assert query is not None
    await query.answer()
    abbrevs = season_team_abbrevs()
    buttons = [
        InlineKeyboardButton(ab, callback_data=f"{TEAM_PROFILE_CALLBACK_PREFIX}{ab}")
        for ab in abbrevs
    ]
    rows = build_menu(buttons, n_cols=4, footer_buttons=[_stats_menu_nav_row(TEAM_STATS)])
    text = "Выберите команду:" if abbrevs else "В базе нет команд для этого сезона."
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows))
    return SECOND


async def bot_team_profile_show(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор аббревиатуры на экране профиля команды (`tp:<ABBR>`):
    строит `team_profile()` и показывает его. «« Назад»» ведёт на список
    команд (`TEAM_PROFILE_PICK`), а не сразу в `TEAM_STATS` — так можно
    выбрать другую команду без лишнего клика.

    Неизвестная/ненайденная в базе аббревиатура — не отдельная ветка здесь:
    `team_profile()` сама возвращает текст с причиной (Задача 36).
    """
    query = update.callback_query
    assert query is not None and query.data is not None
    # `CallbackQueryHandler` вызывает хендлер только на данных, уже прошедших
    # `TEAM_PROFILE_CALLBACK_PATTERN` (`bot.py`) — совпадение гарантировано.
    m = re.match(TEAM_PROFILE_CALLBACK_PATTERN, query.data)
    assert m is not None
    await query.answer()
    text = truncate_telegram_text(team_profile(m.group(1)))
    markup = InlineKeyboardMarkup([_stats_menu_nav_row(TEAM_PROFILE_PICK)])
    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
    return SECOND


def country_rankings_reply() -> Tuple[str, InlineKeyboardMarkup]:
    """Рейтинг стран сезона и сетка кнопок `cn:<CODE>:F:0` — по одной на страну,
    прошедшую порог (Задача 49, команда /countries). Пустой сезон — текст без
    кнопок (Задача 36)."""
    text, codes = country_rankings()
    buttons = [
        InlineKeyboardButton(COUNTRY_LABELS.get(code, code), callback_data=f"cn:{code}:F:0")
        for code in codes
    ]
    return text, InlineKeyboardMarkup(build_menu(buttons, n_cols=2))


async def callback_country(update: Update, context: CallbackContext) -> None:
    """Кнопки /countries: `cn:list` — снова рейтинг стран, `cn:<CODE>:<G>:<off>` —
    страница группы игроков страны с листанием, переключателем групп
    (нападающие / защитники / вратари) и «« Страны». Код вне рейтинга сезона
    `country_page()` сама превращает в текст «Страна не найдена…».
    `BadRequest` «message is not modified» — штатный повтор нажатия."""
    query = update.callback_query
    assert query is not None and query.data is not None
    m = re.match(COUNTRY_CALLBACK_PATTERN, query.data)
    assert m is not None
    await query.answer()
    if m.group(1) is None:
        text, markup = country_rankings_reply()
    else:
        code, group, offset = m.group(1), m.group(2), int(m.group(3))
        text, has_prev, has_next = country_page(code, group, offset)
        rows = _page_nav_rows(f"cn:{code}:{group}", offset, has_prev, has_next)
        rows.append([
            InlineKeyboardButton(
                ("• " if key == group else "") + label, callback_data=f"cn:{code}:{key}:0"
            )
            for key, (label, _cond) in PLAYER_GROUPS.items()
        ])
        rows.append([InlineKeyboardButton("« Страны", callback_data="cn:list")])
        markup = InlineKeyboardMarkup(rows)
    try:
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def callback_stats_player_page(update: Update, context: CallbackContext) -> int:
    """Листает страницу статистики игрока/вратаря по callback_data
    `st:<table>:<column>:<offset>`, перерисовывая сообщение на месте.

    Неизвестная пара (table, column) не роняет диалог: хендлер отвечает на
    callback и возвращает `SECOND`, ничего не перерисовывая. `BadRequest`
    «message is not modified» — штатный повтор нажатия текущей страницы,
    глушится молча.

    Returns:
        Всегда `SECOND` — хендлер зарегистрирован и в `FIRST`, и в `SECOND`.
    """
    query = update.callback_query
    if not query or not query.data:
        return SECOND
    m = re.match(STAT_PAGE_CALLBACK_PATTERN, query.data)
    if not m:
        return SECOND
    table, col, off_s = m.group(1), m.group(2), m.group(3)
    key = (table, col)
    if key not in PLAYER_STAT_TITLES:
        await query.answer()
        return SECOND
    title = PLAYER_STAT_TITLES[key]
    offset = int(off_s)
    await query.answer()
    text, hp, hn = player_stat_leaderboard_page(title, table, col, offset)
    markup = conversation_player_stat_keyboard(table, col, offset, hp, hn)
    try:
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return SECOND


async def callback_stats_team_page(update: Update, context: CallbackContext) -> int:
    """Листает страницу командной статистики по callback_data
    `tm:<column>:<offset>`, перерисовывая сообщение на месте — симметрично
    `callback_stats_player_page`.

    Returns:
        Всегда `SECOND`.
    """
    query = update.callback_query
    if not query or not query.data:
        return SECOND
    m = re.match(TEAM_PAGE_CALLBACK_PATTERN, query.data)
    if not m:
        return SECOND
    col, off_s = m.group(1), m.group(2)
    if col not in TEAM_STAT_TITLES:
        await query.answer()
        return SECOND
    title = TEAM_STAT_TITLES[col]
    offset = int(off_s)
    await query.answer()
    text, hp, hn = team_stat_leaderboard_page(title, col, offset)
    markup = conversation_team_stat_keyboard(col, offset, hp, hn)
    try:
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return SECOND


def standalone_player_stat_keyboard(
    table: str,
    column: str,
    offset: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура страницы статистики игрока в standalone-режиме (`/advanced`,
    кнопки `sa:...`), вне диалога `/stats`.

    От `conversation_player_stat_keyboard` отличается тем, что нет состояний
    FSM: «« К списку статистик» (`sa:menu`) возвращает меню `/advanced` в том же
    сообщении, «Сохранить» (`sa:save`) оставляет страницу в чате без кнопок и
    продолжает листание в её копии ниже.
    """
    rows = _page_nav_rows(f"sa:{table}:{column}", offset, has_prev, has_next)
    rows.append([
        InlineKeyboardButton("« К списку статистик", callback_data="sa:menu"),
        InlineKeyboardButton("Сохранить", callback_data="sa:save"),
    ])
    return InlineKeyboardMarkup(rows)


async def callback_standalone_sa(update: Update, context: CallbackContext) -> None:
    """Обрабатывает нажатия кнопок `sa:...` вне диалога `/stats`:
    `sa:menu` — меню `/advanced` на месте страницы; `sa:save` — страница
    остаётся в чате без кнопок, а её копия с теми же кнопками уходит новым
    сообщением; `sa:<table>:<column>:<offset>` листает страницу статистики так
    же, как `callback_stats_player_page`, но без состояний FSM.
    """
    query = update.callback_query
    if not query or not query.data:
        return
    if query.data == "sa:menu":
        await query.answer()
        await query.edit_message_text(
            text=ADVANCED_COMMAND_INTRO,
            parse_mode="HTML",
            reply_markup=advanced_standalone_keyboard(),
        )
        return
    if query.data == "sa:save":
        await query.answer("Сохранено")
        message = query.message
        assert isinstance(message, Message)
        # Сначала копия, потом снятие кнопок: упади отправка — страница останется живой.
        await context.bot.send_message(
            chat_id=message.chat.id,
            text=message.text_html,
            parse_mode="HTML",
            reply_markup=message.reply_markup,
        )
        await query.edit_message_reply_markup(reply_markup=None)
        return
    m = re.match(r"^sa:([\w_]+):([\w_]+):(\d+)$", query.data)
    if not m:
        await query.answer()
        return
    table, col, off_s = m.group(1), m.group(2), m.group(3)
    key = (table, col)
    if key not in PLAYER_STAT_TITLES:
        await query.answer()
        return
    title = PLAYER_STAT_TITLES[key]
    offset = int(off_s)
    await query.answer()
    text, hp, hn = player_stat_leaderboard_page(title, table, col, offset)
    markup = standalone_player_stat_keyboard(table, col, offset, hp, hn)
    try:
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


# --- Player (field) stats ---

bot_player_points = _make_paginated_player_stat_open_handler(
    "players_season_stats", "points"
)
bot_player_goals = _make_paginated_player_stat_open_handler(
    "players_season_stats", "goals"
)
bot_player_assists = _make_paginated_player_stat_open_handler(
    "players_season_stats", "assists"
)
bot_player_hits = _make_paginated_player_stat_open_handler(
    "players_season_stats", "hits"
)
bot_player_plus_minus = _make_paginated_player_stat_open_handler(
    "players_season_stats", "plus_minus"
)
bot_player_penalties = _make_paginated_player_stat_open_handler(
    "players_season_stats", "pim"
)
bot_player_blocks = _make_paginated_player_stat_open_handler(
    "players_season_stats", "blocked"
)
bot_player_ice_time = _make_paginated_player_stat_open_handler(
    "players_season_stats", "time_on_ice_per_game"
)

bot_player_sat_pct = _make_paginated_player_stat_open_handler(
    "players_advanced_stats", "sat_pct"
)
bot_player_usat_pct = _make_paginated_player_stat_open_handler(
    "players_advanced_stats", "usat_pct"
)
bot_player_goals_for_pct = _make_paginated_player_stat_open_handler(
    "players_advanced_stats", "goals_pct"
)
bot_player_oz_start_pct = _make_paginated_player_stat_open_handler(
    "players_advanced_stats", "oz_start_pct"
)
bot_player_shootout_pct = _make_paginated_player_stat_open_handler(
    "players_season_stats", "shootout_pct"
)

# --- Goalie stats ---

bot_goalie_wins = _make_paginated_player_stat_open_handler(
    "goalies_season_stats", "wins"
)
bot_goalie_percentage = _make_paginated_player_stat_open_handler(
    "goalies_season_stats", "save_percentage"
)
bot_goalie_shootouts = _make_paginated_player_stat_open_handler(
    "goalies_season_stats", "shutouts"
)

# --- Team stats ---

bot_team_procent_wins = _make_paginated_team_stat_open_handler("procent_points")
bot_team_power_play = _make_paginated_team_stat_open_handler("power_play_percentage")
bot_team_power_kill = _make_paginated_team_stat_open_handler("penalty_kill_percentage")
bot_team_conference_stats = _make_team_group_summary_handler(conference_summary)
bot_team_division_stats = _make_team_group_summary_handler(division_summary)


# --- Shot type leaders (players_shot_types) ---

bot_player_shot_wrist = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_wrist"
)
bot_player_shot_slap = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_slap"
)
bot_player_shot_snap = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_snap"
)
bot_player_shot_backhand = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_backhand"
)
bot_player_shot_tip_in = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_tip_in"
)
bot_player_shot_deflected = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_deflected"
)
bot_player_shot_wrap = _make_paginated_player_stat_open_handler(
    "players_shot_types", "goals_wrap_around"
)

# --- Day digest: один матч — полная карточка; несколько — сводка + кнопки матчей ---

# Подсказка после дайджеста вне диалога /stats (из /today и утренней рассылки).
_DIGEST_MORE_HINT = (
    "Ещё: /tonight — расписание NHL, /standings — таблица, /leaders — лидеры, "
    "/team — команды, /stats — меню, /help — справка.\n"
    + SUBSCRIPTIONS_FOOTER
)

# Кнопки видео голов («▶ 1:0 Nelson 6:33») в два столбца: столбик из десятка
# кнопок на всю ширину визуально сливался со следующей карточкой матча.
_GOAL_VIDEO_COLUMNS = 2


def _goal_video_buttons(goals_meta: List[Dict]) -> List[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            g['label'],
            callback_data=f"gv:{g['game_id']}:{g['event_id']}",
        )
        for g in goals_meta
    ]


async def send_game_card_message(
    context: CallbackContext,
    chat_id: int,
    game_id: int,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Message:
    """Полная карточка матча (текст + опционально клавиатура); возвращает
    отправленное сообщение."""
    if not game_exists(game_id):
        return await context.bot.send_message(
            chat_id=chat_id,
            text="Такого матча нет в базе бота.",
            parse_mode="HTML",
        )
    text, goals_meta = game_message(game_id)
    gbtn = _goal_video_buttons(goals_meta)
    if reply_markup is not None:
        markup = reply_markup
    elif gbtn:
        markup = InlineKeyboardMarkup(build_menu(gbtn, n_cols=_GOAL_VIDEO_COLUMNS))
    else:
        markup = None
    return await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
    )


async def dispatch_day_digest_messages(
    context: CallbackContext,
    chat_id: int,
    day_label: Optional[str],
    games: List[Tuple[int, str, List[Dict]]],
    *,
    attach_conv_nav_on_last: bool = True,
    inter_message_sleep_sec: Optional[float] = None,
) -> None:
    """Дайджест дня: один матч — одна карточка; несколько — сводка + кнопки разворота."""

    async def _pause() -> None:
        if inter_message_sleep_sec is not None and inter_message_sleep_sec > 0:
            await asyncio.sleep(inter_message_sleep_sec)

    # Родитель результата дайджеста — меню дайджеста (DAY_DIGEST), а не сразу
    # корень: «Сегодня»/«Вчера»/ввод даты открываются из bot_digest_date_menu,
    # туда и ведёт «« Назад»».
    nav_buttons = [
        InlineKeyboardButton("« Назад", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("В начало", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Закрыть меню", callback_data=str(END_CONVERSATION)),
    ]

    real_games = [(gid, text, meta) for gid, text, meta in games if gid != 0]

    if not real_games:
        _, text, _ = games[0]
        nav_markup = (
            InlineKeyboardMarkup([nav_buttons]) if attach_conv_nav_on_last else None
        )
        safe_text = html.escape(text) if text else ""
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=safe_text,
            parse_mode="HTML",
            reply_markup=nav_markup,
        )
        if attach_conv_nav_on_last:
            _record_menu_message(context, sent.message_id)
        await _pause()
        if not attach_conv_nav_on_last:
            await context.bot.send_message(chat_id=chat_id, text=_DIGEST_MORE_HINT)
            await _pause()
        return

    if len(real_games) == 1:
        _gid, text, goals_meta = real_games[0]
        goal_buttons = _goal_video_buttons(goals_meta)
        rows = build_menu(
            goal_buttons,
            n_cols=_GOAL_VIDEO_COLUMNS,
            footer_buttons=[[b] for b in nav_buttons] if attach_conv_nav_on_last else None,
        )
        markup = InlineKeyboardMarkup(rows) if rows else None
        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
        )
        if attach_conv_nav_on_last:
            _record_menu_message(context, sent.message_id)
        await _pause()
        if not attach_conv_nav_on_last:
            await context.bot.send_message(chat_id=chat_id, text=_DIGEST_MORE_HINT)
            await _pause()
        return

    day_str = day_label or "—"
    game_ids = [gid for gid, _t, _m in real_games]
    lines = day_digest_summary_body(game_ids)
    body = DIGEST_GAME_SEPARATOR.join(lines)
    header = (
        f"<b>Матчи {html.escape(day_str)}</b> ({len(real_games)} игр)\n\n"
    )
    intro = (
        f"{header}{body}\n\n"
        "<i>Кнопка матча — полная карточка и видео голов.</i>"
    )
    # Матч сводки несёт HTML-тег <b>: резать его по символам
    # нельзя — Telegram отвергнет незакрытый тег. Не влезло — оставляем
    # целые матчи, сколько уместилось, и маркер «показаны N из M».
    total_games = len(real_games)
    shown_games = digest_shown_match_count(header, lines, total_games)
    if shown_games < total_games:
        summary_text = header + DIGEST_GAME_SEPARATOR.join(lines[:shown_games]) + "\n\n" + truncation_marker(
            shown_games, total_games, item_word="матчей"
        )
    else:
        summary_text = intro

    expand_buttons = [
        InlineKeyboardButton(label, callback_data=f"{DIGEST_EXPAND_PREFIX}{gid}")
        for label, gid in zip(digest_game_button_labels(game_ids), game_ids)
    ]
    rows = build_menu(expand_buttons, n_cols=3)
    if attach_conv_nav_on_last:
        rows.append(nav_buttons)
    markup = InlineKeyboardMarkup(rows)

    sent = await context.bot.send_message(
        chat_id=chat_id, text=summary_text, parse_mode="HTML", reply_markup=markup,
    )
    if attach_conv_nav_on_last:
        _record_menu_message(context, sent.message_id)
    await _pause()

    if not attach_conv_nav_on_last:
        await context.bot.send_message(chat_id=chat_id, text=_DIGEST_MORE_HINT)
        await _pause()


# Ответы на кнопки матчей /tonight в этом чате (`chat_data`): ключ (game_id,
# карточка ли это) → [id ответа, id последней ссылки на него или None]. Превью и
# карточка — разные ответы: матч, сыгранный после превью, открывается карточкой.
_TONIGHT_SENT_KEY = "tonight_sent"
_TONIGHT_ALREADY_SENT = "↑ Этот матч уже открыт — нажмите на цитату, чтобы перейти к нему."


async def _delete_tonight_pointer(
    context: CallbackContext, chat_id: int, pointer_id: Optional[int]
) -> None:
    """Удаляет прошлую ссылку-reply на ответ `/tonight` (`pointer_id`, None —
    ссылки не было) в чате `chat_id`."""
    if pointer_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=pointer_id)
    except BadRequest as exc:
        # «Message to delete not found» / «message can't be deleted» (старше 48 ч):
        # убирать нечего; остальные ошибки — наверх.
        text = str(exc).lower()
        if "to delete not found" not in text and "can't be deleted" not in text:
            raise


async def _point_to_tonight_answer(
    context: CallbackContext, chat_id: int, entry: List[Optional[int]]
) -> bool:
    """Повторное нажатие кнопки матча `/tonight`: вместо новой копии ответа —
    короткое сообщение-ответ (reply) на уже отправленный; тап по цитате
    прокручивает чат к нему (отзыв 2026-10-03).

    Зачем удалять прошлую ссылку: иначе каждое нажатие всё равно добавляет
    сообщение в чат — так на матч остаётся не больше одной ссылки.

    Аргументы:
        context, chat_id: как у `callback_tonight_game`.
        entry: значение `chat_data[_TONIGHT_SENT_KEY]` для матча — обновляется
            на месте id новой ссылки.

    Возвращает: False, если исходного ответа в чате уже нет (пользователь его
    удалил) — тогда ответ нужно отправить заново; ссылка на удалённый ответ
    при этом тоже удаляется.
    """
    answer_id, pointer_id = entry
    assert answer_id is not None
    try:
        pointer = await context.bot.send_message(
            chat_id=chat_id,
            text=_TONIGHT_ALREADY_SENT,
            reply_parameters=ReplyParameters(message_id=answer_id),
        )
    except BadRequest as exc:
        # «Message to be replied not found»: ответ удалён из чата.
        if "replied not found" not in str(exc).lower():
            raise
        await _delete_tonight_pointer(context, chat_id, pointer_id)
        return False
    await _delete_tonight_pointer(context, chat_id, pointer_id)
    entry[1] = pointer.message_id
    return True


async def callback_tonight_game(update: Update, context: CallbackContext) -> None:
    """Обрабатывает нажатие кнопки матча `/tonight` (`tn:<game_id>:<away>:<home>`).

    Если матч уже есть в базе — шлёт полную карточку (`send_game_card_message`);
    иначе шлёт текстовое превью сезонных встреч команд (`matchup_season_preview`)
    — матч из расписания NHL API мог ещё не попасть в БД. Повторное нажатие
    не шлёт копию, а ссылается на уже отправленный ответ
    (`_point_to_tonight_answer`).
    """
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("tn:"):
        return
    assert query.message is not None
    chat_id = query.message.chat.id
    await query.answer()
    parts = query.data.split(":", 3)
    if len(parts) != 4 or parts[0] != "tn":
        await context.bot.send_message(
            chat_id=chat_id,
            text="Некорректная кнопка.",
        )
        return
    _, gid_s, away_a, home_a = parts
    try:
        game_id = int(gid_s)
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Некорректная кнопка.",
        )
        return
    is_card = game_exists(game_id)
    assert context.chat_data is not None
    sent: Dict[Tuple[int, bool], List[Optional[int]]] = context.chat_data.setdefault(
        _TONIGHT_SENT_KEY, {}
    )
    key = (game_id, is_card)
    if key in sent and await _point_to_tonight_answer(context, chat_id, sent[key]):
        return
    if is_card:
        answer = await send_game_card_message(context, chat_id, game_id)
    else:
        # Проза без счётных элементов — сноска без чисел, но текст берётся из
        # того же единственного хелпера (truncate_telegram_text по умолчанию),
        # а не из литерала, продублированного здесь и в bot.py.
        text = truncate_telegram_text(matchup_season_preview(game_id, away_a, home_a))
        answer = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    sent[key] = [answer.message_id, None]


async def callback_expand_digest_game(update: Update, context: CallbackContext) -> None:
    """Обрабатывает кнопку матча сжатой сводки дайджеста (`dg:<game_id>`) —
    досылает полную карточку конкретного матча отдельным сообщением.
    """
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(DIGEST_EXPAND_PREFIX):
        return
    assert query.message is not None
    chat_id = query.message.chat.id
    await query.answer()
    try:
        game_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await context.bot.send_message(chat_id=chat_id, text="Некорректная ссылка на матч.")
        return
    await send_game_card_message(context, chat_id, game_id)


async def callback_standalone_adv(update: Update, context: CallbackContext) -> None:
    """Обрабатывает нажатие кнопки меню `/advanced` (`adv:<key>`): `adv:close`
    снимает клавиатуру, иначе ключ ищется в `ADV_STANDALONE_TO_STAT`/
    `SHOT_STANDALONE_TO_STAT` и открывается первая страница этой статистики —
    дальше она листается уже как `sa:...` (см. `standalone_player_stat_keyboard`).
    """
    query = update.callback_query
    if not query or not query.data:
        return
    key = query.data.split(":", 1)[1]
    if key == "close":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        return
    pair = ADV_STANDALONE_TO_STAT.get(key) or SHOT_STANDALONE_TO_STAT.get(key)
    if not pair:
        return
    table, col = pair
    await query.answer()
    title = PLAYER_STAT_TITLES[(table, col)]
    text, hp, hn = player_stat_leaderboard_page(title, table, col, 0)
    kb = standalone_player_stat_keyboard(table, col, 0, hp, hn)
    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=kb)


def advanced_standalone_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура меню `/advanced`: продвинутая статистика (SAT/USAT/GF%/OZ
    Start%/Shootout%) и голы по типам бросков. Каждая кнопка — `adv:<key>`,
    который разбирает `callback_standalone_adv`.
    """
    rows = [
        [
            InlineKeyboardButton("SAT %", callback_data="adv:sat"),
            InlineKeyboardButton("USAT %", callback_data="adv:usat"),
        ],
        [
            InlineKeyboardButton("GF %", callback_data="adv:gf"),
            InlineKeyboardButton("OZ Start %", callback_data="adv:oz"),
        ],
        [InlineKeyboardButton("Shootout %", callback_data="adv:so")],
        [
            InlineKeyboardButton("Кистевой", callback_data="adv:wrist"),
            InlineKeyboardButton("Щелчок", callback_data="adv:slap"),
        ],
        [
            InlineKeyboardButton("С полуприёма", callback_data="adv:snap"),
            InlineKeyboardButton("Бэкхенд", callback_data="adv:back"),
        ],
        [
            InlineKeyboardButton("Направление", callback_data="adv:tip"),
            InlineKeyboardButton("Отклонение", callback_data="adv:defl"),
        ],
        [InlineKeyboardButton("С за ворот", callback_data="adv:wrap")],
    ]
    return InlineKeyboardMarkup(rows)


async def bot_league_standings(update: Update, context: CallbackContext) -> int:
    """Турнирная таблица NHL: отдельное сообщение с кнопкой «« Главное меню»».

    `LEAGUE_STANDINGS` — прямой пункт главного меню, поэтому родитель этой
    страницы уже корень: кнопка ведёт в `CHOOSE_STATS`. Сообщение новое
    (`send_message`, не `edit_message_text`), поэтому его id записывается в
    `user_data`, чтобы `/cancel` мог снять с него клавиатуру.
    """
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Главное меню", callback_data=str(CHOOSE_STATS))]]
    )
    sent = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=team_table(),
        parse_mode="HTML",
        reply_markup=markup,
    )
    _record_menu_message(context, sent.message_id)
    return FIRST


async def bot_digest_calendar_today(update: Update, context: CallbackContext) -> int:
    """Кнопка «Сегодня» меню дайджеста дня (диалог `/stats`): собирает и
    рассылает дайджест матчей прошедшей ночи по Москве (`last_night_day()`),
    как `/today`.

    Returns:
        `SECOND` — там же обрабатываются «« Назад»» и «В начало»; кнопки
        матчей (`dg:`) — глобальный хендлер вне диалога.
    """
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    day_label, games = day_digest(last_night_day())
    await dispatch_day_digest_messages(
        context,
        query.message.chat.id,
        day_label,
        games,
        attach_conv_nav_on_last=True,
    )
    return SECOND


async def bot_digest_calendar_yesterday(update: Update, context: CallbackContext) -> int:
    """Кнопка «Вчера» меню дайджеста дня — то же самое, что
    `bot_digest_calendar_today`, но на ночь раньше.

    Returns:
        `SECOND`.
    """
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    day_label, games = day_digest(last_night_day(nights_back=1))
    await dispatch_day_digest_messages(
        context,
        query.message.chat.id,
        day_label,
        games,
        attach_conv_nav_on_last=True,
    )
    return SECOND


def _digest_back_from_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "« Назад",
                    callback_data=DIGEST_BACK_FROM_DATE_CALLBACK,
                )
            ]
        ]
    )


async def bot_digest_pick_date_prompt(update: Update, context: CallbackContext) -> int:
    """Кнопка «Другая дата» меню дайджеста дня: просит прислать дату отдельным
    сообщением в формате `YYYY-MM-DD` и переводит диалог в ожидание текста.

    Returns:
        `THIRD` — там зарегистрирован `bot_digest_custom_date`.
    """
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "Отправьте дату одним сообщением в формате <code>YYYY-MM-DD</code>.\n"
        "/cancel — выход из меню.",
        parse_mode="HTML",
        reply_markup=_digest_back_from_date_keyboard(),
    )
    return THIRD


async def bot_digest_custom_date(update: Update, context: CallbackContext) -> int:
    """Обрабатывает текст с датой, присланный в состоянии `THIRD` (после
    `bot_digest_pick_date_prompt`). При неверном формате переспрашивает новым
    сообщением (id пишется в `user_data` под `LAST_MENU_MESSAGE_ID_KEY`) и
    остаётся в `THIRD`; при корректной дате собирает и рассылает дайджест.

    Returns:
        `THIRD` при ошибке формата, иначе `SECOND`.
    """
    message = update.message
    assert message is not None
    raw = (message.text or "").strip()
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        # Новое сообщение (не edit_message_text) со своей копией «« Назад»» —
        # id записывается в user_data, чтобы /cancel мог снять клавиатуру
        # с этой конкретной (последней) копии.
        sent = await message.reply_text(
            "Нужен формат YYYY-MM-DD (например 2025-12-01). Попробуйте снова или /cancel.",
            reply_markup=_digest_back_from_date_keyboard(),
        )
        _record_menu_message(context, sent.message_id)
        return THIRD
    day_label, games = day_digest(raw)
    await dispatch_day_digest_messages(
        context,
        message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=True,
    )
    return SECOND


def leaders_category_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора категории `/leaders` (Очки/Голы/Передачи); каждая
    кнопка — `pl:pick:<kind>`, разбирает `callback_leaders_pick`.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Очки", callback_data="pl:pick:points"),
                InlineKeyboardButton("Голы", callback_data="pl:pick:goals"),
                InlineKeyboardButton("Передачи", callback_data="pl:pick:assists"),
            ]
        ]
    )


def leaderboard_nav_keyboard(
    kind: str, offset: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    """Клавиатура страницы лидерборда `/leaders`: ряд листания (`_page_nav_rows`) с
    callback_data `pl:<kind>:<offset>` (разбирает `callback_leaderboard_page`),
    под ним — ряд смены категории, переиспользованный из
    `leaders_category_keyboard`.
    """
    rows = _page_nav_rows(f"pl:{kind}", offset, has_prev, has_next)
    rows.extend(list(row) for row in leaders_category_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(rows)


async def callback_leaders_pick(update: Update, context: CallbackContext) -> None:
    """Обрабатывает выбор категории `/leaders` (`pl:pick:<kind>`) — открывает
    первую страницу соответствующего лидерборда на месте (`edit_message_text`).
    """
    query = update.callback_query
    if not query or not query.data:
        return
    m = re.match(LEADERS_PICK_CALLBACK_PATTERN, query.data)
    if not m:
        return
    kind = m.group(1)
    await query.answer()
    text, hp, hn = stat_leaderboard_for_kind(kind, 0)
    markup = leaderboard_nav_keyboard(kind, 0, hp, hn)
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def callback_leaderboard_page(update: Update, context: CallbackContext) -> None:
    """Листает лидерборд `/leaders` по callback_data `pl:<kind>:<offset>` —
    симметрично `callback_leaders_pick`, но с готовым смещением страницы.
    """
    query = update.callback_query
    if not query or not query.data:
        return
    m = re.match(LEADERBOARD_PAGE_CALLBACK_PATTERN, query.data)
    if not m:
        return
    kind, off_s = m.group(1), m.group(2)
    offset = int(off_s)
    await query.answer()
    text, hp, hn = stat_leaderboard_for_kind(kind, offset)
    markup = leaderboard_nav_keyboard(kind, offset, hp, hn)
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def handle_goal_video(update: Update, context: CallbackContext) -> int:
    """Download and send goal video when button is pressed."""
    query = update.callback_query
    assert query is not None and query.data is not None and query.message is not None
    await query.answer("Загружаю видео гола...")

    parts = query.data.split(":")
    game_id = int(parts[1])
    event_id = int(parts[2])

    chat_id = query.message.chat.id

    # В отдельном потоке: скачивание MP4 и два прохода ffmpeg блокируют
    # единственный event loop бота до ~2 минут (см. video_replay).
    delivery = await asyncio.to_thread(download_goal_video, game_id, event_id)
    if delivery is None:
        await context.bot.send_message(chat_id=chat_id, text="Видео пока недоступно.")
        return SECOND

    try:
        send_kw: Dict[str, Any] = {"supports_streaming": True}
        if delivery.width is not None:
            send_kw["width"] = delivery.width
        if delivery.height is not None:
            send_kw["height"] = delivery.height
        if delivery.duration is not None:
            send_kw["duration"] = delivery.duration
        with open(delivery.path, "rb") as video_f:
            if delivery.thumb_path:
                with open(delivery.thumb_path, "rb") as thumb_f:
                    send_kw["thumbnail"] = thumb_f
                    await context.bot.send_video(
                        chat_id=chat_id, video=video_f, **send_kw
                    )
            else:
                await context.bot.send_video(chat_id=chat_id, video=video_f, **send_kw)
    except Exception:
        logger.exception("Failed to send goal video")
        await context.bot.send_message(chat_id=chat_id, text="Ошибка при отправке видео.")
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

    return SECOND
