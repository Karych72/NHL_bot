"""Ответы ИИ на статистические вопросы (Задача 83): наш цикл, а не выбор модели.

Модель подменена заранее заданными ответами (очередь `FakeMessages`), роль БД только на чтение —
скриптованным соединением; проверяются цикл «инструмент → результат → финал», ревью, потолки,
окно продолжения диалога, белый список и журнал. Роль БД на настоящем Postgres проверяет
`tests/test_db_nhl.py`, качество ответов — `make ai-stats-eval` против настоящего API.
"""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import anthropic
import httpx2
import psycopg2
import psycopg2.errors
import pytest

from tests._telegram_fakes import CHAT_ID, body, rich_html


def _response(*content: Any, tokens: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content),
        stop_reason="tool_use" if any(b.type == "tool_use" for b in content) else "end_turn",
        usage=SimpleNamespace(
            input_tokens=tokens, output_tokens=50,
            cache_read_input_tokens=3000, cache_creation_input_tokens=0,
        ),
        _request_id="req_test",
    )


_ids = iter(range(10_000))


def _use(name: str, **params: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=params)


def _sql(sql: str = "SELECT 1") -> SimpleNamespace:
    return _response(_use("run_sql", sql=sql))


def _finish(kind: str = "answer", text: str = "Черновик", continuation: bool = False):
    return _response(_use("finish", kind=kind, continuation=continuation, text=text))


def _verdict(verdict: str = "ok", clarify_question: str = "", reason: str = "сходится"):
    return _response(_use("verdict", verdict=verdict, clarify_question=clarify_question, reason=reason))


class FakeMessages:
    """``client.messages``: отдаёт ответы из очередей по порядку — маршрутизатору 81 (запрос с
    инструментом ``open_command``) и ответам 83 отдельно; запросы запоминает со снимком
    ``messages`` на момент вызова (список потом растёт)."""

    def __init__(self, results: List[Any], router_results: List[Any]) -> None:
        self.results, self.router_results = list(results), list(router_results)
        self.calls: List[Dict[str, Any]] = []
        self.router_calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        is_router = any(t["name"] == "open_command" for t in kwargs["tools"])
        (self.router_calls if is_router else self.calls).append(
            {**kwargs, "messages": list(kwargs["messages"])}
        )
        result = (self.router_results if is_router else self.results).pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeReaderCursor:
    def __init__(self, handler: Callable[..., Any], executed: List[str]) -> None:
        self.handler, self.executed = handler, executed
        self.columns: List[str] = []
        self.rows: List[tuple] = []
        self.cancelled = threading.Event()

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        self.columns, self.rows = self.handler(sql, params)

    def fetchmany(self, size: int) -> List[tuple]:
        return self.rows[:size]

    def fetchall(self) -> List[tuple]:
        return self.rows

    @property
    def description(self):
        return [(c,) for c in self.columns]

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeReaderConnection:
    def __init__(self, handler: Callable[..., Any], executed: List[str]) -> None:
        self.cursor_ = FakeReaderCursor(handler, executed)

    def cursor(self) -> FakeReaderCursor:
        return self.cursor_

    def close(self) -> None:
        return None

    def cancel(self) -> None:
        self.cursor_.cancelled.set()


