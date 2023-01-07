from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from dialog_states import *
from bot_messages import day_digest


def bot_day_digest(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("В главное меню", callback_data=str(CHOOSE_STATS)),
            InlineKeyboardButton("Нет, с меня хватит ...", callback_data=str(END_CONVERSATION)),
    ]
    text = day_digest()
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text=text, parse_mode='MARKDOWN',
        # reply_markup=reply_markup
    )
    return SECOND
