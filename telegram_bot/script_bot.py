import config
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram.ext import ConversationHandler
from dialog_states import *

# Ведение журнала логов
# logging.basicConfig(
#     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
# )
#
# logger = logging.getLogger(__name__)


def stats(update, _):
    """Вызывается по команде `/stats`."""
    # Получаем пользователя, который запустил команду `/start`
    user = update.message.from_user
    # logger.info("Пользователь %s начал разговор", user.first_name)
    # Создаем `InlineKeyboard`, где каждая кнопка имеет
    # отображаемый текст и строку `callback_data`
    # Клавиатура - это список строк кнопок, где каждая строка,
    # в свою очередь, является списком `[[...]]`
    keyboard = [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    # Отправляем сообщение с текстом и добавленной клавиатурой `reply_markup`
    update.message.reply_text(
        text="Запустите обработчик, выберите маршрут", reply_markup=reply_markup
    )
    print('In stats', DAY_DIGEST, PLAYER_STATS, TEAM_STATS)
    # Сообщаем `ConversationHandler`, что сейчас состояние `FIRST`
    return FIRST


def stats_over(update, _):
    """Тот же текст и клавиатура, что и при `/stats_over`, но не как новое сообщение"""
    # Получаем `CallbackQuery` из обновления `update`
    query = update.callback_query
    # На запросы обратного вызова необходимо ответить,
    # даже если уведомление для пользователя не требуется.
    # В противном случае у некоторых клиентов могут возникнуть проблемы.
    query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    # Отредактируем сообщение, вызвавшее обратный вызов.
    # Это создает ощущение интерактивного меню.
    query.edit_message_text(
        text="Выберите статистику", reply_markup=reply_markup
    )
    # Сообщаем `ConversationHandler`, что сейчас находимся в состоянии `FIRST`
    return FIRST


def bot_team_stats(update, _):
    """Показ нового выбора кнопок"""
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


def bot_player_stats(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("Статистика полевых игроков", callback_data=str(PLAYER_FIELD)),
            InlineKeyboardButton("Статистика вратарей", callback_data=str(PLAYER_GOALS))
        ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите тип игроков", reply_markup=reply_markup
    )
    return FIRST


def bot_player_field(update, _):
    """Показ нового выбора кнопок"""
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


def bot_player_goalie(update, _):
    """Показ нового выбора кнопок"""
    query = update.callback_query
    query.answer()
    keyboard = [
            InlineKeyboardButton("Лидеры по победам", callback_data=str(GOALIE_WINS)),
            InlineKeyboardButton("Лидеры по % отр бросков", callback_data=str(GOALIE_PERCENTAGE)),
            InlineKeyboardButton("Лидеры по сухарям", callback_data=str(GOALIE_SHOOTOUTS))
        ]
    reply_markup = InlineKeyboardMarkup(build_menu(keyboard, n_cols=1))
    query.edit_message_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    return FIRST


def end(update, _):
    """Возвращает `ConversationHandler.END`, который говорит
    `ConversationHandler` что разговор окончен"""
    query = update.callback_query
    query.answer()
    query.edit_message_text(text="See you next time!")
    return ConversationHandler.END
