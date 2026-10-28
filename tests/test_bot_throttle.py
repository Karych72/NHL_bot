"""Тесты троттлинга callback-кнопок (`throttle.py`, Задача 10).

`enforce_callback_rate_limit` — outer-middleware aiogram `(handler, event, data)`:
сети/polling здесь нет, вызывается напрямую с настоящим `CallbackQuery`, привязанным
к боту на записывающей сессии (`conftest.FakeSession`); `handler` — следующий шаг
цепочки, который тест считает. Модульные часы (`throttle.time.monotonic`) подменяются
так же, как в `tests/test_bot_database.py` для `database.cached_fetch_all`. Что
middleware висит на `Dispatcher` и останавливает нажатие до хендлеров — в
`tests/test_bot_application.py`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

import pytest


@pytest.fixture
def throttle_module(bot_module, monkeypatch):
    """Модуль `throttle` с пустым `_hits` на каждый тест.

    Модуль импортируется один раз за тестовый процесс (`sys.modules`), а
    `_hits` — module-level dict, переживающий отдельные тесты; без сброса
    один тест видел бы счётчики, оставленные другим.
    """
    throttle = bot_module("throttle")
    monkeypatch.setattr(throttle, "_hits", {})
    return throttle


@pytest.fixture
def clock(throttle_module, monkeypatch) -> dict:
    """Управляемая замена `time.monotonic()`, как в `test_bot_database.py`."""
    state = {"now": 0.0}
    monkeypatch.setattr(throttle_module.time, "monotonic", lambda: state["now"])
    return state


class _Handler:
    """Следующий шаг цепочки middleware: считает, сколько нажатий до него дошло."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def __call__(self, event: Any, data: Dict[str, Any]) -> str:
        self.events.append(event)
        return "handled"


async def _press(throttle_module, make_callback: Callable[..., Any], handler: _Handler, user_id: int = 1) -> Any:
    callback = make_callback("st:players_season_stats:points:0", user_id=user_id)
    return await throttle_module.enforce_callback_rate_limit(handler, callback, {})


# ---------------------------------------------------------------------------
# Базовое поведение окна
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_presses_within_limit_reach_the_handler_without_answer(
    throttle_module, clock, make_callback, api
) -> None:
    handler = _Handler()

    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        assert await _press(throttle_module, make_callback, handler) == "handled"

    assert len(handler.events) == throttle_module.CALLBACK_RATE_LIMIT
    assert api.answers == []


@pytest.mark.asyncio
async def test_exceeding_limit_answers_user_and_stops_the_press(
    throttle_module, clock, make_callback, api
) -> None:
    handler = _Handler()
    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await _press(throttle_module, make_callback, handler)

    assert await _press(throttle_module, make_callback, handler) is None

    # Нажатие не дошло до хендлера, а пользователь получил ответ с текстом, а не молчание.
    assert len(handler.events) == throttle_module.CALLBACK_RATE_LIMIT
    assert api.answers == [throttle_module.RATE_LIMIT_ANSWER_TEXT]


@pytest.mark.asyncio
async def test_window_expiry_releases_the_counter(throttle_module, clock, make_callback, api) -> None:
    handler = _Handler()
    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await _press(throttle_module, make_callback, handler)

    clock["now"] += throttle_module.CALLBACK_RATE_WINDOW_SEC + 0.01

    # Окно истекло — нажатие снова доходит до хендлера.
    assert await _press(throttle_module, make_callback, handler) == "handled"
    assert api.answers == []


@pytest.mark.asyncio
async def test_rate_limit_is_tracked_per_user(throttle_module, clock, make_callback, api) -> None:
    handler = _Handler()
    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await _press(throttle_module, make_callback, handler, user_id=1)
    assert await _press(throttle_module, make_callback, handler, user_id=1) is None

    # Лимит одного пользователя не затрагивает другого.
    assert await _press(throttle_module, make_callback, handler, user_id=2) == "handled"
    assert api.answers == [throttle_module.RATE_LIMIT_ANSWER_TEXT]


# ---------------------------------------------------------------------------
# Ограниченный рост состояния
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_user_entries_are_pruned_from_state(
    throttle_module, clock, make_callback
) -> None:
    """Требование задачи: истёкшие записи вычищаются в том же проходе, без
    отдельного планировщика — иначе `_hits` растёт неограниченно вместе с
    числом когда-либо нажавших кнопку пользователей."""
    handler = _Handler()
    await _press(throttle_module, make_callback, handler, user_id=1)
    assert 1 in throttle_module._hits

    clock["now"] += throttle_module.CALLBACK_RATE_WINDOW_SEC + 0.01
    await _press(throttle_module, make_callback, handler, user_id=2)

    assert 1 not in throttle_module._hits, "истёкшая запись пользователя 1 должна быть вычищена"
    assert 2 in throttle_module._hits
