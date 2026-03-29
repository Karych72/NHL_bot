import logging
import os
from functools import partial
from typing import Callable, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from bot_messages import (
    day_digest,
    day_digest_summary_body,
    game_message,
    game_exists,
    leaders_top10_messages,
    player_stats,
    team_stats,
    truncate_telegram_text,
)
from dialog_states import (
    CHOOSE_STATS,
    END_CONVERSATION,
    SECOND,
    build_menu,
)
from video_replay import download_goal_video

logger = logging.getLogger(__name__)

LEADERS_TOP10_CALLBACK = "lb:10"
DIGEST_EXPAND_PREFIX = "dg:"

# Standalone callbacks (вне ConversationHandler): /advanced, /shottypes
ADV_CALLBACK_PREFIX = "adv:"
SHOT_CALLBACK_PREFIX = "shot:"


def _make_stats_handler(
    data_func: Callable[[], str],
    back_label: str = "В начало",
):
    """Factory: builds a callback handler that fetches stats and shows nav buttons."""
    def handler(update: Update, context: CallbackContext) -> int:
        query = update.callback_query
        query.answer()
        keyboard = [
            InlineKeyboardButton(back_label, callback_data=str(CHOOSE_STATS)),
            InlineKeyboardButton("Готово", callback_data=str(END_CONVERSATION)),
        ]
        reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
        text = data_func()
        query.edit_message_text(text=text, parse_mode='MARKDOWN', reply_markup=reply_markup)
        return SECOND
    return handler


# --- Player (field) stats ---

bot_player_points = _make_stats_handler(partial(player_stats, 'Лучшие бомбардиры', 'players_season_stats', 'points'))
bot_player_goals = _make_stats_handler(partial(player_stats, 'Лучшие Снайперы', 'players_season_stats', 'goals'))
bot_player_assists = _make_stats_handler(partial(player_stats, 'Лучшие Ассистенты', 'players_season_stats', 'assists'))
bot_player_hits = _make_stats_handler(partial(player_stats, 'Лидеры по хитам', 'players_season_stats', 'hits'))
bot_player_plus_minus = _make_stats_handler(partial(player_stats, 'Лидеры по показателю +-', 'players_season_stats', 'plus_minus'))
bot_player_penalties = _make_stats_handler(partial(player_stats, 'Лидеры по штрафным минутам', 'players_season_stats', 'pim'))
bot_player_blocks = _make_stats_handler(partial(player_stats, 'Лидеры по блокам', 'players_season_stats', 'blocked'))
bot_player_ice_time = _make_stats_handler(partial(player_stats, 'Лидеры по среднему игровому времени', 'players_season_stats', 'time_on_ice_per_game'))

bot_player_sat_pct = _make_stats_handler(
    partial(player_stats, 'Лидеры по Corsi (SAT %)', 'players_advanced_stats', 'sat_pct'),
)
bot_player_usat_pct = _make_stats_handler(
    partial(player_stats, 'Лидеры по Fenwick (USAT %)', 'players_advanced_stats', 'usat_pct'),
)
bot_player_goals_for_pct = _make_stats_handler(
    partial(player_stats, 'Лидеры по Goals For %', 'players_advanced_stats', 'goals_pct'),
)
bot_player_oz_start_pct = _make_stats_handler(
    partial(player_stats, 'Лидеры по старту в зоне атаки (OZ Start %)', 'players_advanced_stats', 'oz_start_pct'),
)
bot_player_shootout_pct = _make_stats_handler(
    partial(player_stats, 'Лидеры по реализации буллитов (Shootout %)', 'players_season_stats', 'shootout_pct'),
)

# --- Goalie stats ---

bot_goalie_wins = _make_stats_handler(
    partial(player_stats, 'Лидеры по победам', 'goalies_season_stats', 'wins'),
)
bot_goalie_percentage = _make_stats_handler(
    partial(player_stats, 'Лидеры по проценту отраженных бросков', 'goalies_season_stats', 'save_percentage'),
)
bot_goalie_shootouts = _make_stats_handler(
    partial(player_stats, 'Лидеры cухим матчам', 'goalies_season_stats', 'shutouts'),
)

# --- Team stats ---

bot_team_procent_wins = _make_stats_handler(partial(team_stats, 'Статистика процента набранных очков', 'procent_points'))
bot_team_power_play = _make_stats_handler(partial(team_stats, 'Статистика большинства', 'power_play_percentage'))
bot_team_power_kill = _make_stats_handler(partial(team_stats, 'Статистика меньшинства', 'penalty_kill_percentage'))

# --- Shot type leaders (players_shot_types) ---

bot_player_shot_wrist = _make_stats_handler(
    partial(player_stats, 'Голы с кистевого', 'players_shot_types', 'goals_wrist'),
)
bot_player_shot_slap = _make_stats_handler(
    partial(player_stats, 'Голы щелчком', 'players_shot_types', 'goals_slap'),
)
bot_player_shot_snap = _make_stats_handler(
    partial(player_stats, 'Голы с полуприёма', 'players_shot_types', 'goals_snap'),
)
bot_player_shot_backhand = _make_stats_handler(
    partial(player_stats, 'Голы с бэкхенда', 'players_shot_types', 'goals_backhand'),
)
bot_player_shot_tip_in = _make_stats_handler(
    partial(player_stats, 'Голы направлением', 'players_shot_types', 'goals_tip_in'),
)
bot_player_shot_deflected = _make_stats_handler(
    partial(player_stats, 'Голы отклонением', 'players_shot_types', 'goals_deflected'),
)
bot_player_shot_wrap = _make_stats_handler(
    partial(player_stats, 'Голы с за ворот', 'players_shot_types', 'goals_wrap_around'),
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


def send_game_card_message(
    context: CallbackContext,
    chat_id: int,
    game_id: int,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Полная карточка матча (текст + опционально клавиатура)."""
    if not game_exists(game_id):
        context.bot.send_message(
            chat_id=chat_id,
            text=f"Матч `game_id={game_id}` в базе не найден.",
            parse_mode="Markdown",
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
    context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="MARKDOWN", reply_markup=markup,
    )


def dispatch_day_digest_messages(
    context: CallbackContext,
    chat_id: int,
    day_label: Optional[str],
    games: List[Tuple[int, str, List[Dict]]],
    *,
    attach_conv_nav_on_last: bool = True,
) -> None:
    """Дайджест дня: один матч — одна карточка; несколько — сводка + кнопки разворота."""
    nav_buttons = [
        InlineKeyboardButton("В начало", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Закрыть меню", callback_data=str(END_CONVERSATION)),
    ]

    real_games = [(gid, text, meta) for gid, text, meta in games if gid != 0]

    if not real_games:
        _, text, _ = games[0]
        context.bot.send_message(chat_id=chat_id, text=text, parse_mode="MARKDOWN")
        if not attach_conv_nav_on_last:
            context.bot.send_message(
                chat_id=chat_id,
                text="Ещё: /stats — меню, /table — таблица, /leaders — лидеры, /help — все команды.",
            )
        return

    if len(real_games) == 1:
        _gid, text, goals_meta = real_games[0]
        goal_buttons = _goal_video_buttons(goals_meta)
        buttons = goal_buttons + (nav_buttons if attach_conv_nav_on_last else [])
        markup = InlineKeyboardMarkup(build_menu(buttons, n_cols=1)) if buttons else None
        context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="MARKDOWN", reply_markup=markup,
        )
        if not attach_conv_nav_on_last:
            context.bot.send_message(
                chat_id=chat_id,
                text="Ещё: /stats — меню, /table — таблица, /leaders — лидеры, /help — все команды.",
            )
        return

    day_str = day_label or "—"
    body = day_digest_summary_body(real_games)
    header = f"*Матчи {day_str}* ({len(real_games)} игр)\n\n"
    intro = (
        f"{header}{body}\n\n"
        f"_Нажмите «Матч N» для полной карточки и кнопок видео голов._"
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

    context.bot.send_message(
        chat_id=chat_id, text=summary_text, parse_mode="MARKDOWN", reply_markup=markup,
    )

    if not attach_conv_nav_on_last:
        context.bot.send_message(
            chat_id=chat_id,
            text="Ещё: /stats — меню, /table — таблица, /leaders — лидеры, /game (id), /help — все команды.",
        )


def callback_expand_digest_game(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data.startswith(DIGEST_EXPAND_PREFIX):
        return
    query.answer()
    try:
        game_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        context.bot.send_message(chat_id=query.message.chat_id, text="Некорректная ссылка на матч.")
        return
    send_game_card_message(context, query.message.chat_id, game_id)


_ADV_STANDALONE_FUNCS = {
    "sat": partial(player_stats, 'Лидеры по Corsi (SAT %)', 'players_advanced_stats', 'sat_pct'),
    "usat": partial(player_stats, 'Лидеры по Fenwick (USAT %)', 'players_advanced_stats', 'usat_pct'),
    "gf": partial(player_stats, 'Лидеры по Goals For %', 'players_advanced_stats', 'goals_pct'),
    "oz": partial(player_stats, 'Лидеры по старту в зоне атаки (OZ Start %)', 'players_advanced_stats', 'oz_start_pct'),
    "so": partial(player_stats, 'Лидеры по реализации буллитов (Shootout %)', 'players_season_stats', 'shootout_pct'),
}

_SHOT_STANDALONE_FUNCS = {
    "wrist": partial(player_stats, 'Голы с кистевого', 'players_shot_types', 'goals_wrist'),
    "slap": partial(player_stats, 'Голы щелчком', 'players_shot_types', 'goals_slap'),
    "snap": partial(player_stats, 'Голы с полуприёма', 'players_shot_types', 'goals_snap'),
    "back": partial(player_stats, 'Голы с бэкхенда', 'players_shot_types', 'goals_backhand'),
    "tip": partial(player_stats, 'Голы направлением', 'players_shot_types', 'goals_tip_in'),
    "defl": partial(player_stats, 'Голы отклонением', 'players_shot_types', 'goals_deflected'),
    "wrap": partial(player_stats, 'Голы с за ворот', 'players_shot_types', 'goals_wrap_around'),
}


def callback_standalone_adv(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query:
        return
    key = query.data.split(":", 1)[1]
    if key == "close":
        query.answer()
        query.edit_message_text(
            "Готово. Снова: /advanced. Меню: /stats. Справка: /help.",
            parse_mode="Markdown",
        )
        return
    query.answer()
    fn = _ADV_STANDALONE_FUNCS.get(key)
    if not fn:
        return
    text = fn()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Готово", callback_data="adv:close")]])
    query.edit_message_text(text=text, parse_mode="MARKDOWN", reply_markup=kb)


def callback_standalone_shot(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query:
        return
    key = query.data.split(":", 1)[1]
    if key == "close":
        query.answer()
        query.edit_message_text(
            "Готово. Снова: /shottypes. Меню: /stats. Справка: /help.",
            parse_mode="Markdown",
        )
        return
    query.answer()
    fn = _SHOT_STANDALONE_FUNCS.get(key)
    if not fn:
        return
    text = fn()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Готово", callback_data="shot:close")]])
    query.edit_message_text(text=text, parse_mode="MARKDOWN", reply_markup=kb)


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
    ]
    return InlineKeyboardMarkup(rows)


def shottypes_standalone_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Кистевой", callback_data="shot:wrist"),
            InlineKeyboardButton("Щелчок", callback_data="shot:slap"),
        ],
        [
            InlineKeyboardButton("С полуприёма", callback_data="shot:snap"),
            InlineKeyboardButton("Бэкхенд", callback_data="shot:back"),
        ],
        [
            InlineKeyboardButton("Направление", callback_data="shot:tip"),
            InlineKeyboardButton("Отклонение", callback_data="shot:defl"),
        ],
        [InlineKeyboardButton("С за ворот", callback_data="shot:wrap")],
    ]
    return InlineKeyboardMarkup(rows)


def bot_day_digest(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    day_label, games = day_digest()
    dispatch_day_digest_messages(
        context,
        query.message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=True,
    )
    return SECOND


def callback_leaders_top10(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query:
        query.answer()
        chat_id = query.message.chat_id
        for text in leaders_top10_messages():
            context.bot.send_message(chat_id=chat_id, text=text, parse_mode='MARKDOWN')


def handle_goal_video(update: Update, context: CallbackContext) -> int:
    """Download and send goal video when button is pressed."""
    query = update.callback_query
    query.answer("Загружаю видео гола...")

    parts = query.data.split(":")
    game_id = int(parts[1])
    event_id = int(parts[2])

    chat_id = query.message.chat_id

    filepath = download_goal_video(game_id, event_id)
    if filepath is None:
        context.bot.send_message(chat_id=chat_id, text="Видео пока недоступно.")
        return SECOND

    try:
        with open(filepath, "rb") as video:
            context.bot.send_video(chat_id=chat_id, video=video, supports_streaming=True)
    except Exception:
        logger.exception("Failed to send goal video")
        context.bot.send_message(chat_id=chat_id, text="Ошибка при отправке видео.")
    finally:
        os.unlink(filepath)

    return SECOND
