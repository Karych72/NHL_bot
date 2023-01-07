from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from dialog_states import *
from bot_messages import player_stats


def bot_goalie_wins(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("Хочу выбрать еще одну статистику!", callback_data=str(TEAM_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по победам', 'goalies_season_stats', 'wins')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        reply_markup=reply_markup
    )
    return SECOND


def bot_goalie_percentage(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("Хочу выбрать еще одну статистику!", callback_data=str(TEAM_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры по проценту отраженных бросков', 'goalies_season_stats', 'save_percentage')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        reply_markup=reply_markup
    )
    return SECOND


def bot_goalie_shootouts(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("Хочу выбрать еще одну статистику!", callback_data=str(TEAM_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = player_stats('Лидеры cухим матчам', 'goalies_season_stats', 'shutouts')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        reply_markup=reply_markup
    )
    return SECOND
