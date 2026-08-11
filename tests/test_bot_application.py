"""Каркас бота на python-telegram-bot 21.x: сборка ``Application``.

Проверяет ровно то, что нельзя проверить, просто импортировав ``bot.py``:
какие хендлеры зарегистрированы, в каких группах, в каком порядке и на какие
функции они указывают, плюс что точка входа поднимает polling на токене из
``config``. Сеть не используется: ``Application`` строится с фиктивным токеном,
``run_polling`` подменяется.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, List, Sequence, Tuple

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Синтаксически валидный, но заведомо несуществующий токен: никуда не ходим.
FAKE_TOKEN = "123456789:TEST-TOKEN-NOT-A-REAL-SECRET"


def _describe(handler: Any) -> Tuple[Any, ...]:
    """Различающая подпись хендлера: тип, команда/regex и сам callback.

    Callback сравнивается объектом, а не именем: часть обработчиков
    статистики создаётся фабриками и у всех у них ``__name__ == "handler"``.
    """
    if isinstance(handler, CommandHandler):
        return ("command", sorted(handler.commands), handler.callback)
    if isinstance(handler, CallbackQueryHandler):
        assert handler.pattern is not None
        return ("callback_query", handler.pattern.pattern, handler.callback)
    if isinstance(handler, MessageHandler):
        return ("message", handler.callback)
    raise AssertionError(f"unexpected handler type: {type(handler).__name__}")


def _describe_all(handlers: Sequence[Any]) -> List[Tuple[Any, ...]]:
    return [_describe(h) for h in handlers]


def _cq(pattern: str, callback: Callable) -> Tuple[Any, ...]:
    return ("callback_query", pattern, callback)


def _state(value: int) -> str:
    """Regex, которым bot.py матчит callback_data состояния меню."""
    return f"^{value}$"


@pytest.fixture
def application(bot_module) -> Application:
    """``Application`` со всеми хендлерами, собранный на фиктивном токене."""
    bot = bot_module("bot")
    return bot.build_application(FAKE_TOKEN)


# ---------------------------------------------------------------------------
# Группы и порядок
# ---------------------------------------------------------------------------

def test_application_uses_exactly_two_handler_groups(bot_module, application) -> None:
    bot = bot_module("bot")
    assert sorted(application.handlers) == [bot.STANDALONE_GROUP, 0]
    # Группы просматриваются по возрастанию: standalone обязан идти раньше
    # диалога, иначе открытое меню /stats съедало бы команды верхнего уровня.
    assert bot.STANDALONE_GROUP < 0


def test_default_group_is_conversation_then_cancel(bot_module, application) -> None:
    bot = bot_module("bot")
    conversation, cancel = application.handlers[0]
    assert isinstance(conversation, ConversationHandler)
    assert _describe(cancel) == ("command", ["cancel"], bot.cmd_cancel_outside_conversation)


def test_build_application_binds_the_given_token(application) -> None:
    assert application.bot.token == FAKE_TOKEN


# ---------------------------------------------------------------------------
# Standalone-группа
# ---------------------------------------------------------------------------

def test_standalone_handlers_and_their_order(bot_module, application) -> None:
    bot = bot_module("bot")
    stats_handlers = bot_module("stats_handlers")

    assert _describe_all(application.handlers[bot.STANDALONE_GROUP]) == [
        ("command", ["start"], bot.cmd_start),
        ("command", ["help"], bot.cmd_help),
        ("command", ["day_games"], bot.cmd_day_games),
        ("command", ["today"], bot.cmd_today),
        ("command", ["tonight"], bot.cmd_tonight),
        ("command", ["table"], bot.cmd_table),
        ("command", ["standings"], bot.cmd_standings),
        ("command", ["team"], bot.cmd_team),
        ("command", ["leaders"], bot.cmd_leaders),
        ("command", ["game"], bot.cmd_game),
        ("command", ["advanced"], bot.cmd_advanced),
        ("command", ["subscribe_digest"], bot.cmd_subscribe_digest),
        ("command", ["unsubscribe_digest"], bot.cmd_unsubscribe_digest),
        ("command", ["subscribe_team"], bot.cmd_subscribe_team),
        ("command", ["unsubscribe_team"], bot.cmd_unsubscribe_team),
        _cq(stats_handlers.LEADERS_PICK_CALLBACK_PATTERN, stats_handlers.callback_leaders_pick),
        _cq(stats_handlers.LEADERBOARD_PAGE_CALLBACK_PATTERN, stats_handlers.callback_leaderboard_page),
        _cq(r"^dg:\d+$", stats_handlers.callback_expand_digest_game),
        _cq(stats_handlers.TONIGHT_GAME_CALLBACK_PATTERN, stats_handlers.callback_tonight_game),
        _cq(stats_handlers.STANDALONE_SA_CALLBACK_PATTERN, stats_handlers.callback_standalone_sa),
        _cq(
            r"^adv:(sat|usat|gf|oz|so|wrist|slap|snap|back|tip|defl|wrap|close)$",
            stats_handlers.callback_standalone_adv,
        ),
        _cq("^gv:", stats_handlers.handle_goal_video),
    ]


# ---------------------------------------------------------------------------
# ConversationHandler меню /stats
# ---------------------------------------------------------------------------

def test_conversation_entry_points_and_fallbacks(bot_module, application) -> None:
    bot = bot_module("bot")
    script_bot = bot_module("script_bot")
    conversation = application.handlers[0][0]

    assert _describe_all(conversation.entry_points) == [
        ("command", ["stats"], script_bot.stats),
    ]
    assert _describe_all(conversation.fallbacks) == [
        ("command", ["stats"], script_bot.stats),
        ("command", ["cancel"], bot.cmd_cancel_in_conversation),
    ]


def test_conversation_states_are_exactly_first_second_third(bot_module, application) -> None:
    dialog_states = bot_module("dialog_states")
    conversation = application.handlers[0][0]
    assert sorted(conversation.states) == sorted(
        [dialog_states.FIRST, dialog_states.SECOND, dialog_states.THIRD]
    )


def test_state_first_handlers_and_their_order(bot_module, application) -> None:
    ds = bot_module("dialog_states")
    sb = bot_module("script_bot")
    sh = bot_module("stats_handlers")
    conversation = application.handlers[0][0]

    assert _describe_all(conversation.states[ds.FIRST]) == [
        _cq(_state(ds.CHOOSE_STATS), sb.stats_root_edit),
        _cq(_state(ds.DAY_DIGEST), sb.bot_digest_date_menu),
        _cq(_state(ds.LEAGUE_STANDINGS), sh.bot_league_standings),
        _cq(_state(ds.DIGEST_CALENDAR_TODAY), sh.bot_digest_calendar_today),
        _cq(_state(ds.DIGEST_CALENDAR_YESTERDAY), sh.bot_digest_calendar_yesterday),
        _cq(_state(ds.DIGEST_PICK_DATE), sh.bot_digest_pick_date_prompt),
        _cq(f"^{sb.NAV_PLAYERS}$", sb.nav_back_to_players),
        _cq(f"^{sb.NAV_FIELD}$", sb.nav_back_to_field),
        _cq(sh.STAT_PAGE_CALLBACK_PATTERN, sh.callback_stats_player_page),
        _cq(sh.TEAM_PAGE_CALLBACK_PATTERN, sh.callback_stats_team_page),
        _cq(_state(ds.TEAM_STATS), sb.bot_team_stats),
        _cq(_state(ds.PLAYER_STATS), sb.bot_player_stats),
        _cq(_state(ds.PLAYER_FIELD), sb.bot_player_field),
        _cq(_state(ds.PLAYER_GOALIE), sb.bot_player_goalie),
        _cq(_state(ds.PLAYER_ADVANCED_SUBMENU), sb.bot_player_advanced_menu),
        _cq(_state(ds.PLAYER_POINTS), sh.bot_player_points),
        _cq(_state(ds.PLAYER_GOALS), sh.bot_player_goals),
        _cq(_state(ds.PLAYER_ASSISTS), sh.bot_player_assists),
        _cq(_state(ds.PLAYER_PLUS_MINUS), sh.bot_player_plus_minus),
        _cq(_state(ds.PLAYER_PENALTIES), sh.bot_player_penalties),
        _cq(_state(ds.PLAYER_HITS), sh.bot_player_hits),
        _cq(_state(ds.PLAYER_BLOCKS), sh.bot_player_blocks),
        _cq(_state(ds.PLAYER_ICE_TIME), sh.bot_player_ice_time),
        _cq(_state(ds.PLAYER_SAT_PCT), sh.bot_player_sat_pct),
        _cq(_state(ds.PLAYER_USAT_PCT), sh.bot_player_usat_pct),
        _cq(_state(ds.PLAYER_GOALS_FOR_PCT), sh.bot_player_goals_for_pct),
        _cq(_state(ds.PLAYER_OZ_START_PCT), sh.bot_player_oz_start_pct),
        _cq(_state(ds.PLAYER_SHOOTOUT_PCT), sh.bot_player_shootout_pct),
        _cq(_state(ds.PLAYER_SHOT_WRIST), sh.bot_player_shot_wrist),
        _cq(_state(ds.PLAYER_SHOT_SLAP), sh.bot_player_shot_slap),
        _cq(_state(ds.PLAYER_SHOT_SNAP), sh.bot_player_shot_snap),
        _cq(_state(ds.PLAYER_SHOT_BACKHAND), sh.bot_player_shot_backhand),
        _cq(_state(ds.PLAYER_SHOT_TIP_IN), sh.bot_player_shot_tip_in),
        _cq(_state(ds.PLAYER_SHOT_DEFLECTED), sh.bot_player_shot_deflected),
        _cq(_state(ds.PLAYER_SHOT_WRAP_AROUND), sh.bot_player_shot_wrap),
        _cq(_state(ds.GOALIE_WINS), sh.bot_goalie_wins),
        _cq(_state(ds.GOALIE_PERCENTAGE), sh.bot_goalie_percentage),
        _cq(_state(ds.GOALIE_SHOOTOUTS), sh.bot_goalie_shootouts),
        _cq(_state(ds.TEAM_PROCENT_WINS), sh.bot_team_procent_wins),
        _cq(_state(ds.TEAM_POWER_PLAY), sh.bot_team_power_play),
        _cq(_state(ds.TEAM_POWER_KILL), sh.bot_team_power_kill),
    ]


def test_state_second_handlers_and_their_order(bot_module, application) -> None:
    ds = bot_module("dialog_states")
    sb = bot_module("script_bot")
    sh = bot_module("stats_handlers")
    conversation = application.handlers[0][0]

    assert _describe_all(conversation.states[ds.SECOND]) == [
        _cq(sh.STAT_PAGE_CALLBACK_PATTERN, sh.callback_stats_player_page),
        _cq(sh.TEAM_PAGE_CALLBACK_PATTERN, sh.callback_stats_team_page),
        _cq(_state(ds.CHOOSE_STATS), sb.stats_over),
        _cq(_state(ds.END_CONVERSATION), sb.end),
    ]


def test_state_third_handlers_and_their_order(bot_module, application) -> None:
    ds = bot_module("dialog_states")
    sb = bot_module("script_bot")
    sh = bot_module("stats_handlers")
    conversation = application.handlers[0][0]

    assert _describe_all(conversation.states[ds.THIRD]) == [
        _cq(_state(ds.CHOOSE_STATS), sb.stats_root_edit),
        _cq(f"^{sh.DIGEST_BACK_FROM_DATE_CALLBACK}$", sb.bot_digest_date_menu),
        ("message", sh.bot_digest_custom_date),
    ]


def _text_update(text: str, entities: Tuple[MessageEntity, ...] = ()) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chat=Chat(id=1, type=Chat.PRIVATE),
            from_user=User(id=2, first_name="T", is_bot=False),
            text=text,
            entities=entities,
        ),
    )


def test_state_third_message_handler_takes_text_but_not_commands(bot_module, application) -> None:
    """Ввод даты ловится, а `/cancel` внутри THIRD должен дойти до fallbacks."""
    ds = bot_module("dialog_states")
    conversation = application.handlers[0][0]
    date_input_handler = conversation.states[ds.THIRD][-1]
    assert isinstance(date_input_handler, MessageHandler)

    assert date_input_handler.filters.check_update(_text_update("2026-01-01"))
    assert not date_input_handler.filters.check_update(
        _text_update("/cancel", (MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=7),))
    )
    assert date_input_handler.filters is not filters.TEXT


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def test_main_starts_polling_on_application_built_from_config_token(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = bot_module("bot")
    config = bot_module("config")
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)

    polled: List[Application] = []
    monkeypatch.setattr(
        Application, "run_polling", lambda self, *args, **kwargs: polled.append(self)
    )

    bot.main()

    assert len(polled) == 1
    assert polled[0].bot.token == FAKE_TOKEN
    assert sorted(polled[0].handlers) == [bot.STANDALONE_GROUP, 0]


# ---------------------------------------------------------------------------
# Асинхронность колбэков каркаса
# ---------------------------------------------------------------------------

def test_bot_and_script_bot_callbacks_are_coroutine_functions(bot_module, application) -> None:
    """Всё, что перевела Задача 3a, обязано быть корутинами.

    PTB 21 делает ``await callback(...)``, поэтому синхронный колбэк падает
    в рантайме. Колбэки из ``stats_handlers`` здесь сознательно не проверяются:
    их переводит Задача 3b.
    """
    migrated_modules = {"bot", "script_bot"}
    checked = []
    conversation = application.handlers[0][0]
    registered = [
        *application.handlers[bot_module("bot").STANDALONE_GROUP],
        *application.handlers[0][1:],
        *conversation.entry_points,
        *conversation.fallbacks,
        *[h for state in conversation.states.values() for h in state],
    ]
    for handler in registered:
        callback = handler.callback
        if callback.__module__ not in migrated_modules:
            continue
        checked.append(callback)
        assert asyncio.iscoroutinefunction(callback), f"{callback.__qualname__} is not async"

    # Страховка от «проверили пустой список»: 17 регистраций колбэков bot.py
    # (15 standalone-команд, cmd_cancel_outside_conversation в группе 0,
    # cmd_cancel_in_conversation в fallbacks) и 15 регистраций script_bot.py
    # (12 функций, из них stats, stats_root_edit и bot_digest_date_menu
    # зарегистрированы дважды).
    assert len(checked) == 32
