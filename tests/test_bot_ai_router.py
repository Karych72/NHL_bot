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
from types import SimpleNamespace
from typing import Any, List

import anthropic
import httpx2
import pytest

from tests._telegram_fakes import body, rich_html

TEAMS = [("BOS",), ("WSH",)]


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
    """Ответ модели; перед содержимым — пустой блок мышления, как у Sonnet 5.5 на effort low."""
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
    """Фабрика окружения: чат задал `asked` = (за сутки, за неделю, всего) вопросов, `premium` —
    у него премиум; модель вернёт `result`. Возвращает (bot, курсор БД, FakeMessages)."""

    def _make(result: Any, asked: tuple = (0, 0, 0), premium: bool = False):
        bot = bot_module("bot")
        ai_router = bot_module("ai_router")
        cursor = fake_db_router([
            ("FILTER (WHERE asked_at", [asked]),
            ("FROM premium_access", [(premium,)]),
            ("INSERT INTO ai_questions", []),
            ("AS ab FROM teams", TEAMS),
            ("SELECT team_id FROM teams WHERE season_id", [(15,)]),
            ("SELECT name FROM teams WHERE season_id", [("Washington Capitals",)]),
            ("max(day) AS day FROM games", [(None,)]),
            ("", []),
        ])
        # Журнал пишет через `get_connection`, импортированный в ai_router по имени.
        monkeypatch.setattr(ai_router, "get_connection", bot_module("database").get_connection)
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
    await bot.on_free_text(make_message(text), fake_bot)


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
    "command", ["today", "standings", "stats", "team_stats", "team", "countries", "help"]
)
async def test_open_command_replies_like_the_command(
    ai, make_message, fake_bot, api, monkeypatch, command
):
    bot, _, _ = ai(_response(_tool_use("open_command", command=command)))
    monkeypatch.setattr(bot, "last_night_day", lambda nights_back=0: "2026-10-24")
    await _ask(bot, make_message, fake_bot, "покажи")
    via_ai = list(api.requests)
    await getattr(bot, f"cmd_{command}")(make_message(f"/{command}"), fake_bot)
    direct = api.requests[len(via_ai):]

    assert via_ai == direct
    assert via_ai, "команда что-то отправила"


@pytest.mark.asyncio
@pytest.mark.parametrize(("all_players", "command"), [(True, "/WSH_FULL"), (False, "/WSH")])
async def test_team_profile_replies_like_team_command(
    ai, make_message, fake_bot, api, all_players, command
):
    """Клуб найден (маршрут `TEAM_ID`), статистики нет: профиль и полный список отвечают
    разными текстами, так что сравнение различает `all_players`."""
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
    # `stats_question` — не экран: его вопрос уходит в `ai_stats` (Задача 83).
    assert [t["name"] for t in tools] == [*bot.AI_SCREENS, "stats_question"]
    assert set(ai_router.SCREEN_COMMANDS) == set(bot.AI_COMMANDS)
    for tool in tools:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False


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
    assert row["cost_usd"] == pytest.approx((100 * 2 + 50 * 10 + 3000 * 0.2) / 1e6)
    (call,) = messages.calls
    assert call["model"] == "claude-sonnet-5-5"
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
# Лимит (Задача 96): премиум — 20 в сутки; бесплатный — 3 в неделю и 8 за всё время.
# Сам счёт (границы суток и недели МСК, источники) — на настоящей БД, tests/test_db_nhl.py.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("premium", "asked", "refusal"),
    [
        (True, (19, 30, 500), None),
        (True, (20, 20, 20), "LIMIT_TEXT"),
        (False, (2, 2, 7), None),
        (False, (0, 3, 3), "WEEK_LIMIT_TEXT"),
        # Потолок за всё время — раньше недельного: новой недели бесплатный уже не дождётся.
        (False, (0, 0, 8), "TOTAL_LIMIT_TEXT"),
        (False, (0, 3, 8), "TOTAL_LIMIT_TEXT"),
    ],
)
async def test_limit_is_daily_for_premium_and_weekly_with_total_cap_for_free(
    ai, bot_module, make_message, fake_bot, api, premium, asked, refusal
):
    bot, cursor, messages = ai(_response(_text("Пока не умею")), asked=asked, premium=premium)
    await _ask(bot, make_message, fake_bot)

    assert bool(messages.calls) is (refusal is None)
    if refusal is not None:
        assert _replies(api) == [_as_reply(getattr(bot_module("ai_router"), refusal))]
        assert not any(q.startswith("INSERT") for q, _ in cursor.executed)
    # Премиум — по чату вопроса.
    assert [p for q, p in cursor.executed if "FROM premium_access" in q] == [(make_message("").chat.id,)]


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
