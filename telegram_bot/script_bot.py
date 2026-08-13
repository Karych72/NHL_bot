"""Inline-меню навигации диалога `/stats`.

Роль в пайплайне: рисует корневое меню и подменю (команды, полевые игроки,
вратари, расширенная статистика, выбор даты дайджеста) и возвращает следующее
состояние FSM для `ConversationHandler`, собранного в `bot.py`. Сами выборки
из БД живут в `stats_handlers.py`.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, ConversationHandler

from dialog_states import (
    CHOOSE_STATS,
    DAY_DIGEST,
    DIGEST_CALENDAR_TODAY,
    DIGEST_CALENDAR_YESTERDAY,
    DIGEST_PICK_DATE,
    FIRST,
    GOALIE_PERCENTAGE,
    GOALIE_SHOOTOUTS,
    GOALIE_WINS,
    LAST_MENU_MESSAGE_ID_KEY,
    LEAGUE_STANDINGS,
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
    "Меню статистики NHL. Выберите раздел кнопкой ниже. "
    "Список команд: <code>/help</code>."
)

# Строковые callback для «Назад» между подменю (не занимают числовой диапазон состояний).
NAV_PLAYERS = "nav:players"
NAV_FIELD = "nav:field"


def stats_main_keyboard_rows() -> list:
    return [
        InlineKeyboardButton("Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton("Турнирная таблица", callback_data=str(LEAGUE_STANDINGS)),
        InlineKeyboardButton("Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton("Статистика команд", callback_data=str(TEAM_STATS)),
    ]


def stats_main_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(build_menu(stats_main_keyboard_rows(), n_cols=1))


async def stats_root_edit(update: Update, context: CallbackContext) -> int:
    """Главное меню тем же сообщением (редактирование текста кнопки)."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        text=STATS_MENU_INTRO,
        reply_markup=stats_main_markup(),
        parse_mode="HTML",
    )
    return FIRST


async def stats(update: Update, context: CallbackContext) -> int:
    """Вызывается по команде `/stats`."""
    message = update.message
    assert message is not None
    sent = await message.reply_text(
        text=STATS_MENU_INTRO,
        reply_markup=stats_main_markup(),
        parse_mode="HTML",
    )
    # Новое сообщение (`reply_text`, не `edit_message_text`): id записывается,
    # чтобы /cancel мог снять клавиатуру, если диалог оборвётся до «Готово».
    # Все последующие экраны FIRST/SECOND/THIRD правят этот же message_id
    # через edit_message_text — записывать его повторно не нужно.
    assert context.user_data is not None
    context.user_data[LAST_MENU_MESSAGE_ID_KEY] = sent.message_id
    assert message.from_user is not None
    logger.info("User %s started /stats", message.from_user.first_name)
    return FIRST


async def stats_over(update: Update, context: CallbackContext) -> int:
    """Главное меню новым сообщением — предыдущий контент (статы/дайджест) остаётся в чате."""
    query = update.callback_query
    assert query is not None and query.message is not None
    await query.answer()
    sent = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=STATS_MENU_INTRO,
        reply_markup=stats_main_markup(),
        parse_mode="HTML",
    )
    assert context.user_data is not None
    context.user_data[LAST_MENU_MESSAGE_ID_KEY] = sent.message_id
    return FIRST


