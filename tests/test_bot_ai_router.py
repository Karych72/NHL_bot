"""Свободный текст (Задача 81): маршрутизация ответа модели в экраны бота, лимит, журнал, сбой API.

Модель подменена ответом с заданным ``tool_use`` или текстом — проверяется наша
маршрутизация, а не выбор модели (его проверяет контрольный набор
``scripts/ai_router_eval.py`` против настоящего API). БД — маршрутизирующий фейк
``fake_db_router``: экран, открытый по инструменту, сравнивается с тем, что на тех же данных
рисует команда или кнопка.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List

import anthropic
import httpx2
import pytest

from tests._telegram_fakes import body, rich_html

TEAMS = [("BOS",), ("WSH",)]
# Строка `teams_stats ⋈ teams` клуба 15 (колонки `_TEAM_PROFILE_STATS_COLUMNS`): без неё оба
# экрана клуба ответили бы одним текстом «ещё не сыграла».
WSH_STATS = (
    15, "Washington Capitals", "Capitals", "Metropolitan", "Eastern", 17, 10, 5, 2, 22, 64.71,
    3.1, 2.65, 30.5, 28, 21.5, 80, 51.25, 12, 56, 9,
)


DIVISIONS = [("Eastern", "Atlantic", "BOS"), ("Eastern", "Metropolitan", "WSH")]


class FakeMessages:
    """``client.messages``: отдаёт заданный ответ (или бросает заданное исключение)."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: List[dict] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _response(*content: Any, stop_reason: str = "") -> SimpleNamespace:
    """Ответ модели; перед содержимым — пустой блок мышления, как у Claude 5.5 на effort low."""
    tool = any(b.type == "tool_use" for b in content)
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), *content],
        stop_reason=stop_reason or ("tool_use" if tool else "end_turn"),
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=50,
            cache_read_input_tokens=3000, cache_creation_input_tokens=0,
        ),
        _request_id="req_test",
    )


def _tool_use(name: str, **params: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=params)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


@pytest.fixture
def ai(bot_module, fake_db_router, monkeypatch):
    """Фабрика окружения: чат задал `asked` = (за сутки, за неделю) вопросов, `premium` —
    у него премиум; `spent` — расход на ИИ за сутки, $ (общий бюджет, Задача 141); `previous` —
    (вопрос, ответ) последнего текстового ответа чату в окне уточнения (Задача 114); модель
    вернёт `result`. Возвращает (bot, курсор БД, FakeMessages)."""

    def _make(result: Any, asked: tuple = (0, 0), premium: bool = False, spent: float = 0.0,
              previous: tuple = ()):
        bot = bot_module("bot")
        ai_router = bot_module("ai_router")
        cursor = fake_db_router([
            ("FILTER (WHERE asked_at", [asked]),
            ("sum(cost_usd)", [(spent,)]),
            ("FROM premium_payments", [(False,)]),
            ("FROM premium_access", [(datetime(2099, 1, 1, tzinfo=timezone.utc),)] if premium else []),
            ("INSERT INTO ai_questions", []),
            ("ORDER BY asked_at DESC LIMIT 1", [previous] if previous else []),
            ("AS clubs FROM teams", DIVISIONS),
            ("FROM bot_subscriptions", [("morning_digest", None, None, None), ("team_scores", 15, None, None)]),
            ("FROM bot_schedules", []),
            ("AS ab FROM teams", TEAMS),
            ("SELECT team_id FROM teams WHERE season_id", [(15,)]),
            ("ts.power_play_goals_against FROM teams_stats", [WSH_STATS]),
            ("SELECT name FROM teams WHERE season_id", [("Washington Capitals",)]),
            ("max(day) AS day FROM games", [(None,)]),
            ("", []),
        ])
        # Журналы пишут через `get_connection`, импортированный в модуль по имени.
        monkeypatch.setattr(ai_router, "get_connection", bot_module("database").get_connection)
        monkeypatch.setattr(bot, "get_connection", bot_module("database").get_connection)
        # Пояс черновика по умолчанию — из расписаний чата (Задача 108).
        monkeypatch.setattr(bot_module("schedule_repo"), "get_connection", bot_module("database").get_connection)
        messages = FakeMessages(result)
        monkeypatch.setattr(ai_router, "client", lambda: SimpleNamespace(messages=messages))
        return bot, cursor, messages

    return _make


