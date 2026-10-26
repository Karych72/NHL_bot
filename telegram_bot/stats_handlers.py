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
from typing import Dict, List, Optional, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
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
    conference_summary,
    country_game_block,
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
    LAST_MENU_MESSAGE_ID_KEY,
    PLAYER_ADVANCED_SUBMENU,
    PLAYER_FIELD,
    PLAYER_GOALIE,
    TEAM_PROFILE_PICK,
    TEAM_STATS,
    StatsMenu,
    build_menu,
    callback_message,
)
from help_text import ADVANCED_COMMAND_INTRO, SUBSCRIPTIONS_FOOTER
from subscription_repo import get_chat_subscriptions
from leaderboard_specs import (
    ADVANCED_METRICS,
    PLAYER_STAT_TITLES,
    TEAM_STAT_TITLES,
)
from video_replay import download_goal_video

logger = logging.getLogger(__name__)

# /leaders: выбор категории, затем pl:<kind>:<offset>
LEADERS_PICK_CALLBACK_PATTERN = r"^pl:pick:(points|goals|assists)$"
LEADERBOARD_PAGE_CALLBACK_PATTERN = r"^pl:(points|goals|assists):(\d+)$"
DIGEST_EXPAND_PREFIX = "dg:"
DIGEST_EXPAND_CALLBACK_PATTERN = rf"^{DIGEST_EXPAND_PREFIX}(\d+)$"
DIGEST_BACK_FROM_DATE_CALLBACK = "digest:back"
# /tonight: кнопка матча tn:<game_id>:<away>:<home>
TONIGHT_GAME_CALLBACK_PATTERN = r"^tn:(\d+):([^:]+):([^:]+)$"

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

# Меню /advanced: adv:<ключ ADVANCED_METRICS> открывает статистику, adv:close снимает кнопки
ADVANCED_CALLBACK_PATTERN = rf"^adv:({'|'.join(ADVANCED_METRICS)}|close)$"

# Кнопка видео гола карточки матча: gv:<game_id>:<event_id>
GOAL_VIDEO_CALLBACK_PATTERN = r"^gv:(\d+):(\d+)$"


