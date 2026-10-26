"""Inline-меню навигации диалога `/stats`.

Роль в пайплайне: рисует корневое меню и подменю (команды, полевые игроки,
вратари, расширенная статистика, выбор даты дайджеста) и переводит диалог в
следующее состояние FSM (`dialog_states.StatsMenu`); карту «состояние → хендлеры»
собирает `bot.build_stats_router`. Сами выборки из БД живут в `stats_handlers.py`.
"""

import logging

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from dialog_states import (
    CHOOSE_STATS,
    DAY_DIGEST,
    DIGEST_CALENDAR_TODAY,
    DIGEST_CALENDAR_YESTERDAY,
    DIGEST_PICK_DATE,
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
    TEAM_CONFERENCE_STATS,
    TEAM_DIVISION_STATS,
    TEAM_POWER_KILL,
    TEAM_POWER_PLAY,
    TEAM_PROCENT_WINS,
    TEAM_PROFILE_PICK,
    TEAM_STATS,
    StatsMenu,
    build_menu,
    callback_message,
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
    """Кнопки корневого меню `/stats`: дайджест дня, турнирная таблица,
    статистика игроков, статистика команд — все, кроме турнирной таблицы,
    ведут в своё подменю; «Турнирная таблица» шлёт готовую таблицу отдельным
    сообщением (`bot_league_standings` в `stats_handlers.py`)."""
    return [
        InlineKeyboardButton(text="Статистика дня", callback_data=str(DAY_DIGEST)),
        InlineKeyboardButton(text="Турнирная таблица", callback_data=str(LEAGUE_STANDINGS)),
        InlineKeyboardButton(text="Статистика игроков", callback_data=str(PLAYER_STATS)),
        InlineKeyboardButton(text="Статистика команд", callback_data=str(TEAM_STATS)),
    ]


def stats_main_markup() -> InlineKeyboardMarkup:
    """Оборачивает `stats_main_keyboard_rows` в клавиатуру из одной колонки —
    общий вид корневого меню для `/stats`, `stats_root_edit` и `stats_over`."""
    return InlineKeyboardMarkup(inline_keyboard=build_menu(stats_main_keyboard_rows(), n_cols=1))


async def stats_root_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню тем же сообщением (редактирование текста кнопки)."""
    await callback.answer()
    await callback_message(callback).edit_text(
        text=STATS_MENU_INTRO,
        reply_markup=stats_main_markup(),
        parse_mode="HTML",
    )
    await state.set_state(StatsMenu.first)


async def stats(message: Message, state: FSMContext) -> None:
    """Вызывается по команде `/stats` — в любом состоянии, в том числе вне диалога."""
    sent = await message.answer(
        text=STATS_MENU_INTRO,
        reply_markup=stats_main_markup(),
        parse_mode="HTML",
    )
    # Новое сообщение (`answer`, не `edit_text`): id записывается, чтобы /cancel мог
    # снять клавиатуру, если диалог оборвётся до «Готово». Все последующие экраны
    # диалога правят этот же message_id через edit_text — записывать его повторно не нужно.
    await state.update_data({LAST_MENU_MESSAGE_ID_KEY: sent.message_id})
    assert message.from_user is not None
    logger.info("User %s started /stats", message.from_user.first_name)
    await state.set_state(StatsMenu.first)


async def stats_over(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Главное меню новым сообщением — предыдущий контент (статы/дайджест) остаётся в чате."""
    await callback.answer()
    sent = await bot.send_message(
        chat_id=callback_message(callback).chat.id,
        text=STATS_MENU_INTRO,
        reply_markup=stats_main_markup(),
        parse_mode="HTML",
    )
    await state.update_data({LAST_MENU_MESSAGE_ID_KEY: sent.message_id})
    await state.set_state(StatsMenu.first)


async def bot_team_stats(callback: CallbackQuery, state: FSMContext) -> None:
    """Подменю статистики команд: % набранных очков, большинство, меньшинство,
    сводки по конференциям/дивизионам, профиль команды.

    Кнопки несут состояния FSM (TEAM_PROCENT_WINS/TEAM_POWER_PLAY/
    TEAM_POWER_KILL/TEAM_CONFERENCE_STATS/TEAM_DIVISION_STATS/
    TEAM_PROFILE_PICK), «« Назад»» ведёт в корень меню (CHOOSE_STATS).
    """
    await callback.answer()
    keyboard = [
        InlineKeyboardButton(text="Статистика процент набранных очков", callback_data=str(TEAM_PROCENT_WINS)),
        InlineKeyboardButton(text="Статистика большинства", callback_data=str(TEAM_POWER_PLAY)),
        InlineKeyboardButton(text="Статистика меньшинства", callback_data=str(TEAM_POWER_KILL)),
        InlineKeyboardButton(text="По конференциям", callback_data=str(TEAM_CONFERENCE_STATS)),
        InlineKeyboardButton(text="По дивизионам", callback_data=str(TEAM_DIVISION_STATS)),
        InlineKeyboardButton(text="Профиль команды", callback_data=str(TEAM_PROFILE_PICK)),
    ]
    footer = [[InlineKeyboardButton(text="« Назад", callback_data=str(CHOOSE_STATS))]]
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await callback_message(callback).edit_text(
        text="Выберите статистику", reply_markup=reply_markup
    )
    await state.set_state(StatsMenu.first)


async def bot_player_stats(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор типа игроков: полевые (PLAYER_FIELD) или вратари (PLAYER_GOALIE).
    «« Назад»» ведёт в корень меню (CHOOSE_STATS). Страны — команда /countries."""
    await callback.answer()
    keyboard = [
        InlineKeyboardButton(text="Статистика полевых игроков", callback_data=str(PLAYER_FIELD)),
        InlineKeyboardButton(text="Статистика вратарей", callback_data=str(PLAYER_GOALIE)),
    ]
    footer = [[InlineKeyboardButton(text="« Назад", callback_data=str(CHOOSE_STATS))]]
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await callback_message(callback).edit_text(
        text="Выберите тип игроков", reply_markup=reply_markup
    )
    await state.set_state(StatsMenu.first)


async def bot_player_field(callback: CallbackQuery, state: FSMContext) -> None:
    """Подменю лидеров среди полевых игроков (очки, голы, ассисты, хиты,
    +-, время на льду, штраф, блоки) плюс переход в расширенную статистику
    (PLAYER_ADVANCED_SUBMENU). «« Назад»» ведёт на выбор типа игроков (NAV_PLAYERS).
    """
    await callback.answer()
    keyboard = [
        InlineKeyboardButton(text="Лидеры по очкам", callback_data=str(PLAYER_POINTS)),
        InlineKeyboardButton(text="Лидеры по голам", callback_data=str(PLAYER_GOALS)),
        InlineKeyboardButton(text="Лидеры по ассистам", callback_data=str(PLAYER_ASSISTS)),
        InlineKeyboardButton(text="Лидеры по хитам", callback_data=str(PLAYER_HITS)),
        InlineKeyboardButton(text="Лидеры по показателю +-", callback_data=str(PLAYER_PLUS_MINUS)),
        InlineKeyboardButton(text="Лидеры по игровому времени", callback_data=str(PLAYER_ICE_TIME)),
        InlineKeyboardButton(text="Лидеры по штрафу", callback_data=str(PLAYER_PENALTIES)),
        InlineKeyboardButton(text="Лидеры по блокам", callback_data=str(PLAYER_BLOCKS)),
        InlineKeyboardButton(
            text="Расширенная статистика (SAT, USAT, типы бросков…)",
            callback_data=str(PLAYER_ADVANCED_SUBMENU),
        ),
    ]
    footer = [[InlineKeyboardButton(text="« Назад", callback_data=NAV_PLAYERS)]]
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await callback_message(callback).edit_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    await state.set_state(StatsMenu.first)


async def bot_player_advanced_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Расширенная статистика полевых: Corsi/Fenwick/Goals For %, старт в
    зоне атаки, реализация буллитов и голы по типам броска — каждая кнопка
    своё состояние FSM. «« Назад»» ведёт на статистику полевых (NAV_FIELD).
    """
    await callback.answer()
    keyboard = [
        InlineKeyboardButton(text="Лидеры по Corsi (SAT%)", callback_data=str(PLAYER_SAT_PCT)),
        InlineKeyboardButton(text="Лидеры по Fenwick (USAT%)", callback_data=str(PLAYER_USAT_PCT)),
        InlineKeyboardButton(text="Лидеры по Goals For %", callback_data=str(PLAYER_GOALS_FOR_PCT)),
        InlineKeyboardButton(text="Лидеры по старту в зоне атаки (OZ Start %)", callback_data=str(PLAYER_OZ_START_PCT)),
        InlineKeyboardButton(text="Лидеры по реализации буллитов (Shootout %)", callback_data=str(PLAYER_SHOOTOUT_PCT)),
        InlineKeyboardButton(text="Голы с кистевого", callback_data=str(PLAYER_SHOT_WRIST)),
        InlineKeyboardButton(text="Голы щелчком", callback_data=str(PLAYER_SHOT_SLAP)),
        InlineKeyboardButton(text="Голы с полуприёма", callback_data=str(PLAYER_SHOT_SNAP)),
        InlineKeyboardButton(text="Голы с бэкхенда", callback_data=str(PLAYER_SHOT_BACKHAND)),
        InlineKeyboardButton(text="Голы направлением", callback_data=str(PLAYER_SHOT_TIP_IN)),
        InlineKeyboardButton(text="Голы отклонением", callback_data=str(PLAYER_SHOT_DEFLECTED)),
        InlineKeyboardButton(text="Голы с за ворот", callback_data=str(PLAYER_SHOT_WRAP_AROUND)),
    ]
    footer = [[InlineKeyboardButton(text="« Назад", callback_data=NAV_FIELD)]]
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await callback_message(callback).edit_text(
        text="Расширенная статистика полевых", reply_markup=reply_markup
    )
    await state.set_state(StatsMenu.first)


async def bot_player_goalie(callback: CallbackQuery, state: FSMContext) -> None:
    """Подменю лидеров-вратарей: победы, % отражённых бросков, сухари.
    «« Назад»» ведёт на выбор типа игроков (NAV_PLAYERS)."""
    await callback.answer()
    keyboard = [
        InlineKeyboardButton(text="Лидеры по победам", callback_data=str(GOALIE_WINS)),
        InlineKeyboardButton(text="Лидеры по % отр бросков", callback_data=str(GOALIE_PERCENTAGE)),
        InlineKeyboardButton(text="Лидеры по сухарям", callback_data=str(GOALIE_SHOOTOUTS)),
    ]
    footer = [[InlineKeyboardButton(text="« Назад", callback_data=NAV_PLAYERS)]]
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await callback_message(callback).edit_text(
        text="Выберите тип статистики", reply_markup=reply_markup
    )
    await state.set_state(StatsMenu.first)


async def bot_digest_date_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор даты дайджеста перед загрузкой матчей."""
    await callback.answer()
    keyboard = [
        InlineKeyboardButton(text="Сегодня", callback_data=str(DIGEST_CALENDAR_TODAY)),
        InlineKeyboardButton(text="Вчера", callback_data=str(DIGEST_CALENDAR_YESTERDAY)),
        InlineKeyboardButton(text="Выбрать дату", callback_data=str(DIGEST_PICK_DATE)),
    ]
    footer = [[InlineKeyboardButton(text="« Назад", callback_data=str(CHOOSE_STATS))]]
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=build_menu(keyboard, n_cols=1, footer_buttons=footer)
    )
    await callback_message(callback).edit_text(
        text=(
            "Дайджест по завершённым матчам в базе.\n"
            "<i>Сегодня</i> — матчи прошедшей ночи по Москве, <i>Вчера</i> — ночью раньше. "
            "Своя дата — игровой день NHL (дата по Северной Америке)."
        ),
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    await state.set_state(StatsMenu.first)


async def end(callback: CallbackQuery, state: FSMContext) -> None:
    """Завершает разговор: снимаем inline-кнопки, текст статистики в чате оставляем."""
    await callback.answer()
    await callback_message(callback).edit_reply_markup(reply_markup=None)
    await state.clear()
