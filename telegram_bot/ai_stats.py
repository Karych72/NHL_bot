"""Ответы ИИ на статистические вопросы по нашей БД (Задача 83).

Часть `telegram_bot/`: `bot.py` отдаёт сюда вопрос чата из белого списка (`config.AI_STATS_CHAT_IDS`),
для которого нет экрана. Модель ведёт диалог инструментами — сама пишет SELECT (`run_sql`) или берёт
готовую выборку по игроку (`player_overview`); SQL идёт под ролью `nhl_ai_reader` (только чтение,
миграция 0015). Черновик проверяет отдельный вызов-ревьюер; журнал — `ai_stats_log`.
Промпты и аннотация схемы — файлы в `telegram_bot/prompts/`.
"""

import asyncio
import functools
import json
import logging
import re
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import anthropic
import psycopg2
from psycopg2.errors import QueryCanceled
from psycopg2.extras import Json

import ai_router
import config
from bot_messages import MSK
from database import fetch_all, get_connection

logger = logging.getLogger(__name__)

# Таблицы, доступные роли `nhl_ai_reader`; тот же список в data_tables/ai_reader_grants.sql
# (сверяет tests/test_db_nhl.py) и в схеме, которую видит модель.
AI_TABLES = (
    "games", "teams", "teams_stats", "rosters", "players_season_stats",
    "players_advanced_stats", "players_shot_types", "goalies_season_stats",
    "game_team_stats", "game_player_stats", "game_goalie_stats", "all_goals",
    "game_three_stars", "shot_attempts", "scheduled_games",
)

# Решение человека 2026-10-28: потолки на один вопрос и окно продолжения диалога.
SQL_ATTEMPTS = 4
CALL_CEILING = 7
COST_CEILING = 0.10
WINDOW_MINUTES = 30
HISTORY_PAIRS = 3
ROW_LIMIT = 50
EFFORT = "medium"

ERROR_TEXT = "Не удалось ответить, попробуйте командой: /help"
CEILING_TEXT = "Не удалось ответить, уточните вопрос"

PROMPTS = Path(__file__).parent / "prompts"
DATA_TABLES = Path(__file__).resolve().parent.parent / "data_tables"

_OVERVIEW_PLAYERS_SQL = (
    "SELECT r.player_id, r.name, r.position, t.abbreviation AS team FROM rosters r "
    "LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = r.season_id "
    "WHERE r.season_id = %s AND lower(r.lastname) = lower(%s) "
    "AND (%s = '' OR upper(t.abbreviation) = upper(%s)) ORDER BY r.name"
)
_OVERVIEW_SKATER_SEASONS_SQL = (
    "SELECT season_id, games, goals, assists, points, power_play_points, game_winning_goals, "
    "shots, shot_pct, plus_minus, time_on_ice_per_game, "
    "round(points::numeric / NULLIF(games, 0), 2) AS points_per_game, "
    "round(shots::numeric / NULLIF(games, 0), 2) AS shots_per_game "
    "FROM players_season_stats WHERE player_id = %s AND season_id IN (%s, %s) "
    "ORDER BY season_id DESC"
)
_OVERVIEW_SKATER_GAMES_SQL = (
    "SELECT g.day::text AS day, gps.goals, gps.assists, gps.goals + gps.assists AS points, "
    "gps.shots FROM game_player_stats gps JOIN games g ON g.game_id = gps.game_id "
    "WHERE gps.player_id = %s AND g.season_id = %s ORDER BY g.day DESC, g.game_id DESC"
)
_OVERVIEW_GOALIE_SEASONS_SQL = (
    "SELECT season_id, games, games_started, wins, losses, ot, save_percentage, "
    "goal_against_average, shutouts, saves, shots_against FROM goalies_season_stats "
    "WHERE player_id = %s AND season_id IN (%s, %s) ORDER BY season_id DESC"
)
_OVERVIEW_GOALIE_GAMES_SQL = (
    "SELECT g.day::text AS day, ggs.shots AS shots_against, ggs.saves, "
    "ggs.shots - ggs.saves AS goals_against FROM game_goalie_stats ggs "
    "JOIN games g ON g.game_id = ggs.game_id WHERE ggs.player_id = %s AND g.season_id = %s "
    "ORDER BY g.day DESC, g.game_id DESC LIMIT 10"
)
_HISTORY_SQL = (
    "SELECT question, outcome, steps, answer, "
    "(asked_at + latency_ms * interval '1 millisecond') > now() - %s * interval '1 minute' "
    "AS fresh FROM ai_stats_log WHERE chat_id = %s AND outcome <> 'error' "
    "ORDER BY asked_at DESC LIMIT %s"
)


