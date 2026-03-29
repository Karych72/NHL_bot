import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, ConversationHandler

from dialog_states import (
    DAY_DIGEST,
    FIRST,
    GOALIE_PERCENTAGE,
    GOALIE_SHOOTOUTS,
    GOALIE_WINS,
    PLAYER_ADVANCED_SUBMENU,
    PLAYER_ASSISTS,
    PLAYER_BLOCKS,
    PLAYER_FIELD,
    PLAYER_GOALIE,
    PLAYER_GOALS,
    PLAYER_GOALS_FOR_PCT,
    PLAYER_HITS,
    PLAYER_ICE_TIME,
    PLAYER_PENALTIES,
    PLAYER_PLUS_MINUS,
    PLAYER_POINTS,
    PLAYER_SAT_PCT,
    PLAYER_SHOOTOUT_PCT,
    PLAYER_SHOT_BACKHAND,
    PLAYER_SHOT_DEFLECTED,
    PLAYER_SHOT_SLAP,
    PLAYER_SHOT_SNAP,
    PLAYER_SHOT_TIP_IN,
    PLAYER_SHOT_TYPES_MENU,
    PLAYER_SHOT_WRAP_AROUND,
    PLAYER_SHOT_WRIST,
    PLAYER_STATS,
    PLAYER_USAT_PCT,
    PLAYER_OZ_START_PCT,
    TEAM_POWER_KILL,
    TEAM_POWER_PLAY,
    TEAM_PROCENT_WINS,
    TEAM_STATS,
    build_menu,
)

logger = logging.getLogger(__name__)

STATS_MENU_INTRO = (
    "Что посмотреть? Выберите раздел кнопкой ниже или командами: "
    "`/today` — матчи дня, `/table` — таблица, `/leaders` — лидеры, "
    "`/advanced` — SAT/USAT/GF%, `/shottypes` — голы по типам броска, `/help` — все команды."
)


def stats(update: Update, context: CallbackContext) -> int:
    """Вызывается по команде `/stats`."""
    keyboard = [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    update.message.reply_text(
        text=STATS_MENU_INTRO, reply_markup=reply_markup, parse_mode="Markdown"
    )
    logger.info("User %s started /stats", update.message.from_user.first_name)
    return FIRST


def stats_over(update: Update, context: CallbackContext) -> int:
    """Главное меню новым сообщением — предыдущий контент (статы/дайджест) остаётся в чате."""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    context.bot.send_message(
        chat_id=query.message.chat_id,
        text=STATS_MENU_INTRO,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return FIRST


def bot_team_stats(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика процент набранных очков", callback_data=str(TEAM_PROCENT_WINS)),
        InlineKeyboardButton("Статистика большинства", callback_data=str(TEAM_POWER_PLAY)),
        InlineKeyboardButton("Статистика меньшинства", callback_data=str(TEAM_POWER_KILL)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите статистику", reply_markup=reply_markup
    )
    return FIRST


def bot_player_stats(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика полевых игроков", callback_data=str(PLAYER_FIELD)),
        InlineKeyboardButton("Статистика вратарей", callback_data=str(PLAYER_GOALIE)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите тип игроков", reply_markup=reply_markup
    )
    return FIRST


def bot_player_field(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Лидеры по очкам", callback_data=str(PLAYER_POINTS)),
        InlineKeyboardButton("Лидеры по голам", callback_data=str(PLAYER_GOALS)),
        InlineKeyboardButton("Лидеры по ассистам", callback_data=str(PLAYER_ASSISTS)),
        InlineKeyboardButton("Лидеры по хитам", callback_data=str(PLAYER_HITS)),
        InlineKeyboardButton("Лидеры по показателю +-", callback_data=str(PLAYER_PLUS_MINUS)),
        InlineKeyboardButton("Лидеры по игровому времени", callback_data=str(PLAYER_ICE_TIME)),
        InlineKeyboardButton("Лидеры по штрафу", callback_data=str(PLAYER_PENALTIES)),
        InlineKeyboardButton("Лидеры по блокам", callback_data=str(PLAYER_BLOCKS)),
        InlineKeyboardButton("Расширенная статистика (SAT, USAT, GF%…)", callback_data=str(PLAYER_ADVANCED_SUBMENU)),
        InlineKeyboardButton("Голы по типам броска", callback_data=str(PLAYER_SHOT_TYPES_MENU)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    return FIRST


def bot_player_advanced_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Лидеры по Corsi (SAT%)", callback_data=str(PLAYER_SAT_PCT)),
        InlineKeyboardButton("Лидеры по Fenwick (USAT%)", callback_data=str(PLAYER_USAT_PCT)),
        InlineKeyboardButton("Лидеры по Goals For %", callback_data=str(PLAYER_GOALS_FOR_PCT)),
        InlineKeyboardButton("Лидеры по старту в зоне атаки (OZ Start %)", callback_data=str(PLAYER_OZ_START_PCT)),
        InlineKeyboardButton("Лидеры по реализации буллитов (Shootout %)", callback_data=str(PLAYER_SHOOTOUT_PCT)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Расширенная статистика полевых", reply_markup=reply_markup
    )
    return FIRST


def bot_player_shot_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Голы с кистевого", callback_data=str(PLAYER_SHOT_WRIST)),
        InlineKeyboardButton("Голы щелчком", callback_data=str(PLAYER_SHOT_SLAP)),
        InlineKeyboardButton("Голы с полуприёма", callback_data=str(PLAYER_SHOT_SNAP)),
        InlineKeyboardButton("Голы с бэкхенда", callback_data=str(PLAYER_SHOT_BACKHAND)),
        InlineKeyboardButton("Голы направлением", callback_data=str(PLAYER_SHOT_TIP_IN)),
        InlineKeyboardButton("Голы отклонением", callback_data=str(PLAYER_SHOT_DEFLECTED)),
        InlineKeyboardButton("Голы с за ворот", callback_data=str(PLAYER_SHOT_WRAP_AROUND)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Лидеры по голам с разных типов броска", reply_markup=reply_markup
    )
    return FIRST


def bot_player_goalie(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Лидеры по победам", callback_data=str(GOALIE_WINS)),
        InlineKeyboardButton("Лидеры по % отр бросков", callback_data=str(GOALIE_PERCENTAGE)),
        InlineKeyboardButton("Лидеры по сухарям", callback_data=str(GOALIE_SHOOTOUTS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    return FIRST


def end(update: Update, context: CallbackContext) -> int:
    """Завершает разговор."""
    query = update.callback_query
    query.answer()
    query.edit_message_text(
        text="Готово. Быстрый ввод: /today, /table, /leaders, /game, /advanced, /shottypes, /help.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END