@pytest.fixture
def stats(bot_module, fake_db_router, monkeypatch):
    """Фабрика окружения. `responses` — очередь ответов модели; `reader` — {подстрока SQL:
    (колонки, строки) или исключение}; `history` — строки журнала окна (вопрос, исход,
    шаги, ответ, свежая ли) от новой к старой; `allowed` — чат в белом списке."""

    def _make(responses, reader=None, history=(), allowed=True, asked=0, existing=False,
              reader_error: Optional[Exception] = None, router=None):
        bot, ai_stats, ai_router, config = (bot_module(n) for n in ("bot", "ai_stats", "ai_router", "config"))
        monkeypatch.setattr(config, "AI_STATS_CHAT_IDS", frozenset({CHAT_ID}) if allowed else frozenset())
        monkeypatch.setattr(config, "SEASON_ID", 20262027)
        cursor = fake_db_router([
            ("count(*) FROM ai_questions", [(asked, existing)]),
            ("FROM ai_stats_log WHERE chat_id", list(history)),
            ("INSERT INTO ai_stats_log", []),
            ("INSERT INTO ai_questions", []),
            ("AS ab FROM teams", [("BOS",), ("WSH",)]),
            ("", []),
        ])
        monkeypatch.setattr(ai_stats, "get_connection", bot_module("database").get_connection)
        monkeypatch.setattr(ai_router, "get_connection", bot_module("database").get_connection)
        if router is None:
            # Свежая история открывает окно (маршрутизатор не зовётся), иначе он выберет stats_question.
            router = [] if history or not allowed else [_response(_use("stats_question"))]
        model = FakeMessages(responses, router)
        model.options = []

        def with_options(**kwargs):
            model.options.append(kwargs)
            return SimpleNamespace(messages=model)

        monkeypatch.setattr(
            ai_router, "client", lambda: SimpleNamespace(messages=model, with_options=with_options)
        )
        executed: List[str] = []

        def handler(sql: str, params: Any = None):
            for fragment, result in (reader or {}).items():
                if fragment in sql:
                    if isinstance(result, Exception):
                        raise result
                    return result(params) if callable(result) else result
            raise AssertionError(f"SQL не описан сценарием: {sql}")

        def connect():
            if reader_error is not None:
                raise reader_error
            return FakeReaderConnection(handler, executed)

        monkeypatch.setattr(ai_stats, "_reader_connection", connect)
        return SimpleNamespace(bot=bot, ai_stats=ai_stats, cursor=cursor, model=model, sql=executed)

    return _make


def _logged(cursor) -> dict:
    """Строка журнала `ai_stats_log`, записанная за прогон (ровно одна)."""
    (insert,) = [(q, p) for q, p in cursor.executed if q.startswith("INSERT INTO ai_stats_log")]
    columns = insert[0].split("(", 1)[1].split(")", 1)[0].split(", ")
    row = dict(zip(columns, insert[1]))
    for key in ("steps", "calls"):
        row[key] = row[key].adapted
    return row