def _logged(cursor) -> dict:
    """Строка журнала `ai_questions`, записанная за прогон (ровно одна)."""
    (insert,) = [(q, p) for q, p in cursor.executed if q.startswith("INSERT INTO ai_questions")]
    columns = insert[0].split("(", 1)[1].split(")", 1)[0].split(", ")
    return dict(zip(columns, insert[1]))


async def _ask(bot, make_message, fake_bot, text: str = "вопрос") -> None:
    start = len(fake_bot.session.requests)
    await bot.on_free_text(make_message(text), fake_bot)
    fake_bot.session.remove_ai_progress(start, bot._AI_WAIT_TEXT, list(bot._AI_LIMIT_KINDS))


def _replies(api) -> list:
    """Тела ответов пользователю — текст модели или служебная фраза: rich без кнопок,
    обычных сообщений нет (Задача 91)."""
    assert not api.sent_messages
    assert all(m.reply_markup is None for m in api.sent_rich)
    return [body(m) for m in api.sent_rich]


def _as_reply(text: str) -> str:
    """Ожидаемое тело ответа на *text*: текст экранирован — `<` в тексте модели не должен
    ломать rich-HTML."""
    return rich_html(html.escape(text))


# ---------------------------------------------------------------------------
# Инструмент → тот же экран, что у команды или кнопки
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["today", "standings", "stats", "team_stats", "team", "countries", "help", "premium"]
)
async def test_open_command_replies_like_the_command(
    ai, make_message, fake_bot, api, monkeypatch, command
):
    bot, _, _ = ai(_response(_tool_use("open_command", command=command)))
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: "2026-10-24")
    await _ask(bot, make_message, fake_bot, "покажи")
    via_ai = list(api.requests)
    await bot.COMMAND_HANDLERS[command](make_message(f"/{command}"), fake_bot)
    direct = api.requests[len(via_ai):]

    assert via_ai == direct
    assert via_ai, "команда что-то отправила"


@pytest.mark.asyncio
@pytest.mark.parametrize(("all_players", "command"), [(True, "/WSH_FULL"), (False, "/WSH")])
async def test_team_profile_replies_like_team_command(
    ai, make_message, fake_bot, api, all_players, command
):
    """Клуб найден (маршрут `TEAM_ID`), игр и игроков нет: короткий и полный экран отвечают
    разными текстами (места в лиге, подвал), так что сравнение различает `all_players`."""
    bot, _, _ = ai(_response(_tool_use("team_profile", team="WSH", all_players=all_players)))
    await _ask(bot, make_message, fake_bot, "как там Овечкин")
    other_command = "/WSH" if all_players else "/WSH_FULL"
    for text in (command, other_command):
        await bot.cmd_team_profile(make_message(text), re.match(bot.TEAM_COMMAND_PATTERN, text))

    via_ai, direct, other = api.sent_rich
    assert via_ai == direct
    assert via_ai != other


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "params", "callback", "data", "pattern", "needs_bot"),
    [
        ("leaders", {"category": "goals"}, "callback_stat_screen", "st:players_season_stats:goals:0",
         "STAT_SCREEN_CALLBACK_PATTERN", True),
        ("country_players", {"country": "RUS"}, "callback_country", "cn:RUS:F:0",
         "COUNTRY_CALLBACK_PATTERN", False),
        ("advanced_metric", {"metric": "sat"}, "callback_stat_screen", "sa:players_advanced_stats:sat_pct:0",
         "STAT_SCREEN_CALLBACK_PATTERN", True),
    ],
)
async def test_screen_tool_sends_what_the_button_draws(
    ai, bot_module, make_message, make_callback, fake_bot, api,
    tool, params, callback, data, pattern, needs_bot,
):
    """Экран из кнопки рисуется правкой сообщения, из вопроса — новым сообщением; текст и
    кнопки одни и те же."""
    stats_handlers = bot_module("stats_handlers")
    bot, _, _ = ai(_response(_tool_use(tool, **params)))
    await _ask(bot, make_message, fake_bot)
    match = re.match(getattr(stats_handlers, pattern), data)
    extra = {"bot": fake_bot} if needs_bot else {}
    await getattr(stats_handlers, callback)(make_callback(data), match=match, **extra)

    (reply,) = api.sent_rich
    (edited,) = api.edited_texts
    assert reply.rich_message == edited.rich_message
    assert reply.reply_markup == edited.reply_markup


