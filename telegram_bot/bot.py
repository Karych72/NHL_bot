"""Точка входа Telegram-бота: сборка `Dispatcher` aiogram и регистрация хендлеров.

Роль в пайплайне: `bot.py` собирает aiogram 3 `Dispatcher` из трёх роутеров —
standalone-команды и кнопки, диалог `/stats` (FSM `dialog_states.StatsMenu`),
хвост (`/cancel` вне диалога и свободный текст), — вешает троттлинг нажатий
(`throttle.py`) и запускает long polling. Сами обработчики живут в `script_bot.py`
(навигация) и `stats_handlers.py` (выборки из БД); здесь — только команды
верхнего уровня, подписки, свободный текст и сборка `Dispatcher`.
"""

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import psycopg2
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.magic_filter import MagicFilter

import ai_router
import config
import subscription_repo
from leaderboard_specs import ADVANCED_METRICS
from throttle import enforce_callback_rate_limit
from bot_messages import (
    country_rankings,
    day_digest,
    rich_message,
    last_night_day,
    leaders_menu_intro,
    season_teams_by_division,
    team_list_text,
    team_full_stats,
    team_profile,
    team_table,
    truncate_telegram_text,
)
from nhl_scoreboard import (
    ScoreboardFetchError,
    fetch_score,
    league_today,
    slate_games_sorted,
    tonight_match_button_label,
    tonight_reply_intro,
)
from help_text import ADVANCED_COMMAND_INTRO, BOT_COMMANDS, HELP_MESSAGE, START_MESSAGE
from dialog_states import (
    build_menu,
    callback_message,
    CHOOSE_STATS,
    DAY_DIGEST,
    DIGEST_CALENDAR_TODAY,
    DIGEST_CALENDAR_YESTERDAY,
    DIGEST_PICK_DATE,
    END_CONVERSATION,
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
    StatsMenu,
    TEAM_CONFERENCE_STATS,
    TEAM_DIVISION_STATS,
    TEAM_POWER_KILL,
    TEAM_POWER_PLAY,
    TEAM_PROCENT_WINS,
    TEAM_PROFILE_PICK,
    TEAM_STATS,
)
from script_bot import (
    NAV_FIELD,
    NAV_PLAYERS,
    bot_digest_date_menu,
    bot_player_advanced_menu,
    bot_player_field,
    bot_player_goalie,
    bot_player_stats,
    bot_team_stats,
    end,
    stats,
    stats_over,
    stats_root_edit,
)
from stats_handlers import (
    ADVANCED_CALLBACK_PATTERN,
    COUNTRY_CALLBACK_PATTERN,
    DIGEST_BACK_FROM_DATE_CALLBACK,
    DIGEST_EXPAND_CALLBACK_PATTERN,
    GOAL_VIDEO_CALLBACK_PATTERN,
    LEADERS_PICK_CALLBACK_PATTERN,
    LEADERBOARD_PAGE_CALLBACK_PATTERN,
    STAT_PAGE_CALLBACK_PATTERN,
    STANDALONE_SA_CALLBACK_PATTERN,
    TEAM_PAGE_CALLBACK_PATTERN,
    TEAM_PROFILE_CALLBACK_PATTERN,
    TONIGHT_GAME_CALLBACK_PATTERN,
    advanced_standalone_keyboard,
    callback_country,
    country_rankings_reply,
    bot_digest_calendar_today,
    bot_digest_calendar_yesterday,
    bot_digest_custom_date,
    bot_digest_pick_date_prompt,
    bot_goalie_percentage,
    bot_goalie_shootouts,
    bot_goalie_wins,
    bot_league_standings,
    bot_player_assists,
    bot_player_blocks,
    bot_player_goals,
    bot_player_goals_for_pct,
    bot_player_hits,
    bot_player_ice_time,
    bot_player_oz_start_pct,
    bot_player_penalties,
    bot_player_plus_minus,
    bot_player_points,
    bot_player_sat_pct,
    bot_player_shootout_pct,
    bot_player_shot_backhand,
    bot_player_shot_deflected,
    bot_player_shot_slap,
    bot_player_shot_snap,
    bot_player_shot_tip_in,
    bot_player_shot_wrap,
    bot_player_shot_wrist,
    bot_player_usat_pct,
    bot_team_conference_stats,
    bot_team_division_stats,
    bot_team_power_kill,
    bot_team_power_play,
    bot_team_procent_wins,
    bot_team_profile_pick,
    bot_team_profile_show,
    callback_expand_digest_game,
    callback_leaderboard_page,
    callback_leaders_pick,
    callback_standalone_adv,
    callback_standalone_sa,
    callback_stats_player_page,
    callback_stats_team_page,
    callback_tonight_game,
    country_page_reply,
    leaders_category_keyboard,
    leaders_reply,
    dispatch_day_digest_messages,
    handle_goal_video,
    send_game_card_message,
    standalone_stat_reply,
)

