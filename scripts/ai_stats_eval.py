"""Контрольный прогон ответов на статистические вопросы (Задача 83) против настоящих API и БД.

Ручной инструмент приёмки, не часть `make ci-local`: стоит денег, нужны `ANTHROPIC_API_KEY` и БД
с данными и миграцией 0015. Каждый вопрос `scripts/ai_stats_eval.jsonl` идёт через настоящий
`ai_stats.answer` (SQL под ролью `nhl_ai_reader`, ревьюер, журнал `ai_stats_log` служебного чата).

Ожидание вопроса: `kind` — допустимые исходы; для `answer` — `expect`: числа и слова (посчитаны
вручную SQL-ом по БД на 2026-10-28, вопросы по полным сезонам; плей-офф `pl*` — публичные
итоги, сверяются с БД при приёмке Задачи 105) или `expect_sql` — запрос, которым скрипт считает правильные числа по текущей БД в момент прогона (обзоры текущего сезона).
`list_items` — точное число пунктов «• » / «- » в ответе (Задача 161).
`after` — продолжение диалога: журнал чата не чистится. `reviewer_probe` — вопрос, на котором
модель легко ответит сама, а уточнить должен ревьюер.

Оценка ответа по набору чисел, а не по вхождению. Все ожидаемые числа и слова `expect` должны
быть в ответе, иначе он неверный; если он при этом прошёл ревью — это «неверная цифра в ответе,
прошедшем ревью», порог требует нуля таких. Каждое число ответа, кроме рамочных (годы, сезоны,
даты, «последних N», числа из вопроса), которого нет в `expect` и в результате SQL/обзора,
который модель реально получила (с допуском на округление), печатается отдельно как «проверить
вручную» (выдумка или арифметика модели) и в порог не входит.

Запуск: `make ai-stats-eval`. Аргументы — `make ai-stats-eval ARGS="--answer-model claude-haiku-5-5
--reviewer-model claude-sonnet-5-5 --effort medium --ids q1,q2"`: модели отвечающего и ревьюера
(ключи `PRICES` из `ai_router_eval`, по умолчанию модель бота), `ai_stats.EFFORT` и пробный прогон
на подмножестве вопросов (родитель выбранного `after` обязан быть выбран). Дополнительно к
точности печатаются задержка p50/p95, вызовы модели и попытки SQL на вопрос, число исходов
`error`, итог за прогон и стоимость «каждый вопрос холодный».
Код выхода 1 — верных меньше 90% или есть неверная цифра в ответе,
прошедшем ревью.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, List, Tuple

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import ai_router  # noqa: E402
import ai_stats  # noqa: E402
import config  # noqa: E402
from ai_router_eval import EFFORTS, PRICES, cold_cost, percentile, use_model  # noqa: E402
from database import get_connection  # noqa: E402

EVAL_SET = Path(__file__).with_suffix(".jsonl")
# Служебные чаты прогона: каждой цепочке и каждому одиночному вопросу — свой, чтобы история окна
# не текла между кейсами; журнал чистится в начале и после печати итогов.
EVAL_CHAT_BASE = -999000100
EVAL_CHATS = 1000
MIN_ACCURACY = 0.9

# Рамочные числа: даты, сезоны, «последних N игр». Отдельное четырёхзначное число
# может быть статистикой (1963 передачи Гретцки), поэтому целиком годами их не считаем.
_FRAME = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{4}[/\-–]\d{2,4}|(?<=последних )\d+|(?<=последние )\d+",
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


_call = ai_stats._call
models = {"answer": ai_router.MODEL, "review": ai_router.MODEL}


async def _call_by_role(run: Any, role: str, *args: Any) -> Any:
    """Вызов модели `ai_stats._call` на модели своей роли ("answer"/"review"), цена — по ней же."""
    use_model(models[role])
    return await _call(run, role, *args)


ai_stats._call = _call_by_role  # type: ignore[assignment]


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
            cur.execute("DELETE FROM ai_stats_log WHERE chat_id BETWEEN %s AND %s",
                        (EVAL_CHAT_BASE - EVAL_CHATS, EVAL_CHAT_BASE))
        conn.commit()


def last_row(chat_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, draft, verdict, verdict_reason, cost_usd, latency_ms, steps, calls "
                "FROM ai_stats_log WHERE chat_id = %s ORDER BY id DESC LIMIT 1", (chat_id,),
            )
            columns = [d[0] for d in cur.description]
            return dict(zip(columns, cur.fetchone()))


def select_cases(cases: list, ids: str) -> list:
    """Вопросы с id из списка *ids* через запятую (пусто — все); без выбранного родителя `after` — ошибка."""
    if not ids:
        return cases
    wanted = set(ids.split(","))
    chosen = [c for c in cases if c["id"] in wanted]
    missing = wanted - {c["id"] for c in chosen}
    if missing:
        raise SystemExit(f"--ids: нет вопросов {sorted(missing)}")
    for case in chosen:
        if "after" in case and case["after"] not in wanted:
            raise SystemExit(f"--ids: вопрос {case['id']} продолжает {case['after']}, добавьте его в --ids")
    return chosen


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answer-model", choices=list(PRICES), default=ai_router.MODEL)
    parser.add_argument("--reviewer-model", choices=list(PRICES), default=ai_router.MODEL)
    parser.add_argument("--effort", choices=EFFORTS, default=ai_stats.EFFORT)
    parser.add_argument("--ids", default="")
    args = parser.parse_args()
    models.update(answer=args.answer_model, review=args.reviewer_model)
    ai_stats.EFFORT = args.effort
    cases = select_cases(
        [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines()], args.ids)
    print(f"Модель отвечающего: {args.answer_model}, ревьюера: {args.reviewer_model}, effort: {args.effort}")
    passed = wrong_passed = caught = manual_total = 0
    clarify_model = clarify_reviewer = probes = probes_caught = 0
    costs, latencies, cold_costs, call_counts, sql_counts = [], [], [], [], []
    errors = 0
    clear_log()
    chats: dict = {}
    for case in cases:
        chat_id = chats[case["after"]] if "after" in case else EVAL_CHAT_BASE - len(chats)
        chats[case["id"]] = chat_id
        captured.clear()
        text = await ai_stats.answer(chat_id, case["q"], ai_stats.recent_pairs(chat_id))
        row = last_row(chat_id)
        kinds = case["kind"] if isinstance(case["kind"], list) else [case["kind"]]
        costs.append(float(row["cost_usd"]))
        latencies.append(row["latency_ms"])
        call_counts.append(len(row["calls"]))
        sql_counts.append(sum(step["tool"] == "run_sql" for step in row["steps"]))
        # Холодный кэш только у первого вызова каждой роли: последующие читают записанное им.
        cold_costs.append(sum(
            cold_cost(models[c["role"]], c["input_tokens"], c["output_tokens"], c["cache_read_tokens"],
                      c["cache_write_tokens"], cold=all(p["role"] != c["role"] for p in row["calls"][:i]))
            for i, c in enumerate(row["calls"])))
        errors += row["outcome"] == "error"
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
        if row["outcome"] == "answer" and "list_items" in case:
            items = len(re.findall(r"(?m)^\s*[•-]\s+", text))
            if items != case["list_items"]:
                ok = False
                note += f"  [ФОРМАТ: пунктов {items}, ожидалось {case['list_items']}]"
        passed += ok
        print(f"{'OK  ' if ok else 'FAIL'} [{case['id']}] {case['q']!r} -> {row['outcome']} "
              f"вердикт={row['verdict']} ({row['verdict_reason']}) ${float(row['cost_usd']):.4f} {row['latency_ms']} мс {len(row['calls'])} выз. {note}")
        print(f"       «{text}»")
        for step in row["steps"]:
            if step["tool"] == "review":
                print(f"       review: {step['verdict']} — {step['reason']}")
            else:
                print(f"       {step['tool']}: {step['input'].get('sql') or step['input']}")
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
    print(f"Задержка: p50 {percentile(latencies, 0.5) / 1000:.1f} с, p95 {percentile(latencies, 0.95) / 1000:.1f} с")
    print(f"Вызовов модели на вопрос: среднее {sum(call_counts) / len(call_counts):.1f}, максимум {max(call_counts)}; "
          f"попыток SQL на вопрос: среднее {sum(sql_counts) / len(sql_counts):.1f}, максимум {max(sql_counts)}")
    print(f"Исходов error: {errors}")
    print(f"Стоимость вопроса «каждый вопрос холодный»: средняя ${sum(cold_costs) / len(cold_costs):.4f}, "
          f"максимальная ${max(cold_costs):.4f}")
    print(f"Итого за прогон: ${sum(costs):.4f}")
    exit_code = 0 if accuracy >= MIN_ACCURACY and wrong_passed == 0 else 1
    clear_log()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