@pytest.mark.asyncio
async def test_digest_for_date_asks_that_day_and_sends_it(ai, make_message, fake_bot, api):
    bot, cursor, _ = ai(_response(_tool_use("digest_for_date", date="2026-10-19")))
    await _ask(bot, make_message, fake_bot, "результаты 19 октября")

    assert any("games WHERE day" in q and p[0] == "2026-10-19" for q, p in cursor.executed)
    sent = api.sent_rich[0]
    text = body(sent)
    assert text.startswith(rich_html("За 2026-10-19 завершенных матчей не найдено."))
    # Подсказка «Ещё: …» — подвал этого же сообщения, а не отдельное сообщение.
    assert text.endswith("</footer>") and "<footer>Ещё: /tonight" in text
    assert api.sent_messages == []
    assert sent.reply_markup is None, "у дня без матчей кнопок нет"


def test_every_tool_has_a_screen_and_every_command_a_handler(bot_module):
    bot = bot_module("bot")
    ai_router = bot_module("ai_router")
    tools = ai_router.build_tools(["BOS"])
    # `stats_question` — не экран: его вопрос уходит в `ai_stats` (Задача 83); `schedule_screen` —
    # карточка расписания (Задача 108); `bot_answer` и `my_status` — текст (Задача 114).
    assert [t["name"] for t in tools] == [
        *bot.AI_SCREENS, "stats_question", "schedule_screen", "bot_answer", "my_status",
    ]
    assert set(ai_router.SCREEN_COMMANDS) == set(bot.AI_COMMANDS)
    for tool in tools:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False


def test_schedule_tool_offers_exactly_the_schedulable_screens(bot_module):
    """Задача 108: экраны `schedule_screen` — набор расписаний 107, параметры — схемы тех же
    инструментов-экранов; варианты строгие, экран можно не узнать (`null`)."""
    bot = bot_module("bot")
    tools = {t["name"]: t for t in bot_module("ai_router").build_tools(["BOS", "WSH"])}
    schema = tools["schedule_screen"]["input_schema"]
    *options, unknown = schema["properties"]["screen"]["anyOf"]

    assert unknown == {"type": "null"}
    assert schema["required"] == ["screen", "freq", "weekday", "month_day", "time", "timezone"]
    assert [o["properties"]["tool"]["const"] for o in options] == list(bot.SCHEDULE_SCREENS)
    for option in options:
        name = option["properties"]["tool"]["const"]
        assert option["additionalProperties"] is False
        assert option["required"] == list(option["properties"])
        params = {k: v for k, v in option["properties"].items() if k != "tool"}
        if name == "open_command":
            assert params["command"]["enum"] == list(bot.SCHEDULE_COMMANDS)
        else:
            assert params == tools[name]["input_schema"]["properties"]


