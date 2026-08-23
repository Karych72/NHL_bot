"""Тесты троттлинга callback-кнопок (`throttle.py`, Задача 10).

`enforce_callback_rate_limit` — обычный PTB-колбэк (`update, context`), а не
хендлер, зарегистрированный в `Application`: сети/polling здесь нет, вызывается
напрямую. Единственные границы, которые трогает сама функция —
`update.effective_user`, `update.callback_query.answer()` и модульные часы
(`throttle.time.monotonic`), поэтому оба подменяются так же, как в
`tests/test_bot_database.py` для `database.cached_fetch_all`. Регистрация
хендлера в `Application` (группа, порядок) проверяется отдельно, в
`tests/test_bot_application.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationHandlerStop


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


def _throttled_update(make_callback_update: Callable[..., Any], user_id: int = 1) -> Any:
    update = make_callback_update("st:players_season_stats:points:0")
    update.effective_user = SimpleNamespace(id=user_id)
    return update


def _real_text_update(user_id: int = 1) -> Update:
    """Настоящий `telegram.Update` с текстовым сообщением — как `_text_update`
    в `tests/test_bot_application.py`. В отличие от `make_message_update`
    (упрощённый тестовый дублёр без `callback_query`), у настоящего `Update`
    это поле всегда объявлено (по умолчанию `None`), как и предполагает
    прямой доступ `update.callback_query` в `throttle.py`."""
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chat=Chat(id=1, type=Chat.PRIVATE),
            from_user=User(id=user_id, first_name="T", is_bot=False),
            text="/help",
        ),
    )


# ---------------------------------------------------------------------------
# Базовое поведение окна
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_updates_within_limit_pass_through_without_answer(
    throttle_module, clock, make_callback_update, fake_context
) -> None:
    update = _throttled_update(make_callback_update)

    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await throttle_module.enforce_callback_rate_limit(update, fake_context)

    assert update.callback_query.answers == []


@pytest.mark.asyncio
async def test_exceeding_limit_answers_user_and_stops_the_update(
    throttle_module, clock, make_callback_update, fake_context
) -> None:
    update = _throttled_update(make_callback_update)
    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await throttle_module.enforce_callback_rate_limit(update, fake_context)

    with pytest.raises(ApplicationHandlerStop):
        await throttle_module.enforce_callback_rate_limit(update, fake_context)

    # Пользователь получил answer() с непустым текстом, а не молчание.
    assert len(update.callback_query.answers) == 1
    assert update.callback_query.answers[0]


@pytest.mark.asyncio
async def test_window_expiry_releases_the_counter(
    throttle_module, clock, make_callback_update, fake_context
) -> None:
    update = _throttled_update(make_callback_update)
    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await throttle_module.enforce_callback_rate_limit(update, fake_context)

    clock["now"] += throttle_module.CALLBACK_RATE_WINDOW_SEC + 0.01

    # Окно истекло — апдейт снова проходит, без ApplicationHandlerStop.
    await throttle_module.enforce_callback_rate_limit(update, fake_context)

    assert update.callback_query.answers == []


@pytest.mark.asyncio
async def test_rate_limit_is_tracked_per_user(
    throttle_module, clock, make_callback_update, fake_context
) -> None:
    spammer = _throttled_update(make_callback_update, user_id=1)
    other_user = _throttled_update(make_callback_update, user_id=2)

    for _ in range(throttle_module.CALLBACK_RATE_LIMIT):
        await throttle_module.enforce_callback_rate_limit(spammer, fake_context)

    with pytest.raises(ApplicationHandlerStop):
        await throttle_module.enforce_callback_rate_limit(spammer, fake_context)

    # Лимит одного пользователя не затрагивает другого.
    await throttle_module.enforce_callback_rate_limit(other_user, fake_context)
    assert other_user.callback_query.answers == []


# ---------------------------------------------------------------------------
# Область перехвата (Ruling 4): только апдейты с callback_query и effective_user
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_without_callback_query_is_ignored(
    throttle_module, clock, fake_context
) -> None:
    update = _real_text_update()

    # Не должно ни падать, ни поднимать ApplicationHandlerStop.
    await throttle_module.enforce_callback_rate_limit(update, fake_context)

    assert throttle_module._hits == {}


@pytest.mark.asyncio
async def test_update_without_effective_user_is_not_throttled(
    throttle_module, clock, make_callback_update, fake_context
) -> None:
    update = make_callback_update("st:players_season_stats:points:0")
    update.effective_user = None

    await throttle_module.enforce_callback_rate_limit(update, fake_context)

    assert update.callback_query.answers == []
    assert throttle_module._hits == {}


# ---------------------------------------------------------------------------
# Ограниченный рост состояния
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_user_entries_are_pruned_from_state(
    throttle_module, clock, make_callback_update, fake_context
) -> None:
    """Требование задачи: истёкшие записи вычищаются в том же проходе, без
    отдельного планировщика — иначе `_hits` растёт неограниченно вместе с
    числом когда-либо нажавших кнопку пользователей."""
    stale_update = _throttled_update(make_callback_update, user_id=1)
    await throttle_module.enforce_callback_rate_limit(stale_update, fake_context)
    assert 1 in throttle_module._hits

    clock["now"] += throttle_module.CALLBACK_RATE_WINDOW_SEC + 0.01
    fresh_update = _throttled_update(make_callback_update, user_id=2)
    await throttle_module.enforce_callback_rate_limit(fresh_update, fake_context)

    assert 1 not in throttle_module._hits, "истёкшая запись пользователя 1 должна быть вычищена"
    assert 2 in throttle_module._hits