logger = logging.getLogger(__name__)

# Лимит Bot API на число кнопок в одном inline-сообщении.
_TELEGRAM_INLINE_BUTTON_CAP = 100
# Как у кнопок матчей в /today.
_TONIGHT_BUTTON_COLUMNS = 3

# `/BOS`, `/WSH`… из списка `/team` и `/BOS_FULL` из конца профиля клуба: три
# латинские буквы (+ `_FULL`) — ни одна служебная команда бота под этот шаблон
# не попадает. Вариант `/BOS@bot` не принимается: regex не знает имени бота и
# отвечал бы на команды, адресованные чужим ботам.
TEAM_COMMAND_PATTERN = r"^/([A-Za-z]{3})(_[Ff][Uu][Ll][Ll])?$"


def build_tonight_match_keyboard(games) -> Optional[InlineKeyboardMarkup]:
    """Вертикальный список кнопок матчей: по нажатию — карточка из БД или сезонное сравнение команд."""
    buttons: List[InlineKeyboardButton] = []
    for g in games:
        gid = g.get("id")
        away = ((g.get("awayTeam") or {}).get("abbrev") or "?").strip()
        home = ((g.get("homeTeam") or {}).get("abbrev") or "?").strip()
        if gid is None:
            continue
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            continue
        cb = f"tn:{gid_int}:{away}:{home}"
        if len(cb.encode("utf-8")) > 64:
            continue
        label = tonight_match_button_label(g)
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append(InlineKeyboardButton(text=label, callback_data=cb))
    if not buttons:
        return None
    if len(buttons) > _TELEGRAM_INLINE_BUTTON_CAP:
        buttons = buttons[:_TELEGRAM_INLINE_BUTTON_CAP]
    rows = build_menu(buttons, n_cols=_TONIGHT_BUTTON_COLUMNS)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Команды, которые открывает и свободный текст (`AI_COMMANDS`), принимают (message, bot):
# так их одинаково зовёт и aiogram (бот подставляется по имени параметра), и `on_free_text`.


async def cmd_start(message: Message, bot: Bot, command: CommandObject) -> None:
    """`/start`, в том числе deep link `?start=game_<id>` из карточки матча."""
    args = (command.args or "").split()
    if args and args[0].startswith("game_"):
        try:
            gid = int(args[0].split("_", 1)[1])
        except (IndexError, ValueError):
            await message.answer_rich(rich_message(START_MESSAGE))
            return
        await send_game_card_message(bot, message.chat.id, gid)
        return
    await message.answer_rich(rich_message(START_MESSAGE))


async def cmd_help(message: Message, bot: Bot) -> None:
    """`/help`: список команд бота."""
    await message.answer_rich(rich_message(HELP_MESSAGE))


async def cmd_tonight(message: Message, bot: Bot) -> None:
    """`/tonight`: расписание сегодняшнего дня из NHL API плюс кнопки матчей."""
    try:
        payload = await asyncio.to_thread(fetch_score, league_today())
    except ScoreboardFetchError:
        logger.exception("NHL score request failed")
        await message.answer(
            "Сейчас не удалось загрузить расписание NHL. Попробуйте чуть позже."
        )
        return
    games = slate_games_sorted(payload)
    # Проза без счётных элементов — сноска без чисел приходит из хелпера
    # по умолчанию (truncate_telegram_text), а не из литерала здесь.
    text = truncate_telegram_text(tonight_reply_intro(payload))
    await message.answer_rich(
        rich_message(text),
        reply_markup=build_tonight_match_keyboard(games) if games else None,
    )


async def cmd_standings(message: Message, bot: Bot) -> None:
    """`/standings`: турнирная таблица лиги."""
    await message.answer_rich(rich_message(team_table()))


async def cmd_today(message: Message, bot: Bot) -> None:
    """`/today`: дайджест матчей прошедшей ночи по Москве (`last_night_day()`).

    Ночь без матчей (выходной день лиги или данные ещё не загружены) — честная
    пометка и последний игровой день в базе, а не голое «матчей не найдено»:
    иначе последние результаты нечем посмотреть.
    """
    night = last_night_day()
    day_label, games = day_digest(night)
    if all(gid == 0 for gid, _text, _meta in games):
        await message.answer(
            f"Прошедшей ночью ({night}) матчей в базе нет — показываю последний игровой день."
        )
        day_label, games = day_digest()
    await dispatch_day_digest_messages(bot, message.chat.id, day_label, games)


async def cmd_team(message: Message, bot: Bot) -> None:
    """`/team`: команды сезона кликабельными командами `/ABBR`."""
    await message.answer_rich(rich_message(team_list_text()))


def team_profile_text(abbrev: str, full: bool) -> str:
    """Ответ `/BOS` — статистика клуба (`team_profile()`), при `full` (`/BOS_FULL`) — все
    его игроки (`team_full_stats()`); неизвестную аббревиатуру обе функции сами
    превращают в текст с причиной."""
    body = team_full_stats(abbrev) if full else team_profile(abbrev)
    return truncate_telegram_text(body + "\n\nВсе команды: /team")


async def cmd_team_profile(message: Message, match: re.Match) -> None:
    """`/BOS` и т.п.: профиль клуба, `/BOS_FULL` — все его игроки (`team_profile_text`);
    `match` — совпадение `TEAM_COMMAND_PATTERN`."""
    abbrev, full = match.group(1), match.group(2)
    await message.answer_rich(rich_message(team_profile_text(abbrev, bool(full))))


async def cmd_leaders(message: Message, bot: Bot) -> None:
    """`/leaders`: выбор категории лидеров кнопками."""
    await message.answer_rich(rich_message(leaders_menu_intro()), reply_markup=leaders_category_keyboard())


async def cmd_countries(message: Message, bot: Bot) -> None:
    """`/countries`: рейтинг стран сезона и кнопки стран (Задача 49)."""
    text, markup = country_rankings_reply()
    await message.answer_rich(rich_message(text), reply_markup=markup)


async def cmd_advanced(message: Message, bot: Bot) -> None:
    """`/advanced`: расширенная статистика полевых игроков вне диалога `/stats`."""
    await message.answer_rich(rich_message(ADVANCED_COMMAND_INTRO), reply_markup=advanced_standalone_keyboard())


async def cmd_cancel_in_conversation(message: Message, state: FSMContext, bot: Bot) -> None:
    """`/cancel` внутри диалога `/stats`: закрывает разговор.

    Зачем читает данные диалога: конец диалога не трогает сообщение меню — его
    inline-клавиатура остаётся в чате на вид живой, но вне диалога ни один хендлер
    её callback_data больше не матчит (тот же класс дефекта, что и у просроченной
    кнопки «« Назад»» ввода даты: кнопка матчится только в тех состояниях FSM, где
    для неё явно зарегистрирован хендлер). Если экран меню создавался НОВЫМ
    сообщением, его id записан под `dialog_states.LAST_MENU_MESSAGE_ID_KEY` (там же —
    канонический список мест записи) — снимаем клавиатуру явно. Отсутствие записи —
    штатный случай (`/cancel` без открытого меню), не ошибка.
    """
    menu_message_id = (await state.get_data()).get(LAST_MENU_MESSAGE_ID_KEY)
    if menu_message_id is not None:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            reply_markup=None,
        )
    await state.clear()
    await message.answer("Вы вышли из меню. Снова: /stats")


async def cmd_cancel_outside_conversation(message: Message) -> None:
    """`/cancel` вне диалога: объясняет, что отменять нечего."""
    await message.answer("Меню /stats сейчас не открыто. Справка: /help")


_DELIVERY_NOTE = (
    "Итоги ночи приходят, когда загружены все её матчи, не позже 11:00 МСК.\n"
    "Превью матчей дня — с 15:30 МСК, по одному раз в полчаса."
)
_SUBSCRIPTIONS_DB_ERROR = "Подписки недоступны: выполните `make db-migrate` на вашей БД PostgreSQL."

# Кнопки меню `/subscriptions` (Задача 63): `sub:m` главный экран, `sub:d` дайджест,
# `sub:u` предстоящие матчи (Задача 78), `sub:T` дивизионы, `sub:D:<i>` команды дивизиона i, `sub:t:<i>:<team_id>` команда
# (i — чтобы перерисовать тот же дивизион), `sub:C` страны, `sub:c:<CODE>` страна,
# `sub:x` «Готово». Самая длинная — `sub:t:3:` + id команды, заведомо меньше 64 байт.
SUBSCRIPTIONS_CALLBACK_PATTERN = r"^sub:(m|d|u|T|C|x|D:[0-3]|t:[0-3]:\d+|c:[A-Z]{3})$"
_DIVISION_BUTTON_COLUMNS = 2
_TEAM_BUTTON_COLUMNS = 4
_COUNTRY_BUTTON_COLUMNS = 3

_Screen = Tuple[str, InlineKeyboardMarkup]


