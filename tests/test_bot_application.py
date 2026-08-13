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
        _cq(f"^{sh.DIGEST_BACK_FROM_DATE_CALLBACK}$", sb.bot_digest_date_menu),
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
        _cq(_state(ds.PLAYER_FIELD), sb.bot_player_field),
        _cq(_state(ds.PLAYER_GOALIE), sb.bot_player_goalie),
        _cq(_state(ds.PLAYER_ADVANCED_SUBMENU), sb.bot_player_advanced_menu),
        _cq(_state(ds.TEAM_STATS), sb.bot_team_stats),
        _cq(_state(ds.DAY_DIGEST), sb.bot_digest_date_menu),
        _cq(f"^{sh.DIGEST_BACK_FROM_DATE_CALLBACK}$", sb.bot_digest_date_menu),
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
# Запись id сообщения меню — источник данных для /cancel ниже. stats()/
# stats_over() пишут его инлайн (не через stats_handlers._record_menu_message),
# поэтому не покрыты никаким другим тестом в сьюте: если строка записи
# потеряется при рефакторинге, /cancel в основном сценарии молча перестанет
# снимать клавиатуру, а make ci-local останется зелёным без этих тестов.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_entry_point_records_new_message_id(
    bot_module, make_message_update, fake_context
):
    script_bot = bot_module("script_bot")
    dialog_states = bot_module("dialog_states")
    update = make_message_update("/stats", chat_id=555)

    result = await script_bot.stats(update, fake_context)

    assert result == dialog_states.FIRST
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


@pytest.mark.asyncio
async def test_stats_over_records_new_message_id(
    bot_module, make_callback_update, fake_context
):
    script_bot = bot_module("script_bot")
    dialog_states = bot_module("dialog_states")
    update = make_callback_update(str(dialog_states.CHOOSE_STATS), chat_id=555)

    result = await script_bot.stats_over(update, fake_context)

    assert result == dialog_states.FIRST
    sent = fake_context.bot.sent_messages[0]
    assert sent["chat_id"] == 555
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


# ---------------------------------------------------------------------------
# /cancel внутри диалога — снятие «зомби»-клавиатуры: сообщение меню
# остаётся в чате с виду живым после ConversationHandler.END, хотя ни один
# хендлер его callback_data больше не матчит.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_in_conversation_clears_recorded_menu_keyboard(
    bot_module, make_message_update, fake_context
):
    bot = bot_module("bot")
    dialog_states = bot_module("dialog_states")
    update = make_message_update("/cancel", chat_id=555)
    fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] = 42

    result = await bot.cmd_cancel_in_conversation(update, fake_context)

    assert result == ConversationHandler.END
    [call] = fake_context.bot.edited_markups
    assert call == {"chat_id": 555, "message_id": 42, "reply_markup": None}
    # Ключ снят — второй /cancel подряд не редактирует уже неактуальное сообщение.
    assert dialog_states.LAST_MENU_MESSAGE_ID_KEY not in fake_context.user_data
    assert update.message.replies[0]["text"] == "Вы вышли из меню. Снова: /stats"


@pytest.mark.asyncio
async def test_cancel_in_conversation_without_recorded_id_skips_keyboard_edit(
    bot_module, make_message_update, fake_context
):
    """`/cancel` без открытого кнопкой меню — штатный случай (CLAUDE.md,
    Global Constraint 4): отсутствие записи не ошибка, а не try/except."""
    bot = bot_module("bot")
    update = make_message_update("/cancel", chat_id=555)

    result = await bot.cmd_cancel_in_conversation(update, fake_context)

    assert result == ConversationHandler.END
    assert fake_context.bot.edited_markups == []


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

# config.validate_env() смотрит на сырое окружение (os.environ), а не на уже
# задефолченные config.PG_*, поэтому тесты на main() выставляют все пять
# обязательных переменных явно — не полагаясь на то, чем их снабдил make.
_REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    "PG_HOST": "localhost",
    "PG_PORT": "5432",
    "PG_USER": "nhl_bot_test",
    "PG_DATABASE": "nhl_bot_test",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Выставляет все переменные, обязательные для `config.validate_env()`.

    `overrides` точечно подменяет значения из `_REQUIRED_ENV` (например, на
    пустую строку) — для тестов конкретной недостающей переменной.
    """
    for name, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_main_starts_polling_on_application_built_from_config_token(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = bot_module("bot")
    config = bot_module("config")
    _set_required_env(monkeypatch)
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)

    polled: List[Application] = []
    monkeypatch.setattr(
        Application, "run_polling", lambda self, *args, **kwargs: polled.append(self)
    )

    bot.main()

    assert len(polled) == 1
    assert polled[0].bot.token == FAKE_TOKEN
    assert sorted(polled[0].handlers) == [bot.STANDALONE_GROUP, 0]


def test_main_raises_and_never_polls_when_token_missing(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bot.main()` падает до сборки `Application`, если токена нет."""
    bot = bot_module("bot")
    _set_required_env(monkeypatch, TELEGRAM_BOT_TOKEN="")

    polled: List[Application] = []
    monkeypatch.setattr(
        Application, "run_polling", lambda self, *args, **kwargs: polled.append(self)
    )

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.main()

    assert polled == []