@pytest.mark.asyncio
async def test_schedule_request_gets_a_draft_card(ai, make_message, fake_bot, api):
    """Пояс городом, время с минутами округляется до получаса, ряд «Когда» — дни недели."""
    bot, _, _ = ai(_response(_tool_use(
        "schedule_screen", screen={"tool": "leaders", "category": "goals"}, freq="week", weekday=5,
        month_day=None, time="10:17", timezone="Asia/Yekaterinburg",
    )))
    await _ask(bot, make_message, fake_bot, "присылай лидеров по голам по пятницам в 10:17 по Екатеринбургу")

    [card] = api.sent_rich
    text = body(card)
    for line in ("Экран: Лидеры · Лучшие снайперы", "Когда: по пятницам", "Время: 10:30",
                 "Пояс: Екатеринбург (UTC+5)"):
        assert line in text
    labels = [b.text for row in card.reply_markup.inline_keyboard for b in row]
    assert "•Пт" in labels and "✅ Сохранить" in labels


@pytest.mark.asyncio
async def test_unknown_zone_and_screen(ai, make_message, fake_bot, api):
    """Пояса нет в `zoneinfo` — МСК (у чата нет расписаний); экран не понят — без карточки."""
    params = dict(freq=None, weekday=None, month_day=None, time=None, timezone="Mars/Olympus")
    bot, _, _ = ai(_response(_tool_use("schedule_screen", screen={"tool": "open_command", "command": "today"},
                                       **params)))
    await _ask(bot, make_message, fake_bot, "присылай итоги ночи")
    assert "Пояс: Москва (UTC+3)" in body(api.sent_rich[-1])

    bot, _, _ = ai(_response(_tool_use("schedule_screen", screen=None, **params)))
    await _ask(bot, make_message, fake_bot, "присылай что-нибудь")
    assert body(api.sent_rich[-1]) == rich_html(bot._UNKNOWN_SCREEN_TEXT)
    assert api.sent_rich[-1].reply_markup is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "routed"),
    [("присылай лидеров по голам каждую пятницу", True), ("а у Малкина?", False)],
)
async def test_schedule_request_bypasses_the_stats_dialog_window(
    ai, bot_module, make_message, fake_bot, api, monkeypatch, text, routed
):
    """Окно продолжения 83 открыто: просьба о расписании всё равно идёт маршрутизатору, а
    продолжение диалога — ответам по статистике без него."""
    ai_stats = bot_module("ai_stats")
    stats_answers: List[str] = []

    async def answer(chat_id, question, history):
        stats_answers.append(question)
        return "ответ"

    bot, _, messages = ai(_response(_tool_use(
        "schedule_screen", screen={"tool": "leaders", "category": "goals"}, freq="week", weekday=5,
        month_day=None, time=None, timezone=None,
    )))
    monkeypatch.setattr(ai_stats, "recent_pairs", lambda chat_id: [{"outcome": "answer"}])
    monkeypatch.setattr(ai_stats, "answer", answer)
    await _ask(bot, make_message, fake_bot, text)

    assert bool(messages.calls) is routed
    assert stats_answers == ([] if routed else [text])


def test_team_profile_defaults_to_short_screen(bot_module):
    """Задача 100: вопрос о клубе открывает короткий экран, `all_players=true` — только по
    просьбе всех игроков, состава или подробностей; правила «вопрос об игроке →
    all_players=true» нет ни в промпте, ни в описании инструмента."""
    ai_router = bot_module("ai_router")
    (team_profile,) = [t for t in ai_router.build_tools(["BOS"]) if t["name"] == "team_profile"]
    assert "По умолчанию all_players=false" in team_profile["description"]
    assert "team_profile с all_players=false" in ai_router.SYSTEM_PROMPT
    assert "«подробно про Бостон») — team_profile с all_players=true" in ai_router.SYSTEM_PROMPT
    for text in (ai_router.SYSTEM_PROMPT, team_profile["description"]):
        assert "его команды в текущем сезоне с all_players=true" not in text


