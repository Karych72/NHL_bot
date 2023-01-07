import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
)
from bot_messages import player_stats, team_stats
from player_stats_functions import *
from goalie_stats_functions import *
from team_stats_functions import *
from day_digest import *

import config

from script_bot import *

if __name__ == '__main__':
    updater = Updater(token=config.TOKEN)
    dispatcher = updater.dispatcher

    print('In Bot', DAY_DIGEST, PLAYER_STATS, TEAM_STATS)
    # Настройка обработчика разговоров с состояниями `FIRST` и `SECOND`
    # Используем параметр `pattern` для передачи `CallbackQueries` с
    # определенным шаблоном данных соответствующим обработчикам
    # ^ - означает "начало строки"
    # $ - означает "конец строки"
    # Таким образом, паттерн `^ABC$` будет ловить только 'ABC'
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('stats', stats)],
        states={  # словарь состояний разговора, возвращаемых callback функциями
            FIRST: [
                CallbackQueryHandler(bot_team_stats, pattern='^' + str(TEAM_STATS) + '$'),
                CallbackQueryHandler(bot_player_stats, pattern='^' + str(PLAYER_STATS) + '$'),
                CallbackQueryHandler(bot_day_digest, pattern='^' + str(DAY_DIGEST) + '$'),

                CallbackQueryHandler(bot_player_field, pattern='^' + str(PLAYER_FIELD) + '$'),
                CallbackQueryHandler(bot_player_goalie, pattern='^' + str(PLAYER_GOALIE) + '$'),

                CallbackQueryHandler(bot_player_points, pattern='^' + str(PLAYER_POINTS) + '$'),
                CallbackQueryHandler(bot_player_goals, pattern='^' + str(PLAYER_GOALS) + '$'),
                CallbackQueryHandler(bot_player_assists, pattern='^' + str(PLAYER_ASSISTS) + '$'),
                CallbackQueryHandler(bot_player_plus_minus, pattern='^' + str(PLAYER_PLUS_MINUS) + '$'),
                CallbackQueryHandler(bot_player_penalties, pattern='^' + str(PLAYER_PENALTIES) + '$'),
                CallbackQueryHandler(bot_player_hits, pattern='^' + str(PLAYER_HITS) + '$'),
                CallbackQueryHandler(bot_player_blocks, pattern='^' + str(PLAYER_BLOCKS) + '$'),
                CallbackQueryHandler(bot_player_ice_time, pattern='^' + str(PLAYER_ICE_TIME) + '$'),

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
        fallbacks=[CommandHandler('stats', stats)],
    )

    # Добавляем `ConversationHandler` в диспетчер, который
    # будет использоваться для обработки обновлений
    dispatcher.add_handler(conv_handler)

    updater.start_polling()
    updater.idle()