# ---------------------------------------------------------------------------
# config.validate_env()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing_var", ["TELEGRAM_BOT_TOKEN", "PG_HOST", "PG_PORT", "PG_USER", "PG_DATABASE"]
)
def test_validate_env_raises_on_missing_variable(
    bot_module, monkeypatch: pytest.MonkeyPatch, missing_var: str
) -> None:
    """Отсутствующая (не заданная вовсе) переменная — падение с её именем."""
    config = bot_module("config")
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(RuntimeError, match=missing_var):
        config.validate_env()


@pytest.mark.parametrize("blank_value", ["", "   "])
def test_validate_env_raises_on_blank_variable(
    bot_module, monkeypatch: pytest.MonkeyPatch, blank_value: str
) -> None:
    """Пустая строка или строка из пробелов считается отсутствием переменной."""
    config = bot_module("config")
    _set_required_env(monkeypatch, PG_DATABASE=blank_value)

    with pytest.raises(RuntimeError, match="PG_DATABASE"):
        config.validate_env()


def test_validate_env_error_message_does_not_leak_other_values(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сообщение об ошибке называет переменную, но не значения остальных."""
    config = bot_module("config")
    secret_token = "123456789:SUPER-SECRET-VALUE"
    _set_required_env(monkeypatch, TELEGRAM_BOT_TOKEN=secret_token, PG_HOST="")

    with pytest.raises(RuntimeError) as exc_info:
        config.validate_env()

    assert secret_token not in str(exc_info.value)


def test_validate_env_passes_when_all_required_variables_are_set(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bot_module("config")
    _set_required_env(monkeypatch)

    config.validate_env()  # не должно бросить исключение


# ---------------------------------------------------------------------------
# config._env_int / config._env_float на кривом значении
# ---------------------------------------------------------------------------

def test_env_int_raises_on_garbage_value_without_leaking_it(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bot_module("config")
    monkeypatch.setenv("SEASON_ID", "not-a-number")

    with pytest.raises(ValueError) as exc_info:
        config._env_int("SEASON_ID", 20252026)

    assert "SEASON_ID" in str(exc_info.value)
    assert "not-a-number" not in str(exc_info.value)


def test_env_float_raises_on_garbage_value_without_leaking_it(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bot_module("config")
    monkeypatch.setenv("PUSH_SEND_INTERVAL_SEC", "not-a-number")

    with pytest.raises(ValueError) as exc_info:
        config._env_float("PUSH_SEND_INTERVAL_SEC", 0.05)

    assert "PUSH_SEND_INTERVAL_SEC" in str(exc_info.value)
    assert "not-a-number" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Асинхронность колбэков каркаса
# ---------------------------------------------------------------------------

def test_every_registered_callback_is_a_coroutine_function(bot_module, application) -> None:
    """Все зарегистрированные колбэки обязаны быть корутинами.

    PTB 21 делает ``await callback(...)``, поэтому синхронный колбэк падает
    в рантайме. Исключений больше нет: перевод ``bot.py``/``script_bot.py``
    (Задача 3a) и ``stats_handlers.py`` (Задача 3b) завершён.
    """
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
        assert asyncio.iscoroutinefunction(callback), f"{callback.__qualname__} is not async"

    # Страховка от «проверили пустой список»: 17 регистраций колбэков bot.py
    # (15 standalone-команд, cmd_cancel_outside_conversation в группе 0,
    # cmd_cancel_in_conversation в fallbacks), 22 регистрации script_bot.py
    # (12 функций — stats/stats_root_edit по 2 раза; в SECOND дополнительно
    # висят «« Назад»» на родительские подменю: bot_player_field/
    # bot_player_goalie/bot_player_advanced_menu/bot_team_stats по 2 раза
    # каждый, bot_digest_date_menu 5 раз — FIRST×2, SECOND×2, THIRD×1)
    # и 42 регистрации stats_handlers.py.
    assert len(registered) == 81
    assert {h.callback.__module__ for h in registered} == {
        "bot",
        "script_bot",
        "stats_handlers",
    }
