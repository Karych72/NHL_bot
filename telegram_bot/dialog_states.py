"""Состояния FSM диалога `/stats`, callback_data его кнопок и сборка сеток inline-клавиатур.

`StatsMenu` — состояния диалога (aiogram FSM), числа ниже — callback_data кнопок меню.
Это контракт между `script_bot.py` (рисует меню), `stats_handlers.py` (обрабатывает
callback'и) и `build_stats_router` в `bot.py`, который строит из них карту
«состояние → хендлеры».
"""

from typing import List, Optional

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message


class StatsMenu(StatesGroup):
    """Состояния диалога `/stats`: `first` — меню и подменю, `second` — страница
    статистики или дайджеста, `third` — ожидание даты дайджеста текстом."""

    first = State()
    second = State()
    third = State()


CHOOSE_STATS, TEAM_STATS, PLAYER_STATS, DAY_DIGEST, PLAYER_FIELD, PLAYER_GOALIE, TEAM_PROCENT_WINS, \
    TEAM_POWER_PLAY, TEAM_POWER_KILL, PLAYER_POINTS, PLAYER_GOALS, PLAYER_ASSISTS, \
    PLAYER_PLUS_MINUS, PLAYER_PENALTIES, PLAYER_HITS, PLAYER_BLOCKS, PLAYER_ICE_TIME, GOALIE_WINS, GOALIE_PERCENTAGE, \
    GOALIE_SHOOTOUTS, PLAYER_SAT_PCT, PLAYER_USAT_PCT, PLAYER_GOALS_FOR_PCT, PLAYER_OZ_START_PCT, \
    PLAYER_SHOOTOUT_PCT, END_CONVERSATION, PLAYER_ADVANCED_SUBMENU, \
    PLAYER_SHOT_WRIST, PLAYER_SHOT_SLAP, PLAYER_SHOT_SNAP, PLAYER_SHOT_BACKHAND, \
    PLAYER_SHOT_TIP_IN, PLAYER_SHOT_DEFLECTED, PLAYER_SHOT_WRAP_AROUND, \
    LEAGUE_STANDINGS, DIGEST_CALENDAR_TODAY, DIGEST_CALENDAR_YESTERDAY, DIGEST_PICK_DATE, \
    TEAM_CONFERENCE_STATS, TEAM_DIVISION_STATS, TEAM_PROFILE_PICK = range(41)

# Ключ данных FSM (`FSMContext`) диалога: id последнего сообщения, созданного НОВЫМ
# send_message()/answer() (а не edit_text(), который message_id не меняет) и несущего
# актуальную клавиатуру меню /stats. Записывается в script_bot.py (stats(), stats_over())
# и stats_handlers.py (bot_league_standings(), bot_digest_custom_date() при ошибке формата
# даты, dispatch_day_digest_messages() внутри диалога), читается в bot.py
# (cmd_cancel_in_conversation()), чтобы /cancel мог снять клавиатуру с этого сообщения —
# иначе она виснет «мёртвой» после конца диалога.
LAST_MENU_MESSAGE_ID_KEY = "stats_last_menu_message_id"


def callback_message(callback: CallbackQuery) -> Message:
    """Сообщение с нажатой кнопкой — то, которое хендлер правит на месте.

    Зачем: у `CallbackQuery.message` тип `Message | InaccessibleMessage | None`; кнопки
    бота висят только на его сообщениях, а недоступное (удалённое) сообщение править
    нельзя — распаковываем один раз и громко падаем, если инвариант нарушен.
    `callback` — нажатие, пришедшее в хендлер.
    """
    message = callback.message
    assert isinstance(message, Message)
    return message


def build_menu(
    buttons: list,
    n_cols: int,
    header_buttons: Optional[list] = None,
    footer_buttons: Optional[list] = None,
) -> List[list]:
    """Раскладывает плоский список кнопок в сетку для `InlineKeyboardMarkup`
    по `n_cols` в ряд, добавляя необязательные строки сверху/снизу.

    Args:
        header_buttons: ряд кнопок перед сеткой (напр. заголовок раздела).
        footer_buttons: ряды кнопок после сетки (обычно «« Назад»»/«Готово»).
    """
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.extend(footer_buttons)
    return menu
