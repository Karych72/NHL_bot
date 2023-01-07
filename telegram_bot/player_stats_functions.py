from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from dialog_states import *
from bot_messages import player_stats


def bot_player_points(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    text = player_stats('Лучшие бомбардиры', 'players_season_stats', 'points')
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_goals(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лучшие Снайперы', 'players_season_stats', 'goals')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_assists(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лучшие Ассистенты', 'players_season_stats', 'assists')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_hits(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по хитам', 'players_season_stats', 'hits')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_plus_minus(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по хитам', 'players_season_stats', 'hits')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_penalties(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по показателю +-', 'players_season_stats', 'plus_minus')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_blocks(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по блокам', 'players_season_stats', 'blocked')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_player_ice_time(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
        InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
        InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по среднему игровому времени', 'players_season_stats', 'time_on_ice_per_game')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND
