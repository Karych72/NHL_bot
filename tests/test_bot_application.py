"""Каркас бота на aiogram 3: сборка `Dispatcher` и маршрутизация апдейтов.

Две части. Регистрация: какие хендлеры в каких роутерах и в каком порядке,
троттлинг и публикация меню команд. Маршрутизация: настоящие апдейты Telegram
прогоняются через `Dispatcher.feed_update` на боте с записывающей сессией
(`conftest.FakeSession`) — так проверяется не список регистраций, а куда реально
попадает команда, текст или нажатие. Сеть не используется; где хендлер ходил бы в БД или в Claude API,
подменяется функция модуля, которую он зовёт.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Tuple

import pytest
from aiogram import Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.methods import AnswerPreCheckoutQuery, CreateInvoiceLink, SetMyCommands
from aiogram.types import Message, PreCheckoutQuery

from tests._telegram_fakes import FAKE_TOKEN, body, rich_html


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

def test_dispatcher_checks_commands_then_free_text(dispatcher) -> None:
    """Порядок роутеров — поведение: апдейт берёт первый подошедший хендлер, поэтому
    свободный текст видит только то, что не забрали команды и кнопки."""
    assert [r.name for r in dispatcher.sub_routers] == ["standalone", "tail"]


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
        bot.cmd_team, bot.cmd_team_profile, bot.cmd_stats, bot.cmd_team_stats, bot.cmd_countries,
        bot.cmd_subscriptions, bot.cmd_premium, bot.cmd_paysupport, bot.on_successful_payment,
    ]
    assert isinstance(handlers[0].filters[0].callback, CommandStart)
    assert _commands(router) == [
        "start", "help", "today", "tonight", "standings", "team", "stats", "team_stats", "countries",
        "subscriptions", "premium", "paysupport",
    ]


def test_standalone_buttons_and_their_order(bot_module, dispatcher) -> None:
    bot = bot_module("bot")
    sh = bot_module("stats_handlers")

    assert _callback_handlers(_router(dispatcher, "standalone")) == [
        (sh.STAT_SCREEN_CALLBACK_PATTERN, sh.callback_stat_screen),
        (r"^dg:(\d+)$", sh.callback_expand_digest_game),
        (bot.SUBSCRIPTIONS_CALLBACK_PATTERN, bot.callback_subscriptions),
        (sh.TONIGHT_GAME_CALLBACK_PATTERN, sh.callback_tonight_game),
        (sh.COUNTRY_CALLBACK_PATTERN, sh.callback_country),
        (r"^gv:(\d+):(\d+)$", sh.handle_goal_video),
    ]


def test_tail_takes_only_free_text(bot_module, dispatcher) -> None:
    tail = _router(dispatcher, "tail")

    assert [h.callback for h in tail.message.handlers] == [bot_module("bot").on_free_text]
    assert _commands(tail) == []
    assert tail.callback_query.handlers == []


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
        for observer in (router.message, router.callback_query, router.pre_checkout_query)
        for h in observer.handlers
    ]
    for handler in registered:
        assert asyncio.iscoroutinefunction(handler.callback), f"{handler.callback.__qualname__} is not async"

    # Страховка от «проверили пустой список»: standalone — 14 хендлеров сообщений, 6 кнопок и pre_checkout; хвост — 1.
    assert len(registered) == 21 + 1
    assert {h.callback.__module__ for h in registered} == {"bot", "stats_handlers"}


# ---------------------------------------------------------------------------
# Маршрутизация настоящих апдейтов
# ---------------------------------------------------------------------------

async def _feed(dispatcher: Dispatcher, fake_bot: Any, update: Any) -> None:
    await dispatcher.feed_update(fake_bot, update)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/help", "/HELP", "/help@nhl_test_bot"])
async def test_command_reaches_its_handler_in_any_case_and_with_own_mention(
    bot_module, dispatcher, fake_bot, api, make_update, text
) -> None:
    await _feed(dispatcher, fake_bot, make_update(text))

    [reply] = api.sent_rich
    assert body(reply) == rich_html(bot_module("help_text").HELP_MESSAGE)


@pytest.mark.asyncio
async def test_command_addressed_to_another_bot_is_ignored(dispatcher, fake_bot, api, make_update) -> None:
    await _feed(dispatcher, fake_bot, make_update("/help@OtherBot"))

    assert api.outgoing == []


@pytest.mark.asyncio
async def test_team_command_routes_abbreviation_and_full_flag(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch
) -> None:
    bot = bot_module("bot")
    monkeypatch.setattr(bot, "team_profile_text", lambda abbrev, full: f"{abbrev} full={full}")

    await _feed(dispatcher, fake_bot, make_update("/BOS"))
    await _feed(dispatcher, fake_bot, make_update("/wsh_full"))

    assert [body(m) for m in api.sent_rich] == [
        rich_html("BOS full=False"), rich_html("wsh full=True"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, intro, data, render",
    [
        ("/stats", "STATS_COMMAND_INTRO", "st:players_season_stats:goals:0", "player_stat_leaderboard_page"),
        ("/team_stats", "TEAM_STATS_COMMAND_INTRO", "tm:goals:0", "team_stat_leaderboard_page"),
    ],
)
async def test_stats_command_menu_then_table_button(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch, command, intro, data, render
) -> None:
    """Команда присылает меню, кнопка меню — таблицу на месте того же сообщения; без
    состояния диалога: кнопка работает и в меню, присланном давно."""
    sh = bot_module("stats_handlers")
    monkeypatch.setattr(sh, render, lambda *args: ("TABLE", False, True))

    await _feed(dispatcher, fake_bot, make_update(command))
    await _feed(dispatcher, fake_bot, make_update(data=data))

    [menu] = api.sent_rich
    assert body(menu) == rich_html(getattr(bot_module("help_text"), intro))
    assert data in [b.callback_data for row in menu.reply_markup.inline_keyboard for b in row]
    [page] = api.edited_texts
    assert body(page) == rich_html("TABLE")


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [{"text": "/leaders"}, {"text": "/cancel"}, {"data": "0"}, {"data": "pl:pick:goals"}])
async def test_removed_commands_and_old_menu_buttons_are_silent(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch, update
) -> None:
    """Убранные `/leaders`, `/cancel` и кнопки старого меню `/stats` (числа, `pl:`) никуда
    не ведут: команда не уходит в ИИ как вопрос, кнопку не берёт ни один хендлер."""
    bot = bot_module("bot")

    async def no_ai(*args: Any) -> Any:
        raise AssertionError("команда — не вопрос модели")

    monkeypatch.setattr(bot.ai_router, "ask", no_ai)

    await _feed(dispatcher, fake_bot, make_update(**update))

    assert api.requests == []


@pytest.mark.asyncio
async def test_free_text_goes_to_ai_only_in_private_chat(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch
) -> None:
    """Задача 81: текст в личке — вопрос модели; в группе бот молчит."""
    bot = bot_module("bot")
    asked: List[str] = []

    async def ask(chat_id: int, text: str) -> Any:
        asked.append(text)
        return bot.ai_router.Reply(text="ответ модели", tool=None, params={})

    monkeypatch.setattr(bot.ai_router, "limit_refusal", lambda chat_id: None)
    monkeypatch.setattr(bot.ai_stats, "recent_pairs", lambda chat_id: [])
    monkeypatch.setattr(bot.ai_router, "ask", ask)

    await _feed(dispatcher, fake_bot, make_update("как сыграл Колорадо"))
    await _feed(dispatcher, fake_bot, make_update("таблица Востока", chat_id=-5, chat_type="group"))

    assert asked == ["как сыграл Колорадо"]
    assert [body(m) for m in api.outgoing] == [rich_html("ответ модели")]


@pytest.mark.asyncio
async def test_spam_presses_are_stopped_before_any_handler(
    bot_module, dispatcher, fake_bot, api, make_update
) -> None:
    """Задача 10: девятое нажатие за окно не доходит до хендлера (ни одной правки
    сообщения), пользователь получает ответ с текстом."""
    throttle = bot_module("throttle")
    throttle._hits.clear()
    for _ in range(throttle.CALLBACK_RATE_LIMIT + 1):
        await _feed(dispatcher, fake_bot, make_update(data="st:save"))
    throttle._hits.clear()

    assert len(api.edited_markups) == throttle.CALLBACK_RATE_LIMIT
    assert api.answers[-1] == throttle.RATE_LIMIT_ANSWER_TEXT


@pytest.mark.asyncio
async def test_double_tap_of_one_user_is_handled_one_after_another(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch
) -> None:
    """Ревью переезда: апдейты одного пользователя — по одному (`SimpleEventIsolation`,
    как в python-telegram-bot). Без изоляции второй тап по матчу `/tonight` начинался,
    пока первый ещё слал карточку, и карточка уходила дважды вместо ссылки на неё."""
    sh = bot_module("stats_handlers")
    monkeypatch.setattr(sh, "_tonight_sent", {})
    monkeypatch.setattr(sh, "game_exists", lambda game_id: True)
    send_card = sh.send_game_card_message

    async def slow_card(bot: Any, chat_id: int, game_id: int) -> Any:
        await asyncio.sleep(0.05)  # отправка тяжёлой карточки: второй тап приходит раньше её конца
        return await send_card(bot, chat_id, game_id)

    monkeypatch.setattr(sh, "send_game_card_message", slow_card)
    monkeypatch.setattr(sh, "game_message", lambda game_id: ("CARD", []))
    monkeypatch.setattr(sh, "get_chat_subscriptions", lambda chat_id: bot_module("subscription_repo").ChatSubscriptions(
        False, False, set(), []
    ))

    await asyncio.gather(*(
        _feed(dispatcher, fake_bot, make_update(data="tn:555:DET:NYR")) for _ in range(2)
    ))

    card, pointer = api.sent_rich  # карточка — одна, второе сообщение — ссылка на неё
    assert body(card) == rich_html("CARD")
    assert body(pointer) == rich_html(sh._TONIGHT_ALREADY_SENT)
    assert pointer.reply_parameters.message_id == 1, "второй тап — ссылка на неё"


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
    assert [r.name for r in dispatcher.sub_routers] == ["standalone", "tail"]


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


# ---------------------------------------------------------------------------
# Премиум за Stars (Задача 97)
# ---------------------------------------------------------------------------

def _premium_until(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "until_days, subscribed, status",
    [(None, False, "Премиума нет"), (5, False, "Премиум до "), (5, True, "Премиум до ")],
    ids=["free", "manual-grant", "paid-subscription"],
)
async def test_premium_command_offers_payment_unless_subscription_is_active(
    bot_module, dispatcher, fake_bot, api, make_update, monkeypatch, until_days, subscribed, status
) -> None:
    """Кнопка оплаты — бесплатному чату и вручную выданному премиуму; при действующей подписке
    Stars её нет (вторая подписка пошла бы параллельно), вместо неё — как отменить автопродление."""
    premium = bot_module("premium")
    monkeypatch.setattr(
        premium, "premium_until", lambda chat_id: None if until_days is None else _premium_until(until_days)
    )
    monkeypatch.setattr(premium, "has_paid_subscription", lambda chat_id: subscribed)

    await _feed(dispatcher, fake_bot, make_update("/premium"))

    [reply] = api.sent_rich
    html = body(reply)
    assert status in html
    assert str(bot_module("ai_router").PREMIUM_DAILY_LIMIT) in html and "100 ⭐" in html
    assert ("автопродление" in html) is subscribed
    if subscribed:
        assert api.sent(CreateInvoiceLink) == []
        assert reply.reply_markup is None
        return
    [invoice] = api.sent(CreateInvoiceLink)
    assert (invoice.currency, invoice.provider_token) == ("XTR", None)
    assert [p.amount for p in invoice.prices] == [100]
    assert invoice.subscription_period == 2592000
    [button] = [b for row in reply.reply_markup.inline_keyboard for b in row]
    assert button.url == "https://t.me/$fake-invoice"


@pytest.mark.asyncio
async def test_premium_command_in_group_has_no_button_and_no_invoice(
    bot_module, dispatcher, fake_bot, api, make_update
) -> None:
    await _feed(dispatcher, fake_bot, make_update("/premium", chat_id=-500, chat_type="supergroup"))

    [reply] = api.sent_messages
    assert "только в личном чате" in reply.text
    assert reply.reply_markup is None
    assert api.sent(CreateInvoiceLink) == []


@pytest.mark.asyncio
async def test_paysupport_names_the_support_contact(dispatcher, fake_bot, api, make_update) -> None:
    await _feed(dispatcher, fake_bot, make_update("/paysupport"))

    [reply] = api.sent_messages
    assert "@karych_72" in reply.text


@pytest.mark.asyncio
async def test_pre_checkout_is_confirmed_without_database(bot_module, fake_bot, api) -> None:
    query = PreCheckoutQuery.model_validate(
        {"id": "q1", "from": {"id": 200, "is_bot": False, "first_name": "T"}, "currency": "XTR",
         "total_amount": 100, "invoice_payload": "premium"},
        context={"bot": fake_bot},
    )

    await bot_module("bot").on_pre_checkout(query)

    [answer] = api.sent(AnswerPreCheckoutQuery)
    assert (answer.pre_checkout_query_id, answer.ok) == ("q1", True)


def _payment_message(fake_bot: Any) -> Message:
    return Message.model_validate(
        {"message_id": 1, "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "chat": {"id": 200, "type": "private"},
         "successful_payment": {
             "currency": "XTR", "total_amount": 100, "invoice_payload": "premium",
             "telegram_payment_charge_id": "tg-charge-1", "provider_payment_charge_id": "",
             "is_recurring": True}},
        context={"bot": fake_bot},
    )


@pytest.mark.asyncio
async def test_successful_payment_extends_premium_and_thanks(bot_module, fake_bot, api, monkeypatch) -> None:
    calls: List[Tuple[Any, ...]] = []

    def extend(*args: Any) -> datetime:
        calls.append(args)
        return datetime(2026, 12, 1, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(bot_module("premium"), "extend_premium", extend)

    await bot_module("bot").on_successful_payment(_payment_message(fake_bot), fake_bot)

    assert calls == [(200, "tg-charge-1", 100, True)]
    [reply] = api.sent_messages
    assert "Премиум до 01.12" in reply.text


@pytest.mark.asyncio
async def test_failed_payment_record_is_logged_with_charge_id_and_raised(
    bot_module, fake_bot, api, monkeypatch, caplog
) -> None:
    def broken(*args: Any) -> datetime:
        raise RuntimeError("db down")

    monkeypatch.setattr(bot_module("premium"), "extend_premium", broken)

    with pytest.raises(RuntimeError, match="db down"):
        await bot_module("bot").on_successful_payment(_payment_message(fake_bot), fake_bot)

    assert "tg-charge-1" in caplog.text and "chat_id=200" in caplog.text
    assert api.outgoing == []
