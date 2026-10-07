"""Точка входа Telegram-бота: сборка `Application` и регистрация хендлеров.

Роль в пайплайне: `bot.py` собирает python-telegram-bot 21.x `Application`,
вешает на него троттлинг callback-кнопок (`throttle.py`), standalone-команды/
кнопки и `ConversationHandler` меню `/stats`, затем запускает long polling.
Сами обработчики живут в `script_bot.py` (навигация) и `stats_handlers.py`
(выборки из БД); здесь — только команды верхнего уровня и сборка `Application`.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    BaseHandler,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import subscription_repo
from throttle import enforce_callback_rate_limit
from bot_messages import (
    country_rankings,
    day_digest,
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
    CHOOSE_STATS,
    DAY_DIGEST,
    DIGEST_CALENDAR_TODAY,
    DIGEST_CALENDAR_YESTERDAY,
    DIGEST_PICK_DATE,
    END_CONVERSATION,
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
    SECOND,
    TEAM_CONFERENCE_STATS,
    TEAM_DIVISION_STATS,
    TEAM_POWER_KILL,
    TEAM_POWER_PLAY,
    TEAM_PROCENT_WINS,
    TEAM_PROFILE_PICK,
    TEAM_STATS,
    THIRD,
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
    nav_back_to_field,
    nav_back_to_players,
    stats,
    stats_over,
    stats_root_edit,
)
from stats_handlers import (
    COUNTRY_CALLBACK_PATTERN,
    DIGEST_BACK_FROM_DATE_CALLBACK,
    DIGEST_EXPAND_CALLBACK_PATTERN,
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
    leaders_category_keyboard,
    dispatch_day_digest_messages,
    handle_goal_video,
    send_game_card_message,
)

logger = logging.getLogger(__name__)

STANDALONE_GROUP = -1

# Троттлинг callback-кнопок (throttle.py) обязан видеть апдейт раньше любого
# адресного хендлера, включая standalone-команды/кнопки, — иначе спам успеет
# дойти до БД до того, как перехватчик его остановит.
THROTTLE_GROUP = STANDALONE_GROUP - 1

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
        buttons.append(InlineKeyboardButton(label, callback_data=cb))
    if not buttons:
        return None
    if len(buttons) > _TELEGRAM_INLINE_BUTTON_CAP:
        buttons = buttons[:_TELEGRAM_INLINE_BUTTON_CAP]
    rows = build_menu(buttons, n_cols=_TONIGHT_BUTTON_COLUMNS)
    return InlineKeyboardMarkup(rows)


def _message(update: Update) -> Message:
    """Сообщение, на которое отвечает команда.

    Зачем: у `Update.message` тип `Optional`, а CommandHandler вызывает нас
    только на апдейтах с сообщением — распаковываем один раз и громко падаем,
    если инвариант нарушен. `update` — апдейт, пришедший в хендлер.
    """
    message = update.message
    assert message is not None
    return message


def _chat_id(update: Update) -> int:
    """Чат, из которого пришла команда (для записи подписок)."""
    chat = update.effective_chat
    assert chat is not None
    return chat.id


async def cmd_start(update: Update, context: CallbackContext) -> None:
    """`/start`, в том числе deep link `?start=game_<id>` из карточки матча."""
    message = _message(update)
    args = context.args or []
    if args and args[0].startswith("game_"):
        try:
            gid = int(args[0].split("_", 1)[1])
        except (IndexError, ValueError):
            await message.reply_text(START_MESSAGE, parse_mode="HTML")
            return
        await send_game_card_message(context, message.chat_id, gid)
        return
    await message.reply_text(START_MESSAGE, parse_mode="HTML")


async def cmd_help(update: Update, context: CallbackContext) -> None:
    """`/help`: список команд бота."""
    await _message(update).reply_text(HELP_MESSAGE, parse_mode="HTML")


async def cmd_tonight(update: Update, context: CallbackContext) -> None:
    """`/tonight`: расписание сегодняшнего дня из NHL API плюс кнопки матчей."""
    message = _message(update)
    try:
        payload = await asyncio.to_thread(fetch_score, league_today())
    except ScoreboardFetchError:
        logger.exception("NHL score request failed")
        await message.reply_text(
            "Сейчас не удалось загрузить расписание NHL. Попробуйте чуть позже."
        )
        return
    games = slate_games_sorted(payload)
    # Проза без счётных элементов — сноска без чисел приходит из хелпера
    # по умолчанию (truncate_telegram_text), а не из литерала здесь.
    text = truncate_telegram_text(tonight_reply_intro(payload))
    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=build_tonight_match_keyboard(games) if games else None,
    )


async def cmd_standings(update: Update, context: CallbackContext) -> None:
    """`/standings`: турнирная таблица лиги."""
    await _message(update).reply_text(team_table(), parse_mode="HTML")


async def cmd_today(update: Update, context: CallbackContext) -> None:
    """`/today`: дайджест матчей прошедшей ночи по Москве (`last_night_day()`).

    Ночь без матчей (выходной день лиги или данные ещё не загружены) — честная
    пометка и последний игровой день в базе, а не голое «матчей не найдено»:
    иначе последние результаты нечем посмотреть.
    """
    message = _message(update)
    night = last_night_day()
    day_label, games = day_digest(night)
    if all(gid == 0 for gid, _text, _meta in games):
        await message.reply_text(
            f"Прошедшей ночью ({night}) матчей в базе нет — показываю последний игровой день."
        )
        day_label, games = day_digest()
    await dispatch_day_digest_messages(
        context,
        message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=False,
    )


async def cmd_team(update: Update, context: CallbackContext) -> None:
    """`/team`: команды сезона кликабельными командами `/ABBR`."""
    await _message(update).reply_text(team_list_text(), parse_mode="HTML")


async def cmd_team_profile(update: Update, context: CallbackContext) -> None:
    """`/BOS` и т.п.: статистика клуба (`team_profile()`), `/BOS_FULL` — все
    его игроки (`team_full_stats()`); неизвестную аббревиатуру обе функции
    сами превращают в текст с причиной."""
    message = _message(update)
    assert context.matches
    abbrev, full = context.matches[0].group(1), context.matches[0].group(2)
    body = team_full_stats(abbrev) if full else team_profile(abbrev)
    text = truncate_telegram_text(body + "\n\nВсе команды: /team")
    await message.reply_text(text, parse_mode="HTML")


async def cmd_leaders(update: Update, context: CallbackContext) -> None:
    """`/leaders`: выбор категории лидеров кнопками."""
    await _message(update).reply_text(
        leaders_menu_intro(),
        parse_mode="HTML",
        reply_markup=leaders_category_keyboard(),
    )


async def cmd_countries(update: Update, context: CallbackContext) -> None:
    """`/countries`: рейтинг стран сезона и кнопки стран (Задача 49)."""
    text, markup = country_rankings_reply()
    await _message(update).reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_game(update: Update, context: CallbackContext) -> None:
    """`/game <id>`: карточка конкретного матча."""
    message = _message(update)
    if not context.args:
        await message.reply_text(
            "Карточку матча проще открыть так: отправьте /today "
            "и нажмите кнопку нужной игры под сводкой.",
            parse_mode="HTML",
        )
        return
    try:
        game_id = int(context.args[0])
    except ValueError:
        await message.reply_text("После `/game` укажите одно целое число.")
        return
    await send_game_card_message(context, message.chat_id, game_id)


async def cmd_advanced(update: Update, context: CallbackContext) -> None:
    """`/advanced`: расширенная статистика полевых игроков вне диалога `/stats`."""
    await _message(update).reply_text(
        ADVANCED_COMMAND_INTRO,
        parse_mode="HTML",
        reply_markup=advanced_standalone_keyboard(),
    )


async def cmd_cancel_in_conversation(update: Update, context: CallbackContext) -> int:
    """`/cancel` внутри диалога `/stats`: закрывает разговор.

    Зачем читает `user_data`: `ConversationHandler.END` не трогает сообщение
    меню — его inline-клавиатура остаётся в чате на вид живой, но после конца
    диалога ни один хендлер её callback_data больше не матчит (тот же класс
    дефекта, что и у просроченной кнопки «« Назад»» ввода даты: кнопка матчится
    только в тех состояниях FSM, где для неё явно зарегистрирован хендлер).
    Если экран меню создавался НОВЫМ сообщением, его id записан под
    `dialog_states.LAST_MENU_MESSAGE_ID_KEY` (там же — канонический список
    мест записи) — снимаем клавиатуру явно. Отсутствие записи — штатный
    случай (`/cancel` без открытого меню), не ошибка.
    """
    message = _message(update)
    assert context.user_data is not None
    menu_message_id = context.user_data.pop(LAST_MENU_MESSAGE_ID_KEY, None)
    if menu_message_id is not None:
        await context.bot.edit_message_reply_markup(
            chat_id=message.chat_id,
            message_id=menu_message_id,
            reply_markup=None,
        )
    await message.reply_text("Вы вышли из меню. Снова: /stats")
    return ConversationHandler.END


async def cmd_cancel_outside_conversation(update: Update, context: CallbackContext) -> None:
    """`/cancel` вне диалога: объясняет, что отменять нечего."""
    await _message(update).reply_text("Меню /stats сейчас не открыто. Справка: /help")


_DELIVERY_NOTE = (
    "Рассылка приходит, когда загружены все матчи ночи (проверка раз в 30 минут), "
    "не позже 11:00 МСК."
)
_SUBSCRIPTIONS_DB_ERROR = "Подписки недоступны: выполните `make db-migrate` на вашей БД PostgreSQL."

# Кнопки меню `/subscriptions` (Задача 63): `sub:m` главный экран, `sub:d` дайджест,
# `sub:T` дивизионы, `sub:D:<i>` команды дивизиона i, `sub:t:<i>:<team_id>` команда
# (i — чтобы перерисовать тот же дивизион), `sub:C` страны, `sub:c:<CODE>` страна,
# `sub:x` «Готово». Самая длинная — `sub:t:3:` + id команды, заведомо меньше 64 байт.
SUBSCRIPTIONS_CALLBACK_PATTERN = r"^sub:(m|d|T|C|x|D:\d|t:\d:\d+|c:[A-Z]{3})$"
_DIVISION_BUTTON_COLUMNS = 2
_TEAM_BUTTON_COLUMNS = 4
_COUNTRY_BUTTON_COLUMNS = 3

_Screen = Tuple[str, InlineKeyboardMarkup]


def _sub_button(label: str, selected: bool, data: str) -> InlineKeyboardButton:
    """Кнопка переключателя: у включённого пункта подпись начинается с ✅."""
    return InlineKeyboardButton(f"✅ {label}" if selected else label, callback_data=data)


def _subscriptions_main_screen(
    digest: bool, team_abbrevs: List[str], countries: List[str]
) -> _Screen:
    """Главный экран: сводка подписок, переключатель дайджеста, переходы в подменю."""
    lines = ["Ваши подписки"]
    if digest:
        lines.append("✅ Утренний дайджест")
    if team_abbrevs:
        lines.append("🏒 Команды: " + ", ".join(team_abbrevs))
    if countries:
        lines.append("🌍 Страны: " + ", ".join(countries))
    if len(lines) == 1:
        lines.append("Пока ничего нет.")
    lines.append(_DELIVERY_NOTE)
    markup = InlineKeyboardMarkup([
        [_sub_button("Дайджест", digest, "sub:d")],
        [
            InlineKeyboardButton(f"🏒 Команды ({len(team_abbrevs)})", callback_data="sub:T"),
            InlineKeyboardButton(f"🌍 Страны ({len(countries)})", callback_data="sub:C"),
        ],
        [InlineKeyboardButton("Готово", callback_data="sub:x")],
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
        buttons.append(InlineKeyboardButton(label, callback_data=f"sub:D:{i}"))
    rows = build_menu(buttons, n_cols=_DIVISION_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton("« Назад", callback_data="sub:m")])
    return "Команды: выберите дивизион.", InlineKeyboardMarkup(rows)


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
    rows.append([InlineKeyboardButton("« К дивизионам", callback_data="sub:T")])
    return f"{division}: нажмите на команду, чтобы подписаться или отписаться.", InlineKeyboardMarkup(rows)


def _subscriptions_countries_screen(codes: List[str], chosen: List[str]) -> _Screen:
    """Страны рейтинга `/countries`, у выбранных ✅."""
    buttons = [_sub_button(code, code in chosen, f"sub:c:{code}") for code in codes]
    rows = build_menu(buttons, n_cols=_COUNTRY_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton("« Назад", callback_data="sub:m")])
    return "Страны: нажмите на страну, чтобы подписаться или отписаться.", InlineKeyboardMarkup(rows)


def _subscriptions_main_screen_for(chat_id: int) -> _Screen:
    """Главный экран по текущим подпискам чата; читает БД."""
    digest, team_ids, countries = subscription_repo.get_chat_subscriptions(chat_id)
    abbrevs = sorted(
        abbrev
        for teams in season_teams_by_division().values()
        for team_id, abbrev in teams
        if team_id in team_ids
    )
    return _subscriptions_main_screen(digest, abbrevs, countries)


async def cmd_subscriptions(update: Update, context: CallbackContext) -> None:
    """`/subscriptions`: меню подписок — сводка, дайджест, команды и страны (Задача 63)."""
    try:
        text, markup = _subscriptions_main_screen_for(_chat_id(update))
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await _message(update).reply_text(_SUBSCRIPTIONS_DB_ERROR)
        return
    await _message(update).reply_text(text, reply_markup=markup)


def _toggle_subscription(chat_id: int, action: str, args: List[str]) -> str:
    """Переключает подписку по нажатой кнопке (`sub:d`, `sub:t:…`, `sub:c:…`), пишет в БД
    и возвращает подсказку для `answer_callback_query`."""
    digest, team_ids, countries = subscription_repo.get_chat_subscriptions(chat_id)
    if action == "d":
        if digest:
            subscription_repo.deactivate_morning_digest(chat_id)
        else:
            subscription_repo.upsert_morning_digest(chat_id)
        return "Дайджест " + ("выключен" if digest else "включён")
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


async def callback_subscriptions(update: Update, context: CallbackContext) -> None:
    """Кнопки `sub:…` меню `/subscriptions`: переключают подписку (каждое нажатие сразу
    пишется в БД) и перерисовывают то же сообщение; ✅ рисуются по данным БД после записи,
    поэтому при сбое записи отметки нет. «Готово» убирает клавиатуру."""
    query = update.callback_query
    assert query is not None and query.data is not None
    chat_id = _chat_id(update)
    _, action, *args = query.data.split(":")
    hint = None
    try:
        if action in ("d", "t", "c"):
            hint = _toggle_subscription(chat_id, action, args)
        if action in ("m", "d", "x"):
            text, markup = _subscriptions_main_screen_for(chat_id)
        elif action in ("T", "D", "t"):
            teams_by_division = season_teams_by_division()
            _, team_ids, _ = subscription_repo.get_chat_subscriptions(chat_id)
            if not any(teams_by_division.values()):
                text, markup = "В базе нет списка команд для этого сезона.", InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« Назад", callback_data="sub:m")]]
                )
            elif action == "T":
                text, markup = _subscriptions_divisions_screen(teams_by_division, team_ids)
            else:
                text, markup = _subscriptions_teams_screen(
                    teams_by_division, team_ids, int(args[0])
                )
        else:
            rankings_text, codes = country_rankings()
            _, _, chosen = subscription_repo.get_chat_subscriptions(chat_id)
            if codes:
                text, markup = _subscriptions_countries_screen(codes, chosen)
            else:
                text, markup = rankings_text, InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« Назад", callback_data="sub:m")]]
                )
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await query.answer()
        await query.edit_message_text(_SUBSCRIPTIONS_DB_ERROR)
        return
    await query.answer(hint)
    await query.edit_message_text(text, reply_markup=None if action == "x" else markup)


def build_conversation_handler() -> ConversationHandler:
    """Собирает `ConversationHandler` меню `/stats`.

    Зачем отдельной функцией: состав и порядок хендлеров внутри состояний FSM —
    поведение бота, и его надо проверять тестом без запуска polling.
    """
    return ConversationHandler(
        entry_points=[CommandHandler('stats', stats)],
        states={
            FIRST: [
                CallbackQueryHandler(stats_root_edit, pattern='^' + str(CHOOSE_STATS) + '$'),
                CallbackQueryHandler(bot_digest_date_menu, pattern='^' + str(DAY_DIGEST) + '$'),
                CallbackQueryHandler(bot_league_standings, pattern='^' + str(LEAGUE_STANDINGS) + '$'),
                CallbackQueryHandler(
                    bot_digest_calendar_today, pattern='^' + str(DIGEST_CALENDAR_TODAY) + '$'
                ),
                CallbackQueryHandler(
                    bot_digest_calendar_yesterday,
                    pattern='^' + str(DIGEST_CALENDAR_YESTERDAY) + '$',
                ),
                CallbackQueryHandler(
                    bot_digest_pick_date_prompt, pattern='^' + str(DIGEST_PICK_DATE) + '$'
                ),
                # Просроченная кнопка «« Назад»» ввода даты (её копии переживают
                # выход из THIRD, см. stats_handlers.bot_digest_custom_date) —
                # открывает меню дайджеста, родительский экран.
                CallbackQueryHandler(
                    bot_digest_date_menu, pattern=f"^{DIGEST_BACK_FROM_DATE_CALLBACK}$"
                ),
                CallbackQueryHandler(nav_back_to_players, pattern=f'^{NAV_PLAYERS}$'),
                CallbackQueryHandler(nav_back_to_field, pattern=f'^{NAV_FIELD}$'),
                CallbackQueryHandler(
                    callback_stats_player_page, pattern=STAT_PAGE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(
                    callback_stats_team_page, pattern=TEAM_PAGE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(bot_team_stats, pattern='^' + str(TEAM_STATS) + '$'),
                CallbackQueryHandler(bot_player_stats, pattern='^' + str(PLAYER_STATS) + '$'),

                CallbackQueryHandler(bot_player_field, pattern='^' + str(PLAYER_FIELD) + '$'),
                CallbackQueryHandler(bot_player_goalie, pattern='^' + str(PLAYER_GOALIE) + '$'),
                CallbackQueryHandler(bot_player_advanced_menu, pattern='^' + str(PLAYER_ADVANCED_SUBMENU) + '$'),

                CallbackQueryHandler(bot_player_points, pattern='^' + str(PLAYER_POINTS) + '$'),
                CallbackQueryHandler(bot_player_goals, pattern='^' + str(PLAYER_GOALS) + '$'),
                CallbackQueryHandler(bot_player_assists, pattern='^' + str(PLAYER_ASSISTS) + '$'),
                CallbackQueryHandler(bot_player_plus_minus, pattern='^' + str(PLAYER_PLUS_MINUS) + '$'),
                CallbackQueryHandler(bot_player_penalties, pattern='^' + str(PLAYER_PENALTIES) + '$'),
                CallbackQueryHandler(bot_player_hits, pattern='^' + str(PLAYER_HITS) + '$'),
                CallbackQueryHandler(bot_player_blocks, pattern='^' + str(PLAYER_BLOCKS) + '$'),
                CallbackQueryHandler(bot_player_ice_time, pattern='^' + str(PLAYER_ICE_TIME) + '$'),

                CallbackQueryHandler(bot_player_sat_pct, pattern='^' + str(PLAYER_SAT_PCT) + '$'),
                CallbackQueryHandler(bot_player_usat_pct, pattern='^' + str(PLAYER_USAT_PCT) + '$'),
                CallbackQueryHandler(bot_player_goals_for_pct, pattern='^' + str(PLAYER_GOALS_FOR_PCT) + '$'),
                CallbackQueryHandler(bot_player_oz_start_pct, pattern='^' + str(PLAYER_OZ_START_PCT) + '$'),
                CallbackQueryHandler(bot_player_shootout_pct, pattern='^' + str(PLAYER_SHOOTOUT_PCT) + '$'),

                CallbackQueryHandler(bot_player_shot_wrist, pattern='^' + str(PLAYER_SHOT_WRIST) + '$'),
                CallbackQueryHandler(bot_player_shot_slap, pattern='^' + str(PLAYER_SHOT_SLAP) + '$'),
                CallbackQueryHandler(bot_player_shot_snap, pattern='^' + str(PLAYER_SHOT_SNAP) + '$'),
                CallbackQueryHandler(bot_player_shot_backhand, pattern='^' + str(PLAYER_SHOT_BACKHAND) + '$'),
                CallbackQueryHandler(bot_player_shot_tip_in, pattern='^' + str(PLAYER_SHOT_TIP_IN) + '$'),
                CallbackQueryHandler(bot_player_shot_deflected, pattern='^' + str(PLAYER_SHOT_DEFLECTED) + '$'),
                CallbackQueryHandler(bot_player_shot_wrap, pattern='^' + str(PLAYER_SHOT_WRAP_AROUND) + '$'),

                CallbackQueryHandler(bot_goalie_wins, pattern='^' + str(GOALIE_WINS) + '$'),
                CallbackQueryHandler(bot_goalie_percentage, pattern='^' + str(GOALIE_PERCENTAGE) + '$'),
                CallbackQueryHandler(bot_goalie_shootouts, pattern='^' + str(GOALIE_SHOOTOUTS) + '$'),

                CallbackQueryHandler(bot_team_procent_wins, pattern='^' + str(TEAM_PROCENT_WINS) + '$'),
                CallbackQueryHandler(bot_team_power_play, pattern='^' + str(TEAM_POWER_PLAY) + '$'),
                CallbackQueryHandler(bot_team_power_kill, pattern='^' + str(TEAM_POWER_KILL) + '$'),
                CallbackQueryHandler(bot_team_conference_stats, pattern='^' + str(TEAM_CONFERENCE_STATS) + '$'),
                CallbackQueryHandler(bot_team_division_stats, pattern='^' + str(TEAM_DIVISION_STATS) + '$'),
                CallbackQueryHandler(bot_team_profile_pick, pattern='^' + str(TEAM_PROFILE_PICK) + '$'),
            ],
            SECOND: [
                CallbackQueryHandler(
                    callback_stats_player_page, pattern=STAT_PAGE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(
                    callback_stats_team_page, pattern=TEAM_PAGE_CALLBACK_PATTERN
                ),
                # «« Назад»» страницы стата ведёт на родительское подменю —
                # эти подменю возвращают FIRST, страница стата в SECOND,
                # поэтому их хендлеры нужны и здесь.
                CallbackQueryHandler(bot_player_field, pattern='^' + str(PLAYER_FIELD) + '$'),
                CallbackQueryHandler(bot_player_goalie, pattern='^' + str(PLAYER_GOALIE) + '$'),
                CallbackQueryHandler(
                    bot_player_advanced_menu, pattern='^' + str(PLAYER_ADVANCED_SUBMENU) + '$'
                ),
                CallbackQueryHandler(bot_team_stats, pattern='^' + str(TEAM_STATS) + '$'),
                CallbackQueryHandler(bot_team_profile_show, pattern=TEAM_PROFILE_CALLBACK_PATTERN),
                CallbackQueryHandler(bot_team_profile_pick, pattern='^' + str(TEAM_PROFILE_PICK) + '$'),
                # «« Назад»» результата дайджеста ведёт на меню дайджеста;
                # та же просроченная кнопка ввода даты, что и в FIRST — сюда
                # тоже можно вернуться из SECOND.
                CallbackQueryHandler(bot_digest_date_menu, pattern='^' + str(DAY_DIGEST) + '$'),
                CallbackQueryHandler(
                    bot_digest_date_menu, pattern=f"^{DIGEST_BACK_FROM_DATE_CALLBACK}$"
                ),
                CallbackQueryHandler(stats_over, pattern='^' + str(CHOOSE_STATS) + '$'),
                CallbackQueryHandler(end, pattern='^' + str(END_CONVERSATION) + '$'),
            ],
            THIRD: [
                CallbackQueryHandler(stats_root_edit, pattern='^' + str(CHOOSE_STATS) + '$'),
                CallbackQueryHandler(
                    bot_digest_date_menu,
                    pattern=f"^{DIGEST_BACK_FROM_DATE_CALLBACK}$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot_digest_custom_date),
            ],
        },
        fallbacks=[
            CommandHandler('stats', stats),
            CommandHandler('cancel', cmd_cancel_in_conversation),
        ],
    )

def build_standalone_handlers() -> List[BaseHandler]:
    """Хендлеры вне диалога `/stats`: команды верхнего уровня и inline-кнопки.

    Регистрируются в группе STANDALONE_GROUP, то есть просматриваются раньше
    `ConversationHandler`, и потому работают и при открытом меню `/stats`.
    """
    return [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("today", cmd_today),
        CommandHandler("tonight", cmd_tonight),
        CommandHandler("standings", cmd_standings),
        CommandHandler("team", cmd_team),
        MessageHandler(filters.Regex(TEAM_COMMAND_PATTERN), cmd_team_profile),
        CommandHandler("leaders", cmd_leaders),
        CommandHandler("countries", cmd_countries),
        CommandHandler("game", cmd_game),
        CommandHandler("advanced", cmd_advanced),
        CommandHandler("subscriptions", cmd_subscriptions),
        CallbackQueryHandler(callback_leaders_pick, pattern=LEADERS_PICK_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_leaderboard_page, pattern=LEADERBOARD_PAGE_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_expand_digest_game, pattern=DIGEST_EXPAND_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_subscriptions, pattern=SUBSCRIPTIONS_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_tonight_game, pattern=TONIGHT_GAME_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_country, pattern=COUNTRY_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_standalone_sa, pattern=STANDALONE_SA_CALLBACK_PATTERN),
        CallbackQueryHandler(
            callback_standalone_adv,
            pattern=r"^adv:(sat|usat|gf|oz|so|wrist|slap|snap|back|tip|defl|wrap|close)$",
        ),
        CallbackQueryHandler(handle_goal_video, pattern="^gv:"),
    ]


async def _publish_command_menu(application: Application) -> None:
    """Публикует список команд в Telegram (`set_my_commands`) при старте бота.

    Зачем: без него кнопка «Меню» клиента пуста или показывает устаревший
    список — пользователь не видит, какие команды есть. Источник —
    `help_text.BOT_COMMANDS`, тот же, что у `/help`.
    """
    await application.bot.set_my_commands([BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS])


def build_application(token: str) -> Application:
    """Собирает `Application` 21.x со всеми хендлерами бота.

    Зачем: единственная точка сборки, которую тест может построить с фиктивным
    токеном и проверить состав и порядок хендлеров, не поднимая polling.
    `token` — токен бота (в проде `config.TOKEN`).

    Первой регистрируется троттлинг-хендлер (`throttle.py`) в группе
    `THROTTLE_GROUP`, ниже `STANDALONE_GROUP`: он просматривает апдейт раньше
    остальных и при превышении лимита обрывает его через
    `ApplicationHandlerStop`, не давая дойти до standalone-хендлеров и
    `ConversationHandler`.
    """
    application = Application.builder().token(token).post_init(_publish_command_menu).build()
    application.add_handler(TypeHandler(Update, enforce_callback_rate_limit), group=THROTTLE_GROUP)
    for handler in build_standalone_handlers():
        application.add_handler(handler, group=STANDALONE_GROUP)
    application.add_handler(build_conversation_handler())
    application.add_handler(CommandHandler('cancel', cmd_cancel_outside_conversation))
    return application


def main() -> None:
    """Точка входа `python bot.py`: проверка окружения, затем long polling
    до сигнала остановки."""
    config.validate_env()
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
    )
    logger.info("Starting bot")
    build_application(config.TOKEN).run_polling()


if __name__ == '__main__':
    main()