# ---------------------------------------------------------------------------
# Текст, журнал, запрос к модели
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_answer_goes_to_user_and_to_log(ai, make_message, fake_bot, api):
    bot, cursor, messages = ai(_response(_text("Рейнджерс или Айлендерс? <уточните>")))
    await _ask(bot, make_message, fake_bot, "как сыграл Нью-Йорк")

    assert _replies(api) == ["<p>Рейнджерс или Айлендерс? &lt;уточните&gt;</p>"]
    row = _logged(cursor)
    assert (row["outcome"], row["answer"], row["question"]) == (
        "text", "Рейнджерс или Айлендерс? <уточните>", "как сыграл Нью-Йорк",
    )
    assert row["cost_usd"] == pytest.approx((100 * 0.10 + 50 * 0.50 + 3000 * 0.01) / 1e6)
    (call,) = messages.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "как сыграл Нью-Йорк" in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_screen_choice_is_logged_with_tool_and_params(ai, make_message, fake_bot):
    bot, cursor, _ = ai(_response(_tool_use("leaders", category="points")))
    await _ask(bot, make_message, fake_bot)

    row = _logged(cursor)
    assert (row["outcome"], row["tool"], row["tool_input"].adapted) == (
        "screen", "leaders", {"category": "points"},
    )
    assert row["request_id"] == "req_test"


@pytest.mark.asyncio
async def test_refusal_is_answered_as_unsupported(ai, bot_module, make_message, fake_bot, api):
    bot, _, _ = ai(_response(stop_reason="refusal"))
    await _ask(bot, make_message, fake_bot)

    assert _replies(api) == [_as_reply(bot_module("ai_router").UNSUPPORTED_TEXT)]


# ---------------------------------------------------------------------------
# Лимит (Задача 96): премиум — 20 в сутки; бесплатный — 3 в неделю, потолка за всё время нет
# (Задача 124). Сам счёт (границы суток и недели МСК, источники) — на настоящей БД,
# tests/test_db_nhl.py.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("premium", "asked", "refusal", "limit"),
    [
        (True, (19, 30), None, None),
        (True, (20, 20), "LIMIT_TEXT", "day"),
        (False, (2, 2), None, None),
        (False, (0, 3), "WEEK_LIMIT_TEXT", "week"),
    ],
)
async def test_limit_is_daily_for_premium_and_weekly_for_free(
    ai, bot_module, make_message, fake_bot, api, premium, asked, refusal, limit
):
    """При исчерпанном лимите модель спрашивается (вдруг вопрос о боте, Задача 114), но её
    ответ вне лимита не проходит: отказ и строка `ai_limit` с видом лимита в журнал действий
    (Задача 99)."""
    bot, cursor, messages = ai(_response(_text("Пока не умею")), asked=asked, premium=premium)
    await _ask(bot, make_message, fake_bot)

    assert len(messages.calls) == 1
    if refusal is not None:
        assert _replies(api) == [_as_reply(getattr(bot_module("ai_router"), refusal))]
        assert ("/premium" in getattr(bot_module("ai_router"), refusal)) is not premium
        message = make_message("")
        assert [q.split(" (")[0] for q, p in cursor.executed if q.startswith("INSERT")] == [
            "INSERT INTO ai_questions", "INSERT INTO bot_actions",
        ]
        assert cursor.executed[-1][1] == (message.chat.id, message.from_user.id, "ai_limit", limit)
    # Премиум — по чату вопроса.
    assert [p for q, p in cursor.executed if "FROM premium_access" in q] == [(make_message("").chat.id,)]


@pytest.mark.asyncio
async def test_exhausted_budget_refuses_without_model_and_logs_budget(
    ai, bot_module, make_message, fake_bot, api
):
    """Общий суточный бюджет (Задача 141): отказ без вызова модели, в журнале — `budget`."""
    ai_router = bot_module("ai_router")
    bot, cursor, messages = ai(
        _response(_text("Пока не умею")), spent=ai_router.DAILY_BUDGET_USD
    )
    await _ask(bot, make_message, fake_bot)

    assert not messages.calls
    assert _replies(api) == [_as_reply(ai_router.BUDGET_TEXT)]
    message = make_message("")
    assert [(q.split(" (")[0], p) for q, p in cursor.executed if q.startswith("INSERT")] == [
        ("INSERT INTO bot_actions", (message.chat.id, message.from_user.id, "ai_limit", "budget"))
    ]