class _Ceiling(Exception):
    """Потолок попыток SQL, вызовов модели или стоимости на вопрос."""


class _ProtocolError(Exception):
    """Модель нарушила протокол (не вызвала инструмент, пустой вердикт): громкий сбой."""


class _Result(NamedTuple):
    """Итог диалога: исход, текст пользователю и поля журнала."""

    outcome: str
    text: str
    continuation: bool = False
    draft: Optional[str] = None
    verdict: Optional[str] = None
    verdict_reason: Optional[str] = None


class _Run:
    """Состояние одного вопроса: шаги, вызовы модели, накопленная стоимость."""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.evidence: List[str] = []
        self.cost = 0.0
        self.sql_attempts = 0


def _tools() -> List[Dict[str, Any]]:
    """Инструменты диалога; порядок и текст стабильны ради кэша префикса."""
    return [
        ai_router.tool_schema(
            "run_sql", "Выполнить один SELECT к базе (read-only, до 50 строк).",
            {"sql": {"type": "string"}},
        ),
        ai_router.tool_schema(
            "player_overview",
            "Готовая выборка по игроку текущего сезона. team — аббревиатура клуба для "
            "однофамильцев, иначе пустая строка.",
            {"last_name": {"type": "string"}, "team": {"type": "string"}},
        ),
        ai_router.tool_schema(
            "finish", "Последний ход: ответ пользователю.",
            {
                "kind": {"type": "string", "enum": ["answer", "no_data", "off_topic", "clarify"]},
                "continuation": {"type": "boolean"},
                "text": {"type": "string"},
            },
        ),
    ]


def _verdict_tools() -> List[Dict[str, Any]]:
    return [ai_router.tool_schema(
        "verdict", "Решение ревьюера: ok или clarify.",
        {
            "verdict": {"type": "string", "enum": ["ok", "clarify"]},
            "clarify_question": {"type": "string"},
            "reason": {"type": "string"},
        },
    )]


@functools.lru_cache(maxsize=1)
def _schema() -> str:
    """DDL разрешённых таблиц из `data_tables/t.*.sql` без комментариев — схема для промпта."""
    tables = []
    for name in AI_TABLES:
        ddl = re.search(r"CREATE TABLE.*?\n\);", (DATA_TABLES / f"t.{name}.sql").read_text(), re.S)
        if ddl is None:
            raise RuntimeError(f"В data_tables/t.{name}.sql нет CREATE TABLE")
        tables.append("\n".join(
            line for line in ddl.group(0).splitlines() if not line.strip().startswith("--")
        ))
    return "\n\n".join(tables)


@functools.lru_cache(maxsize=2)
def _system(prompt_file: str) -> List[Dict[str, Any]]:
    """Системный блок с `cache_control`: промпт роли + аннотация + схема. Без даты — префикс
    кэшируется; сегодняшняя дата идёт в сообщении пользователя."""
    text = "\n\n".join([
        (PROMPTS / prompt_file).read_text(),
        (PROMPTS / "stats_schema_notes.md").read_text(),
        "## Схема таблиц\n\n" + _schema(),
    ])
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _reader_connection():
    """Соединение под `nhl_ai_reader`: таймаут и «только чтение» продублированы в параметрах
    (на роли они тоже заданы миграцией 0015)."""
    return psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, user=config.AI_PG_USER,
        database=config.PG_DATABASE,
        options="-c statement_timeout=5000 -c default_transaction_read_only=on",
    )


class _SqlOutcome(NamedTuple):
    """Результат `run_sql` для модели: текст, число строк (None при ошибке), ошибка SQL."""

    text: str
    rows: Optional[int]
    error: Optional[str]


