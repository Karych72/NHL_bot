import asyncio
import html
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackContext

from bot_messages import (
    LEADERBOARD_PAGE_SIZE,
    day_digest,
    day_digest_summary_body,
    game_exists,
    game_message,
    matchup_season_preview,
    player_stat_leaderboard_page,
    stat_leaderboard_for_kind,
    team_stat_leaderboard_page,
    team_table,
    truncate_telegram_text,
)
from dialog_states import (
    CHOOSE_STATS,
    END_CONVERSATION,
    FIRST,
    SECOND,
    THIRD,
    build_menu,
)
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
DIGEST_BACK_FROM_DATE_CALLBACK = "digest:back"
# /tonight: кнопка матча tn:<game_id>:<away>:<home>
TONIGHT_GAME_CALLBACK_PATTERN = r"^tn:\d+:[^:]+:[^:]+$"

# Пагинация в /stats (игроки / вратари / типы бросков / advanced)
STAT_PAGE_CALLBACK_PATTERN = r"^st:([\w_]+):([\w_]+):(\d+)$"
TEAM_PAGE_CALLBACK_PATTERN = r"^tm:([\w_]+):(\d+)$"

# Standalone: /advanced — листание sa:<table>:<col>:<offset>, sa:close
STANDALONE_SA_CALLBACK_PATTERN = r"^sa:"

ADV_CALLBACK_PREFIX = "adv:"


