"""Контрольный прогон ответов на статистические вопросы (Задача 83) против настоящих API и БД.

Ручной инструмент приёмки, не часть `make ci-local`: стоит денег, нужны `ANTHROPIC_API_KEY` и БД
с данными и миграцией 0015. Каждый вопрос `scripts/ai_stats_eval.jsonl` идёт через настоящий
`ai_stats.answer` (SQL под ролью `nhl_ai_reader`, ревьюер, журнал `ai_stats_log` служебного чата).
Ожидание вопроса: `kind` — допустимые исходы; для `answer` — `expect`: числа и слова, которые
должны быть в ответе (посчитаны вручную SQL-ом по БД на 2026-10-28, вопросы по полным сезонам),
или `expect_sql` — запрос, которым скрипт считает правильные числа по текущей БД в момент
прогона (обзоры текущего сезона). `after` — продолжение диалога: журнал чата не чистится.
В конце: доля верных, ответы с неверной цифрой, прошедшие ревью (должно быть 0), сколько
неверных черновиков ревью поймало, стоимость вопроса (средняя и максимальная).

Запуск: `make ai-stats-eval`. Код выхода 1 — верных меньше 90% или есть неверная цифра в ответе,
прошедшем ревью.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import ai_stats  # noqa: E402
import config  # noqa: E402
from database import get_connection  # noqa: E402

EVAL_SET = Path(__file__).with_suffix(".jsonl")
EVAL_CHAT = -999000083
MIN_ACCURACY = 0.9


def matches(text: str, item) -> bool:
    """Есть ли в *text* ожидаемое: число — целым токеном (запятая в дроби = точка), слово — как
    подстрока без учёта регистра. *item* — строка или список допустимых вариантов."""
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    for alt in item if isinstance(item, list) else [item]:
        if re.search(r"\d", alt):
            if re.search(rf"(?<![\d.]){re.escape(alt)}(?!\d)", text):
                return True
        elif alt.lower() in text.lower():
            return True
    return False


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
    passed = wrong_passed = caught = 0
    costs, latencies = [], []
    clear_log()
    try:
        for case in cases:
            if "after" not in case:
                clear_log()
            text = await ai_stats.answer(EVAL_CHAT, case["q"])
            row = last_row()
            kinds = case["kind"] if isinstance(case["kind"], list) else [case["kind"]]
            costs.append(float(row["cost_usd"]))
            latencies.append(row["latency_ms"])
            ok = row["outcome"] in kinds
            note = ""
            if "answer" in kinds:
                wants = expected(case)
                if ok and not all(matches(text, w) for w in wants):
                    ok, note = False, f"ожидалось {wants}"
                    if row["outcome"] == "answer":
                        wrong_passed += 1
                        note += "  [НЕВЕРНАЯ ЦИФРА ПРОШЛА РЕВЬЮ]"
                if row["draft"] and row["outcome"] == "clarify" and not all(matches(row["draft"], w) for w in wants):
                    caught += 1
                    note += "  (ревью поймало неверный черновик)"
            passed += ok
            print(f"{'OK  ' if ok else 'FAIL'} [{case['id']}] {case['q']!r} -> {row['outcome']} "
                  f"${float(row['cost_usd']):.4f} {row['latency_ms']} мс {note}")
            print(f"       «{text}»")
            for step in row["steps"]:
                print(f"       {step['tool']}: {step['input'].get('sql') or step['input']}")
    finally:
        clear_log()
    accuracy = passed / len(cases)
    print(f"\nВерных: {passed}/{len(cases)} = {accuracy:.0%} (порог {MIN_ACCURACY:.0%})")
    print(f"Неверных цифр в ответах, прошедших ревью: {wrong_passed} (должно быть 0)")
    print(f"Неверных черновиков поймало ревью: {caught}")
    print(f"Стоимость вопроса: средняя ${sum(costs) / len(costs):.4f}, максимальная ${max(costs):.4f}; "
          f"задержка: средняя {sum(latencies) / len(latencies) / 1000:.1f} с, "
          f"максимальная {max(latencies) / 1000:.1f} с")
    return 0 if accuracy >= MIN_ACCURACY and wrong_passed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