def run_readonly(sql: str) -> _SqlOutcome:
    """Выполнить SQL модели ролью только на чтение; до `ROW_LIMIT` строк.

    Ошибка самого запроса (синтаксис, нет колонки, нет прав, таймаут `QueryCanceled`) —
    не сбой: текст ошибки уходит модели, это её попытка. Сбой соединения (в том числе
    `OperationalError` посреди запроса) поднимается выше.
    """
    with closing(_reader_connection()) as conn, conn.cursor() as cur:
        try:
            cur.execute(sql)
            fetched = cur.fetchmany(ROW_LIMIT + 1)
            columns = [d[0] for d in cur.description]
        except QueryCanceled as exc:
            return _SqlOutcome(f"Ошибка SQL: {str(exc).strip()}", None, str(exc).strip())
        except psycopg2.OperationalError:
            raise
        except psycopg2.Error as exc:
            return _SqlOutcome(f"Ошибка SQL: {str(exc).strip()}", None, str(exc).strip())
    rows = [list(r) for r in fetched[:ROW_LIMIT]]
    payload: Dict[str, Any] = {"columns": columns, "rows": rows, "truncated": len(fetched) > ROW_LIMIT}
    if not rows:
        payload["note"] = "запрос выполнен, строк нет"
    return _SqlOutcome(_dump(payload), len(rows), None)