def _stats_menu_nav_row(parent_state: int) -> List[InlineKeyboardButton]:
    """Нижний ряд навигации страницы стата: родитель / корень / выход.

    `parent_state` — callback_data родительского экрана (подменю, из которого
    открыли эту страницу): «« Назад» ведёт туда, а не сразу в корень.
    Родитель для страниц статов игроков/вратарей выводится из
    (table, column) — см. `_player_stat_parent_state`.
    """
    return [
        InlineKeyboardButton(text="« Назад", callback_data=str(parent_state)),
        InlineKeyboardButton(text="В начало", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton(text="Готово", callback_data=str(END_CONVERSATION)),
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
        nav.append(InlineKeyboardButton(text=f"« {span(0)}", callback_data=f"{prefix}:0"))
    if has_prev:
        prev_off = max(0, offset - size)
        nav.append(InlineKeyboardButton(text=f"← {span(prev_off)}", callback_data=f"{prefix}:{prev_off}"))
    if has_next:
        next_off = offset + size
        nav.append(InlineKeyboardButton(text=f"{span(next_off)} →", callback_data=f"{prefix}:{next_off}"))
    return [nav] if nav else []


async def _edit_page(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовывает листаемую страницу (HTML) на месте сообщения с нажатой кнопкой.

    Зачем: повторное нажатие кнопки текущей страницы Telegram отвергает «message is
    not modified» — это штатный случай, он глушится; любая другая ошибка всплывает.
    Общая для всех листаемых экранов: таблиц `/stats`, `/advanced`, `/leaders`, `/countries`.

    Аргументы: `callback` — нажатие, `text` — HTML страницы, `markup` — её клавиатура.
    """
    try:
        await callback_message(callback).edit_text(text=text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


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
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _make_paginated_player_stat_open_handler(table: str, column: str):
    title = PLAYER_STAT_TITLES[(table, column)]

    async def handler(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        text, hp, hn = player_stat_leaderboard_page(title, table, column, 0)
        markup = conversation_player_stat_keyboard(table, column, 0, hp, hn)
        await callback_message(callback).edit_text(text=text, parse_mode="HTML", reply_markup=markup)
        await state.set_state(StatsMenu.second)

    return handler


def _make_paginated_team_stat_open_handler(column: str):
    title = TEAM_STAT_TITLES[column]

    async def handler(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        text, hp, hn = team_stat_leaderboard_page(title, column, 0)
        markup = conversation_team_stat_keyboard(column, 0, hp, hn)
        await callback_message(callback).edit_text(text=text, parse_mode="HTML", reply_markup=markup)
        await state.set_state(StatsMenu.second)

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

    async def handler(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        markup = InlineKeyboardMarkup(inline_keyboard=[_stats_menu_nav_row(TEAM_STATS)])
        await callback_message(callback).edit_text(
            text=summary_func(), parse_mode="HTML", reply_markup=markup
        )
        await state.set_state(StatsMenu.second)

    return handler


async def bot_team_profile_pick(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Профиль команды» подменю команд: список аббревиатур сезона
    для выбора (Задача 41, Фаза D) — сетка кнопок `tp:<ABBR>` (разбирает
    `bot_team_profile_show`), футер «« Назад»» на подменю команд (`TEAM_STATS`).
    Тот же хендлер перерисовывает экран, когда «« Назад»» с профиля
    возвращает сюда (`TEAM_PROFILE_PICK` зарегистрирован и в `first`, и в
    `second` — см. `bot.py`).

    Пустой сезон (Задача 36): нет команд в базе — текст с причиной вместо
    клавиатуры без кнопок.
    """
    await callback.answer()
    abbrevs = season_team_abbrevs()
    buttons = [
        InlineKeyboardButton(text=ab, callback_data=f"{TEAM_PROFILE_CALLBACK_PREFIX}{ab}")
        for ab in abbrevs
    ]
    rows = build_menu(buttons, n_cols=4, footer_buttons=[_stats_menu_nav_row(TEAM_STATS)])
    text = "Выберите команду:" if abbrevs else "В базе нет команд для этого сезона."
    await callback_message(callback).edit_text(
        text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await state.set_state(StatsMenu.second)


async def bot_team_profile_show(callback: CallbackQuery, state: FSMContext, match: re.Match) -> None:
    """Обрабатывает выбор аббревиатуры на экране профиля команды (`tp:<ABBR>`):
    строит `team_profile()` и показывает его. «« Назад»» ведёт на список
    команд (`TEAM_PROFILE_PICK`), а не сразу в `TEAM_STATS` — так можно
    выбрать другую команду без лишнего клика.

    `match` — совпадение `TEAM_PROFILE_CALLBACK_PATTERN` (фильтр регистрации в `bot.py`).
    Неизвестная/ненайденная в базе аббревиатура — не отдельная ветка здесь:
    `team_profile()` сама возвращает текст с причиной (Задача 36).
    """
    await callback.answer()
    text = truncate_telegram_text(team_profile(match.group(1)))
    markup = InlineKeyboardMarkup(inline_keyboard=[_stats_menu_nav_row(TEAM_PROFILE_PICK)])
    await callback_message(callback).edit_text(text=text, parse_mode="HTML", reply_markup=markup)
    await state.set_state(StatsMenu.second)


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


async def callback_stats_player_page(callback: CallbackQuery, state: FSMContext, match: re.Match) -> None:
    """Листает страницу статистики игрока/вратаря по callback_data
    `st:<table>:<column>:<offset>` (`match` — совпадение `STAT_PAGE_CALLBACK_PATTERN`),
    перерисовывая сообщение на месте; диалог переходит в `second` — хендлер
    зарегистрирован и в `first`, и в `second`.

    Неизвестная пара (table, column) не роняет диалог: хендлер отвечает на
    нажатие, ничего не перерисовывая и не меняя состояния.
    """
    table, col, off_s = match.group(1), match.group(2), match.group(3)
    key = (table, col)
    await callback.answer()
    if key not in PLAYER_STAT_TITLES:
        return
    offset = int(off_s)
    text, hp, hn = player_stat_leaderboard_page(PLAYER_STAT_TITLES[key], table, col, offset)
    await _edit_page(callback, text, conversation_player_stat_keyboard(table, col, offset, hp, hn))
    await state.set_state(StatsMenu.second)


async def callback_stats_team_page(callback: CallbackQuery, state: FSMContext, match: re.Match) -> None:
    """Листает страницу командной статистики по callback_data `tm:<column>:<offset>`
    (`match` — совпадение `TEAM_PAGE_CALLBACK_PATTERN`), перерисовывая сообщение на
    месте, — симметрично `callback_stats_player_page`.
    """
    col, off_s = match.group(1), match.group(2)
    await callback.answer()
    if col not in TEAM_STAT_TITLES:
        return
    offset = int(off_s)
    text, hp, hn = team_stat_leaderboard_page(TEAM_STAT_TITLES[col], col, offset)
    await _edit_page(callback, text, conversation_team_stat_keyboard(col, offset, hp, hn))
    await state.set_state(StatsMenu.second)


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
        InlineKeyboardButton(text="« К списку статистик", callback_data="sa:menu"),
        InlineKeyboardButton(text="Сохранить", callback_data="sa:save"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def standalone_stat_reply(table: str, column: str, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Страница статистики меню `/advanced` с клавиатурой `standalone_player_stat_keyboard`.

    Аргументы: `(table, column)` — ключ `PLAYER_STAT_TITLES`, `offset` — сдвиг страницы.
    """
    text, has_prev, has_next = player_stat_leaderboard_page(
        PLAYER_STAT_TITLES[(table, column)], table, column, offset
    )
    return text, standalone_player_stat_keyboard(table, column, offset, has_prev, has_next)


async def callback_standalone_sa(callback: CallbackQuery, bot: Bot) -> None:
    """Обрабатывает нажатия кнопок `sa:...` вне диалога `/stats`:
    `sa:menu` — меню `/advanced` на месте страницы; `sa:save` — страница
    остаётся в чате без кнопок, а её копия с теми же кнопками уходит новым
    сообщением; `sa:<table>:<column>:<offset>` листает страницу статистики так
    же, как `callback_stats_player_page`, но без состояний FSM.
    """
    message = callback_message(callback)
    if callback.data == "sa:menu":
        await callback.answer()
        await message.edit_text(
            text=ADVANCED_COMMAND_INTRO,
            parse_mode="HTML",
            reply_markup=advanced_standalone_keyboard(),
        )
        return
    if callback.data == "sa:save":
        await callback.answer("Сохранено")
        # Сначала копия, потом снятие кнопок: упади отправка — страница останется живой.
        await bot.send_message(
            chat_id=message.chat.id,
            text=message.html_text,
            parse_mode="HTML",
            reply_markup=message.reply_markup,
        )
        await message.edit_reply_markup(reply_markup=None)
        return
    await callback.answer()
    m = re.match(r"^sa:([\w_]+):([\w_]+):(\d+)$", callback.data or "")
    if not m or (m.group(1), m.group(2)) not in PLAYER_STAT_TITLES:
        return
    text, markup = standalone_stat_reply(m.group(1), m.group(2), int(m.group(3)))
    await _edit_page(callback, text, markup)


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
        return await bot.send_message(
            chat_id=chat_id,
            text="Такого матча нет в базе бота.",
            parse_mode="HTML",
        )
    text, goals_meta = game_message(game_id)
    for country in get_chat_subscriptions(chat_id).countries:
        block = country_game_block(country, game_id)
        if block is not None:
            text += f"\n\n{block}"
    return await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=goal_video_markup(goals_meta),
    )


async def dispatch_day_digest_messages(
    bot: Bot,
    chat_id: int,
    day_label: Optional[str],
    games: List[Tuple[int, str, List[Dict]]],
    *,
    dialog: Optional[FSMContext] = None,
    inter_message_sleep_sec: Optional[float] = None,
) -> None:
    """Дайджест дня: один матч — одна карточка; несколько — сводка + кнопки разворота.

    Аргументы:
        bot, chat_id: куда слать.
        day_label, games: ответ `day_digest()`.
        dialog: состояние диалога `/stats`, если дайджест открыт из него: под последним
            сообщением — кнопки навигации диалога, а его id записывается для `/cancel`.
            Без него (`/today`, ИИ, рассылка) вместо навигации — подсказка «Ещё: …».
        inter_message_sleep_sec: пауза после каждого сообщения (рассылка).
    """

    async def _pause() -> None:
        if inter_message_sleep_sec is not None and inter_message_sleep_sec > 0:
            await asyncio.sleep(inter_message_sleep_sec)

    async def _finish(sent: Message) -> None:
        """Конец дайджеста: в диалоге — запись id для `/cancel`, вне его — подсказка."""
        if dialog is not None:
            await dialog.update_data({LAST_MENU_MESSAGE_ID_KEY: sent.message_id})
        await _pause()
        if dialog is None:
            await bot.send_message(chat_id=chat_id, text=_DIGEST_MORE_HINT)
            await _pause()

    # Родитель результата дайджеста — меню дайджеста (DAY_DIGEST), а не сразу
    # корень: «Сегодня»/«Вчера»/ввод даты открываются из bot_digest_date_menu,
    # туда и ведёт «« Назад»».
    nav_buttons = [
        InlineKeyboardButton(text="« Назад", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton(text="В начало", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton(text="Закрыть меню", callback_data=str(END_CONVERSATION)),
    ]

    real_games = [(gid, text, meta) for gid, text, meta in games if gid != 0]

    if not real_games:
        _, text, _ = games[0]
        nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons]) if dialog else None
        safe_text = html.escape(text) if text else ""
        sent = await bot.send_message(
            chat_id=chat_id,
            text=safe_text,
            parse_mode="HTML",
            reply_markup=nav_markup,
        )
        await _finish(sent)
        return

    if len(real_games) == 1:
        _gid, text, goals_meta = real_games[0]
        goal_buttons = _goal_video_buttons(goals_meta)
        rows = build_menu(
            goal_buttons,
            n_cols=_GOAL_VIDEO_COLUMNS,
            footer_buttons=[[b] for b in nav_buttons] if dialog else None,
        )
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        sent = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
        )
        await _finish(sent)
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
        InlineKeyboardButton(text=label, callback_data=f"{DIGEST_EXPAND_PREFIX}{gid}")
        for label, gid in zip(digest_game_button_labels(game_ids), game_ids)
    ]
    rows = build_menu(expand_buttons, n_cols=3)
    if dialog:
        rows.append(nav_buttons)
    sent = await bot.send_message(
        chat_id=chat_id,
        text=summary_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await _finish(sent)


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
        pointer = await bot.send_message(
            chat_id=chat_id,
            text=_TONIGHT_ALREADY_SENT,
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
        text = truncate_telegram_text(matchup_season_preview(game_id, away, home))
        answer = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    sent[key] = [answer.message_id, None]


async def callback_expand_digest_game(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Обрабатывает кнопку матча сжатой сводки дайджеста (`dg:<game_id>`, `match` —
    совпадение `DIGEST_EXPAND_CALLBACK_PATTERN`) — досылает полную карточку
    конкретного матча отдельным сообщением.
    """
    assert callback.message is not None
    await callback.answer()
    await send_game_card_message(bot, callback.message.chat.id, int(match.group(1)))


async def callback_standalone_adv(callback: CallbackQuery, match: re.Match) -> None:
    """Обрабатывает нажатие кнопки меню `/advanced` (`adv:<key>`, `match` — совпадение
    `ADVANCED_CALLBACK_PATTERN`): `adv:close` снимает клавиатуру, иначе по ключу из
    `ADVANCED_METRICS` открывается первая страница этой статистики — дальше она
    листается уже как `sa:...` (см. `standalone_player_stat_keyboard`).
    """
    await callback.answer()
    key = match.group(1)
    if key == "close":
        await callback_message(callback).edit_reply_markup(reply_markup=None)
        return
    text, kb = standalone_stat_reply(*ADVANCED_METRICS[key], 0)
    await callback_message(callback).edit_text(text=text, parse_mode="HTML", reply_markup=kb)


def advanced_standalone_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура меню `/advanced`: продвинутая статистика (SAT/USAT/GF%/OZ
    Start%/Shootout%) и голы по типам бросков. Каждая кнопка — `adv:<key>`,
    который разбирает `callback_standalone_adv`.
    """
    rows = [
        [
            InlineKeyboardButton(text="SAT %", callback_data="adv:sat"),
            InlineKeyboardButton(text="USAT %", callback_data="adv:usat"),
        ],
        [
            InlineKeyboardButton(text="GF %", callback_data="adv:gf"),
            InlineKeyboardButton(text="OZ Start %", callback_data="adv:oz"),
        ],
        [InlineKeyboardButton(text="Shootout %", callback_data="adv:so")],
        [
            InlineKeyboardButton(text="Кистевой", callback_data="adv:wrist"),
            InlineKeyboardButton(text="Щелчок", callback_data="adv:slap"),
        ],
        [
            InlineKeyboardButton(text="С полуприёма", callback_data="adv:snap"),
            InlineKeyboardButton(text="Бэкхенд", callback_data="adv:back"),
        ],
        [
            InlineKeyboardButton(text="Направление", callback_data="adv:tip"),
            InlineKeyboardButton(text="Отклонение", callback_data="adv:defl"),
        ],
        [InlineKeyboardButton(text="С за ворот", callback_data="adv:wrap")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def bot_league_standings(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Турнирная таблица NHL: отдельное сообщение с кнопкой «« Главное меню»».

    `LEAGUE_STANDINGS` — прямой пункт главного меню, поэтому родитель этой
    страницы уже корень: кнопка ведёт в `CHOOSE_STATS`. Сообщение новое
    (`send_message`, не `edit_text`), поэтому его id записывается в данные
    диалога, чтобы `/cancel` мог снять с него клавиатуру.
    """
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Главное меню", callback_data=str(CHOOSE_STATS))]]
    )
    sent = await bot.send_message(
        chat_id=callback_message(callback).chat.id,
        text=team_table(),
        parse_mode="HTML",
        reply_markup=markup,
    )
    await state.update_data({LAST_MENU_MESSAGE_ID_KEY: sent.message_id})
    await state.set_state(StatsMenu.first)


async def bot_digest_calendar_today(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Кнопка «Сегодня» меню дайджеста дня (диалог `/stats`): собирает и
    рассылает дайджест матчей прошедшей ночи по Москве (`last_night_day()`),
    как `/today`, и переводит диалог в `second` — там же обрабатываются
    «« Назад»» и «В начало»; кнопки матчей (`dg:`) — глобальный хендлер вне диалога.
    """
    await callback.answer()
    day_label, games = day_digest(last_night_day())
    await dispatch_day_digest_messages(
        bot, callback_message(callback).chat.id, day_label, games, dialog=state,
    )
    await state.set_state(StatsMenu.second)


async def bot_digest_calendar_yesterday(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Кнопка «Вчера» меню дайджеста дня — то же самое, что
    `bot_digest_calendar_today`, но на ночь раньше.
    """
    await callback.answer()
    day_label, games = day_digest(last_night_day(nights_back=1))
    await dispatch_day_digest_messages(
        bot, callback_message(callback).chat.id, day_label, games, dialog=state,
    )
    await state.set_state(StatsMenu.second)


def _digest_back_from_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="« Назад",
                    callback_data=DIGEST_BACK_FROM_DATE_CALLBACK,
                )
            ]
        ]
    )


async def bot_digest_pick_date_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Другая дата» меню дайджеста дня: просит прислать дату отдельным
    сообщением в формате `YYYY-MM-DD` и переводит диалог в ожидание текста
    (`third` — там зарегистрирован `bot_digest_custom_date`).
    """
    await callback.answer()
    await callback_message(callback).edit_text(
        "Отправьте дату одним сообщением в формате <code>YYYY-MM-DD</code>.\n"
        "/cancel — выход из меню.",
        parse_mode="HTML",
        reply_markup=_digest_back_from_date_keyboard(),
    )
    await state.set_state(StatsMenu.third)


async def bot_digest_custom_date(message: Message, state: FSMContext, bot: Bot) -> None:
    """Обрабатывает текст с датой, присланный в состоянии `third` (после
    `bot_digest_pick_date_prompt`). При неверном формате переспрашивает новым
    сообщением (id пишется в данные диалога под `LAST_MENU_MESSAGE_ID_KEY`) и
    остаётся в `third`; при корректной дате собирает и рассылает дайджест и
    переводит диалог в `second`.
    """
    raw = (message.text or "").strip()
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        # Новое сообщение (не edit_text) со своей копией «« Назад»» — id
        # записывается, чтобы /cancel мог снять клавиатуру с этой конкретной
        # (последней) копии.
        sent = await message.answer(
            "Нужен формат YYYY-MM-DD (например 2025-12-01). Попробуйте снова или /cancel.",
            reply_markup=_digest_back_from_date_keyboard(),
        )
        await state.update_data({LAST_MENU_MESSAGE_ID_KEY: sent.message_id})
        return
    day_label, games = day_digest(raw)
    await dispatch_day_digest_messages(bot, message.chat.id, day_label, games, dialog=state)
    await state.set_state(StatsMenu.second)


def leaders_category_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора категории `/leaders` (Очки/Голы/Передачи); каждая
    кнопка — `pl:pick:<kind>`, разбирает `callback_leaders_pick`.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Очки", callback_data="pl:pick:points"),
                InlineKeyboardButton(text="Голы", callback_data="pl:pick:goals"),
                InlineKeyboardButton(text="Передачи", callback_data="pl:pick:assists"),
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


def leaders_reply(kind: str, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Страница лидерборда `/leaders` категории `kind` (points / goals / assists) со сдвигом
    `offset` и её клавиатура `leaderboard_nav_keyboard`."""
    text, has_prev, has_next = stat_leaderboard_for_kind(kind, offset)
    return text, leaderboard_nav_keyboard(kind, offset, has_prev, has_next)


async def callback_leaders_pick(callback: CallbackQuery, match: re.Match) -> None:
    """Обрабатывает выбор категории `/leaders` (`pl:pick:<kind>`, `match` — совпадение
    `LEADERS_PICK_CALLBACK_PATTERN`) — открывает первую страницу соответствующего
    лидерборда на месте.
    """
    await callback.answer()
    text, markup = leaders_reply(match.group(1), 0)
    await _edit_page(callback, text, markup)


async def callback_leaderboard_page(callback: CallbackQuery, match: re.Match) -> None:
    """Листает лидерборд `/leaders` по callback_data `pl:<kind>:<offset>` (`match` —
    совпадение `LEADERBOARD_PAGE_CALLBACK_PATTERN`) — симметрично
    `callback_leaders_pick`, но с готовым смещением страницы.
    """
    await callback.answer()
    text, markup = leaders_reply(match.group(1), int(match.group(2)))
    await _edit_page(callback, text, markup)


async def handle_goal_video(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Скачивает клип гола по кнопке `gv:<game_id>:<event_id>` (`match` — совпадение
    `GOAL_VIDEO_CALLBACK_PATTERN`) и шлёт его в чат с подписью
    ``GoalVideoDelivery.caption`` (кто забил, с чьих передач, когда)."""
    assert callback.message is not None
    await callback.answer("Загружаю видео гола...")
    game_id, event_id = int(match.group(1)), int(match.group(2))
    chat_id = callback.message.chat.id

    # В отдельном потоке: скачивание MP4 и два прохода ffmpeg блокируют
    # единственный event loop бота до ~2 минут (см. video_replay).
    delivery = await asyncio.to_thread(download_goal_video, game_id, event_id)
    if delivery is None:
        await bot.send_message(chat_id=chat_id, text="Видео пока недоступно.")
        return

    try:
        await bot.send_video(
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
        await bot.send_message(chat_id=chat_id, text="Ошибка при отправке видео.")
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