def _sub_button(
    label: str, selected: bool, data: str, off_prefix: str = ""
) -> InlineKeyboardButton:
    """Кнопка переключателя: у включённого пункта подпись начинается с ✅, у выключенного —
    с `off_prefix` (для дайджеста «➕»)."""
    prefix = "✅ " if selected else off_prefix
    return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=data)


def _subscriptions_main_screen(
    digest: bool, upcoming: bool, team_abbrevs: List[str], countries: List[str]
) -> _Screen:
    """Главный экран: сводка подписок, переключатели дайджеста и предстоящих матчей
    (каждый в своём ряду — вдвоём в ряд на телефоне не влезают), переходы в подменю."""
    lines = ["Ваши подписки"]
    if digest:
        lines.append("✅ Утренний дайджест")
    if upcoming:
        lines.append("⏰ Предстоящие матчи")
    if team_abbrevs:
        lines.append("🏒 Команды: " + ", ".join(team_abbrevs))
    if countries:
        lines.append("🌍 Страны: " + ", ".join(countries))
    if len(lines) == 1:
        lines.append("Пока ничего нет.")
    lines.append(_DELIVERY_NOTE)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_sub_button("Дайджест", digest, "sub:d", "➕ ")],
        [_sub_button("Предстоящие матчи", upcoming, "sub:u", "➕ ")],
        [
            InlineKeyboardButton(text=f"🏒 Команды ({len(team_abbrevs)})", callback_data="sub:T"),
            InlineKeyboardButton(text=f"🌍 Страны ({len(countries)})", callback_data="sub:C"),
        ],
        [InlineKeyboardButton(text="Готово", callback_data="sub:x")],
    ])
    return "\n".join(lines), markup