# ---------------------------------------------------------------------------
# Сбой API
# ---------------------------------------------------------------------------

def _server_error() -> anthropic.InternalServerError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(500, request=request, headers={"request-id": "req_500"})
    return anthropic.InternalServerError("overloaded", response=response, body=None)


@pytest.mark.asyncio
async def test_api_failure_tells_user_to_use_commands_and_is_logged(
    ai, bot_module, make_message, fake_bot, api
):
    bot, cursor, _ = ai(_server_error())
    await _ask(bot, make_message, fake_bot)

    assert _replies(api) == [_as_reply(bot_module("ai_router").FAILURE_TEXT)]
    row = _logged(cursor)
    assert (row["outcome"], row["request_id"]) == ("error", "req_500")
    assert row["error"].startswith("InternalServerError")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, "timeout", "client", "truncated"])
async def test_upcoming_ai_cache_and_failure(bot_module, fake_db_router, monkeypatch, failure):
    ai_router = bot_module("ai_router")
    cursor = fake_db_router([
        ("INSERT INTO bot_upcoming_ai", [(95,)]),
        ("INSERT INTO ai_questions", []),
        ("UPDATE bot_upcoming_ai", []),
    ])
    monkeypatch.setattr(ai_router, "get_connection", bot_module("database").get_connection)
    result = {
        False: _response(_text("Текст превью.")),
        "timeout": TimeoutError(), "client": anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        "truncated": _response(_text("Оборванный текст"), stop_reason="max_tokens"),
    }[failure]
    messages = FakeMessages(result)
    monkeypatch.setattr(ai_router, "client", lambda: SimpleNamespace(messages=messages))
    result = await ai_router.upcoming_preview_text(95, "BOS: 12 побед. TOR: 10 побед.")
    assert result == ("" if failure else "Текст превью.")
    assert messages.calls[0]["messages"] == [
        {"role": "user", "content": "BOS: 12 побед. TOR: 10 побед."},
    ]
    assert "исключительно факты" in messages.calls[0]["system"]
    assert "Прогноз модели не используй" in messages.calls[0]["system"]
    logged = _logged(cursor)
    if failure == "truncated":
        assert logged["cost_usd"] > 0
        assert logged["input_tokens"] == 100
    if not failure:
        statements = [q for q, _ in cursor.executed]
        assert next(i for i, q in enumerate(statements) if q.startswith("UPDATE")) < next(
            i for i, q in enumerate(statements) if q.startswith("INSERT INTO ai_questions")
        )
    fake_db_router([
        ("INSERT INTO bot_upcoming_ai", []),
        ("SELECT body FROM bot_upcoming_ai", [(result or None,)]),
    ])
    monkeypatch.setattr(ai_router, "get_connection", bot_module("database").get_connection)
    assert await ai_router.upcoming_preview_text(95, "changed") == result
    assert len(messages.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_ai_progress_surrounds_model_and_answer(
    ai, make_message, fake_bot, api, monkeypatch, slow
):
    """Статус уже в чате во время API; долгий запрос правит его один раз до ответа."""
    import asyncio
    from aiogram.methods import DeleteMessage, EditMessageText, SendRichMessage

    bot, _, messages = ai(_response(_text("Готово")))
    original = messages.create
    monkeypatch.setattr(bot, "_AI_PROGRESS_DELAY", 0.01 if slow else 60)

    async def create(**kwargs):
        assert [body(r) for r in api.sent_rich] == [rich_html(bot._AI_WAIT_TEXT)]
        if slow:
            await asyncio.sleep(0.03)
        return await original(**kwargs)

    monkeypatch.setattr(messages, "create", create)
    await bot.on_free_text(make_message("вопрос"), fake_bot)
    assert [body(r) for r in api.sent_rich] == [rich_html(bot._AI_WAIT_TEXT), _as_reply("Готово")]
    edits = api.sent(EditMessageText)
    assert len(edits) == int(slow)
    if slow:
        assert body(edits[0]) == rich_html(bot._AI_LONG_WAIT_TEXT)
        assert edits[0].message_id == 1
    assert isinstance(api.requests[-2], SendRichMessage)
    assert isinstance(api.requests[-1], DeleteMessage)
    assert api.requests[-1].message_id == 1
    count = len(api.requests)
    await asyncio.sleep(0.03)
    assert len(api.requests) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_ai_progress_cleans_up_on_error_or_cancellation(
    bot_module, make_message, fake_bot, api, cancel
):
    import asyncio
    from aiogram.methods import DeleteMessage

    bot = bot_module("bot")
    error = asyncio.CancelledError if cancel else RuntimeError
    with pytest.raises(error):
        async with bot._ai_progress(make_message("вопрос"), fake_bot):
            raise error()
    assert len(api.sent(DeleteMessage)) == 1
    count = len(api.requests)
    await asyncio.sleep(0.01)
    assert len(api.requests) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["send", "edit", "delete"])
