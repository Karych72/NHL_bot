"""Контрольный прогон свободного текста (Задача 81) против настоящего Claude API.

Ручной инструмент приёмки, не часть `make ci-local`: стоит денег и требует ключа. Каждая
фраза `scripts/ai_router_eval.jsonl` уходит модели тем же запросом, что в боте
(`ai_router.call_model`, без записи в журнал `ai_questions`), и сверяется с ожидаемым
инструментом и параметрами (проверяются только перечисленные в наборе) или с текстовым
ответом (`"tool": null`). Фраза с `"with_stats": true` идёт запросом чата из белого списка
Задачи 83 — с инструментом `stats_question`. Текстовые ответы печатаются целиком — их
проверяют глазами на выдуманные факты; ответы с цифрами помечены. В конце — точность, стоимость вопроса
(средняя и максимальная, из `usage`) и размер префикса (`messages.count_tokens`).

Запуск: `make ai-eval` (нужны `ANTHROPIC_API_KEY` и БД с командами сезона в `.env`).
Код выхода 1 — точность ниже 90%.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import ai_router  # noqa: E402

EVAL_SET = Path(__file__).with_suffix(".jsonl")
MIN_ACCURACY = 0.9


async def main() -> int:
    cases = [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines()]
    params = ai_router.request_params("")
    prefix = await ai_router.client().messages.count_tokens(**params)
    passed, costs = 0, []
    for case in cases:
        response = await ai_router.call_model(case["q"], case.get("with_stats", False))
        reply = ai_router.decide(response)
        costs.append(ai_router.cost_usd(response.usage))
        if case["tool"] is None:
            ok = reply.tool is None
        else:
            expected = case["params"]
            ok = reply.tool == case["tool"] and all(reply.params.get(k) == v for k, v in expected.items())
        passed += ok
        got = f"{reply.tool} {json.dumps(reply.params, ensure_ascii=False)}" if reply.tool else "текст"
        mark = " [83]" if case.get("with_stats") else ""
        print(f"{'OK  ' if ok else 'FAIL'}{mark} {case['q']!r} -> {got}")
        if reply.text is not None:
            flag = "  [ЦИФРЫ — проверить]" if re.search(r"\d", reply.text) else ""
            print(f"       «{reply.text}»{flag}")
    accuracy = passed / len(cases)
    print(f"\nСовпадений: {passed}/{len(cases)} = {accuracy:.0%} (порог {MIN_ACCURACY:.0%})")
    print(f"Стоимость вопроса: средняя ${sum(costs) / len(costs):.4f}, максимальная ${max(costs):.4f}")
    print(f"Префикс (системный промпт + инструменты + пустой вопрос): {prefix.input_tokens} токенов")
    return 0 if accuracy >= MIN_ACCURACY else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
