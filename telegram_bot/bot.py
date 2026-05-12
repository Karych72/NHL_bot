import logging
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    Updater,
)

import config
from bot_messages import day_digest, leaders_menu_intro, team_table, truncate_telegram_text
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
    END_CONVERSATION,
    FIRST,
    GOALIE_PERCENTAGE,
    GOALIE_SHOOTOUTS,
    GOALIE_WINS,
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
)
from script_bot import (
    bot_player_advanced_menu,
    bot_player_field,
    bot_player_goalie,
    bot_player_stats,
    bot_team_stats,
    end,
    stats,
    stats_over,
)
from stats_handlers import (
    LEADERS_PICK_CALLBACK_PATTERN,
    LEADERBOARD_PAGE_CALLBACK_PATTERN,
    STAT_PAGE_CALLBACK_PATTERN,
    STANDALONE_SA_CALLBACK_PATTERN,
    TEAM_PAGE_CALLBACK_PATTERN,
    TONIGHT_GAME_CALLBACK_PATTERN,
    advanced_standalone_keyboard,
    bot_day_digest,
    bot_goalie_percentage,
    bot_goalie_shootouts,
    bot_goalie_wins,
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


def cmd_start(update, context):
    args = context.args or []
    if args and args[0].startswith("game_"):
        try:
            gid = int(args[0].split("_", 1)[1])
        except (IndexError, ValueError):
            update.message.reply_text(START_MESSAGE, parse_mode="MARKDOWN")
            return
        send_game_card_message(context, update.message.chat_id, gid)
        return
    update.message.reply_text(START_MESSAGE, parse_mode="MARKDOWN")


def cmd_help(update, context):
    update.message.reply_text(HELP_MESSAGE, parse_mode="MARKDOWN")


def cmd_day_games(update, context):
    day_label, games = day_digest()
    dispatch_day_digest_messages(
        context,
        update.message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=False,
    )


def cmd_tonight(update, context):
    try:
        payload = fetch_score_now()
    except ScoreboardFetchError:
        logger.exception("NHL score/now request failed")
        update.message.reply_text(
            "Сейчас не удалось загрузить расписание NHL. Попробуйте чуть позже."
        )
        return
    games = slate_games_sorted(payload)
    markup = build_tonight_match_keyboard(games) if games else None
    text = truncate_telegram_text(
        tonight_reply_intro(payload),
        footer_note="\n\n(Текст обрезан — лимит Telegram.)",
    )
    reply_kwargs = {"parse_mode": "MARKDOWN"}
    if markup is not None:
        reply_kwargs["reply_markup"] = markup
    update.message.reply_text(text, **reply_kwargs)


def cmd_table(update, context):
    update.message.reply_text(team_table(), parse_mode="MARKDOWN")


def cmd_leaders(update, context):
    update.message.reply_text(
        leaders_menu_intro(),
        parse_mode="MARKDOWN",
        reply_markup=leaders_category_keyboard(),
    )


def cmd_game(update, context):
    if not context.args:
        update.message.reply_text(
            "Карточку матча проще открыть так: отправьте `/day_games` и нажмите кнопку нужной игры под сводкой.",
            parse_mode="Markdown",
        )
        return
    try:
        game_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("После `/game` укажите одно целое число.")
        return
    send_game_card_message(context, update.message.chat_id, game_id)


def cmd_advanced(update, context):
    update.message.reply_text(
        ADVANCED_COMMAND_INTRO,
        parse_mode="Markdown",
        reply_markup=advanced_standalone_keyboard(),
    )


def cmd_cancel_in_conversation(update, context: CallbackContext) -> int:
    update.message.reply_text("Вы вышли из меню. Снова: /stats")
    return ConversationHandler.END


def cmd_cancel_outside_conversation(update, context):
    update.message.reply_text("Меню /stats сейчас не открыто. Справка: /help")


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if __name__ == '__main__':
    updater = Updater(token=config.TOKEN)
    dispatcher = updater.dispatcher  # type: ignore[has-type]

    logger.info("Starting bot")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('stats', stats)],
        states={
            FIRST: [
                CallbackQueryHandler(
                    callback_stats_player_page, pattern=STAT_PAGE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(
                    callback_stats_team_page, pattern=TEAM_PAGE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(bot_team_stats, pattern='^' + str(TEAM_STATS) + '$'),
                CallbackQueryHandler(bot_player_stats, pattern='^' + str(PLAYER_STATS) + '$'),
                CallbackQueryHandler(bot_day_digest, pattern='^' + str(DAY_DIGEST) + '$'),

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
                CallbackQueryHandler(stats_over, pattern='^' + str(CHOOSE_STATS) + '$'),
                CallbackQueryHandler(end, pattern='^' + str(END_CONVERSATION) + '$'),
            ],
        },
        fallbacks=[
            CommandHandler('stats', stats),
            CommandHandler('cancel', cmd_cancel_in_conversation),
        ],
    )

    standalone = [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("day_games", cmd_day_games),
        CommandHandler("tonight", cmd_tonight),
        CommandHandler("table", cmd_table),
        CommandHandler("leaders", cmd_leaders),
        CommandHandler("game", cmd_game),
        CommandHandler("advanced", cmd_advanced),
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
    for h in standalone:
        dispatcher.add_handler(h, group=STANDALONE_GROUP)

    dispatcher.add_handler(conv_handler)
    dispatcher.add_handler(CommandHandler('cancel', cmd_cancel_outside_conversation))

    updater.start_polling()
    updater.idle()