async def test_ai_progress_telegram_failure_preserves_answer(
    ai, make_message, fake_bot, api, monkeypatch, caplog, method_name
):
    import asyncio
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage, EditMessageText, SendRichMessage

    bot, _, messages = ai(_response(_text("Готово")))
    method = {"send": SendRichMessage, "edit": EditMessageText, "delete": DeleteMessage}[method_name]
    api.fail_next(method, TelegramBadRequest(method=method.model_construct(), message="unavailable"))
    monkeypatch.setattr(bot, "_AI_PROGRESS_DELAY", 0)
    original = messages.create

    async def create(**kwargs):
        await asyncio.sleep(0.01)
        return await original(**kwargs)

    monkeypatch.setattr(messages, "create", create)
    await bot.on_free_text(make_message("вопрос"), fake_bot)
    assert body(api.sent_rich[-1]) == _as_reply("Готово")
    failures = [r for r in caplog.records if f"AI progress {method_name} failed" in r.message]
    assert len(failures) == 1
    assert failures[0].exc_info is None
    assert not api._errors, "ожидаемая ошибка Telegram должна сработать"
    if method_name == "send":
        assert not api.sent(EditMessageText)
        assert not api.sent(DeleteMessage)


@pytest.mark.asyncio
async def test_ai_progress_preserves_cancellation_during_timer_cleanup(
    bot_module, make_message, fake_bot, api, monkeypatch
):
    """Отмена текущего обработчика при ожидании таймера доходит до вызывающего кода."""
    import asyncio
    from aiogram.methods import DeleteMessage

    bot = bot_module("bot")
    original_wait = asyncio.wait

    async def cancel_during_wait(tasks):
        asyncio.current_task().cancel()
        return await original_wait(tasks)

    monkeypatch.setattr(bot.asyncio, "wait", cancel_during_wait)

    async def handler():
        async with bot._ai_progress(make_message("вопрос"), fake_bot):
            pass

    task = asyncio.create_task(handler())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert len(api.sent(DeleteMessage)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["other_delete", "double_delete", "double_status"])
async def test_progress_filter_does_not_hide_unexpected_messages(fake_bot, api, extra):
    """Общий фильтр сохраняет посторонние удаления и отвергает повторный статус/удаление."""
    from aiogram.methods import DeleteMessage
    from rich_markup import rich_message

    status = await fake_bot.send_rich_message(chat_id=100, rich_message=rich_message("ожидание"))
    await fake_bot.delete_message(chat_id=100, message_id=status.message_id)
    if extra == "double_status":
        await fake_bot.send_rich_message(chat_id=100, rich_message=rich_message("ожидание"))
    else:
        await fake_bot.delete_message(
            chat_id=100, message_id=status.message_id if extra == "double_delete" else 999
        )
    if extra == "other_delete":
        api.remove_ai_progress(0, "ожидание")
        assert len(api.requests) == 1
        assert isinstance(api.requests[0], DeleteMessage)
        assert api.requests[0].message_id == 999
    else:
        with pytest.raises(AssertionError, match="ровно один раз"):
            api.remove_ai_progress(0, "ожидание")


