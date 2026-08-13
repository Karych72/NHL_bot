"""Точка входа Telegram-бота: сборка `Application` и регистрация хендлеров.

Роль в пайплайне: `bot.py` собирает python-telegram-bot 21.x `Application`,
вешает на него standalone-команды/кнопки и `ConversationHandler` меню `/stats`
и запускает long polling. Сами обработчики живут в `script_bot.py` (навигация)
и `stats_handlers.py` (выборки из БД); здесь — только команды верхнего уровня.
"""

import asyncio
import logging
from datetime import date
from typing import List, Optional

import psycopg2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    BaseHandler,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import subscription_repo
from bot_messages import (
    day_digest,
    leaders_menu_intro,
    season_team_abbrev_help_text,
    team_table,
    truncate_telegram_text,
)
from nhl_scoreboard import (
    ScoreboardFetchError,
    fetch_score_now,
    slate_games_sorted,
    tonight_match_button_label,
    tonight_reply_intro,
)
from help_text import ADVANCED_COMMAND_INTRO, HELP_MESSAGE, START_MESSAGE
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
    TEAM_POWER_KILL,
    TEAM_POWER_PLAY,
    TEAM_PROCENT_WINS,
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
    DIGEST_BACK_FROM_DATE_CALLBACK,
    LEADERS_PICK_CALLBACK_PATTERN,
    LEADERBOARD_PAGE_CALLBACK_PATTERN,
    STAT_PAGE_CALLBACK_PATTERN,
    STANDALONE_SA_CALLBACK_PATTERN,
    TEAM_PAGE_CALLBACK_PATTERN,
    TONIGHT_GAME_CALLBACK_PATTERN,
    advanced_standalone_keyboard,
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
    bot_team_power_kill,
    bot_team_power_play,
    bot_team_procent_wins,
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

# Лимит Bot API на число кнопок в одном inline-сообщении.
_TELEGRAM_INLINE_BUTTON_CAP = 100
# Как у кнопок «Матч N» в /day_games.
_TONIGHT_BUTTON_COLUMNS = 4


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


async def cmd_day_games(update: Update, context: CallbackContext) -> None:
    """`/day_games`: дайджест последнего игрового дня, который есть в базе."""
    message = _message(update)
    day_label, games = day_digest()
    await dispatch_day_digest_messages(
        context,
        message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=False,
    )


async def cmd_tonight(update: Update, context: CallbackContext) -> None:
    """`/tonight`: расписание сегодняшнего дня из NHL API плюс кнопки матчей."""
    message = _message(update)
    try:
        payload = await asyncio.to_thread(fetch_score_now)
    except ScoreboardFetchError:
        logger.exception("NHL score/now request failed")
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


async def cmd_table(update: Update, context: CallbackContext) -> None:
    """`/table`: турнирная таблица лиги."""
    await _message(update).reply_text(team_table(), parse_mode="HTML")


async def cmd_standings(update: Update, context: CallbackContext) -> None:
    """`/standings`: синоним `/table`."""
    await cmd_table(update, context)


async def cmd_today(update: Update, context: CallbackContext) -> None:
    """`/today`: дайджест за сегодняшнюю календарную дату."""
    message = _message(update)
    day_label, games = day_digest(date.today().isoformat())
    await dispatch_day_digest_messages(
        context,
        message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=False,
    )


async def cmd_team(update: Update, context: CallbackContext) -> None:
    """`/team`: справка по аббревиатурам команд текущего сезона."""
    await _message(update).reply_text(season_team_abbrev_help_text(), parse_mode="HTML")


async def cmd_leaders(update: Update, context: CallbackContext) -> None:
    """`/leaders`: выбор категории лидеров кнопками."""
    await _message(update).reply_text(
        leaders_menu_intro(),
        parse_mode="HTML",
        reply_markup=leaders_category_keyboard(),
    )


async def cmd_game(update: Update, context: CallbackContext) -> None:
    """`/game <id>`: карточка конкретного матча."""
    message = _message(update)
    if not context.args:
        await message.reply_text(
            "Карточку матча проще открыть так: отправьте <code>/day_games</code> "
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


async def _subscriptions_db_error_reply(update: Update) -> None:
    """Ответ на недоступную таблицу подписок — одинаковый для всех четырёх команд."""
    await _message(update).reply_text(
        "Подписки недоступны: выполните `make db-bot-subscriptions` "
        "или SQL из файла scripts/create_bot_subscriptions.sql на вашей БД PostgreSQL."
    )


async def cmd_subscribe_digest(update: Update, context: CallbackContext) -> None:
    """`/subscribe_digest`: подписка чата на утренний дайджест."""
    try:
        subscription_repo.upsert_morning_digest(_chat_id(update))
    except psycopg2.Error:
        logger.exception("subscribe_digest DB error")
        await _subscriptions_db_error_reply(update)
        return
    await _message(update).reply_text(
        "Вы подписаны на утренний дайджест (тот же контент, что даёт /day_games по последнему игровому дню в базе). "
        "Отписка: /unsubscribe_digest. Фактическая отправка по расписанию включается администратором (`ENABLE_PUSH_DIGEST=1`)."
    )


async def cmd_unsubscribe_digest(update: Update, context: CallbackContext) -> None:
    """`/unsubscribe_digest`: отключает утренний дайджест для чата."""
    try:
        subscription_repo.deactivate_morning_digest(_chat_id(update))
    except psycopg2.Error:
        logger.exception("unsubscribe_digest DB error")
        await _subscriptions_db_error_reply(update)
        return
    await _message(update).reply_text(
        "Утренний дайджест отключён для этого чата (подписка помечена неактивной)."
    )


async def cmd_subscribe_team(update: Update, context: CallbackContext) -> None:
    """`/subscribe_team <ABBREV>`: подписка чата на итоги игр команды."""
    message = _message(update)
    args = context.args or []
    if not args:
        await message.reply_text(
            "Укажите аббревиатуру команды, например: /subscribe_team WSH"
        )
        return
    tid = subscription_repo.resolve_team_id_by_abbrev(args[0])
    if tid is None:
        await message.reply_text(
            f"Команда «{args[0]}» не найдена для сезона в базе. Список: /team"
        )
        return
    try:
        subscription_repo.upsert_team_scores(_chat_id(update), tid)
    except psycopg2.Error:
        logger.exception("subscribe_team DB error")
        await _subscriptions_db_error_reply(update)
        return
    await message.reply_text(
        f"Подписка на итоги игр команды сохранена (аббревиатура {args[0].strip().upper()}). "
        "Отписка: /unsubscribe_team с тем же кодом. Рассылка по календарному «вчера» при ENABLE_PUSH_DIGEST=1."
    )


async def cmd_unsubscribe_team(update: Update, context: CallbackContext) -> None:
    """`/unsubscribe_team <ABBREV>`: отключает подписку чата на итоги игр команды."""
    message = _message(update)
    args = context.args or []
    if not args:
        await message.reply_text(
            "Укажите ту же аббревиатуру, например: /unsubscribe_team WSH"
        )
        return
    tid = subscription_repo.resolve_team_id_by_abbrev(args[0])
    if tid is None:
        await message.reply_text(
            f"Команда «{args[0]}» не найдена для сезона в базе."
        )
        return
    try:
        subscription_repo.deactivate_team_scores(_chat_id(update), tid)
    except psycopg2.Error:
        logger.exception("unsubscribe_team DB error")
        await _subscriptions_db_error_reply(update)
        return
    await message.reply_text("Подписка на эту команду отключена.")


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
        CommandHandler("day_games", cmd_day_games),
        CommandHandler("today", cmd_today),
        CommandHandler("tonight", cmd_tonight),
        CommandHandler("table", cmd_table),
        CommandHandler("standings", cmd_standings),
        CommandHandler("team", cmd_team),
        CommandHandler("leaders", cmd_leaders),
        CommandHandler("game", cmd_game),
        CommandHandler("advanced", cmd_advanced),
        CommandHandler("subscribe_digest", cmd_subscribe_digest),
        CommandHandler("unsubscribe_digest", cmd_unsubscribe_digest),
        CommandHandler("subscribe_team", cmd_subscribe_team),
        CommandHandler("unsubscribe_team", cmd_unsubscribe_team),
        CallbackQueryHandler(callback_leaders_pick, pattern=LEADERS_PICK_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_leaderboard_page, pattern=LEADERBOARD_PAGE_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_expand_digest_game, pattern=r"^dg:\d+$"),
        CallbackQueryHandler(callback_tonight_game, pattern=TONIGHT_GAME_CALLBACK_PATTERN),
        CallbackQueryHandler(callback_standalone_sa, pattern=STANDALONE_SA_CALLBACK_PATTERN),
        CallbackQueryHandler(
            callback_standalone_adv,
            pattern=r"^adv:(sat|usat|gf|oz|so|wrist|slap|snap|back|tip|defl|wrap|close)$",
        ),
        CallbackQueryHandler(handle_goal_video, pattern="^gv:"),
    ]


def build_application(token: str) -> Application:
    """Собирает `Application` 21.x со всеми хендлерами бота.

    Зачем: единственная точка сборки, которую тест может построить с фиктивным
    токеном и проверить состав и порядок хендлеров, не поднимая polling.
    `token` — токен бота (в проде `config.TOKEN`).
    """
    application = Application.builder().token(token).build()
    for handler in build_standalone_handlers():
        application.add_handler(handler, group=STANDALONE_GROUP)
    application.add_handler(build_conversation_handler())
    application.add_handler(CommandHandler('cancel', cmd_cancel_outside_conversation))
    return application


def main() -> None:
    """Точка входа `python bot.py`: long polling до сигнала остановки."""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
    )
    logger.info("Starting bot")
    build_application(config.TOKEN).run_polling()


if __name__ == '__main__':
    main()