def _dicts(cur) -> List[Dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def player_overview(last_name: str, team: str) -> Tuple[str, str]:
    """Фиксированная выборка по игроку текущего сезона (маршрут О, обзор игрока).

    Находит игрока в `rosters` по фамилии (и клубу для однофамильцев); для полевого — сезон и
    прошлый сезон, последние 10 игр, серия игр с очками; для вратаря — свой набор. Нашлось
    несколько — возвращает кандидатов модели. Все запросы идут под read-only ролью.

    Returns:
        Пара (JSON для модели, короткая сводка для журнала).
    """
    if config.SEASON_ID is None:
        raise RuntimeError("SEASON_ID не задан: обзор игрока не собрать")
    season, previous = config.SEASON_ID, config.SEASON_ID - 10001
    with closing(_reader_connection()) as conn, conn.cursor() as cur:
        cur.execute(_OVERVIEW_PLAYERS_SQL, (season, last_name.strip(), team.strip(), team.strip()))
        players = _dicts(cur)
        if len(players) != 1:
            if players:
                return _dump({"candidates": players}), f"кандидатов: {len(players)}"
            return (
                _dump({"error": "в текущем сезоне такого игрока нет; для прошлых сезонов "
                       "используй run_sql"}),
                "не найден",
            )
        player = players[0]
        goalie = player["position"] == "G"
        cur.execute(
            _OVERVIEW_GOALIE_SEASONS_SQL if goalie else _OVERVIEW_SKATER_SEASONS_SQL,
            (player["player_id"], season, previous),
        )
        payload: Dict[str, Any] = {"player": player, "seasons": _dicts(cur)}
        if goalie:
            cur.execute(_OVERVIEW_GOALIE_GAMES_SQL, (player["player_id"], season))
            payload["last_10_games"] = _dicts(cur)
        else:
            cur.execute(_OVERVIEW_SKATER_GAMES_SQL, (player["player_id"], season))
            games = _dicts(cur)
            payload["last_10_games"] = games[:10]
            streak = 0
            for game in games:
                if game["points"] <= 0:
                    break
                streak += 1
            payload["point_streak_games"] = streak
    return _dump(payload), f"игрок {player['name']}, игр в сезоне: {len(payload['last_10_games'])}+"


def recent_pairs(chat_id: int) -> List[Dict[str, Any]]:
    """Последние `HISTORY_PAIRS` ответов 83 этому чату, от старых к новым; пусто, если с
    последнего ответа прошло больше `WINDOW_MINUTES` (вне окна история не передаётся).
    Строки с `outcome = 'error'` не в счёт: ответа пользователь не получил."""
    rows = fetch_all(
        _HISTORY_SQL, (WINDOW_MINUTES, chat_id, HISTORY_PAIRS),
        columns=["question", "outcome", "steps", "answer", "fresh"],
    )
    if not rows["count_rows"] or not rows["fresh"][0]:
        return []
    pairs = [
        {"question": q, "outcome": o, "steps": s, "answer": a}
        for q, o, s, a in zip(rows["question"], rows["outcome"], rows["steps"], rows["answer"])
    ]
    return pairs[::-1]


def in_window(chat_id: int) -> bool:
    """Был ли у чата ответ 83 за последние `WINDOW_MINUTES`: тогда сообщение сразу идёт в
    `answer` (продолжение или новый вопрос решает модель), маршрутизатор 81 не зовётся."""
    return bool(recent_pairs(chat_id))


def _user_text(question: str, history: List[Dict[str, Any]]) -> str:
    """Первое сообщение диалога: дата, история окна (вопрос, SQL, ответ) и вопрос."""
    parts = [f"Сегодня {datetime.now(MSK).date().isoformat()} (МСК)."]
    if history:
        blocks = []
        for pair in history:
            sql = "\n".join(s["input"]["sql"] for s in pair["steps"] if s["tool"] == "run_sql")
            blocks.append(f"Вопрос: {pair['question']}\nSQL: {sql or '—'}\nОтвет: {pair['answer']}")
        parts.append("<история>\n" + "\n\n".join(blocks) + "\n</история>")
        if history[-1]["outcome"] == "clarify":
            parts.append("Предыдущий ответ был уточнением: сообщение ниже — ответ на него.")
    parts.append(f"<вопрос>\n{question}\n</вопрос>")
    return "\n".join(parts)


async def _call(run: _Run, role: str, system: Any, tools: List[Dict[str, Any]],
                messages: List[Dict[str, Any]]) -> Any:
    """Один вызов модели с учётом потолков; расход попадает в `run.calls`.

    Raises:
        _Ceiling: вызовов уже `CALL_CEILING` или стоимость вопроса дошла до `COST_CEILING`.
    """
    if len(run.calls) >= CALL_CEILING or run.cost >= COST_CEILING:
        raise _Ceiling
    # Принудительный tool_choice (any/tool) Sonnet 5.5 отвергает: инструмент требует промпт,
    # а его отсутствие — _ProtocolError.
    params: Dict[str, Any] = {
        "model": ai_router.MODEL, "max_tokens": 4096, "output_config": {"effort": EFFORT},
        "system": system, "tools": tools, "messages": messages,
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    }
    response = await ai_router.client().messages.create(**params)
    usage = response.usage
    cost = ai_router.cost_usd(usage)
    run.cost += cost
    run.calls.append({
        "role": role, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_input_tokens or 0,
        "cache_write_tokens": usage.cache_creation_input_tokens or 0,
        "cost_usd": round(cost, 6), "request_id": response._request_id,
    })
    return response


def _tool_use(response: Any) -> Any:
    """Блок `tool_use` ответа; без него или при обрыве (`max_tokens`) — `_ProtocolError`."""
    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None or response.stop_reason != "tool_use":
        raise _ProtocolError(f"модель не вызвала инструмент, stop_reason={response.stop_reason}")
    return block


async def _execute(run: _Run, tool: Any) -> Tuple[str, bool]:
    """Выполнить `run_sql` или `player_overview`; вернуть (текст для модели, это ошибка).

    Raises:
        _Ceiling: пятая попытка `run_sql`.
    """
    if tool.name == "run_sql":
        if run.sql_attempts >= SQL_ATTEMPTS:
            raise _Ceiling
        run.sql_attempts += 1
        sql = tool.input["sql"]
        outcome = await asyncio.to_thread(run_readonly, sql)
        run.steps.append({"tool": "run_sql", "input": {"sql": sql},
                          "rows": outcome.rows, "error": outcome.error})
        run.evidence.append(f"SQL: {sql}\nРезультат: {outcome.text}")
        return outcome.text, outcome.error is not None
    if tool.name == "player_overview":
        text, summary = await asyncio.to_thread(
            player_overview, tool.input["last_name"], tool.input["team"]
        )
        run.steps.append({"tool": "player_overview", "input": dict(tool.input), "result": summary})
        run.evidence.append(f"Обзор игрока {dict(tool.input)}: {text}")
        return text, False
    raise _ProtocolError(f"неизвестный инструмент {tool.name}")


async def _review(run: _Run, user_text: str, draft: str) -> Dict[str, Any]:
    """Вызов ревьюера: вопрос с историей, выполненное и черновик → вердикт `ok`/`clarify`."""
    content = (
        f"{user_text}\n\n<результат>\n" + "\n\n".join(run.evidence)
        + f"\n</результат>\n\n<черновик>\n{draft}\n</черновик>"
    )
    response = await _call(
        run, "review", _system("stats_reviewer.md"), _verdict_tools(),
        [{"role": "user", "content": content}],
    )
    verdict = _tool_use(response).input
    if verdict["verdict"] == "clarify" and not verdict["clarify_question"].strip():
        raise _ProtocolError("ревьюер выбрал clarify без вопроса")
    return dict(verdict)


async def _dialog(run: _Run, user_text: str, has_history: bool) -> _Result:
    """Цикл «модель → инструмент» до `finish`; для `answer` — ревью черновика."""
    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_text}]
    system = _system("stats_answer.md")
    while True:
        response = await _call(run, "answer", system, _tools(), messages)
        if run.cost > COST_CEILING:
            raise _Ceiling
        tool = _tool_use(response)
        if tool.name == "finish":
            break
        result, is_error = await _execute(run, tool)
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tool.id, "content": result, "is_error": is_error,
        }]})
    kind, text = tool.input["kind"], tool.input["text"]
    continuation = has_history and tool.input["continuation"]
    if kind != "answer":
        return _Result(kind, text, continuation)
    verdict = await _review(run, user_text, text)
    if verdict["verdict"] == "ok":
        return _Result("answer", text, continuation, text, "ok", verdict["reason"])
    return _Result(
        "clarify", verdict["clarify_question"], continuation, text, "clarify", verdict["reason"]
    )