# ---------------------------------------------------------------------------
# Вопросы о самом боте, правилах и структуре НХЛ (Задача 114)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bot_answer_goes_to_user_and_is_logged_as_free_text(ai, make_message, fake_bot, api):
    bot, cursor, _ = ai(_response(_tool_use("bot_answer", answer="Премиум — 100 ⭐ за 30 дней: /premium")))
    await _ask(bot, make_message, fake_bot, "сколько стоит премиум")

    assert _replies(api) == [_as_reply("Премиум — 100 ⭐ за 30 дней: /premium")]
    row = _logged(cursor)
    assert (row["outcome"], row["tool"], row["answer"]) == (
        "text", "bot_answer", "Премиум — 100 ⭐ за 30 дней: /premium",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("premium", [True, False])
async def test_my_status_numbers_come_from_the_bot(ai, make_message, fake_bot, api, premium):
    """Цифры чата — по тем же счётчикам, что лимит; модель их не пишет."""
    bot, _, _ = ai(_response(_tool_use("my_status")), asked=(2, 2), premium=premium)
    await _ask(bot, make_message, fake_bot, "сколько вопросов у меня осталось")

    (reply,) = _replies(api)
    if premium:
        assert "Премиум до 01.01.2099." in reply and "осталось 18 из 20" in reply
    else:
        assert "Премиума нет" in reply and "осталось 1 из 3" in reply
    assert "Подписок: 2, расписаний: 0" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "free"),
    [
        (_response(_tool_use("bot_answer", answer="Овертайм — 5 минут")), True),
        (_response(_tool_use("my_status")), True),
        (_response(_tool_use("open_command", command="premium")), True),
        (_response(_tool_use("open_command", command="standings")), False),
        (_response(_tool_use("stats_question")), False),
    ],
)
async def test_exhausted_limit_still_answers_about_the_bot(
    ai, bot_module, make_message, fake_bot, api, result, free
):
    """Решение человека 1: ответ о боте проходит при исчерпанном лимите, остальное — отказ."""
    ai_router = bot_module("ai_router")
    bot, cursor, _ = ai(result, asked=(0, ai_router.FREE_WEEKLY_LIMIT))
    await _ask(bot, make_message, fake_bot)

    refused = any(q.startswith("INSERT INTO bot_actions") for q, _ in cursor.executed)
    assert refused is not free
    assert (body(api.sent_rich[-1]) == _as_reply(ai_router.WEEK_LIMIT_TEXT)) is not free


@pytest.mark.asyncio
async def test_clarification_is_context_for_the_next_question(ai, make_message, fake_bot, api):
    bot, _, messages = ai(
        _response(_tool_use("bot_answer", answer="Дайджест — /subscriptions")),
        previous=("как отключить?", "Что отключить: дайджест, команду или расписание?"),
    )
    await _ask(bot, make_message, fake_bot, "дайджест")

    (call,) = messages.calls
    assert [m["role"] for m in call["messages"]] == ["user", "assistant", "user"]
    assert "как отключить?" in call["messages"][0]["content"]
    assert call["messages"][1]["content"] == "Что отключить: дайджест, команду или расписание?"
    assert "дайджест" in call["messages"][2]["content"]


@pytest.mark.asyncio
async def test_reference_with_rules_and_divisions_is_in_the_cached_prefix(ai, make_message, fake_bot):
    bot, _, messages = ai(_response(_text("Пока не умею")))
    await _ask(bot, make_message, fake_bot)

    (call,) = messages.calls
    rules, reference = call["system"]
    assert "cache_control" not in rules and reference["cache_control"] == {"type": "ephemeral"}
    assert "Правила НХЛ сезона 2026/27" in reference["text"]
    assert "Eastern · Atlantic: BOS" in reference["text"]
    assert "100 ⭐" in reference["text"] and "3 в неделю" in reference["text"]
    assert len(call["messages"]) == 1