def _subscriptions_divisions_screen(
    teams_by_division: Dict[str, List[Tuple[int, str]]], team_ids: Set[int]
) -> _Screen:
    """Шаг 1 выбора команд: дивизионы с числом выбранных команд в скобках."""
    buttons = []
    for i, (division, teams) in enumerate(teams_by_division.items()):
        chosen = sum(1 for team_id, _ in teams if team_id in team_ids)
        label = f"{division} ({chosen})" if chosen else division
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"sub:D:{i}"))
    rows = build_menu(buttons, n_cols=_DIVISION_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:m")])
    return "Команды: выберите дивизион.", InlineKeyboardMarkup(inline_keyboard=rows)


def _subscriptions_teams_screen(
    teams_by_division: Dict[str, List[Tuple[int, str]]], team_ids: Set[int], division_index: int
) -> _Screen:
    """Шаг 2 выбора команд: команды дивизиона номер `division_index`, у выбранных ✅."""
    division, teams = list(teams_by_division.items())[division_index]
    buttons = [
        _sub_button(abbrev, team_id in team_ids, f"sub:t:{division_index}:{team_id}")
        for team_id, abbrev in teams
    ]
    rows = build_menu(buttons, n_cols=_TEAM_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton(text="« К дивизионам", callback_data="sub:T")])
    return (
        f"{division}: нажмите на команду, чтобы подписаться или отписаться.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _subscriptions_countries_screen(codes: List[str], chosen: List[str]) -> _Screen:
    """Страны рейтинга `/countries`, у выбранных ✅. Подписанная страна, выпавшая из рейтинга,
    тоже получает кнопку — иначе от неё нельзя отписаться."""
    buttons = [
        _sub_button(code, code in chosen, f"sub:c:{code}")
        for code in codes + [c for c in chosen if c not in codes]
    ]
    rows = build_menu(buttons, n_cols=_COUNTRY_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:m")])
    return (
        "Страны: нажмите на страну, чтобы подписаться или отписаться.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _subscriptions_main_screen_for(chat_id: int) -> _Screen:
    """Главный экран по текущим подпискам чата; читает БД."""
    subs = subscription_repo.get_chat_subscriptions(chat_id)
    abbrevs = sorted(
        abbrev
        for teams in season_teams_by_division().values()
        for team_id, abbrev in teams
        if team_id in subs.team_ids
    )
    return _subscriptions_main_screen(subs.digest, subs.upcoming, abbrevs, subs.countries)


async def cmd_subscriptions(message: Message, bot: Bot) -> None:
    """`/subscriptions`: меню подписок — сводка, дайджест, предстоящие матчи, команды и
    страны (Задачи 63, 78)."""
    try:
        text, markup = _subscriptions_main_screen_for(message.chat.id)
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await message.answer(_SUBSCRIPTIONS_DB_ERROR)
        return
    await message.answer(text, reply_markup=markup)


def _toggle_subscription(chat_id: int, action: str, args: List[str]) -> str:
    """Переключает подписку по нажатой кнопке (`sub:d`, `sub:u`, `sub:t:…`, `sub:c:…`), пишет
    в БД и возвращает подсказку для ответа на нажатие."""
    digest, upcoming, team_ids, countries = subscription_repo.get_chat_subscriptions(chat_id)
    if action in ("d", "u"):
        kind, on = ("morning_digest", digest) if action == "d" else ("upcoming_games", upcoming)
        if on:
            subscription_repo.mark_subscription_inactive_by_chat_kind_team(chat_id, kind)
        else:
            subscription_repo.upsert_chat_kind(chat_id, kind)
        if action == "d":
            return "Дайджест " + ("выключен" if on else "включён")
        return "Предстоящие матчи " + ("выключены" if on else "включены")
    if action == "t":
        team_id = int(args[1])
        abbrev = next(
            a for teams in season_teams_by_division().values() for t, a in teams if t == team_id
        )
        if team_id in team_ids:
            subscription_repo.deactivate_team_scores(chat_id, team_id)
        else:
            subscription_repo.upsert_team_scores(chat_id, team_id)
        return f"Подписка на {abbrev} " + ("отключена" if team_id in team_ids else "включена")
    code = args[0]
    if code in countries:
        subscription_repo.mark_subscription_inactive_by_chat_kind_team(
            chat_id, "country_players", None, code
        )
    else:
        subscription_repo.upsert_country(chat_id, code)
    return f"Подписка на {code} " + ("отключена" if code in countries else "включена")


async def callback_subscriptions(callback: CallbackQuery) -> None:
    """Кнопки `sub:…` меню `/subscriptions`: переключают подписку (каждое нажатие сразу
    пишется в БД) и перерисовывают то же сообщение; ✅ рисуются по данным БД после записи,
    поэтому при сбое записи отметки нет. «Готово» убирает клавиатуру."""
    message = callback_message(callback)
    chat_id = message.chat.id
    assert callback.data is not None
    _, action, *args = callback.data.split(":")
    hint = None
    try:
        if action in ("d", "u", "t", "c"):
            hint = _toggle_subscription(chat_id, action, args)
        if action in ("m", "d", "u", "x"):
            text, markup = _subscriptions_main_screen_for(chat_id)
        elif action in ("T", "D", "t"):
            teams_by_division = season_teams_by_division()
            team_ids = subscription_repo.get_chat_subscriptions(chat_id).team_ids
            if not any(teams_by_division.values()):
                text, markup = "В базе нет списка команд для этого сезона.", InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="sub:m")]]
                )
            elif action == "T":
                text, markup = _subscriptions_divisions_screen(teams_by_division, team_ids)
            else:
                text, markup = _subscriptions_teams_screen(
                    teams_by_division, team_ids, int(args[0])
                )
        else:
            rankings_text, codes = country_rankings()
            chosen = subscription_repo.get_chat_subscriptions(chat_id).countries
            if codes or chosen:
                text, markup = _subscriptions_countries_screen(codes, chosen)
            else:
                text, markup = rankings_text, InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="sub:m")]]
                )
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await callback.answer()
        await message.edit_text(_SUBSCRIPTIONS_DB_ERROR)
        return
    await callback.answer(hint)
    await message.edit_text(text, reply_markup=None if action == "x" else markup)


# Свободный текст (Задача 81): экран, выбранный моделью (`ai_router`), бот рисует тем же
# кодом, что команду или кнопку, но новым сообщением в ответ на вопрос.
AI_COMMANDS: Dict[str, Callable[[Message, Bot], Awaitable[None]]] = {
    "today": cmd_today,
    "tonight": cmd_tonight,
    "standings": cmd_standings,
    "team": cmd_team,
    "leaders": cmd_leaders,
    "countries": cmd_countries,
    "advanced": cmd_advanced,
    "subscriptions": cmd_subscriptions,
    "help": cmd_help,
}


async def _ai_digest_for_date(message: Message, bot: Bot, params: Dict[str, Any]) -> None:
    """Дайджест игрового дня `params["date"]` — как ввод даты в `/stats`, но без кнопок меню."""
    day_label, games = day_digest(params["date"])
    await dispatch_day_digest_messages(bot, message.chat.id, day_label, games)


async def _ai_team_profile(message: Message, bot: Bot, params: Dict[str, Any]) -> None:
    """Профиль клуба, как `/WSH` (`all_players=false`) или `/WSH_FULL` (`true`)."""
    text = team_profile_text(params["team"], params["all_players"])
    await message.answer_rich(rich_message(text))