def _stats_menu_nav_row() -> List[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("В начало", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Готово", callback_data=str(END_CONVERSATION)),
    ]


def conversation_player_stat_keyboard(
    table: str,
    column: str,
    offset: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    nav: List[InlineKeyboardButton] = []
    if has_prev:
        prev_off = max(0, offset - LEADERBOARD_PAGE_SIZE)
        nav.append(
            InlineKeyboardButton(
                "← prev",
                callback_data=f"st:{table}:{column}:{prev_off}",
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                "next →",
                callback_data=f"st:{table}:{column}:{offset + LEADERBOARD_PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(_stats_menu_nav_row())
    return InlineKeyboardMarkup(rows)


def conversation_team_stat_keyboard(
    column: str,
    offset: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    nav: List[InlineKeyboardButton] = []
    if has_prev:
        prev_off = max(0, offset - LEADERBOARD_PAGE_SIZE)
        nav.append(
            InlineKeyboardButton(
                "← prev",
                callback_data=f"tm:{column}:{prev_off}",
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                "next →",
                callback_data=f"tm:{column}:{offset + LEADERBOARD_PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(_stats_menu_nav_row())
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


async def callback_stats_player_page(update: Update, context: CallbackContext) -> int:
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
    rows: List[List[InlineKeyboardButton]] = []
    nav: List[InlineKeyboardButton] = []
    if has_prev:
        prev_off = max(0, offset - LEADERBOARD_PAGE_SIZE)
        nav.append(
            InlineKeyboardButton(
                "← prev",
                callback_data=f"sa:{table}:{column}:{prev_off}",
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                "next →",
                callback_data=f"sa:{table}:{column}:{offset + LEADERBOARD_PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton("Готово", callback_data="sa:close")]
    )
    return InlineKeyboardMarkup(rows)


async def callback_standalone_sa(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if query.data == "sa:close":
        await query.answer()
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

# --- Day digest: один матч — полная карточка; несколько — сводка + «Матч N» ---

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
) -> None:
    """Полная карточка матча (текст + опционально клавиатура)."""
    if not game_exists(game_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Такого матча нет в базе бота.",
            parse_mode="HTML",
        )
        return
    text, goals_meta = game_message(game_id)
    gbtn = _goal_video_buttons(goals_meta)
    if reply_markup is not None:
        markup = reply_markup
    elif gbtn:
        markup = InlineKeyboardMarkup(build_menu(gbtn, n_cols=1))
    else:
        markup = None
    await context.bot.send_message(
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

    nav_buttons = [
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
        await context.bot.send_message(
            chat_id=chat_id,
            text=safe_text,
            parse_mode="HTML",
            reply_markup=nav_markup,
        )
        await _pause()
        if not attach_conv_nav_on_last:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Ещё: /stats — меню, /table — таблица, /leaders — лидеры, /help — справка.",
            )
            await _pause()
        return

    if len(real_games) == 1:
        _gid, text, goals_meta = real_games[0]
        goal_buttons = _goal_video_buttons(goals_meta)
        buttons = goal_buttons + (nav_buttons if attach_conv_nav_on_last else [])
        markup = InlineKeyboardMarkup(build_menu(buttons, n_cols=1)) if buttons else None
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
        )
        await _pause()
        if not attach_conv_nav_on_last:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Ещё: /stats — меню, /table — таблица, /leaders — лидеры, /help — справка.",
            )
            await _pause()
        return

    day_str = day_label or "—"
    body = day_digest_summary_body(real_games)
    header = (
        f"<b>Матчи {html.escape(day_str)}</b> ({len(real_games)} игр)\n\n"
    )
    intro = (
        f"{header}{body}\n\n"
        "<i>Нажмите «Матч N» для полной карточки и кнопок видео голов.</i>"
    )
    summary_text = truncate_telegram_text(intro)

    expand_buttons = [
        InlineKeyboardButton(f"Матч {i + 1}", callback_data=f"{DIGEST_EXPAND_PREFIX}{gid}")
        for i, (gid, _t, _m) in enumerate(real_games)
    ]
    rows = build_menu(expand_buttons, n_cols=4)
    if attach_conv_nav_on_last:
        rows.append(nav_buttons)
    markup = InlineKeyboardMarkup(rows)

    await context.bot.send_message(
        chat_id=chat_id, text=summary_text, parse_mode="HTML", reply_markup=markup,
    )
    await _pause()

    if not attach_conv_nav_on_last:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Ещё: /stats — меню, /table — таблица, /leaders — лидеры, /day_games — матчи из базы, /tonight — расписание NHL, /help — справка.",
        )
        await _pause()


async def callback_tonight_game(update: Update, context: CallbackContext) -> None:
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
    if game_exists(game_id):
        await send_game_card_message(context, chat_id, game_id)
        return
    text = truncate_telegram_text(
        matchup_season_preview(away_a, home_a),
        footer_note="\n\n(Текст обрезан — лимит Telegram.)",
    )
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def callback_expand_digest_game(update: Update, context: CallbackContext) -> None:
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
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Главное меню", callback_data=str(CHOOSE_STATS))]]
    )
    await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=team_table(),
        parse_mode="HTML",
        reply_markup=markup,
    )
    return FIRST


async def bot_digest_calendar_today(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    day_label, games = day_digest(date.today().isoformat())
    await dispatch_day_digest_messages(
        context,
        query.message.chat.id,
        day_label,
        games,
        attach_conv_nav_on_last=True,
    )
    return SECOND


async def bot_digest_calendar_yesterday(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    day_label, games = day_digest((date.today() - timedelta(days=1)).isoformat())
    await dispatch_day_digest_messages(
        context,
        query.message.chat.id,
        day_label,
        games,
        attach_conv_nav_on_last=True,
    )
    return SECOND


async def bot_digest_pick_date_prompt(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    back_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "« Назад",
                    callback_data=DIGEST_BACK_FROM_DATE_CALLBACK,
                )
            ]
        ]
    )
    await query.edit_message_text(
        "Отправьте дату одним сообщением в формате <code>YYYY-MM-DD</code>.\n"
        "/cancel — выход из меню.",
        parse_mode="HTML",
        reply_markup=back_kb,
    )
    return THIRD


async def bot_digest_custom_date(update: Update, context: CallbackContext) -> int:
    message = update.message
    assert message is not None
    raw = (message.text or "").strip()
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        await message.reply_text(
            "Нужен формат YYYY-MM-DD (например 2025-12-01). Попробуйте снова или /cancel."
        )
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
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    if has_prev:
        prev_off = max(0, offset - LEADERBOARD_PAGE_SIZE)
        row.append(
            InlineKeyboardButton(
                "← prev",
                callback_data=f"pl:{kind}:{prev_off}",
            )
        )
    if has_next:
        row.append(
            InlineKeyboardButton(
                "next →",
                callback_data=f"pl:{kind}:{offset + LEADERBOARD_PAGE_SIZE}",
            )
        )
    if row:
        rows.append(row)
    rows.extend(list(row) for row in leaders_category_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(rows)


async def callback_leaders_pick(update: Update, context: CallbackContext) -> None:
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

    delivery = download_goal_video(game_id, event_id)
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
