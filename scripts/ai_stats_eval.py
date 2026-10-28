"""Контрольный прогон ответов на статистические вопросы (Задача 83) против настоящих API и БД.

Ручной инструмент приёмки, не часть `make ci-local`: стоит денег, нужны `ANTHROPIC_API_KEY` и БД
с данными и миграцией 0015. Каждый вопрос `scripts/ai_stats_eval.jsonl` идёт через настоящий
`ai_stats.answer` (SQL под ролью `nhl_ai_reader`, ревьюер, журнал `ai_stats_log` служебного чата).

Ожидание вопроса: `kind` — допустимые исходы; для `answer` — `expect`: числа и слова (посчитаны
вручную SQL-ом по БД на 2026-10-28, вопросы по полным сезонам) или `expect_sql` — запрос, которым
скрипт считает правильные числа по текущей БД в момент прогона (обзоры текущего сезона).
`after` — продолжение диалога: журнал чата не чистится. `reviewer_probe` — вопрос, на котором
модель легко ответит сама, а уточнить должен ревьюер.

Оценка ответа по набору чисел, а не по вхождению. Все ожидаемые числа и слова `expect` должны
быть в ответе, иначе он неверный; если он при этом прошёл ревью — это «неверная цифра в ответе,
прошедшем ревью», порог требует нуля таких. Каждое число ответа, кроме рамочных (годы, сезоны,
даты, «последних N», числа из вопроса), которого нет в `expect` и в результате SQL/обзора,
который модель реально получила (с допуском на округление), печатается отдельно как «проверить
вручную» (выдумка или арифметика модели) и в порог не входит.

Запуск: `make ai-stats-eval`. Код выхода 1 — верных меньше 90% или есть неверная цифра в ответе,
прошедшем ревью.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, List, Tuple

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import ai_stats  # noqa: E402
import config  # noqa: E402
from database import get_connection  # noqa: E402

EVAL_SET = Path(__file__).with_suffix(".jsonl")
EVAL_CHAT = -999000083
MIN_ACCURACY = 0.9

# Рамочные числа: даты, сезоны, годы, «последних N игр».
_FRAME = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{4}[/\-–]\d{2,4}|(?<!\d)(?:19|20)\d{2}(?!\d)|(?<=последних )\d+|(?<=последние )\d+",
    re.I,
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Результаты, которые модель получила за вопрос: подмена `ai_stats.run_readonly` и
# `player_overview` с записью выдачи (`_execute` берёт их из модуля при каждом вызове).
captured: List[str] = []
_run_readonly, _player_overview = ai_stats.run_readonly, ai_stats.player_overview


def _capturing_run_readonly(sql: str):
    outcome = _run_readonly(sql)
    captured.append(outcome.text)
    return outcome


def _capturing_player_overview(*args: Any):
    text, summary = _player_overview(*args)
    captured.append(text)
    return text, summary


ai_stats.run_readonly = _capturing_run_readonly  # type: ignore[assignment]
ai_stats.player_overview = _capturing_player_overview  # type: ignore[assignment]


def _plain(text: str) -> str:
    """Текст без рамочных чисел; запятая в дроби = точка."""
    return re.sub(r"(?<=\d),(?=\d)", ".", _FRAME.sub(" ", text))


def _decimals(raw: str) -> int:
    return len(raw.split(".")[1]) if "." in raw else 0


def close(value: float, other: float, decimals: int) -> bool:
    """Совпадают ли числа с точностью до округления до *decimals* знаков."""
    return abs(value - other) <= 0.5 * 10 ** -decimals + 1e-9


def judge(text: str, question: str, wants: list, evidence: List[str]) -> Tuple[List[str], List[str]]:
    """Оценка ответа: (неверное, проверить вручную).

    Неверное — нет ожидаемого слова или числа из *wants* (слова — подстрока без учёта регистра).
    Проверить вручную — число, которого нет ни в *wants*, ни в результатах SQL, ни в вопросе:
    это может быть выдумка, а может быть арифметика модели (разность, доля), без человека не
    различить.
    """
    problems: List[str] = []
    manual: List[str] = []
    got = _NUMBER.findall(_plain(text))
    expected_numbers: List[float] = []
    for item in wants:
        alts = item if isinstance(item, list) else [item]
        if all(_NUMBER.fullmatch(a) for a in alts):
            expected_numbers.extend(float(a) for a in alts)
            if not any(close(float(g), float(a), _decimals(a)) for g in got for a in alts):
                problems.append(f"нет ожидаемого числа {alts}")
        elif not any(a.lower() in text.lower() for a in alts):
            problems.append(f"нет ожидаемого {alts}")
    seen = [float(n) for e in evidence for n in _NUMBER.findall(_plain(e))]
    allowed = expected_numbers + seen + [float(n) for n in _NUMBER.findall(_plain(question))]
    for raw in got:
        if not any(close(float(raw), a, _decimals(raw)) for a in allowed):
            manual.append(f"число {raw} не из результата SQL")
    return problems, manual


def expected(case: dict) -> list:
    """Ожидания вопроса: `expect` или значения `expect_sql`, посчитанные по БД сейчас."""
    if "expect_sql" not in case:
        return case.get("expect", [])
    with psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, user=config.PG_USER, database=config.PG_DATABASE
    ) as conn, conn.cursor() as cur:
        cur.execute(case["expect_sql"].format(season=config.SEASON_ID))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"expect_sql вопроса {case['id']} ничего не вернул")
    return [str(v) for v in row]


def clear_log() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_stats_log WHERE chat_id = %s", (EVAL_CHAT,))
        conn.commit()


def last_row() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, draft, verdict, cost_usd, latency_ms, steps FROM ai_stats_log "
                "WHERE chat_id = %s ORDER BY id DESC LIMIT 1", (EVAL_CHAT,),
            )
            columns = [d[0] for d in cur.description]
            return dict(zip(columns, cur.fetchone()))


async def main() -> int:
    cases = [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines()]
    passed = wrong_passed = caught = manual_total = 0
    clarify_model = clarify_reviewer = probes = probes_caught = 0
    costs, latencies = [], []
    clear_log()
    try:
        for case in cases:
            if "after" not in case:
                clear_log()
            captured.clear()
            text = await ai_stats.answer(EVAL_CHAT, case["q"], ai_stats.recent_pairs(EVAL_CHAT))
            row = last_row()
            kinds = case["kind"] if isinstance(case["kind"], list) else [case["kind"]]
            costs.append(float(row["cost_usd"]))
            latencies.append(row["latency_ms"])
            ok = row["outcome"] in kinds
            note = ""
            if row["outcome"] == "clarify":
                if row["verdict"] == "clarify":
                    clarify_reviewer += 1
                else:
                    clarify_model += 1
            if case.get("reviewer_probe"):
                probes += 1
                probes_caught += row["verdict"] == "clarify"
            if "answer" in kinds:
                wants = expected(case)
                if row["outcome"] == "answer":
                    problems, manual = judge(text, case["q"], wants, captured)
                    if problems:
                        ok = False
                        wrong_passed += 1
                        note = f"{problems}  [НЕВЕРНАЯ ЦИФРА ПРОШЛА РЕВЬЮ]"
                    if manual:
                        manual_total += 1
                        note += f"  [ПРОВЕРИТЬ ВРУЧНУЮ: {manual}]"
                if row["outcome"] == "clarify" and row["draft"] and judge(row["draft"], case["q"], wants, captured)[0]:
                    caught += 1
                    note = "(ревью поймало неверный черновик)"
            passed += ok
            print(f"{'OK  ' if ok else 'FAIL'} [{case['id']}] {case['q']!r} -> {row['outcome']} "
                  f"вердикт={row['verdict']} ${float(row['cost_usd']):.4f} {row['latency_ms']} мс {note}")
            print(f"       «{text}»")
            for step in row["steps"]:
                print(f"       {step['tool']}: {step['input'].get('sql') or step['input']}")
    finally:
        clear_log()
    accuracy = passed / len(cases)
    print(f"\nВерных: {passed}/{len(cases)} = {accuracy:.0%} (порог {MIN_ACCURACY:.0%})")
    print(f"Неверных цифр в ответах, прошедших ревью: {wrong_passed} (должно быть 0)")
    print(f"Ответов с числами не из результата (проверить вручную): {manual_total}")
    print(f"Неверных черновиков поймало ревью: {caught}")
    print(f"Уточнений: от модели {clarify_model}, от ревьюера {clarify_reviewer}; "
          f"на вопросах-ловушках для ревьюера он уточнил {probes_caught} из {probes}")
    print(f"Стоимость вопроса: средняя ${sum(costs) / len(costs):.4f}, максимальная ${max(costs):.4f}; "
          f"задержка: средняя {sum(latencies) / len(latencies) / 1000:.1f} с, "
          f"максимальная {max(latencies) / 1000:.1f} с")
    return 0 if accuracy >= MIN_ACCURACY and wrong_passed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