def _ai_reply(screen: Callable[[Dict[str, Any]], Tuple[str, InlineKeyboardMarkup]]):
    """Экран из функции «параметры → (текст, кнопки)» ответом на вопрос пользователя."""
    async def _send(message: Message, bot: Bot, params: Dict[str, Any]) -> None:
        text, markup = screen(params)
        await message.answer_rich(rich_message(text), reply_markup=markup)
    return _send


# Инструмент модели (`ai_router.build_tools`) → функция экрана.
AI_SCREENS: Dict[str, Callable[[Message, Bot, Dict[str, Any]], Awaitable[None]]] = {
    "open_command": lambda message, bot, p: AI_COMMANDS[p["command"]](message, bot),
    "digest_for_date": _ai_digest_for_date,
    "team_profile": _ai_team_profile,
    "leaders": _ai_reply(lambda p: leaders_reply(p["category"], 0)),
    "country_players": _ai_reply(lambda p: country_page_reply(p["country"], "F", 0)),
    "advanced_metric": _ai_reply(lambda p: standalone_stat_reply(*ADVANCED_METRICS[p["metric"]], 0)),
}


async def on_free_text(message: Message, bot: Bot) -> None:
    """Текст в личке вне команд и вне ввода даты `/stats`: вопрос модели (`ai_router.ask`).

    Лимит исчерпан — ответ без вызова модели; модель выбрала экран — бот показывает его
    (`AI_SCREENS`); иначе — её текст: отказ, уточнение или сообщение о сбое API.
    """
    chat_id = message.chat.id
    if ai_router.questions_left(chat_id) <= 0:
        await message.answer(ai_router.LIMIT_TEXT)
        return
    reply = await ai_router.ask(chat_id, message.text or "")
    if reply.tool is None:
        await message.answer(reply.text or ai_router.UNSUPPORTED_TEXT)
        return
    await AI_SCREENS[reply.tool](message, bot, reply.params)


def is_plain_text(message: Message) -> bool:
    """Текстовое сообщение, не начинающееся с команды бота.

    Зачем: ввод даты в `/stats` и свободный текст ловят только обычный текст, а
    `/cancel` и любая другая команда должны дойти до своих хендлеров. Команда — как
    у Telegram: сущность `bot_command` в самом начале текста.
    """
    if message.text is None:
        return False
    entities = message.entities or []
    return not (entities and entities[0].type == "bot_command" and entities[0].offset == 0)


def _state(value: int) -> str:
    """Regex callback_data кнопки меню с числовым значением `value` (`dialog_states`)."""
    return f"^{value}$"