def _log(chat_id: int, question: str, run: _Run, started: float, result: _Result,
         continuation: bool, error: Optional[str] = None) -> None:
    """Строка журнала `ai_stats_log` на вопрос."""
    columns = {
        "chat_id": chat_id, "question": question, "continuation": continuation,
        "outcome": result.outcome, "steps": Json(run.steps), "draft": result.draft,
        "verdict": result.verdict, "verdict_reason": result.verdict_reason,
        "answer": result.text, "calls": Json(run.calls), "cost_usd": round(run.cost, 6),
        "latency_ms": int((time.monotonic() - started) * 1000), "error": error,
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO ai_stats_log ({', '.join(columns)}) "
                f"VALUES ({', '.join(['%s'] * len(columns))})",
                tuple(columns.values()),
            )
        conn.commit()


async def answer(chat_id: int, question: str) -> str:
    """Ответить на вопрос о статистике: текст для пользователя.

    Зачем: вопросы без готового экрана (прошлые сезоны, игрок против команды, личные встречи).
    Внутри окна `WINDOW_MINUTES` после прошлого ответа в запрос идут последние
    `HISTORY_PAIRS` пар «вопрос–ответ», продолжение это или новый вопрос решает модель; ответ на
    уточнение считается продолжением всегда. Потолки (`SQL_ATTEMPTS`, `CALL_CEILING`,
    `COST_CEILING`) — `CEILING_TEXT`; сбой API или БД — `ERROR_TEXT`, ошибка в лог и журнал.

    Аргументы:
        chat_id: чат из белого списка.
        question: текст сообщения как есть.
    """
    started = time.monotonic()
    history = recent_pairs(chat_id)
    after_clarify = bool(history) and history[-1]["outcome"] == "clarify"
    run = _Run()
    error = None
    try:
        result = await _dialog(run, _user_text(question, history), bool(history))
    except _Ceiling:
        result = _Result("ceiling", CEILING_TEXT)
        logger.warning("Потолок ответа 83: chat_id=%s, SQL=%s, вызовов=%s, $%.4f",
                       chat_id, run.sql_attempts, len(run.calls), run.cost)
    except (anthropic.APIError, psycopg2.Error, _ProtocolError) as exc:
        request_id = exc.request_id if isinstance(exc, anthropic.APIStatusError) else None
        logger.exception("Ошибка ответа 83, chat_id=%s, request_id=%s", chat_id, request_id)
        error = f"{type(exc).__name__}: {exc}" + (f" request_id={request_id}" if request_id else "")
        result = _Result("error", ERROR_TEXT)
    _log(chat_id, question, run, started, result, result.continuation or after_clarify, error)
    return result.text
