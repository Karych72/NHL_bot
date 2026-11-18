"""Контрольный прогон свободного текста (Задача 81) против настоящего Claude API.

Ручной инструмент приёмки, не часть `make ci-local`: стоит денег и требует ключа. Каждая
фраза `scripts/ai_router_eval.jsonl` уходит модели тем же запросом, что в боте
(`ai_router.call_model`, без записи в журнал `ai_questions`), и сверяется с ожидаемым
инструментом и параметрами (проверяются только перечисленные в наборе) или с текстовым
ответом (`"tool": null`); `stats_question` — передача ответам по статистике (Задача 83).
Ответ по справке (`bot_answer`, Задача 114) сверяется по `expect`: каждое слово (варианты через
`|`) должно встретиться в тексте без учёта регистра.
Текстовые ответы печатаются целиком — их
проверяют глазами на выдуманные факты; ответы с цифрами помечены. В конце — точность, стоимость вопроса
(средняя и максимальная, из `usage`) и размер префикса (`messages.count_tokens`).

Запуск: `make ai-eval` (нужны `ANTHROPIC_API_KEY` и БД с командами сезона в `.env`).
Аргументы — `make ai-eval ARGS="--model claude-haiku-5-5 --effort low"`: `--model` (ключ `PRICES`,
по умолчанию модель бота) и `--effort` подменяют `ai_router.MODEL`/`EFFORT` только в прогоне.
Дополнительно к точности печатаются задержка p50/p95, итог за прогон, стоимость «каждый вопрос
холодный» (чтение кэша оплачено как запись), кэш первого ответа и число отказов (`refusal`).
Код выхода 1 — точность ниже 90%.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import ai_router  # noqa: E402

EVAL_SET = Path(__file__).with_suffix(".jsonl")
MIN_ACCURACY = 0.9
# Цены моделей, $ за 1M токенов: вход, выход, чтение кэша, запись кэша (5 мин). Множители кэша
# у обеих моделей чтение 0.1x, запись 1.25x от входа (у Haiku сверено с прайсом API 2026-11-18).
PRICES = {"claude-sonnet-5-5": (2.0, 10.0, 0.2, 2.5), "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def use_model(name: str) -> None:
    """Переключает модель и цены `ai_router` (`cost_usd` читает их как глобали модуля)."""
    ai_router.MODEL = name
    (ai_router._PRICE_INPUT, ai_router._PRICE_OUTPUT,
     ai_router._PRICE_CACHE_READ, ai_router._PRICE_CACHE_WRITE) = PRICES[name]


def cold_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
              cold: bool = True) -> float:
    """Стоимость вызова на холодном кэше: при *cold* чтение кэша оплачено как запись, иначе как чтение."""
    price_in, price_out, price_read, price_write = PRICES[model]
    read_price = price_write if cold else price_read
    return (input_tokens * price_in + output_tokens * price_out + cache_read * read_price
            + cache_write * price_write) / 1e6


def percentile(values: list, share: float) -> float:
    """Значение на доле *share* (0..1) отсортированного списка (ближайший ранг, ceil(p*n)-1)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=list(PRICES), default=ai_router.MODEL)
    parser.add_argument("--effort", choices=EFFORTS, default=ai_router.EFFORT)
    args = parser.parse_args()
    use_model(args.model)
    ai_router.EFFORT = args.effort
    print(f"Модель: {args.model}, effort: {args.effort}")
    cases = [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines()]
    params = ai_router.request_params("")
    prefix = await ai_router.client().messages.count_tokens(**params)
    passed, refusals = 0, 0
    costs, cold_costs, latencies = [], [], []
    first_usage = None
    for case in cases:
        started = time.monotonic()
        response = await ai_router.call_model(case["q"])
        latencies.append(time.monotonic() - started)
        usage = response.usage
        first_usage = first_usage or usage
        refusals += response.stop_reason == "refusal"
        cold_costs.append(cold_cost(args.model, usage.input_tokens, usage.output_tokens,
                                    usage.cache_read_input_tokens or 0, usage.cache_creation_input_tokens or 0))
        reply = ai_router.decide(response)
        costs.append(ai_router.cost_usd(response.usage))
        if case["tool"] is None:
            ok = reply.tool is None
        else:
            expected = case.get("params", {})
            ok = reply.tool == case["tool"] and all(reply.params.get(k) == v for k, v in expected.items())
            text = (reply.text or "").lower()
            ok = ok and all(any(w.lower() in text for w in alts.split("|")) for alts in case.get("expect", []))
        passed += ok
        got = f"{reply.tool} {json.dumps(reply.params, ensure_ascii=False)}" if reply.tool else "текст"
        print(f"{'OK  ' if ok else 'FAIL'} {case['q']!r} -> {got}")
        if reply.text is not None:
            flag = "  [ЦИФРЫ — проверить]" if re.search(r"\d", reply.text) else ""
            print(f"       «{reply.text}»{flag}")
    accuracy = passed / len(cases)
    print(f"\nСовпадений: {passed}/{len(cases)} = {accuracy:.0%} (порог {MIN_ACCURACY:.0%})")
    print(f"Стоимость вопроса: средняя ${sum(costs) / len(costs):.4f}, максимальная ${max(costs):.4f}")
    print(f"Стоимость вопроса «каждый вопрос холодный»: средняя ${sum(cold_costs) / len(cold_costs):.4f}, "
          f"максимальная ${max(cold_costs):.4f}")
    print(f"Задержка: p50 {percentile(latencies, 0.5):.2f} с, p95 {percentile(latencies, 0.95):.2f} с")
    print(f"Кэш первого ответа: cache_creation_input_tokens {first_usage.cache_creation_input_tokens or 0}, "
          f"cache_read_input_tokens {first_usage.cache_read_input_tokens or 0}")
    print(f"Ответов со stop_reason refusal: {refusals}")
    print(f"Итого за прогон: ${sum(costs):.4f}")
    print(f"Префикс (системный промпт + инструменты + пустой вопрос): {prefix.input_tokens} токенов")
    return 0 if accuracy >= MIN_ACCURACY else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