def _tool_results(call: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Блоки tool_result в сообщениях запроса — то, что модель увидела от нас."""
    return [
        b for m in call["messages"] if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"] if b["type"] == "tool_result"
    ]


async def _ask(s, make_message, fake_bot, api, text: str = "сколько голов") -> List[str]:
    await s.bot.on_free_text(make_message(text), fake_bot)
    assert not api.sent_messages
    return [body(m) for m in api.sent_rich]


def _as_reply(text: str) -> str:
    return rich_html(text)


# ---------------------------------------------------------------------------
# Цикл и ревью
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reviewed_ok_draft_goes_to_user_and_is_logged(stats, make_message, fake_bot, api):
    s = stats(
        [_sql("SELECT goals FROM players_season_stats"), _finish(text="Овечкин: 42 гола"), _verdict()],
        reader={"players_season_stats": (["goals"], [(42,)])},
    )
    replies = await _ask(s, make_message, fake_bot, api, "голы Овечкина")

    assert replies == [_as_reply("Овечкин: 42 гола")]
    row = _logged(s.cursor)
    assert (row["outcome"], row["verdict"], row["draft"], row["answer"]) == (
        "answer", "ok", "Овечкин: 42 гола", "Овечкин: 42 гола",
    )
    assert row["steps"] == [{"tool": "run_sql", "input": {"sql": "SELECT goals FROM players_season_stats"},
                             "rows": 1, "error": None}]
    assert [c["role"] for c in row["calls"]] == ["answer", "answer", "review"]
    assert row["cost_usd"] == pytest.approx(3 * (100 * 2 + 50 * 10 + 3000 * 0.2) / 1e6)
    review = s.model.calls[-1]
    content = review["messages"][0]["content"]
    assert "SELECT goals FROM players_season_stats" in content and "[[42]]" in content.replace(" ", "")
    assert "Овечкин: 42 гола" in content and "голы Овечкина" in content


@pytest.mark.asyncio
async def test_clarify_verdict_sends_question_and_withholds_draft(stats, make_message, fake_bot, api):
    s = stats(
        [_sql(), _finish(text="У Овечкина 42 гола"),
         _verdict("clarify", "Вы про Джека или про Люка Хьюза?", "в базе два Хьюза в одном клубе")],
        reader={"SELECT 1": (["x"], [(1,)])},
    )
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply("Вы про Джека или про Люка Хьюза?")]
    assert "42 гола" not in " ".join(replies)
    row = _logged(s.cursor)
    assert (row["outcome"], row["draft"], row["answer"], row["verdict_reason"]) == (
        "clarify", "У Овечкина 42 гола", "Вы про Джека или про Люка Хьюза?", "в базе два Хьюза в одном клубе",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_data", "off_topic"])
async def test_no_data_and_off_topic_skip_review(stats, make_message, fake_bot, api, kind):
    s = stats([_finish(kind, "Травм у меня нет")])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply("Травм у меня нет")]
    assert len(s.model.calls) == 1, "ревьюер не вызывался"
    assert _logged(s.cursor)["outcome"] == kind


@pytest.mark.asyncio
async def test_model_clarify_goes_to_user_without_review(stats, make_message, fake_bot, api):
    s = stats([_finish("clarify", "Какой Овечкин?")])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply("Какой Овечкин?")]
    assert len(s.model.calls) == 1
    assert _logged(s.cursor)["outcome"] == "clarify"


@pytest.mark.asyncio
async def test_user_text_is_escaped(stats, make_message, fake_bot, api):
    s = stats([_finish("off_topic", "a < b & c")])
    assert await _ask(s, make_message, fake_bot, api) == [_as_reply("a &lt; b &amp; c")]


@pytest.mark.asyncio
async def test_sql_error_goes_back_to_model_and_next_attempt_passes(stats, make_message, fake_bot, api):
    s = stats(
        [_sql("SELECT gols FROM players_season_stats"), _sql("SELECT goals FROM players_season_stats"),
         _finish(text="42"), _verdict()],
        reader={
            "gols": psycopg2.errors.UndefinedColumn('column "gols" does not exist'),
            "SELECT goals": (["goals"], [(42,)]),
        },
    )
    assert await _ask(s, make_message, fake_bot, api) == [_as_reply("42")]

    first, second = _tool_results(s.model.calls[2])
    assert first["is_error"] and 'column "gols" does not exist' in first["content"]
    assert not second["is_error"]
    row = _logged(s.cursor)
    assert [(st["rows"], st["error"] is not None) for st in row["steps"]] == [(None, True), (1, False)]


@pytest.mark.asyncio
async def test_statement_timeout_is_a_model_attempt_not_a_failure(stats, make_message, fake_bot, api):
    s = stats(
        [_sql("SELECT slow"), _finish("no_data", "Не получилось посчитать")],
        reader={"slow": psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")},
    )
    await _ask(s, make_message, fake_bot, api)

    (result,) = _tool_results(s.model.calls[1])
    assert result["is_error"] and "statement timeout" in result["content"]
    assert _logged(s.cursor)["outcome"] == "no_data"


@pytest.mark.asyncio
async def test_empty_result_is_reported_to_model(stats, make_message, fake_bot, api):
    s = stats([_sql(), _finish("no_data", "Нет")], reader={"SELECT 1": (["goals"], [])})
    await _ask(s, make_message, fake_bot, api)

    (result,) = _tool_results(s.model.calls[1])
    assert json.loads(result["content"]) == {
        "columns": ["goals"], "rows": [], "truncated": False, "note": "запрос выполнен, строк нет",
    }


def test_run_readonly_cuts_rows_and_flags_truncation(stats):
    s = stats([], reader={"SELECT": (["n"], [(i,) for i in range(51)])})
    outcome = s.ai_stats.run_readonly("SELECT n FROM t")

    payload = json.loads(outcome.text)
    assert (len(payload["rows"]), payload["truncated"], outcome.rows) == (50, True, 50)


# ---------------------------------------------------------------------------
# Потолки
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fifth_sql_attempt_hits_ceiling(stats, make_message, fake_bot, api):
    s = stats([_sql()] * 5, reader={"SELECT 1": (["x"], [(1,)])})
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.CEILING_TEXT)]
    assert len(s.sql) == 4, "пятый запрос не выполнен"
    row = _logged(s.cursor)
    assert (row["outcome"], len(row["steps"])) == ("ceiling", 4)


@pytest.mark.asyncio
async def test_cost_ceiling_stops_before_tool_runs(stats, make_message, fake_bot, api):
    # 60 000 входных токенов по $2/1M — $0.12 одним вызовом, выше потолка $0.10.
    s = stats([_response(_use("run_sql", sql="SELECT 1"), tokens=60_000)])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.CEILING_TEXT)]
    assert s.sql == [] and len(s.model.calls) == 1
    row = _logged(s.cursor)
    assert row["outcome"] == "ceiling" and row["cost_usd"] > 0.10


@pytest.mark.asyncio
async def test_call_ceiling_is_seven_model_calls(stats, make_message, fake_bot, api):
    overview = _response(_use("player_overview", last_name="Nobody", team="", player_id=None))
    s = stats([overview] * 8, reader={"FROM rosters": (PLAYER_COLUMNS, [])})
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.CEILING_TEXT)]
    assert len(s.model.calls) == 7
    assert _logged(s.cursor)["outcome"] == "ceiling"


# ---------------------------------------------------------------------------
# Сбои
# ---------------------------------------------------------------------------

def _server_error() -> anthropic.InternalServerError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(500, request=request, headers={"request-id": "req_500"})
    return anthropic.InternalServerError("overloaded", response=response, body=None)


@pytest.mark.asyncio
async def test_api_failure_tells_user_to_use_commands_and_is_logged(stats, make_message, fake_bot, api):
    s = stats([_server_error()])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.ERROR_TEXT)]
    row = _logged(s.cursor)
    assert row["outcome"] == "error" and row["error"].startswith("InternalServerError")
    assert "req_500" in row["error"]


@pytest.mark.asyncio
async def test_reader_connection_failure_is_an_error_not_a_model_attempt(stats, make_message, fake_bot, api):
    s = stats([_sql()], reader_error=psycopg2.OperationalError("connection refused"))
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.ERROR_TEXT)]
    assert len(s.model.calls) == 1
    assert _logged(s.cursor)["error"].startswith("OperationalError")


@pytest.mark.asyncio
async def test_model_reply_without_tool_is_an_error(stats, make_message, fake_bot, api):
    s = stats([_response(SimpleNamespace(type="text", text="Просто текст"))])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.ERROR_TEXT)]
    assert "не вызвала инструмент" in _logged(s.cursor)["error"]


@pytest.mark.asyncio
async def test_reviewer_clarify_without_question_is_an_error(stats, make_message, fake_bot, api):
    s = stats([_finish(), _verdict("clarify", "  ")])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.ERROR_TEXT)]
    assert _logged(s.cursor)["outcome"] == "error"


# ---------------------------------------------------------------------------
# Окно продолжения диалога
# ---------------------------------------------------------------------------

def _past(outcome: str = "answer", question: str = "голы Овечкина",
          sql: str = "SELECT sum(goals) FROM players_season_stats", answer: str = "42 гола"):
    steps = [{"tool": "run_sql", "input": {"sql": sql}, "rows": 1, "error": None}]
    return (question, outcome, steps, answer)


@pytest.mark.asyncio
async def test_outside_window_history_is_not_sent(stats, make_message, fake_bot, api):
    """Окно закрыто (история пуста; границу окна на настоящей БД проверяет
    `TestAiStatsWindow` в tests/test_db_nhl.py): в запросе нет истории, продолжения нет."""
    s = stats([_finish("off_topic", "x", continuation=True)], history=[])
    await _ask(s, make_message, fake_bot, api)

    content = s.model.calls[0]["messages"][0]["content"]
    assert "<история>" not in content
    assert _logged(s.cursor)["continuation"] is False, "без истории продолжением быть нечему"


@pytest.mark.asyncio
async def test_inside_window_last_pairs_with_sql_are_sent_oldest_first(stats, make_message, fake_bot, api):
    history = [_past(question="третий", answer="a3", sql="SELECT 3"),
               _past(question="второй", answer="a2", sql="SELECT 2"),
               _past(question="первый", answer="a1", sql="SELECT 1")]
    s = stats([_finish("answer", "ок", continuation=True), _verdict()], history=history)
    await _ask(s, make_message, fake_bot, api, "а в прошлом сезоне?")

    content = s.model.calls[0]["messages"][0]["content"]
    assert content.index("Вопрос: первый") < content.index("Вопрос: второй") < content.index("Вопрос: третий")
    assert "SQL: SELECT 2" in content and "Ответ: a2" in content
    assert "<вопрос>\nа в прошлом сезоне?\n</вопрос>" in content
    assert "ответ на него" not in content
    assert _logged(s.cursor)["continuation"] is True
    history_queries = {p for q, p in s.cursor.executed if q.startswith("SELECT question")}
    assert history_queries == {(CHAT_ID, 30, 3)}, "окно 30 минут, три пары"


@pytest.mark.asyncio
async def test_model_marks_new_question_inside_window(stats, make_message, fake_bot, api):
    s = stats([_finish("off_topic", "x", continuation=False)], history=[_past()])
    await _ask(s, make_message, fake_bot, api)

    assert "<история>" in s.model.calls[0]["messages"][0]["content"]
    assert _logged(s.cursor)["continuation"] is False


@pytest.mark.asyncio
async def test_reply_to_clarification_is_forced_continuation(stats, make_message, fake_bot, api):
    s = stats(
        [_finish("off_topic", "x", continuation=False)],
        history=[_past("clarify", question="голы Овечкина", answer="Какой Овечкин?")],
    )
    await _ask(s, make_message, fake_bot, api, "Александр")

    assert "Предыдущий ответ был уточнением" in s.model.calls[0]["messages"][0]["content"]
    assert _logged(s.cursor)["continuation"] is True


# ---------------------------------------------------------------------------
# Белый список и подключение к маршрутизатору 81
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_outside_whitelist_gets_neither_tool_nor_stats(stats, make_message, fake_bot, api):
    s = stats([], allowed=False, history=[_past()],
              router=[_response(SimpleNamespace(type="text", text="Пока не умею"))])
    await _ask(s, make_message, fake_bot, api)

    (call,) = s.model.router_calls
    assert s.model.calls == []
    assert "stats_question" not in [t["name"] for t in call["tools"]]
    assert "stats_question" not in call["system"][0]["text"]
    assert not any(q.startswith("SELECT question") for q, _ in s.cursor.executed), "история 83 не читалась"
    assert not any(q.startswith("INSERT INTO ai_stats_log") for q, _ in s.cursor.executed)


@pytest.mark.asyncio
async def test_whitelisted_chat_router_gets_stats_question_and_hands_over(stats, make_message, fake_bot, api):
    s = stats([_finish("off_topic", "Ответ 83")])
    replies = await _ask(s, make_message, fake_bot, api, "сколько Овечкин забил Коламбусу")

    (router_call,), (stats_call,) = s.model.router_calls, s.model.calls
    tools = {t["name"]: t for t in router_call["tools"]}
    assert tools["stats_question"]["strict"] is True
    assert tools["stats_question"]["input_schema"]["properties"] == {}
    assert "stats_question" in router_call["system"][0]["text"]
    assert "run_sql" in [t["name"] for t in stats_call["tools"]]
    assert replies == [_as_reply("Ответ 83")]
    assert _logged(s.cursor)["question"] == "сколько Овечкин забил Коламбусу"


@pytest.mark.asyncio
async def test_chat_inside_window_skips_router(stats, make_message, fake_bot, api):
    s = stats([_finish("off_topic", "Продолжение")], history=[_past()])
    replies = await _ask(s, make_message, fake_bot, api, "а в прошлом сезоне?")

    (call,) = s.model.calls
    assert s.model.router_calls == [], "вопрос ушёл сразу в 83, не в маршрутизатор"
    assert "run_sql" in [t["name"] for t in call["tools"]]
    assert replies == [_as_reply("Продолжение")]


def test_router_request_for_other_chats_is_unchanged(stats, bot_module):
    stats([])  # подменяет БД: список клубов для enum
    ai_router = bot_module("ai_router")
    plain = ai_router.request_params("вопрос")
    assert plain["system"][0]["text"] == ai_router.SYSTEM_PROMPT
    assert [t["name"] for t in plain["tools"]] == [
        "open_command", "digest_for_date", "team_profile", "leaders", "country_players", "advanced_metric",
    ]


@pytest.mark.asyncio
async def test_daily_limit_covers_whitelisted_chat(stats, make_message, fake_bot, api):
    s = stats([], asked=5)
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.bot.ai_router.LIMIT_TEXT)]
    assert s.model.calls == []


# ---------------------------------------------------------------------------
# Обзор игрока
# ---------------------------------------------------------------------------

PLAYER_COLUMNS = ["player_id", "name", "lastname", "position", "team"]
PLAYER = (PLAYER_COLUMNS, [(8471214, "Kirill Kaprizov", "Kaprizov", "L", "MIN")])


@pytest.mark.asyncio
async def test_player_overview_collects_season_form_and_point_streak(stats, make_message, fake_bot, api):
    s = stats(
        [_response(_use("player_overview", last_name="Kaprizov", team="", player_id=None)),
         _finish("answer", "Обзор"), _verdict()],
        reader={
            "FROM rosters": PLAYER,
            "FROM players_season_stats": (["season_id", "games", "points"], [(20262027, 8, 9), (20252026, 82, 90)]),
            "FROM game_player_stats": (
                ["day", "goals", "assists", "points", "shots"],
                [("2026-10-27", 1, 1, 2, 4), ("2026-10-25", 0, 1, 1, 3), ("2026-10-23", 0, 0, 0, 2),
                 ("2026-10-21", 2, 0, 2, 5)],
            ),
        },
    )
    await _ask(s, make_message, fake_bot, api, "как играет Капризов")

    (result,) = _tool_results(s.model.calls[1])
    payload = json.loads(result["content"])
    assert payload["player"]["name"] == "Kirill Kaprizov"
    assert [r["season_id"] for r in payload["seasons"]] == [20262027, 20252026]
    assert len(payload["last_10_games"]) == 4 and payload["point_streak_games"] == 2
    assert [c["role"] for c in _logged(s.cursor)["calls"]] == ["answer", "answer", "review"]
    assert "Обзор игрока" in s.model.calls[2]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_player_overview_goalie_has_own_set(stats, make_message, fake_bot, api):
    s = stats(
        [_response(_use("player_overview", last_name="Shesterkin", team="NYR", player_id=None)), _finish("off_topic", "x")],
        reader={
            "FROM rosters": (PLAYER_COLUMNS, [(8478048, "Igor Shesterkin", "Shesterkin", "G", "NYR"),
                                              (1, "Other Shesterkin", "Shesterkin", "D", "BOS")]),
            "FROM goalies_season_stats": (["season_id", "save_percentage"], [(20262027, 91.2)]),
            "FROM game_goalie_stats": (["day", "shots_against", "saves", "goals_against"], [("2026-10-27", 30, 28, 2)]),
        },
    )
    await _ask(s, make_message, fake_bot, api)

    payload = json.loads(_tool_results(s.model.calls[1])[0]["content"])
    assert "point_streak_games" not in payload and payload["last_10_games"][0]["saves"] == 28
    assert payload["player"]["team"] == "NYR", "клуб сужает однофамильцев"


@pytest.mark.asyncio
async def test_player_overview_returns_candidates_for_namesakes(stats, make_message, fake_bot, api):
    s = stats(
        [_response(_use("player_overview", last_name="Hughes", team="NJD", player_id=None)),
         _response(_use("player_overview", last_name="Hughes", team="NJD", player_id=2)),
         _finish("off_topic", "x")],
        reader={
            "FROM rosters": lambda params: (PLAYER_COLUMNS, [
                row for row in [(1, "Jack Hughes", "Hughes", "C", "NJD"), (2, "Luke Hughes", "Hughes", "D", "NJD")]
                if params[1] is None or row[0] == params[1]
            ]),
            "FROM players_season_stats": (["season_id", "points"], [(20262027, 3)]),
            "FROM game_player_stats": (["day", "goals", "assists", "points", "shots"], []),
        },
    )
    await _ask(s, make_message, fake_bot, api)

    first, second = (json.loads(r["content"]) for r in _tool_results(s.model.calls[2]))
    assert [c["name"] for c in first["candidates"]] == ["Jack Hughes", "Luke Hughes"]
    assert [c["player_id"] for c in first["candidates"]] == [1, 2]
    assert "lastname" not in first["candidates"][0]
    assert second["player"]["name"] == "Luke Hughes", "player_id выбирает однофамильца одного клуба"


@pytest.mark.parametrize(("stored", "asked"), [
    ("Stützle", "Stutzle"), ("Slafkovský", "slafkovsky"), ("O'Reilly", "OReilly"),
    ("Lafrenière", "LAFRENIERE"), ("Kaprizov", "Kaprizov"),
])
def test_last_name_match_ignores_diacritics_apostrophes_and_case(bot_module, stored, asked):
    ai_stats = bot_module("ai_stats")
    assert ai_stats._fold(stored) == ai_stats._fold(asked)
    assert ai_stats._fold("Hughes") != ai_stats._fold("Tkachuk")


# ---------------------------------------------------------------------------
# Промпты, схема, конфиг
# ---------------------------------------------------------------------------

def test_system_prompt_is_stable_cached_and_has_only_data_tables(bot_module):
    ai_stats = bot_module("ai_stats")
    (block,) = ai_stats._system("stats_answer.md")
    text = block["text"]

    assert block["cache_control"] == {"type": "ephemeral"}
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text), "дата в префиксе ломает кэш"
    for table in ai_stats.AI_TABLES:
        assert f"CREATE TABLE {table}(" in text
    for private in ("bot_subscriptions", "ai_questions", "ai_stats_log", "prediction_log", "game_predictions"):
        assert private not in text
    assert "ai_stats" not in ai_stats._system("stats_reviewer.md")[0]["text"].split("## Схема таблиц")[1]
    assert "2021/22" in text and "плей-офф" in text


def test_every_tool_is_strict(bot_module):
    ai_stats = bot_module("ai_stats")
    for tool in ai_stats._tools() + ai_stats._verdict_tools():
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False


def test_chat_ids_env_parsing(bot_module, monkeypatch):
    config = bot_module("config")
    monkeypatch.setenv("AI_STATS_CHAT_IDS", "100, -200 ,")
    assert config._env_chat_ids("AI_STATS_CHAT_IDS") == frozenset({100, -200})
    monkeypatch.setenv("AI_STATS_CHAT_IDS", "")
    assert config._env_chat_ids("AI_STATS_CHAT_IDS") == frozenset()
    monkeypatch.setenv("AI_STATS_CHAT_IDS", "100,abc")
    with pytest.raises(ValueError, match="AI_STATS_CHAT_IDS"):
        config._env_chat_ids("AI_STATS_CHAT_IDS")


# ---------------------------------------------------------------------------
# Один SELECT, потолок стоимости, журнал при сбоях, настройки вызова
# ---------------------------------------------------------------------------

def test_model_sql_is_wrapped_into_a_single_select(stats):
    s = stats([], reader={"FROM (": (["n"], [(1,)])})
    s.ai_stats.run_readonly("SELECT 1 -- комментарий\n;  ")
    s.ai_stats.run_readonly("WITH x AS (SELECT 2) SELECT * FROM x")

    first, second = s.sql
    assert first == "SELECT * FROM (\nSELECT 1 -- комментарий\n) AS q LIMIT 51"
    assert second == "SELECT * FROM (\nWITH x AS (SELECT 2) SELECT * FROM x\n) AS q LIMIT 51"


@pytest.mark.asyncio
async def test_calls_use_long_timeout_and_room_for_thinking(stats, make_message, fake_bot, api):
    s = stats([_finish("off_topic", "x")])
    await _ask(s, make_message, fake_bot, api)

    assert s.model.options == [{"timeout": 60.0}]
    assert s.model.calls[0]["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_projected_cost_stops_before_a_call_that_would_break_ceiling(stats, make_message, fake_bot, api):
    # Первый вызов $0.06 (30 000 входных токенов): ещё один такой же пробил бы $0.10.
    s = stats([_response(_use("run_sql", sql="SELECT 1"), tokens=30_000), _finish("off_topic", "x")],
              reader={"SELECT 1": (["x"], [(1,)])})
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.CEILING_TEXT)]
    assert len(s.model.calls) == 1 and len(s.sql) == 1
    row = _logged(s.cursor)
    assert row["outcome"] == "ceiling" and row["cost_usd"] < 0.10


@pytest.mark.asyncio
async def test_ready_finish_is_not_thrown_away_when_it_crosses_the_ceiling(stats, make_message, fake_bot, api):
    s = stats([_response(_use("finish", kind="no_data", continuation=False, text="Нет"), tokens=60_000)])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply("Нет")]
    assert _logged(s.cursor)["outcome"] == "no_data"


@pytest.mark.asyncio
async def test_draft_is_logged_when_review_hits_the_ceiling(stats, make_message, fake_bot, api):
    s = stats([_response(_use("finish", kind="answer", continuation=False, text="Черновик 42"), tokens=60_000)])
    replies = await _ask(s, make_message, fake_bot, api)

    assert replies == [_as_reply(s.ai_stats.CEILING_TEXT)]
    row = _logged(s.cursor)
    assert (row["outcome"], row["draft"]) == ("ceiling", "Черновик 42")


@pytest.mark.asyncio
async def test_unexpected_exception_is_logged_and_raised(stats, make_message, fake_bot):
    # Ответ без обязательного поля: наш код упадёт KeyError — громко, но строка журнала останется.
    s = stats([_response(_use("finish", kind="answer", continuation=False))])
    with pytest.raises(KeyError):
        await s.bot.on_free_text(make_message("вопрос"), fake_bot)

    row = _logged(s.cursor)
    assert row["outcome"] == "error" and row["error"].startswith("KeyError")


def test_semicolon_inside_query_is_a_model_error_and_never_reaches_the_db(stats):
    s = stats([])
    attack = "SELECT 1) AS x; SET statement_timeout=0; SELECT pg_sleep(7) FROM (SELECT 1"
    outcome = s.ai_stats.run_readonly(attack)

    assert outcome.error and "точка с запятой" in outcome.text and outcome.rows is None
    assert s.sql == []


def test_watchdog_cancels_a_hung_query(stats, monkeypatch):
    s = stats([], reader={"FROM (": (["n"], [])})
    monkeypatch.setattr(s.ai_stats, "STATEMENT_TIMEOUT_SEC", -0.95)  # таймер на 0.05 с
    hang = psycopg2.errors.QueryCanceled("canceling statement due to user request")

    def execute(self, sql, params=None):
        self.executed.append(sql)
        assert self.cancelled.wait(5), "таймер не отменил запрос"
        raise hang

    monkeypatch.setattr(FakeReaderCursor, "execute", execute)
    outcome = s.ai_stats.run_readonly("SELECT pg_sleep(30)")

    assert outcome.rows is None and "user request" in outcome.error
