"""Каркас бота на aiogram 3: сборка `Dispatcher` и маршрутизация апдейтов.

Две части. Регистрация: какие хендлеры в каких роутерах и в каком порядке, карта
диалога `/stats` по состояниям, троттлинг и публикация меню команд. Маршрутизация:
настоящие апдейты Telegram прогоняются через `Dispatcher.feed_update` на боте с
записывающей сессией (`conftest.FakeSession`) — так проверяется не список
регистраций, а куда реально попадает команда, текст или нажатие при данном
состоянии диалога. Сеть не используется; где хендлер ходил бы в БД или в Claude API,
подменяется функция модуля, которую он зовёт.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, List, Tuple

import pytest
from aiogram import Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.methods import SetMyCommands

from tests.conftest import CHAT_ID, FAKE_TOKEN, USER_ID


def _router(dispatcher: Dispatcher, name: str) -> Any:
    [router] = [r for r in dispatcher.sub_routers if r.name == name]
    return router


def _callback_handlers(router: Any) -> List[Tuple[str, Callable]]:
    """(regex callback_data, хендлер) нажатий роутера в порядке регистрации."""
    return [
        (_regexp_of(handler), handler.callback)
        for handler in router.callback_query.handlers
    ]


def _regexp_of(handler: Any) -> str:
    """Regex фильтра `F.data.regexp(...)` хендлера нажатия: aiogram хранит его как
    вызов `re.Pattern.match` над `callback.data`."""
    [match] = [op.function for op in handler.filters[-1].magic._operations if hasattr(op, "function")]
    return match.__self__.pattern


def _commands(router: Any) -> List[str]:
    """Команды, на которые отвечают хендлеры сообщений роутера (фильтры `Command`)."""
    return [
        command
        for handler in router.message.handlers
        for f in handler.filters or []
        if isinstance(f.callback, Command)
        for command in f.callback.commands
    ]


@pytest.fixture
def dispatcher(bot_module) -> Dispatcher:
    return bot_module("bot").build_dispatcher()


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------

def test_dispatcher_checks_standalone_then_dialog_then_tail(dispatcher) -> None:
    """Порядок роутеров — поведение: апдейт берёт первый подошедший хендлер, поэтому
    команды верхнего уровня работают и при открытом меню `/stats`, а свободный текст
    видит только то, что не нужно диалогу."""
    assert [r.name for r in dispatcher.sub_routers] == ["standalone", "stats", "tail"]


def test_throttle_is_the_only_outer_middleware_of_button_presses(bot_module, dispatcher) -> None:
    throttle = bot_module("throttle")
    assert list(dispatcher.callback_query.outer_middleware) == [throttle.enforce_callback_rate_limit]
    assert list(dispatcher.message.outer_middleware) == []


def test_standalone_commands_and_their_order(bot_module, dispatcher) -> None:
    bot = bot_module("bot")
    router = _router(dispatcher, "standalone")
    handlers = router.message.handlers

    assert [h.callback for h in handlers] == [
        bot.cmd_start, bot.cmd_help, bot.cmd_today, bot.cmd_tonight, bot.cmd_standings,
        bot.cmd_team, bot.cmd_team_profile, bot.cmd_leaders, bot.cmd_countries,
        bot.cmd_advanced, bot.cmd_subscriptions,
    ]
    assert isinstance(handlers[0].filters[0].callback, CommandStart)
    assert _commands(router) == [
        "start", "help", "today", "tonight", "standings", "team", "leaders", "countries", "advanced",
        "subscriptions",
    ]


def test_standalone_buttons_and_their_order(bot_module, dispatcher) -> None:
    bot = bot_module("bot")
    sh = bot_module("stats_handlers")

    assert _callback_handlers(_router(dispatcher, "standalone")) == [
        (sh.LEADERS_PICK_CALLBACK_PATTERN, sh.callback_leaders_pick),
        (sh.LEADERBOARD_PAGE_CALLBACK_PATTERN, sh.callback_leaderboard_page),
        (r"^dg:(\d+)$", sh.callback_expand_digest_game),
        (bot.SUBSCRIPTIONS_CALLBACK_PATTERN, bot.callback_subscriptions),
        (sh.TONIGHT_GAME_CALLBACK_PATTERN, sh.callback_tonight_game),
        (sh.COUNTRY_CALLBACK_PATTERN, sh.callback_country),
        (sh.STANDALONE_SA_CALLBACK_PATTERN, sh.callback_standalone_sa),
        (r"^adv:(sat|usat|gf|oz|so|wrist|slap|snap|back|tip|defl|wrap|close)$", sh.callback_standalone_adv),
        (r"^gv:(\d+):(\d+)$", sh.handle_goal_video),
    ]


def test_dialog_router_registers_every_button_of_the_dialog_map(bot_module, dispatcher) -> None:
    """Роутер диалога — ровно карта `STATS_DIALOG`: состояние за состоянием, в её порядке."""
    bot = bot_module("bot")
    expected = [(pattern, callback) for handlers in bot.STATS_DIALOG.values() for pattern, callback in handlers]
    assert _callback_handlers(_router(dispatcher, "stats")) == expected


def test_dialog_map_states_and_buttons(bot_module) -> None:
    ds = bot_module("dialog_states")
    sb = bot_module("script_bot")
    sh = bot_module("stats_handlers")
    bot = bot_module("bot")

    def state(value: int) -> str:
        return f"^{value}$"

    back_from_date = f"^{sh.DIGEST_BACK_FROM_DATE_CALLBACK}$"
    assert list(bot.STATS_DIALOG) == [ds.StatsMenu.first, ds.StatsMenu.second, ds.StatsMenu.third]
    assert bot.STATS_DIALOG[ds.StatsMenu.first] == [
        (state(ds.CHOOSE_STATS), sb.stats_root_edit),
        (state(ds.DAY_DIGEST), sb.bot_digest_date_menu),
        (state(ds.LEAGUE_STANDINGS), sh.bot_league_standings),
        (state(ds.DIGEST_CALENDAR_TODAY), sh.bot_digest_calendar_today),
        (state(ds.DIGEST_CALENDAR_YESTERDAY), sh.bot_digest_calendar_yesterday),
        (state(ds.DIGEST_PICK_DATE), sh.bot_digest_pick_date_prompt),
        (back_from_date, sb.bot_digest_date_menu),
        (f"^{sb.NAV_PLAYERS}$", sb.bot_player_stats),
        (f"^{sb.NAV_FIELD}$", sb.bot_player_field),
        (sh.STAT_PAGE_CALLBACK_PATTERN, sh.callback_stats_player_page),
        (sh.TEAM_PAGE_CALLBACK_PATTERN, sh.callback_stats_team_page),
        (state(ds.TEAM_STATS), sb.bot_team_stats),
        (state(ds.PLAYER_STATS), sb.bot_player_stats),
        (state(ds.PLAYER_FIELD), sb.bot_player_field),
        (state(ds.PLAYER_GOALIE), sb.bot_player_goalie),
        (state(ds.PLAYER_ADVANCED_SUBMENU), sb.bot_player_advanced_menu),
        (state(ds.PLAYER_POINTS), sh.bot_player_points),
        (state(ds.PLAYER_GOALS), sh.bot_player_goals),
        (state(ds.PLAYER_ASSISTS), sh.bot_player_assists),
        (state(ds.PLAYER_PLUS_MINUS), sh.bot_player_plus_minus),
        (state(ds.PLAYER_PENALTIES), sh.bot_player_penalties),
        (state(ds.PLAYER_HITS), sh.bot_player_hits),
        (state(ds.PLAYER_BLOCKS), sh.bot_player_blocks),
        (state(ds.PLAYER_ICE_TIME), sh.bot_player_ice_time),
        (state(ds.PLAYER_SAT_PCT), sh.bot_player_sat_pct),
        (state(ds.PLAYER_USAT_PCT), sh.bot_player_usat_pct),
        (state(ds.PLAYER_GOALS_FOR_PCT), sh.bot_player_goals_for_pct),
        (state(ds.PLAYER_OZ_START_PCT), sh.bot_player_oz_start_pct),
        (state(ds.PLAYER_SHOOTOUT_PCT), sh.bot_player_shootout_pct),
        (state(ds.PLAYER_SHOT_WRIST), sh.bot_player_shot_wrist),
        (state(ds.PLAYER_SHOT_SLAP), sh.bot_player_shot_slap),
        (state(ds.PLAYER_SHOT_SNAP), sh.bot_player_shot_snap),
        (state(ds.PLAYER_SHOT_BACKHAND), sh.bot_player_shot_backhand),
        (state(ds.PLAYER_SHOT_TIP_IN), sh.bot_player_shot_tip_in),
        (state(ds.PLAYER_SHOT_DEFLECTED), sh.bot_player_shot_deflected),
        (state(ds.PLAYER_SHOT_WRAP_AROUND), sh.bot_player_shot_wrap),
        (state(ds.GOALIE_WINS), sh.bot_goalie_wins),
        (state(ds.GOALIE_PERCENTAGE), sh.bot_goalie_percentage),
        (state(ds.GOALIE_SHOOTOUTS), sh.bot_goalie_shootouts),
        (state(ds.TEAM_PROCENT_WINS), sh.bot_team_procent_wins),
        (state(ds.TEAM_POWER_PLAY), sh.bot_team_power_play),
        (state(ds.TEAM_POWER_KILL), sh.bot_team_power_kill),
        (state(ds.TEAM_CONFERENCE_STATS), sh.bot_team_conference_stats),
        (state(ds.TEAM_DIVISION_STATS), sh.bot_team_division_stats),
        (state(ds.TEAM_PROFILE_PICK), sh.bot_team_profile_pick),
    ]
    assert bot.STATS_DIALOG[ds.StatsMenu.second] == [
        (sh.STAT_PAGE_CALLBACK_PATTERN, sh.callback_stats_player_page),
        (sh.TEAM_PAGE_CALLBACK_PATTERN, sh.callback_stats_team_page),
        (state(ds.PLAYER_FIELD), sb.bot_player_field),
        (state(ds.PLAYER_GOALIE), sb.bot_player_goalie),
        (state(ds.PLAYER_ADVANCED_SUBMENU), sb.bot_player_advanced_menu),
        (state(ds.TEAM_STATS), sb.bot_team_stats),
        (sh.TEAM_PROFILE_CALLBACK_PATTERN, sh.bot_team_profile_show),
        (state(ds.TEAM_PROFILE_PICK), sh.bot_team_profile_pick),
        (state(ds.DAY_DIGEST), sb.bot_digest_date_menu),
        (back_from_date, sb.bot_digest_date_menu),
        (state(ds.CHOOSE_STATS), sb.stats_over),
        (state(ds.END_CONVERSATION), sb.end),
    ]
    assert bot.STATS_DIALOG[ds.StatsMenu.third] == [
        (state(ds.CHOOSE_STATS), sb.stats_root_edit),
        (back_from_date, sb.bot_digest_date_menu),
    ]


def test_dialog_and_tail_message_handlers(bot_module, dispatcher) -> None:
    bot = bot_module("bot")
    sb = bot_module("script_bot")
    sh = bot_module("stats_handlers")
    stats_router, tail = _router(dispatcher, "stats"), _router(dispatcher, "tail")

    assert [h.callback for h in stats_router.message.handlers] == [
        sb.stats, bot.cmd_cancel_in_conversation, sh.bot_digest_custom_date,
    ]
    assert _commands(stats_router) == ["stats", "cancel"]
    assert [h.callback for h in tail.message.handlers] == [
        bot.cmd_cancel_outside_conversation, bot.on_free_text,
    ]
    assert _commands(tail) == ["cancel"]


@pytest.mark.parametrize(
    "text, abbrev, full",
    [("/BOS", "BOS", False), ("/bos", "bos", False), ("/WSH", "WSH", False),
     ("/BOS_FULL", "BOS", True), ("/bos_full", "bos", True)],
)
def test_team_command_pattern_extracts_three_letter_abbreviation(
    bot_module, text, abbrev, full
) -> None:
    """`/BOS` — профиль клуба, `/BOS_FULL` — все его игроки (группа 2)."""
    bot = bot_module("bot")
    match = re.match(bot.TEAM_COMMAND_PATTERN, text)
    assert match is not None and match.group(1) == abbrev
    assert bool(match.group(2)) is full


@pytest.mark.parametrize(
    "text", ["/team", "/today", "/help", "/stats", "/TO", "/BOSS", "BOS", "/BOS 1", "/B0S", "/BOS@OtherBot",
             "/BOS_FUL", "/BOS_FULL@OtherBot"]
)
def test_team_command_pattern_ignores_service_commands_and_malformed_input(bot_module, text) -> None:
    bot = bot_module("bot")
    assert re.match(bot.TEAM_COMMAND_PATTERN, text) is None


def test_every_bot_command_is_a_registered_command(bot_module, dispatcher) -> None:
    """Меню команд Telegram и /help строятся из `BOT_COMMANDS`: команда без
    хендлера показывалась бы пользователю и молчала."""
    help_text = bot_module("help_text")
    registered = {cmd for router in dispatcher.sub_routers for cmd in _commands(router)}
    menu = [cmd for cmd, _desc in help_text.BOT_COMMANDS]
    assert len(menu) == len(set(menu))
    # /start — точка входа, в меню команд не выводится; всё остальное — ровно меню.
    assert set(menu) == registered - {"start"}


@pytest.mark.asyncio
async def test_startup_publishes_bot_commands_to_telegram_menu(bot_module, dispatcher, fake_bot, api) -> None:
    bot = bot_module("bot")
    help_text = bot_module("help_text")
    assert [h.callback for h in dispatcher.startup.handlers] == [bot._publish_command_menu]

    await bot._publish_command_menu(fake_bot)

    [request] = api.sent(SetMyCommands)
    assert [(c.command, c.description) for c in request.commands] == list(help_text.BOT_COMMANDS)


def test_every_registered_handler_is_a_coroutine_function(dispatcher) -> None:
    """aiogram ждёт каждый хендлер (`await handler(...)`): синхронный упал бы в рантайме."""
    registered = [
        h
        for router in dispatcher.sub_routers
        for observer in (router.message, router.callback_query)
        for h in observer.handlers
    ]
    for handler in registered:
        assert asyncio.iscoroutinefunction(handler.callback), f"{handler.callback.__qualname__} is not async"

    # Страховка от «проверили пустой список»: standalone — 11 команд и 9 кнопок; диалог —
    # /stats, /cancel, ввод даты и 45 + 12 + 2 кнопок по состояниям; хвост — 2.
    assert len(registered) == 20 + 62 + 2
    assert {h.callback.__module__ for h in registered} == {"bot", "script_bot", "stats_handlers"}


# ---------------------------------------------------------------------------
# Маршрутизация настоящих апдейтов
# ---------------------------------------------------------------------------

async def _feed(dispatcher: Dispatcher, fake_bot: Any, update: Any) -> None:
    await dispatcher.feed_update(fake_bot, update)


async def _state(dispatcher: Dispatcher, fake_bot: Any) -> Any:
    return await dispatcher.fsm.get_context(bot=fake_bot, chat_id=CHAT_ID, user_id=USER_ID).get_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/help", "/HELP", "/help@nhl_test_bot"])
async def test_command_reaches_its_handler_in_any_case_and_with_own_mention(
    bot_module, dispatcher, fake_bot, api, make_update, text
) -> None:
    await _feed(dispatcher, fake_bot, make_update(text))

    [reply] = api.sent_messages
    assert reply.text == bot_module("help_text").HELP_MESSAGE


@pytest.mark.asyncio
async def test_command_addressed_to_another_bot_is_ignored(dispatcher, fake_bot, api, make_update) -> None:
    await _feed(dispatcher, fake_bot, make_update("/help@OtherBot"))

    assert api.sent_messages == []


@pytest.mark.asyncio
async def test_team_command_routes_abbreviation_and_full_flag(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch
) -> None:
    bot = bot_module("bot")
    monkeypatch.setattr(bot, "team_profile_text", lambda abbrev, full: f"{abbrev} full={full}")

    await _feed(dispatcher, fake_bot, make_update("/BOS"))
    await _feed(dispatcher, fake_bot, make_update("/wsh_full"))

    assert [m.text for m in api.sent_messages] == ["BOS full=False", "wsh full=True"]


@pytest.mark.asyncio
async def test_dialog_buttons_do_nothing_outside_the_dialog(
    bot_module, dispatcher, fake_bot, api, make_update
) -> None:
    """Кнопка меню `/stats` без открытого диалога (после «Готово» или /cancel) не
    срабатывает: её хендлеры зарегистрированы только в состояниях диалога."""
    ds = bot_module("dialog_states")
    await _feed(dispatcher, fake_bot, make_update(data=str(ds.CHOOSE_STATS)))

    assert api.requests == []


@pytest.mark.asyncio
async def test_dialog_walks_menu_then_date_input_then_cancel(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch
) -> None:
    """/stats → меню (`first`) → «Выбрать дату» (`third`) → дата уходит в диалог, а не ИИ
    (`second`) → /cancel закрывает диалог."""
    ds = bot_module("dialog_states")
    sh = bot_module("stats_handlers")
    bot = bot_module("bot")
    monkeypatch.setattr(sh, "day_digest", lambda day: (day, [(0, f"За {day} матчей нет", [])]))

    async def no_ai(*args: Any) -> Any:
        raise AssertionError("дату в диалоге не должен видеть ИИ")

    monkeypatch.setattr(bot.ai_router, "ask", no_ai)

    await _feed(dispatcher, fake_bot, make_update("/stats"))
    assert await _state(dispatcher, fake_bot) == ds.StatsMenu.first.state
    await _feed(dispatcher, fake_bot, make_update(data=str(ds.DAY_DIGEST)))
    await _feed(dispatcher, fake_bot, make_update(data=str(ds.DIGEST_PICK_DATE)))
    assert await _state(dispatcher, fake_bot) == ds.StatsMenu.third.state

    await _feed(dispatcher, fake_bot, make_update("2026-01-01"))
    assert await _state(dispatcher, fake_bot) == ds.StatsMenu.second.state
    assert api.sent_messages[-1].text == "За 2026-01-01 матчей нет"

    await _feed(dispatcher, fake_bot, make_update("/cancel"))
    assert await _state(dispatcher, fake_bot) is None
    assert api.sent_messages[-1].text == "Вы вышли из меню. Снова: /stats"


@pytest.mark.asyncio
async def test_cancel_inside_date_input_reaches_cancel_not_date_parser(
    bot_module, dispatcher, fake_bot, api, make_update
) -> None:
    ds = bot_module("dialog_states")
    await _feed(dispatcher, fake_bot, make_update("/stats"))
    await _feed(dispatcher, fake_bot, make_update(data=str(ds.DIGEST_PICK_DATE)))

    await _feed(dispatcher, fake_bot, make_update("/cancel"))

    assert await _state(dispatcher, fake_bot) is None
    assert api.sent_messages[-1].text == "Вы вышли из меню. Снова: /stats"


@pytest.mark.asyncio
async def test_cancel_outside_dialog_explains_there_is_nothing_to_cancel(
    dispatcher, fake_bot, api, make_update
) -> None:
    await _feed(dispatcher, fake_bot, make_update("/cancel"))

    [reply] = api.sent_messages
    assert reply.text == "Меню /stats сейчас не открыто. Справка: /help"


@pytest.mark.asyncio
async def test_top_level_command_works_inside_the_dialog_and_keeps_its_state(
    bot_module, dispatcher, fake_bot, api, make_update
) -> None:
    ds = bot_module("dialog_states")
    await _feed(dispatcher, fake_bot, make_update("/stats"))
    await _feed(dispatcher, fake_bot, make_update(data=str(ds.DIGEST_PICK_DATE)))

    await _feed(dispatcher, fake_bot, make_update("/help"))

    assert api.sent_messages[-1].text == bot_module("help_text").HELP_MESSAGE
    assert await _state(dispatcher, fake_bot) == ds.StatsMenu.third.state


@pytest.mark.asyncio
async def test_free_text_goes_to_ai_only_in_private_chat(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch
) -> None:
    """Задача 81: текст в личке вне диалога — вопрос модели; в группе бот молчит."""
    bot = bot_module("bot")
    asked: List[str] = []

    async def ask(chat_id: int, text: str) -> Any:
        asked.append(text)
        return bot.ai_router.Reply(text="ответ модели", tool=None, params={})

    monkeypatch.setattr(bot.ai_router, "questions_left", lambda chat_id: 5)
    monkeypatch.setattr(bot.ai_router, "ask", ask)

    await _feed(dispatcher, fake_bot, make_update("как сыграл Колорадо"))
    await _feed(dispatcher, fake_bot, make_update("таблица Востока", chat_id=-5, chat_type="group"))

    assert asked == ["как сыграл Колорадо"]
    assert [m.text for m in api.sent_messages] == ["ответ модели"]


@pytest.mark.asyncio
async def test_spam_presses_are_stopped_before_any_handler(
    bot_module, dispatcher, fake_bot, api, make_update
) -> None:
    """Задача 10: девятое нажатие за окно не доходит до хендлера (ни одной правки
    сообщения), пользователь получает ответ с текстом."""
    throttle = bot_module("throttle")
    throttle._hits.clear()
    for _ in range(throttle.CALLBACK_RATE_LIMIT + 1):
        await _feed(dispatcher, fake_bot, make_update(data="adv:close"))
    throttle._hits.clear()

    assert len(api.edited_markups) == throttle.CALLBACK_RATE_LIMIT
    assert api.answers[-1] == throttle.RATE_LIMIT_ANSWER_TEXT


# ---------------------------------------------------------------------------
# Запись id сообщения меню — источник данных для /cancel ниже. stats()/
# stats_over() пишут его сами (как и страницы дайджеста в stats_handlers), и
# если строка записи потеряется при рефакторинге, /cancel в основном сценарии
# молча перестанет снимать клавиатуру, а make ci-local останется зелёным без
# этих тестов.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_entry_point_records_new_message_id(bot_module, make_message, fsm_state, api):
    script_bot = bot_module("script_bot")
    dialog_states = bot_module("dialog_states")

    await script_bot.stats(make_message("/stats"), fsm_state)

    assert await fsm_state.get_state() == dialog_states.StatsMenu.first.state
    assert (await fsm_state.get_data())[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


@pytest.mark.asyncio
async def test_stats_over_records_new_message_id(bot_module, make_callback, fsm_state, fake_bot, api):
    script_bot = bot_module("script_bot")
    dialog_states = bot_module("dialog_states")

    await script_bot.stats_over(make_callback(str(dialog_states.CHOOSE_STATS), chat_id=555), fsm_state, fake_bot)

    assert await fsm_state.get_state() == dialog_states.StatsMenu.first.state
    [sent] = api.sent_messages
    assert sent.chat_id == 555
    assert (await fsm_state.get_data())[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


# ---------------------------------------------------------------------------
# /cancel внутри диалога — снятие «зомби»-клавиатуры: сообщение меню
# остаётся в чате с виду живым после конца диалога, хотя ни один хендлер
# его callback_data больше не матчит.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_in_conversation_clears_recorded_menu_keyboard(
    bot_module, make_message, fsm_state, fake_bot, api
):
    bot = bot_module("bot")
    dialog_states = bot_module("dialog_states")
    await fsm_state.set_state(dialog_states.StatsMenu.second)
    await fsm_state.update_data({dialog_states.LAST_MENU_MESSAGE_ID_KEY: 42})

    await bot.cmd_cancel_in_conversation(make_message("/cancel", chat_id=555), fsm_state, fake_bot)

    [edit] = api.edited_markups
    assert (edit.chat_id, edit.message_id, edit.reply_markup) == (555, 42, None)
    # Диалог закрыт и id снят — второй /cancel подряд не редактирует уже неактуальное сообщение.
    assert await fsm_state.get_state() is None
    assert await fsm_state.get_data() == {}
    assert api.sent_messages[0].text == "Вы вышли из меню. Снова: /stats"


@pytest.mark.asyncio
async def test_cancel_in_conversation_without_recorded_id_skips_keyboard_edit(
    bot_module, make_message, fsm_state, fake_bot, api
):
    """`/cancel` без открытого кнопкой меню — штатный случай (CLAUDE.md,
    Global Constraint 4): отсутствие записи не ошибка, а не try/except."""
    bot = bot_module("bot")

    await bot.cmd_cancel_in_conversation(make_message("/cancel"), fsm_state, fake_bot)

    assert api.edited_markups == []


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def _record_polling(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Dispatcher, Any]]:
    polled: List[Tuple[Dispatcher, Any]] = []

    async def start_polling(self: Dispatcher, *bots: Any, **kwargs: Any) -> None:
        polled.append((self, bots))

    monkeypatch.setattr(Dispatcher, "start_polling", start_polling)
    return polled


def test_main_starts_polling_with_bot_built_from_config_token(
    bot_module, monkeypatch: pytest.MonkeyPatch, set_required_bot_env
) -> None:
    bot = bot_module("bot")
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.setattr(config, "TOKEN", FAKE_TOKEN)
    polled = _record_polling(monkeypatch)

    bot.main()

    [(dispatcher, (polled_bot,))] = polled
    assert polled_bot.token == FAKE_TOKEN
    assert [r.name for r in dispatcher.sub_routers] == ["standalone", "stats", "tail"]


def test_main_raises_and_never_polls_when_token_missing(
    bot_module, monkeypatch: pytest.MonkeyPatch, set_required_bot_env
) -> None:
    """`bot.main()` падает до сборки бота, если токена нет."""
    bot = bot_module("bot")
    set_required_bot_env(TELEGRAM_BOT_TOKEN="")
    polled = _record_polling(monkeypatch)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.main()

    assert polled == []


def test_main_raises_and_never_polls_without_anthropic_key(
    bot_module, monkeypatch: pytest.MonkeyPatch, set_required_bot_env
) -> None:
    """Задача 81: без ключа Claude API бот не стартует, рассылка — да (ей ключ не нужен)."""
    bot = bot_module("bot")
    config = bot_module("config")
    set_required_bot_env(ANTHROPIC_API_KEY="")
    polled = _record_polling(monkeypatch)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        bot.main()

    assert polled == []
    config.validate_env()


# ---------------------------------------------------------------------------
# config.validate_env()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing_var",
    ["TELEGRAM_BOT_TOKEN", "PG_HOST", "PG_PORT", "PG_USER", "PG_DATABASE", "SEASON_ID"],
)
def test_validate_env_raises_on_missing_variable(
    bot_module, monkeypatch: pytest.MonkeyPatch, set_required_bot_env, missing_var: str
) -> None:
    """Отсутствующая (не заданная вовсе) переменная — падение с её именем."""
    config = bot_module("config")
    set_required_bot_env()
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(RuntimeError, match=missing_var):
        config.validate_env()


@pytest.mark.parametrize("blank_value", ["", "   "])
def test_validate_env_raises_on_blank_variable(
    bot_module, set_required_bot_env, blank_value: str
) -> None:
    """Пустая строка или строка из пробелов считается отсутствием переменной."""
    config = bot_module("config")
    set_required_bot_env(PG_DATABASE=blank_value)

    with pytest.raises(RuntimeError, match="PG_DATABASE"):
        config.validate_env()


def test_validate_env_error_message_does_not_leak_other_values(
    bot_module, set_required_bot_env
) -> None:
    """Сообщение об ошибке называет переменную, но не значения остальных."""
    config = bot_module("config")
    secret_token = "123456789:SUPER-SECRET-VALUE"
    set_required_bot_env(TELEGRAM_BOT_TOKEN=secret_token, PG_HOST="")

    with pytest.raises(RuntimeError) as exc_info:
        config.validate_env()

    assert secret_token not in str(exc_info.value)


def test_validate_env_passes_when_all_required_variables_are_set(
    bot_module, set_required_bot_env
) -> None:
    config = bot_module("config")
    set_required_bot_env()

    config.validate_env()  # не должно бросить исключение


# ---------------------------------------------------------------------------
# config._season_id_from_env / config._env_float на кривом значении
# ---------------------------------------------------------------------------

def test_season_id_from_env_raises_on_garbage_value_without_leaking_it(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bot_module("config")
    monkeypatch.setenv("SEASON_ID", "not-a-number")

    with pytest.raises(ValueError) as exc_info:
        config._season_id_from_env()

    assert "SEASON_ID" in str(exc_info.value)
    assert "not-a-number" not in str(exc_info.value)


def test_season_id_from_env_returns_none_when_unset(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отсутствие `SEASON_ID` не должно валить импорт `config.py` (тесты/CI
    импортируют его без `.env`) — падать обязаны только явные точки входа."""
    config = bot_module("config")
    monkeypatch.delenv("SEASON_ID", raising=False)

    assert config._season_id_from_env() is None


# ---------------------------------------------------------------------------
# config.derive_season() — единственный источник CURRENT_SEASON / DATE_FROM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "season_id,expected_label,expected_date_from",
    [
        (20262027, "26/27", "2026-09-01"),
        (20252026, "25/26", "2025-09-01"),
        (20242025, "24/25", "2024-09-01"),
        (20212022, "21/22", "2021-09-01"),
    ],
)
def test_derive_season(
    bot_module, season_id: int, expected_label: str, expected_date_from: str
) -> None:
    config = bot_module("config")

    assert config.derive_season(season_id) == (expected_label, expected_date_from)


def test_env_float_raises_on_garbage_value_without_leaking_it(
    bot_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bot_module("config")
    monkeypatch.setenv("PUSH_SEND_INTERVAL_SEC", "not-a-number")

    with pytest.raises(ValueError) as exc_info:
        config._env_float("PUSH_SEND_INTERVAL_SEC", 0.05)

    assert "PUSH_SEND_INTERVAL_SEC" in str(exc_info.value)
    assert "not-a-number" not in str(exc_info.value)