async def bot_team_stats(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика процент набранных очков", callback_data=str(TEAM_PROCENT_WINS)),
        InlineKeyboardButton("Статистика большинства", callback_data=str(TEAM_POWER_PLAY)),
        InlineKeyboardButton("Статистика меньшинства", callback_data=str(TEAM_POWER_KILL)),
    ]
    footer = [[InlineKeyboardButton("« Назад", callback_data=str(CHOOSE_STATS))]]
    reply_markup = InlineKeyboardMarkup(
        build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await query.edit_message_text(
        text="Выберите статистику", reply_markup=reply_markup
    )
    return FIRST


async def bot_player_stats(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    keyboard = [
        InlineKeyboardButton("Статистика полевых игроков", callback_data=str(PLAYER_FIELD)),
        InlineKeyboardButton("Статистика вратарей", callback_data=str(PLAYER_GOALIE)),
    ]
    footer = [[InlineKeyboardButton("« Назад", callback_data=str(CHOOSE_STATS))]]
    reply_markup = InlineKeyboardMarkup(
        build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await query.edit_message_text(
        text="Выберите тип игроков", reply_markup=reply_markup
    )
    return FIRST


async def bot_player_field(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    keyboard = [
        InlineKeyboardButton("Лидеры по очкам", callback_data=str(PLAYER_POINTS)),
        InlineKeyboardButton("Лидеры по голам", callback_data=str(PLAYER_GOALS)),
        InlineKeyboardButton("Лидеры по ассистам", callback_data=str(PLAYER_ASSISTS)),
        InlineKeyboardButton("Лидеры по хитам", callback_data=str(PLAYER_HITS)),
        InlineKeyboardButton("Лидеры по показателю +-", callback_data=str(PLAYER_PLUS_MINUS)),
        InlineKeyboardButton("Лидеры по игровому времени", callback_data=str(PLAYER_ICE_TIME)),
        InlineKeyboardButton("Лидеры по штрафу", callback_data=str(PLAYER_PENALTIES)),
        InlineKeyboardButton("Лидеры по блокам", callback_data=str(PLAYER_BLOCKS)),
        InlineKeyboardButton(
            "Расширенная статистика (SAT, USAT, типы бросков…)",
            callback_data=str(PLAYER_ADVANCED_SUBMENU),
        ),
    ]
    footer = [[InlineKeyboardButton("« Назад", callback_data=NAV_PLAYERS)]]
    reply_markup = InlineKeyboardMarkup(
        build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await query.edit_message_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    return FIRST


async def bot_player_advanced_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    keyboard = [
        InlineKeyboardButton("Лидеры по Corsi (SAT%)", callback_data=str(PLAYER_SAT_PCT)),
        InlineKeyboardButton("Лидеры по Fenwick (USAT%)", callback_data=str(PLAYER_USAT_PCT)),
        InlineKeyboardButton("Лидеры по Goals For %", callback_data=str(PLAYER_GOALS_FOR_PCT)),
        InlineKeyboardButton("Лидеры по старту в зоне атаки (OZ Start %)", callback_data=str(PLAYER_OZ_START_PCT)),
        InlineKeyboardButton("Лидеры по реализации буллитов (Shootout %)", callback_data=str(PLAYER_SHOOTOUT_PCT)),
        InlineKeyboardButton("Голы с кистевого", callback_data=str(PLAYER_SHOT_WRIST)),
        InlineKeyboardButton("Голы щелчком", callback_data=str(PLAYER_SHOT_SLAP)),
        InlineKeyboardButton("Голы с полуприёма", callback_data=str(PLAYER_SHOT_SNAP)),
        InlineKeyboardButton("Голы с бэкхенда", callback_data=str(PLAYER_SHOT_BACKHAND)),
        InlineKeyboardButton("Голы направлением", callback_data=str(PLAYER_SHOT_TIP_IN)),
        InlineKeyboardButton("Голы отклонением", callback_data=str(PLAYER_SHOT_DEFLECTED)),
        InlineKeyboardButton("Голы с за ворот", callback_data=str(PLAYER_SHOT_WRAP_AROUND)),
    ]
    footer = [[InlineKeyboardButton("« Назад", callback_data=NAV_FIELD)]]
    reply_markup = InlineKeyboardMarkup(
        build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await query.edit_message_text(
        text="Расширенная статистика полевых", reply_markup=reply_markup
    )
    return FIRST


async def bot_player_goalie(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    keyboard = [
        InlineKeyboardButton("Лидеры по победам", callback_data=str(GOALIE_WINS)),
        InlineKeyboardButton("Лидеры по % отр бросков", callback_data=str(GOALIE_PERCENTAGE)),
        InlineKeyboardButton("Лидеры по сухарям", callback_data=str(GOALIE_SHOOTOUTS)),
    ]
    footer = [[InlineKeyboardButton("« Назад", callback_data=NAV_PLAYERS)]]
    reply_markup = InlineKeyboardMarkup(
        build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await query.edit_message_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    return FIRST


async def bot_digest_date_menu(update: Update, context: CallbackContext) -> int:
    """Выбор даты дайджеста перед загрузкой матчей."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    keyboard = [
        InlineKeyboardButton("Сегодня", callback_data=str(DIGEST_CALENDAR_TODAY)),
        InlineKeyboardButton("Вчера", callback_data=str(DIGEST_CALENDAR_YESTERDAY)),
        InlineKeyboardButton("Выбрать дату", callback_data=str(DIGEST_PICK_DATE)),
    ]
    footer = [[InlineKeyboardButton("« Назад", callback_data=str(CHOOSE_STATS))]]
    reply_markup = InlineKeyboardMarkup(
        build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await query.edit_message_text(
        text=(
            "Дайджест по завершённым матчам в базе.\n"
            "<i>Сегодня</i> и <i>Вчера</i> — календарные даты "
            "(UTC может отличаться от «игрового дня» NHL)."
        ),
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    return FIRST


async def nav_back_to_players(update: Update, context: CallbackContext) -> int:
    return await bot_player_stats(update, context)


async def nav_back_to_field(update: Update, context: CallbackContext) -> int:
    return await bot_player_field(update, context)


async def end(update: Update, context: CallbackContext) -> int:
    """Завершает разговор: снимаем inline-кнопки, текст статистики в чате оставляем."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return ConversationHandler.END
