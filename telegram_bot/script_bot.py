import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, ConversationHandler

from dialog_states import (
    DAY_DIGEST,
    FIRST,
    GOALIE_PERCENTAGE,
    GOALIE_SHOOTOUTS,
    GOALIE_WINS,
    PLAYER_ASSISTS,
    PLAYER_BLOCKS,
    PLAYER_FIELD,
    PLAYER_GOALIE,
    PLAYER_GOALS,
    PLAYER_HITS,
    PLAYER_ICE_TIME,
    PLAYER_PENALTIES,
    PLAYER_PLUS_MINUS,
    PLAYER_POINTS,
    PLAYER_STATS,
    TEAM_POWER_KILL,
    TEAM_POWER_PLAY,
    TEAM_PROCENT_WINS,
    TEAM_STATS,
    build_menu,
)

logger = logging.getLogger(__name__)


def stats(update: Update, context: CallbackContext) -> int:
    """Вызывается по команде `/stats`."""
    keyboard = [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    update.message.reply_text(
        text="Запустите обработчик, выберите маршрут", reply_markup=reply_markup
    )
    logger.info("User %s started /stats", update.message.from_user.first_name)
    return FIRST


def stats_over(update: Update, context: CallbackContext) -> int:
    """Возвращает главное меню (без создания нового сообщения)."""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите статистику", reply_markup=reply_markup
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
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите тип статистики", reply_markup=reply_markup
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
    query.edit_message_text(text="See you next time!")
    return ConversationHandler.END