# Диалог `/stats`: состояние → (regex callback_data, хендлер) в порядке проверки. Кнопка
# срабатывает только в тех состояниях, где она здесь указана: вне диалога его кнопки молчат.
STATS_DIALOG: Dict[State, List[Tuple[str, Callable[..., Awaitable[None]]]]] = {
    StatsMenu.first: [
        (_state(CHOOSE_STATS), stats_root_edit),
        (_state(DAY_DIGEST), bot_digest_date_menu),
        (_state(LEAGUE_STANDINGS), bot_league_standings),
        (_state(DIGEST_CALENDAR_TODAY), bot_digest_calendar_today),
        (_state(DIGEST_CALENDAR_YESTERDAY), bot_digest_calendar_yesterday),
        (_state(DIGEST_PICK_DATE), bot_digest_pick_date_prompt),
        # Просроченная кнопка «« Назад»» ввода даты (её копии переживают выход из
        # `third`, см. stats_handlers.bot_digest_custom_date) — открывает меню
        # дайджеста, родительский экран.
        (f"^{DIGEST_BACK_FROM_DATE_CALLBACK}$", bot_digest_date_menu),
        (f"^{NAV_PLAYERS}$", bot_player_stats),
        (f"^{NAV_FIELD}$", bot_player_field),
        (STAT_PAGE_CALLBACK_PATTERN, callback_stats_player_page),
        (TEAM_PAGE_CALLBACK_PATTERN, callback_stats_team_page),
        (_state(TEAM_STATS), bot_team_stats),
        (_state(PLAYER_STATS), bot_player_stats),
        (_state(PLAYER_FIELD), bot_player_field),
        (_state(PLAYER_GOALIE), bot_player_goalie),
        (_state(PLAYER_ADVANCED_SUBMENU), bot_player_advanced_menu),
        (_state(PLAYER_POINTS), bot_player_points),
        (_state(PLAYER_GOALS), bot_player_goals),
        (_state(PLAYER_ASSISTS), bot_player_assists),
        (_state(PLAYER_PLUS_MINUS), bot_player_plus_minus),
        (_state(PLAYER_PENALTIES), bot_player_penalties),
        (_state(PLAYER_HITS), bot_player_hits),
        (_state(PLAYER_BLOCKS), bot_player_blocks),
        (_state(PLAYER_ICE_TIME), bot_player_ice_time),
        (_state(PLAYER_SAT_PCT), bot_player_sat_pct),
        (_state(PLAYER_USAT_PCT), bot_player_usat_pct),
        (_state(PLAYER_GOALS_FOR_PCT), bot_player_goals_for_pct),
        (_state(PLAYER_OZ_START_PCT), bot_player_oz_start_pct),
        (_state(PLAYER_SHOOTOUT_PCT), bot_player_shootout_pct),
        (_state(PLAYER_SHOT_WRIST), bot_player_shot_wrist),
        (_state(PLAYER_SHOT_SLAP), bot_player_shot_slap),
        (_state(PLAYER_SHOT_SNAP), bot_player_shot_snap),
        (_state(PLAYER_SHOT_BACKHAND), bot_player_shot_backhand),
        (_state(PLAYER_SHOT_TIP_IN), bot_player_shot_tip_in),
        (_state(PLAYER_SHOT_DEFLECTED), bot_player_shot_deflected),
        (_state(PLAYER_SHOT_WRAP_AROUND), bot_player_shot_wrap),
        (_state(GOALIE_WINS), bot_goalie_wins),
        (_state(GOALIE_PERCENTAGE), bot_goalie_percentage),
        (_state(GOALIE_SHOOTOUTS), bot_goalie_shootouts),
        (_state(TEAM_PROCENT_WINS), bot_team_procent_wins),
        (_state(TEAM_POWER_PLAY), bot_team_power_play),
        (_state(TEAM_POWER_KILL), bot_team_power_kill),
        (_state(TEAM_CONFERENCE_STATS), bot_team_conference_stats),
        (_state(TEAM_DIVISION_STATS), bot_team_division_stats),
        (_state(TEAM_PROFILE_PICK), bot_team_profile_pick),
    ],
    StatsMenu.second: [
        (STAT_PAGE_CALLBACK_PATTERN, callback_stats_player_page),
        (TEAM_PAGE_CALLBACK_PATTERN, callback_stats_team_page),
        # «« Назад»» страницы стата ведёт на родительское подменю — эти подменю
        # переводят диалог в `first`, страница стата — в `second`, поэтому их
        # хендлеры нужны и здесь.
        (_state(PLAYER_FIELD), bot_player_field),
        (_state(PLAYER_GOALIE), bot_player_goalie),
        (_state(PLAYER_ADVANCED_SUBMENU), bot_player_advanced_menu),
        (_state(TEAM_STATS), bot_team_stats),
        (TEAM_PROFILE_CALLBACK_PATTERN, bot_team_profile_show),
        (_state(TEAM_PROFILE_PICK), bot_team_profile_pick),
        # «« Назад»» результата дайджеста ведёт на меню дайджеста; та же
        # просроченная кнопка ввода даты, что и в `first` — сюда тоже можно
        # вернуться из `second`.
        (_state(DAY_DIGEST), bot_digest_date_menu),
        (f"^{DIGEST_BACK_FROM_DATE_CALLBACK}$", bot_digest_date_menu),
        (_state(CHOOSE_STATS), stats_over),
        (_state(END_CONVERSATION), end),
    ],
    StatsMenu.third: [
        (_state(CHOOSE_STATS), stats_root_edit),
        (f"^{DIGEST_BACK_FROM_DATE_CALLBACK}$", bot_digest_date_menu),
    ],
}


def _command(name: str) -> Command:
    """Фильтр команды `/name` (и `/name@бот`) без учёта регистра."""
    return Command(name, ignore_case=True)


def _callback(pattern: str) -> MagicFilter:
    """Фильтр callback_data по regex `pattern`; совпадение уходит хендлеру аргументом `match`."""
    return F.data.regexp(pattern).as_("match")


