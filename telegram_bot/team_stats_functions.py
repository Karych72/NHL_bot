from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from dialog_states import *
from bot_messages import team_stats, team_table


def bot_team_procent_wins(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = team_stats('Статистика процента набранных очков', 'procent_points')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_team_power_play(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = team_stats('Статистика большинства', 'power_play_percentage')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND


def bot_team_power_kill(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = team_stats('Статистика меньшинства', 'penalty_kill_percentage')
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND
