import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    Updater,
)

import config
from bot_messages import day_digest, leaders_compact, team_table
from help_text import HELP_MESSAGE, START_MESSAGE
from dialog_states import (
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
    PLAYER_SHOT_TYPES_MENU,
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
    bot_player_shot_menu,
    bot_player_stats,
    bot_team_stats,
    end,
    stats,
    stats_over,
)
from stats_handlers import (
    LEADERS_TOP10_CALLBACK,
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
    callback_leaders_top10,
    callback_standalone_adv,
    callback_standalone_shot,
    dispatch_day_digest_messages,
    handle_goal_video,
    send_game_card_message,
    shottypes_standalone_keyboard,
)

STANDALONE_GROUP = -1


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


def cmd_today(update, context):
    day_label, games = day_digest()
    dispatch_day_digest_messages(
        context,
        update.message.chat_id,
        day_label,
        games,
        attach_conv_nav_on_last=False,
    )


def cmd_table(update, context):
    update.message.reply_text(team_table(), parse_mode="MARKDOWN")


def cmd_leaders(update, context):
    text = leaders_compact()
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Топ-10 (очки и голы)", callback_data=LEADERS_TOP10_CALLBACK)]]
    )
    update.message.reply_text(text, parse_mode="MARKDOWN", reply_markup=markup)


def cmd_game(update, context):
    if not context.args:
        update.message.reply_text(
            "Укажите номер матча (NHL game_id), например: `/game 2025020567`\n"
            "Deep-link: откройте бота по ссылке `https://t.me/<ваш_бот>?start=game_2025020567`",
            parse_mode="Markdown",
        )
        return
    try:
        game_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("Номер матча должен быть целым числом.")
        return
    send_game_card_message(context, update.message.chat_id, game_id)


def cmd_advanced(update, context):
    update.message.reply_text(
        "Расширенная статистика сезона:",
        reply_markup=advanced_standalone_keyboard(),
    )


def cmd_shottypes(update, context):
    update.message.reply_text(
        "Голы по типам броска (сезон):",
        reply_markup=shottypes_standalone_keyboard(),
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
    dispatcher = updater.dispatcher

    logger.info("Starting bot")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('stats', stats)],
        states={
            FIRST: [
                CallbackQueryHandler(bot_team_stats, pattern='^' + str(TEAM_STATS) + '$'),
                CallbackQueryHandler(bot_player_stats, pattern='^' + str(PLAYER_STATS) + '$'),
                CallbackQueryHandler(bot_day_digest, pattern='^' + str(DAY_DIGEST) + '$'),

                CallbackQueryHandler(bot_player_field, pattern='^' + str(PLAYER_FIELD) + '$'),
                CallbackQueryHandler(bot_player_goalie, pattern='^' + str(PLAYER_GOALIE) + '$'),
                CallbackQueryHandler(bot_player_advanced_menu, pattern='^' + str(PLAYER_ADVANCED_SUBMENU) + '$'),
                CallbackQueryHandler(bot_player_shot_menu, pattern='^' + str(PLAYER_SHOT_TYPES_MENU) + '$'),

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
        CommandHandler("today", cmd_today),
        CommandHandler("day", cmd_today),
        CommandHandler("table", cmd_table),
        CommandHandler("standings", cmd_table),
        CommandHandler("leaders", cmd_leaders),
        CommandHandler("game", cmd_game),
        CommandHandler("advanced", cmd_advanced),
        CommandHandler("shottypes", cmd_shottypes),
        CallbackQueryHandler(callback_leaders_top10, pattern=f"^{LEADERS_TOP10_CALLBACK}$"),
        CallbackQueryHandler(callback_expand_digest_game, pattern=r"^dg:\d+$"),
        CallbackQueryHandler(callback_standalone_adv, pattern=r"^adv:(sat|usat|gf|oz|so|close)$"),
        CallbackQueryHandler(callback_standalone_shot, pattern=r"^shot:(wrist|slap|snap|back|tip|defl|wrap|close)$"),
        CallbackQueryHandler(handle_goal_video, pattern="^gv:"),
    ]
    for h in standalone:
        dispatcher.add_handler(h, group=STANDALONE_GROUP)

    dispatcher.add_handler(conv_handler)
    dispatcher.add_handler(CommandHandler('cancel', cmd_cancel_outside_conversation))

    updater.start_polling()
    updater.idle()