def build_standalone_router() -> Router:
    """Команды верхнего уровня и inline-кнопки вне диалога `/stats`.

    Без фильтра состояния и первым в `Dispatcher`: работают и при открытом меню `/stats`.
    """
    router = Router(name="standalone")
    router.message.register(cmd_start, CommandStart(ignore_case=True))
    for name, handler in (
        ("help", cmd_help),
        ("today", cmd_today),
        ("tonight", cmd_tonight),
        ("standings", cmd_standings),
        ("team", cmd_team),
    ):
        router.message.register(handler, _command(name))
    router.message.register(cmd_team_profile, F.text.regexp(TEAM_COMMAND_PATTERN).as_("match"))
    for name, handler in (
        ("leaders", cmd_leaders),
        ("countries", cmd_countries),
        ("advanced", cmd_advanced),
        ("subscriptions", cmd_subscriptions),
    ):
        router.message.register(handler, _command(name))
    for pattern, callback in (
        (LEADERS_PICK_CALLBACK_PATTERN, callback_leaders_pick),
        (LEADERBOARD_PAGE_CALLBACK_PATTERN, callback_leaderboard_page),
        (DIGEST_EXPAND_CALLBACK_PATTERN, callback_expand_digest_game),
        (SUBSCRIPTIONS_CALLBACK_PATTERN, callback_subscriptions),
        (TONIGHT_GAME_CALLBACK_PATTERN, callback_tonight_game),
        (COUNTRY_CALLBACK_PATTERN, callback_country),
        (STANDALONE_SA_CALLBACK_PATTERN, callback_standalone_sa),
        (ADVANCED_CALLBACK_PATTERN, callback_standalone_adv),
        (GOAL_VIDEO_CALLBACK_PATTERN, handle_goal_video),
    ):
        router.callback_query.register(callback, _callback(pattern))
    return router


def build_stats_router() -> Router:
    """Диалог `/stats` (FSM `StatsMenu`) по карте `STATS_DIALOG`.

    `/stats` открывает меню в любом состоянии, `/cancel` и ввод даты (`third`) — только
    внутри диалога; ввод даты ловит лишь обычный текст (`is_plain_text`), так что
    `/cancel` в `third` доходит до своего хендлера.
    """
    router = Router(name="stats")
    router.message.register(stats, _command("stats"))
    router.message.register(cmd_cancel_in_conversation, _command("cancel"), StateFilter(StatsMenu))
    router.message.register(bot_digest_custom_date, StatsMenu.third, is_plain_text)
    for state, handlers in STATS_DIALOG.items():
        for pattern, callback in handlers:
            router.callback_query.register(callback, state, _callback(pattern))
    return router


def build_tail_router() -> Router:
    """Последний роутер: `/cancel` вне диалога и свободный текст (Задача 81).

    Стоит после диалога: в aiogram апдейт берёт первый подошедший хендлер, поэтому ввод
    даты в `third` и `/cancel` в диалоге забирает `build_stats_router`, а ИИ видит лишь
    текст, который диалогу не нужен. Свободный текст — только в личке.
    """
    router = Router(name="tail")
    router.message.register(cmd_cancel_outside_conversation, _command("cancel"))
    router.message.register(on_free_text, F.chat.type == "private", is_plain_text)
    return router


async def _publish_command_menu(bot: Bot) -> None:
    """Публикует список команд в Telegram (`set_my_commands`) при старте бота.

    Зачем: без него кнопка «Меню» клиента пуста или показывает устаревший
    список — пользователь не видит, какие команды есть. Источник —
    `help_text.BOT_COMMANDS`, тот же, что у `/help`.
    """
    await bot.set_my_commands(
        [BotCommand(command=cmd, description=desc) for cmd, desc in BOT_COMMANDS]
    )


def build_dispatcher() -> Dispatcher:
    """Собирает `Dispatcher` со всеми хендлерами бота.

    Зачем: единственная точка сборки, которую тест может построить без сети и
    проверить маршрутизацию апдейтов (`Dispatcher.feed_update`), не поднимая polling.

    Троттлинг нажатий (`throttle.py`) — outer-middleware `callback_query`: он видит
    нажатие раньше фильтров любого роутера и при превышении лимита обрывает его, не
    давая дойти ни до standalone-кнопок, ни до диалога. Роутеры проверяются по
    порядку: standalone → диалог `/stats` → хвост (`build_tail_router`).

    Апдейты обрабатываются конкурентно (polling aiogram запускает каждый задачей):
    ожидание ответа модели на свободный текст (секунды, при сбое API — до ~30 с) не
    останавливает кнопки и команды остальных пользователей.
    """
    dispatcher = Dispatcher()
    dispatcher.callback_query.outer_middleware(enforce_callback_rate_limit)
    dispatcher.startup.register(_publish_command_menu)
    dispatcher.include_routers(build_standalone_router(), build_stats_router(), build_tail_router())
    return dispatcher


async def run_polling(token: str) -> None:
    """Long polling бота с токеном `token` (в проде `config.TOKEN`) до сигнала остановки."""
    await build_dispatcher().start_polling(Bot(token))


def main() -> None:
    """Точка входа `python bot.py`: проверка окружения, затем long polling
    до сигнала остановки."""
    config.validate_env("ANTHROPIC_API_KEY")
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
    )
    logger.info("Starting bot")
    asyncio.run(run_polling(config.TOKEN))


if __name__ == '__main__':
    main()
