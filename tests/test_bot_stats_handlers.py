"""Unit tests for telegram_bot/stats_handlers.py: parsing of inline-keyboard
callback_data strings and the pure keyboard builders that produce them.

Handlers get real aiogram objects (conftest: ``make_callback``/``make_message``
bound to ``fake_bot``) and the arguments aiogram's
dependency injection would pass — the regex ``match`` of the registration filter
included; what they ask Telegram to do is read back from the recording session
(``api``). Where a handler calls into bot_messages for DB-backed rendering, that
collaborator function is patched on stats_handlers' own namespace (the same
boundary style already used for bot_messages.fetch_all elsewhere in this
suite) so what's actually under test — callback_data parsing, offset arithmetic,
TelegramBadRequest handling — is isolated from database access.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    CopyMessage, DeleteMessage, EditMessageText, SendMessage, SendRichMessage, SendVideo,
)

from tests._telegram_fakes import MENU_MESSAGE_ID, body, rich_html


def _bad_request(message: str) -> TelegramBadRequest:
    """Ответ Telegram 400 с текстом *message*, как его поднимает aiogram."""
    return TelegramBadRequest(method=SendMessage(chat_id=100, text="x"), message=message)


@pytest.fixture(autouse=True)
def no_country_subscriptions(bot_module, monkeypatch):
    """Карточка матча спрашивает подписки чата ради блока страны (Задача 76); здесь
    у чата их нет — та же граница с БД, что у ``game_message``. Блок страны
    подписчику — в tests/test_bot_integration.py."""
    monkeypatch.setattr(
        bot_module("stats_handlers"), "get_chat_subscriptions",
        lambda chat_id: bot_module("subscription_repo").ChatSubscriptions(False, False, set(), []),
    )


# ---------------------------------------------------------------------------
# Меню /stats, его подменю расширенной статистики, /team_stats — каждая кнопка ведёт на известную таблицу
# ---------------------------------------------------------------------------

def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_stats_menus_offer_every_player_and_goalie_table_once(bot_module):
    """Основные метрики — в `/stats`, расширенные — в его подменю (`sa:menu`, последняя
    кнопка `/stats`, «« Назад» — обратно): каждая таблица игроков — ровно одной кнопкой
    одного из двух меню, первая страница."""
    stats_handlers = bot_module("stats_handlers")
    specs = bot_module("leaderboard_specs")

    main = [b.callback_data for b in _buttons(stats_handlers.stats_menu_keyboard())]
    advanced = [b.callback_data for b in _buttons(stats_handlers.advanced_menu_keyboard())]

    assert (main[-1], advanced[-1]) == ("sa:menu", "st:menu")
    advanced_tables = {f"{t}:{c}" for t, c in specs.ADVANCED_METRICS.values()}
    assert sorted(main[:-1] + advanced[:-1]) == sorted(
        f"{'sa' if f'{t}:{c}' in advanced_tables else 'st'}:{t}:{c}:0" for t, c in specs.PLAYER_STAT_TITLES
    )
    assert all(re.match(stats_handlers.STAT_SCREEN_CALLBACK_PATTERN, d) for d in main + advanced), (
        "кнопка без хендлера"
    )


def test_stats_menu_highlights_only_points_goals_assists(bot_module):
    """Сдержанный акцент: цветом выделен только первый ряд — бывший /leaders."""
    stats_handlers = bot_module("stats_handlers")

    rows = stats_handlers.stats_menu_keyboard().inline_keyboard

    assert [(b.text, b.style) for b in rows[0]] == [
        ("Очки", "primary"), ("Голы", "primary"), ("Передачи", "primary"),
    ]
    assert {b.style for row in rows[1:] for b in row} == {None}


def test_team_stats_menu_offers_every_team_table_once(bot_module):
    stats_handlers = bot_module("stats_handlers")
    specs = bot_module("leaderboard_specs")

    data = [b.callback_data for b in _buttons(stats_handlers.team_stats_keyboard())]

    assert sorted(data) == sorted(f"tm:{key}:0" for key in specs.TEAM_STAT_SPECS)
    assert all(re.match(stats_handlers.STAT_SCREEN_CALLBACK_PATTERN, d) for d in data), "кнопка без хендлера"


# ---------------------------------------------------------------------------
# stat_page_reply — листание и подвал страницы у всех трёх команд
# ---------------------------------------------------------------------------

_PAGE_KEYS = [
    ("st", "players_season_stats:points", "player_stat_leaderboard_page"),
    ("sa", "players_advanced_stats:sat_pct", "player_stat_leaderboard_page"),
    ("tm", "goals", "team_stat_leaderboard_page"),
]


@pytest.mark.parametrize("prefix, key, render", _PAGE_KEYS)
def test_stat_page_offsets_prev_floor_and_footer(bot_module, prefix, key, render):
    stats_handlers = bot_module("stats_handlers")
    page = f"{prefix}:{key}:"

    with patch.object(stats_handlers, render, return_value=("T", True, True)):
        _, markup = stats_handlers.stat_page_reply(prefix, key, 20)
    with patch.object(stats_handlers, render, return_value=("T", True, False)):
        _, floor = stats_handlers.stat_page_reply(prefix, key, 5)

    # С третьей страницы первой кнопкой идёт «« 1–10» на начало списка.
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [f"{page}0", f"{page}10", f"{page}30"]
    # prev never goes negative, even near the start of the list
    assert [b.callback_data for b in floor.inline_keyboard[0]] == [f"{page}0"]
    # Подвал: меню своей команды и «Сохранить».
    assert [b.callback_data for b in markup.inline_keyboard[-1]] == [f"{prefix}:menu", f"{prefix}:save"]


def test_stat_page_single_page_has_only_footer(bot_module):
    stats_handlers = bot_module("stats_handlers")

    with patch.object(stats_handlers, "team_stat_leaderboard_page", return_value=("T", False, False)):
        _, markup = stats_handlers.stat_page_reply("tm", "pp", 0)

    assert len(markup.inline_keyboard) == 1, "без пустого ряда листания"


def test_stat_page_renders_player_title_and_team_columns(bot_module):
    """Ключ кнопки раскрывается в заголовок и колонки справочника `leaderboard_specs`."""
    stats_handlers = bot_module("stats_handlers")

    with patch.object(stats_handlers, "player_stat_leaderboard_page", return_value=("P", False, False)) as player, \
            patch.object(stats_handlers, "team_stat_leaderboard_page", return_value=("T", False, False)) as team:
        assert stats_handlers.stat_page_reply("st", "goalies_season_stats:wins", 30)[0] == "P"
        assert stats_handlers.stat_page_reply("tm", "goals", 10)[0] == "T"

    player.assert_called_once_with("Лидеры по победам", "goalies_season_stats", "wins", 30)
    team.assert_called_once_with(
        "Голы за и против в среднем за игру", ("goals_per_game", "goals_against_per_game"), 10
    )


# ---------------------------------------------------------------------------
# callback_stat_screen
# ---------------------------------------------------------------------------

def _match(stats_handlers, pattern_name: str, data: str) -> re.Match:
    """Совпадение, которое aiogram передаёт хендлеру нажатия (`F.data.regexp(...).as_("match")`)."""
    match = re.match(getattr(stats_handlers, pattern_name), data)
    assert match is not None
    return match


async def _press_stat(stats_handlers, make_callback, fake_bot, data):
    await stats_handlers.callback_stat_screen(
        make_callback(data), fake_bot, _match(stats_handlers, "STAT_SCREEN_CALLBACK_PATTERN", data)
    )


@pytest.mark.asyncio
async def test_stat_screen_parses_offset_and_renders_page_in_place(bot_module, make_callback, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    data = "st:players_season_stats:points:20"

    with patch.object(
        stats_handlers, "player_stat_leaderboard_page", return_value=("PAGE TEXT", True, True)
    ) as mock_page:
        await _press_stat(stats_handlers, make_callback, fake_bot, data)

    mock_page.assert_called_once_with("Лучшие бомбардиры", "players_season_stats", "points", 20)
    [edit] = api.edited_texts
    assert edit.text is None
    assert body(edit) == rich_html("PAGE TEXT")
    assert edit.message_id == MENU_MESSAGE_ID, "страница — на месте сообщения с кнопкой"
    assert [b.callback_data for b in edit.reply_markup.inline_keyboard[0]] == [
        "st:players_season_stats:points:0",
        "st:players_season_stats:points:10",
        "st:players_season_stats:points:30",
    ]


@pytest.mark.parametrize(
    "prefix, intro, keyboard",
    [
        ("st", "STATS_COMMAND_INTRO", "stats_menu_keyboard"),
        ("sa", "ADVANCED_COMMAND_INTRO", "advanced_menu_keyboard"),
        ("tm", "TEAM_STATS_COMMAND_INTRO", "team_stats_keyboard"),
    ],
)
@pytest.mark.asyncio
async def test_stat_screen_menu_returns_own_command_menu_in_place(
    bot_module, make_callback, fake_bot, api, prefix, intro, keyboard
):
    stats_handlers = bot_module("stats_handlers")
    help_text = bot_module("help_text")

    await _press_stat(stats_handlers, make_callback, fake_bot, f"{prefix}:menu")

    [edit] = api.edited_texts
    assert body(edit) == rich_html(getattr(help_text, intro))
    assert edit.reply_markup == getattr(stats_handlers, keyboard)()
    assert api.outgoing == [], "меню — в том же сообщении"


@pytest.mark.asyncio
async def test_stat_screen_save_freezes_page_and_sends_copy_with_same_keyboard(
    bot_module, fake_bot, api
):
    """«Сохранить»: текущая страница остаётся в чате без кнопок, её копия
    (`copyMessage`) с теми же кнопками уходит новым сообщением ниже."""
    from aiogram.types import CallbackQuery

    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "team_stat_leaderboard_page", return_value=("T", True, True)):
        _, markup = stats_handlers.stat_page_reply("tm", "shots", 10)
    callback = CallbackQuery.model_validate(
        {
            "id": "1",
            "from": {"id": 200, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": "tm:save",
            "message": {
                "message_id": MENU_MESSAGE_ID,
                "date": datetime.now(timezone.utc),
                "chat": {"id": 100, "type": "private"},
                "text": "Броски",
                "reply_markup": markup.model_dump(),
            },
        },
        context={"bot": fake_bot},
    )

    await stats_handlers.callback_stat_screen(
        callback, fake_bot, _match(stats_handlers, "STAT_SCREEN_CALLBACK_PATTERN", "tm:save")
    )

    assert api.answers == ["Сохранено"]
    [frozen] = api.edited_markups
    assert (frozen.message_id, frozen.reply_markup) == (MENU_MESSAGE_ID, None)
    [sent] = api.sent(CopyMessage)
    assert (sent.chat_id, sent.from_chat_id, sent.message_id) == (100, 100, MENU_MESSAGE_ID)
    assert api.outgoing == [], "копия — copyMessage, а не пересборка текста"
    assert [[(b.text, b.callback_data) for b in row] for row in sent.reply_markup.inline_keyboard] == [
        [(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard
    ]
    # Сначала копия, потом снятие кнопок: упади отправка — страница останется живой.
    assert api.requests.index(sent) < api.requests.index(frozen)


# ---------------------------------------------------------------------------
# «message is not modified» при повторном нажатии глушится; любая другая
# ошибка правки всплывает (`stats_handlers._edit_page`)
# ---------------------------------------------------------------------------

# Все листаемые экраны рисуют страницу через `_edit_page`; варьируются хендлер, его
# callback_data, паттерн регистрации и функция рендера, которую тест подменяет.
_EDIT_PAGE_CASES = [
    ("callback_stat_screen", "st:players_season_stats:points:0", "STAT_SCREEN_CALLBACK_PATTERN",
     "player_stat_leaderboard_page", ("t", False, False)),
    ("callback_stat_screen", "tm:pp:0", "STAT_SCREEN_CALLBACK_PATTERN",
     "team_stat_leaderboard_page", ("t", False, False)),
    ("callback_country", "cn:list", "COUNTRY_CALLBACK_PATTERN", "country_rankings", ("t", [])),
]


async def _press_page(stats_handlers, make_callback, fake_bot, name, data, pattern_name):
    """Нажатие листаемого экрана с теми аргументами, которые подставил бы aiogram."""
    handler = getattr(stats_handlers, name)
    kwargs = {"match": _match(stats_handlers, pattern_name, data)}
    if "bot" in handler.__code__.co_varnames[:handler.__code__.co_argcount]:
        kwargs["bot"] = fake_bot
    await handler(make_callback(data), **kwargs)


@pytest.mark.parametrize("name, data, pattern_name, patched_func, rendered", _EDIT_PAGE_CASES)
@pytest.mark.asyncio
async def test_page_edit_swallows_message_not_modified(
    bot_module, make_callback, fake_bot, api, name, data, pattern_name, patched_func, rendered
):
    stats_handlers = bot_module("stats_handlers")
    api.fail_next(EditMessageText, _bad_request("Bad Request: message is not modified: specified new "
                                                "message content and reply markup are exactly the same"))

    with patch.object(stats_handlers, patched_func, return_value=rendered):
        await _press_page(stats_handlers, make_callback, fake_bot, name, data, pattern_name)

    # Хендлер дошёл до правки (не выпал раньше на проверке данных) — и правка не прошла.
    assert api.answers == [None]
    assert api.edited_texts == []


@pytest.mark.parametrize("name, data, pattern_name, patched_func, rendered", _EDIT_PAGE_CASES)
@pytest.mark.asyncio
async def test_page_edit_reraises_other_bad_request(
    bot_module, make_callback, fake_bot, api, name, data, pattern_name, patched_func, rendered
):
    stats_handlers = bot_module("stats_handlers")
    api.fail_next(EditMessageText, _bad_request("Bad Request: chat not found"))

    with patch.object(stats_handlers, patched_func, return_value=rendered):
        with pytest.raises(TelegramBadRequest, match="chat not found"):
            await _press_page(stats_handlers, make_callback, fake_bot, name, data, pattern_name)


# ---------------------------------------------------------------------------
# _page_nav_rows — подписи диапазонов
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "offset, has_prev, has_next, expected",
    [
        (0, False, True, [("11–20 →", "p:10")]),
        (10, True, True, [("← 1–10", "p:0"), ("21–30 →", "p:20")]),
        (20, True, True, [("« 1–10", "p:0"), ("← 11–20", "p:10"), ("31–40 →", "p:30")]),
        (50, True, False, [("« 1–10", "p:0"), ("← 41–50", "p:40")]),
    ],
)
def test_page_nav_rows_label_ranges_and_offer_first_page_only_from_offset_twenty(
    bot_module, offset, has_prev, has_next, expected
):
    stats_handlers = bot_module("stats_handlers")

    [row] = stats_handlers._page_nav_rows("p", offset, has_prev, has_next)

    assert [(b.text, b.callback_data) for b in row] == expected


def test_page_nav_rows_empty_without_neighbouring_pages(bot_module):
    stats_handlers = bot_module("stats_handlers")
    assert stats_handlers._page_nav_rows("p", 0, False, False) == []


# ---------------------------------------------------------------------------
# callback_tonight_game
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_tonight_registry(bot_module, monkeypatch):
    """Реестр ответов `/tonight` — модульный словарь процесса; каждому тесту — пустой."""
    monkeypatch.setattr(bot_module("stats_handlers"), "_tonight_sent", {})


async def _tap_tonight(stats_handlers, make_callback, fake_bot, data="tn:555:DET:NYR"):
    match = _match(stats_handlers, "TONIGHT_GAME_CALLBACK_PATTERN", data)
    await stats_handlers.callback_tonight_game(make_callback(data), fake_bot, match)


@pytest.mark.asyncio
async def test_tonight_game_opens_card_when_game_already_in_db(bot_module, make_callback, fake_bot):
    stats_handlers = bot_module("stats_handlers")

    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "send_game_card_message") as mock_send_card:
        await _tap_tonight(stats_handlers, make_callback, fake_bot)

    mock_send_card.assert_awaited_once_with(fake_bot, 100, 555)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("premium", "footer"),
    [
        (False, "<footer>Сезон 2026/27<br>🔒 Прогноз модели — в премиуме: /premium</footer>"),
        (True, "<footer>Сезон 2026/27<br>Оценка модели (не совет): победа New York Rangers — 61%</footer>"),
    ],
)
async def test_tonight_game_shows_season_preview_when_game_not_yet_in_db(
    bot_module, make_callback, fake_bot, api, premium, footer
):
    """Превью матча, которого ещё нет в БД; строка прогноза — только премиум-чату (Задача 96):
    премиум проверяется по чату нажатия, прогноз — настоящей `with_model_prediction`."""
    stats_handlers = bot_module("stats_handlers")
    prediction = {"count_rows": 1, "task": ["home_win"], "probability": [0.61]}

    with patch.object(stats_handlers, "game_exists", return_value=False), \
            patch.object(stats_handlers, "matchup_season_preview", return_value=("PREVIEW", "Сезон 2026/27")) as mock_preview, \
            patch.object(stats_handlers.premium, "is_premium", return_value=premium) as mock_premium, \
            patch.object(bot_module("bot_messages"), "fetch_all", return_value=prediction) as mock_fetch, \
            patch.object(bot_module("bot_messages"), "team_name_for_abbrev", return_value="New York Rangers"), \
            patch.object(stats_handlers, "truncate_telegram_text", side_effect=lambda text, **kw: text):
        await _tap_tonight(stats_handlers, make_callback, fake_bot)

    mock_preview.assert_called_once_with("DET", "NYR")
    mock_premium.assert_called_once_with(make_callback("").message.chat.id)
    assert [c.args[1] for c in mock_fetch.call_args_list] == ([(555,)] if premium else [])
    [sent] = api.sent_rich
    # Тело превью, затем подвал: «Сезон …» уехал из заголовка в <footer>.
    assert body(sent) == rich_html("PREVIEW") + footer


@pytest.mark.asyncio
async def test_tonight_game_repeat_tap_points_to_sent_card_instead_of_copy(
    bot_module, make_callback, fake_bot, api
):
    """Повторное нажатие той же кнопки — не вторая карточка, а ответ-цитата на
    уже отправленную; третье нажатие убирает прошлую ссылку, чтобы ссылки не
    копились (отзыв 2026-10-03)."""
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        await _tap_tonight(stats_handlers, make_callback, fake_bot)

        sent = api.outgoing
        assert [body(m) for m in sent] == [rich_html("CARD"), rich_html(stats_handlers._TONIGHT_ALREADY_SENT)]
        assert sent[1].reply_parameters.message_id == 1, "цитата — на первую карточку"
        assert api.sent(DeleteMessage) == []

        await _tap_tonight(stats_handlers, make_callback, fake_bot)

    sent = api.outgoing
    assert [body(m) for m in sent].count(rich_html("CARD")) == 1
    assert sent[2].reply_parameters.message_id == 1
    assert [(d.chat_id, d.message_id) for d in api.sent(DeleteMessage)] == [(100, 2)]


@pytest.mark.asyncio
async def test_tonight_game_resends_card_when_original_was_deleted(bot_module, make_callback, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        api.fail_next(SendRichMessage, _bad_request("Bad Request: message to be replied not found"))
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        # Следующее нажатие ссылается уже на новую карточку.
        await _tap_tonight(stats_handlers, make_callback, fake_bot)

    sent = api.outgoing
    assert [body(m) for m in sent] == [
        rich_html("CARD"), rich_html("CARD"), rich_html(stats_handlers._TONIGHT_ALREADY_SENT),
    ]
    assert sent[2].reply_parameters.message_id == 2


@pytest.mark.asyncio
async def test_tonight_game_resend_removes_pointer_to_deleted_card(bot_module, make_callback, fake_bot, api):
    """Карточку удалили, а ссылка на неё осталась — при повторной отправке
    карточки ссылка на удалённую тоже удаляется."""
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        api.fail_next(SendRichMessage, _bad_request("Bad Request: message to be replied not found"))
        await _tap_tonight(stats_handlers, make_callback, fake_bot)

    assert [body(m) for m in api.outgoing] == [
        rich_html("CARD"), rich_html(stats_handlers._TONIGHT_ALREADY_SENT), rich_html("CARD"),
    ]
    assert [(d.chat_id, d.message_id) for d in api.sent(DeleteMessage)] == [(100, 2)]


@pytest.mark.asyncio
async def test_tonight_game_pointer_delete_tolerates_only_gone_messages(
    bot_module, make_callback, fake_bot, api
):
    """Прошлую ссылку уже удалили — нажатие проходит; посторонняя ошибка
    удаления всплывает."""
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        api.fail_next(DeleteMessage, _bad_request("Bad Request: message to delete not found"))
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        assert len(api.outgoing) == 3, "третье нажатие отправило ссылку, ошибка удаления не помешала"

        api.fail_next(DeleteMessage, _bad_request("Bad Request: chat not found"))
        with pytest.raises(TelegramBadRequest, match="chat not found"):
            await _tap_tonight(stats_handlers, make_callback, fake_bot)


@pytest.mark.asyncio
async def test_tonight_game_pointer_reraises_unrelated_bad_request(bot_module, make_callback, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        await _tap_tonight(stats_handlers, make_callback, fake_bot)
        api.fail_next(SendRichMessage, _bad_request("Bad Request: chat not found"))
        with pytest.raises(TelegramBadRequest, match="chat not found"):
            await _tap_tonight(stats_handlers, make_callback, fake_bot)


@pytest.mark.asyncio
async def test_tonight_game_card_after_preview_is_a_new_answer(bot_module, make_callback, fake_bot, api):
    """Превью (матча ещё нет в БД) и карточка — разные ответы: после загрузки
    матча то же нажатие шлёт карточку, а не ссылку на превью."""
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "matchup_season_preview", return_value=("PREVIEW", "Сезон 2026/27")), \
            patch.object(stats_handlers.premium, "is_premium", return_value=False), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        with patch.object(stats_handlers, "game_exists", return_value=False):
            await _tap_tonight(stats_handlers, make_callback, fake_bot)
        with patch.object(stats_handlers, "game_exists", return_value=True):
            await _tap_tonight(stats_handlers, make_callback, fake_bot)
        # Другой матч — свой ответ, не ссылка.
        with patch.object(stats_handlers, "game_exists", return_value=True):
            await _tap_tonight(stats_handlers, make_callback, fake_bot, "tn:556:BOS:MIN")

    assert [body(m) for m in api.outgoing] == [
        rich_html("PREVIEW") + "<footer>Сезон 2026/27<br>🔒 Прогноз модели — в премиуме: /premium</footer>",
        rich_html("CARD"), rich_html("CARD"),
    ]


@pytest.mark.asyncio
async def test_tonight_answers_are_kept_per_chat(bot_module, make_callback, fake_bot, api):
    """Ответ, отправленный в одном чате, не превращает нажатие в другом чате в ссылку."""
    stats_handlers = bot_module("stats_handlers")
    data = "tn:555:DET:NYR"
    match = _match(stats_handlers, "TONIGHT_GAME_CALLBACK_PATTERN", data)
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("CARD", [])):
        await stats_handlers.callback_tonight_game(make_callback(data, chat_id=100), fake_bot, match)
        await stats_handlers.callback_tonight_game(make_callback(data, chat_id=101), fake_bot, match)

    assert [(m.chat_id, body(m)) for m in api.outgoing] == [(100, rich_html("CARD")), (101, rich_html("CARD"))]


# ---------------------------------------------------------------------------
# callback_expand_digest_game
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expand_digest_game_opens_card_for_valid_id(bot_module, make_callback, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    data = "dg:777"

    with patch.object(stats_handlers, "send_game_card_message") as mock_send:
        await stats_handlers.callback_expand_digest_game(
            make_callback(data), fake_bot, _match(stats_handlers, "DIGEST_EXPAND_CALLBACK_PATTERN", data)
        )

    mock_send.assert_awaited_once_with(fake_bot, 100, 777)
    assert api.answers == [None]


# ---------------------------------------------------------------------------
# send_game_card_message — shared by callback_tonight_game and
# callback_expand_digest_game, but only ever exercised there via a mock; test
# its own branching directly so a regression here isn't hidden by the mocks.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_game_card_message_reports_missing_game(bot_module, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=False), \
            patch.object(stats_handlers, "game_message") as mock_game_message:
        await stats_handlers.send_game_card_message(fake_bot, chat_id=42, game_id=999)

    mock_game_message.assert_not_called()
    assert [body(m) for m in api.outgoing] == [rich_html("Такого матча нет в базе бота.")]


@pytest.mark.asyncio
async def test_send_game_card_message_builds_goal_video_buttons(bot_module, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    goals_meta = [
        {"label": "1:0 Ovechkin 5:30", "game_id": 999, "event_id": 1},
        {"label": "2:0 Ovechkin 10:00", "game_id": 999, "event_id": 2},
    ]
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("GAME TEXT", goals_meta)):
        await stats_handlers.send_game_card_message(fake_bot, chat_id=42, game_id=999)

    [sent] = api.sent_rich
    assert body(sent) == rich_html("GAME TEXT")
    assert sent.chat_id == 42
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["gv:999:1", "gv:999:2"]
    assert all(re.match(stats_handlers.GOAL_VIDEO_CALLBACK_PATTERN, b.callback_data) for b in buttons)


@pytest.mark.asyncio
async def test_send_game_card_message_uses_no_markup_when_no_goals(bot_module, fake_bot, api):
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("GAME TEXT", [])):
        await stats_handlers.send_game_card_message(fake_bot, chat_id=42, game_id=999)

    [sent] = api.sent_rich
    assert sent.reply_markup is None


# ---------------------------------------------------------------------------
# handle_goal_video
# ---------------------------------------------------------------------------

async def _tap_goal(stats_handlers, make_callback, fake_bot, data="gv:100:200"):
    match = _match(stats_handlers, "GOAL_VIDEO_CALLBACK_PATTERN", data)
    await stats_handlers.handle_goal_video(make_callback(data), fake_bot, match)


@pytest.mark.asyncio
async def test_goal_video_reports_unavailable_when_download_fails(
    bot_module, make_callback, fake_bot, api, goal_clip_store
):
    stats_handlers = bot_module("stats_handlers")

    with patch.object(stats_handlers, "download_goal_video", return_value=None) as mock_download:
        await _tap_goal(stats_handlers, make_callback, fake_bot)

    mock_download.assert_called_once_with(100, 200)
    assert [body(m) for m in api.outgoing] == [rich_html("Видео пока недоступно.")]
    assert api.answers == ["Загружаю видео гола..."]


@pytest.mark.asyncio
async def test_goal_video_sends_video_with_hints_and_deletes_local_files(
    bot_module, make_callback, fake_bot, api, tmp_path, goal_clip_store
):
    from aiogram.types import FSInputFile

    stats_handlers = bot_module("stats_handlers")
    video_replay = bot_module("video_replay")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-mp4")
    thumb_path = tmp_path / "thumb.jpg"
    thumb_path.write_bytes(b"fake-jpg")
    delivery = video_replay.GoalVideoDelivery(
        path=str(video_path), caption="🚨 <b>W. Borgen</b>",
        width=640, height=360, duration=5, thumb_path=str(thumb_path),
    )

    with patch.object(stats_handlers, "download_goal_video", return_value=delivery):
        await _tap_goal(stats_handlers, make_callback, fake_bot)

    [sent] = api.sent(SendVideo)
    assert sent.chat_id == 100
    assert sent.supports_streaming is True
    assert (sent.width, sent.height, sent.duration) == (640, 360, 5)
    assert isinstance(sent.video, FSInputFile) and sent.video.path == str(video_path)
    assert isinstance(sent.thumbnail, FSInputFile) and sent.thumbnail.path == str(thumb_path)
    assert sent.caption == "🚨 <b>W. Borgen</b>"
    assert sent.parse_mode == "HTML"
    assert not video_path.exists(), "clip must be deleted after sending"
    assert not thumb_path.exists(), "thumbnail must be deleted after sending"
    assert goal_clip_store == {(100, 200): ("fid-1", "🚨 <b>W. Borgen</b>")}, "file_id — в кэш"


@pytest.mark.asyncio
async def test_goal_video_reports_error_and_still_cleans_up_on_send_failure(
    bot_module, make_callback, fake_bot, api, tmp_path, goal_clip_store
):
    stats_handlers = bot_module("stats_handlers")
    video_replay = bot_module("video_replay")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-mp4")
    delivery = video_replay.GoalVideoDelivery(path=str(video_path), caption="")
    api.fail_next(SendVideo, RuntimeError("network down"))

    with patch.object(stats_handlers, "download_goal_video", return_value=delivery):
        await _tap_goal(stats_handlers, make_callback, fake_bot)

    assert api.sent(SendVideo) == []
    assert body(api.outgoing[-1]) == rich_html("Ошибка при отправке видео.")
    assert not video_path.exists(), "clip must be deleted even when send_video() raises"
    assert goal_clip_store == {}


@pytest.mark.asyncio
async def test_goal_video_reuploads_once_when_telegram_rejects_cached_file_id(
    bot_module, make_callback, fake_bot, api, tmp_path, goal_clip_store, caplog
):
    """Задача 87: протухший ``file_id`` из кэша — ошибка в логе, клип скачивается и
    загружается заново, кэш получает новый ``file_id``."""
    stats_handlers = bot_module("stats_handlers")
    video_replay = bot_module("video_replay")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-mp4")
    delivery = video_replay.GoalVideoDelivery(path=str(video_path), caption="<b>GOAL</b>")
    goal_clip_store[(100, 200)] = ("stale", "<b>GOAL</b>")
    api.fail_next(SendVideo, _bad_request("Bad Request: wrong file identifier/HTTP URL specified"))

    with patch.object(stats_handlers, "download_goal_video", return_value=delivery) as mock_download:
        await _tap_goal(stats_handlers, make_callback, fake_bot)

    mock_download.assert_called_once_with(100, 200)
    [sent] = api.sent(SendVideo)
    assert sent.video.path == str(video_path)
    assert goal_clip_store == {(100, 200): ("fid-1", "<b>GOAL</b>")}
    assert [r.levelname for r in caplog.records if "rejected cached clip" in r.getMessage()] == ["ERROR"]


def _replay(**goal):
    """Ответ /v1/ppt-replay в форме NHL API (гол Borgen, BOS@SJS 2026-10-13), поля
    гола переопределяются аргументами."""
    base = {
        "periodDescriptor": {"number": 3, "periodType": "REG", "maxRegulationPeriods": 3},
        "strength": "ev",
        "name": {"default": "W. Borgen"},
        "teamAbbrev": {"default": "BOS"},
        "highlightClip": 6406828806112,
        "goalsToDate": 1,
        "awayScore": 6,
        "homeScore": 3,
        "timeInPeriod": "18:18",
        "goalModifier": "empty-net",
        "assists": [],
    }
    return {
        "awayTeam": {"abbrev": "BOS"}, "homeTeam": {"abbrev": "SJS"},
        "goal": {**base, **goal},
    }


_TEAM_NAMES = {"BOS": "Boston Bruins", "SJS": "San Jose Sharks", "MTL": "Montréal Canadiens"}


@pytest.fixture
def video_replay(bot_module):
    """`video_replay` с названиями команд из `_TEAM_NAMES` вместо запроса к `teams`."""
    module = bot_module("video_replay")
    with patch.object(module, "team_name_for_abbrev", side_effect=_TEAM_NAMES.get):
        yield module


def test_goal_caption_scorer_season_goal_time_score_and_empty_net(video_replay):
    """Команды в подписи — полными названиями по коду из ответа API (Задача 98)."""
    assert video_replay.goal_caption(_replay()) == (
        "🚨 <b>W. Borgen</b> (Boston Bruins) — 1-й гол в сезоне\n"
        "Без передач\n"
        "3-й период, 18:18 · Boston Bruins 6:3 San Jose Sharks · в пустые ворота"
    )


def test_goal_caption_assists_with_season_totals_overtime_power_play(video_replay):
    replay = _replay(
        periodDescriptor={"number": 4, "periodType": "OT", "maxRegulationPeriods": 3},
        strength="pp",
        goalModifier="none",
        goalsToDate=3,
        assists=[
            {"name": {"default": "C. Mittelstadt"}, "assistsToDate": 1},
            {"name": {"default": "N. Zadorov"}, "assistsToDate": 3},
        ],
    )

    assert video_replay.goal_caption(replay) == (
        "🚨 <b>W. Borgen</b> (Boston Bruins) — 3-й гол в сезоне\n"
        "Передачи: C. Mittelstadt (1), N. Zadorov (3)\n"
        "овертайм, 18:18 · Boston Bruins 6:3 San Jose Sharks · в большинстве"
    )


def test_goal_caption_shootout_goal_without_season_number(video_replay):
    """У гола серии буллитов (BUF@MTL 2026020092/804) нет ``goalsToDate``."""
    replay = _replay(
        periodDescriptor={"number": 5, "periodType": "SO", "maxRegulationPeriods": 3},
        name={"default": "C. Caufield"},
        teamAbbrev={"default": "MTL"},
        goalModifier="none",
    )
    del replay["goal"]["goalsToDate"]

    assert video_replay.goal_caption(replay) == (
        "🚨 <b>C. Caufield</b> (Montréal Canadiens) — гол в серии буллитов, "
        "Boston Bruins – San Jose Sharks"
    )


def test_download_goal_video_fails_loudly_without_ffmpeg_before_any_network(
    bot_module, monkeypatch
):
    video_replay = bot_module("video_replay")
    http_calls = []

    def record_get(*args, **kwargs):
        http_calls.append(args)
        raise AssertionError("no HTTP request expected when ffmpeg is missing")

    monkeypatch.setattr(video_replay.shutil, "which", lambda name: None)
    monkeypatch.setattr(video_replay.requests, "get", record_get)

    with pytest.raises(RuntimeError, match="ffmpeg"):
        video_replay.download_goal_video(100, 200)

    assert http_calls == []


# ---------------------------------------------------------------------------
# dispatch_day_digest_messages — ветки «нет матчей», «один матч», «несколько»
# ---------------------------------------------------------------------------

def _digest_db_routes(home, away):
    """Маршруты сводки дня: подписи кнопок «AAA – BBB» любому game_id и счёт
    1:0 (гол в первом периоде) с командами `home(gid)`/`away(gid)`."""
    return [
        ("JOIN teams th", lambda params: [(gid, "AAA", "BBB") for gid in params[0]]),
        ("get_game_stats", lambda params: [
            (1, 0, 0, 0, 0, False, False, "home", home(params[0])),
            (0, 0, 0, 0, 0, False, False, "away", away(params[0])),
        ]),
        ("get_goals_game", [
            ("X", "C", None, None, None, None, None, 1, "01:00", 1, 0,
             False, False, False, True, 1, 1),
        ]),
    ]


@pytest.fixture
def digest_labels_db(fake_db_router):
    """БД сводки дня: матч game_id — «H<id> 1:0 A<id>», кнопки «AAA – BBB»."""
    return fake_db_router(_digest_db_routes(lambda gid: f"H{gid}", lambda gid: f"A{gid}"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "games",
    [
        pytest.param([(0, "Матчей не найдено", [])], id="no_real_games"),
        pytest.param(
            [(1, "Матч 1 текст", [{"label": "▶ 1:0", "game_id": 1, "event_id": 5}])], id="single_game"
        ),
        pytest.param(
            [(1, "Матч 1 текст", []), (2, "Матч 2 текст", [])], id="multi_game"
        ),
    ],
)
async def test_dispatch_day_digest_ends_with_hint_and_carries_only_match_buttons(
    bot_module, fake_bot, api, digest_labels_db, games
):
    """Дайджест (`/today`, ИИ, рассылка): подсказка «Ещё: …» — подвал последнего (единственного)
    сообщения, отдельного сообщения нет; кнопки — только карточки матчей (`dg:`) и видео голов (`gv:`)."""
    stats_handlers = bot_module("stats_handlers")

    await stats_handlers.dispatch_day_digest_messages(fake_bot, 100, "2025-12-01", games)

    [digest] = api.sent_rich
    assert api.sent_messages == [], "подсказка — подвал, а не отдельное сообщение"
    text = body(digest)
    assert text.endswith("</footer>")
    assert text.count("<footer>") == 1
    footer = text.rsplit("<footer>", 1)[1]
    assert "Ещё: /tonight" in footer
    assert footer.endswith("Настроить подписки: /subscriptions</footer>")
    rows = digest.reply_markup.inline_keyboard if digest.reply_markup else []
    data = [b.callback_data for row in rows for b in row]
    assert all(d.startswith(("dg:", "gv:")) for d in data)
    assert bool(data) is (games[0][0] != 0), "у «матчей не найдено» кнопок нет"


@pytest.mark.asyncio
async def test_dispatch_day_digest_multi_game_numbers_summary_and_labels_buttons_in_three_columns(
    bot_module, fake_bot, api, digest_labels_db
):
    stats_handlers = bot_module("stats_handlers")
    games = [(i, f"<b>H{i} 1:0 A{i}</b>\nдетали", []) for i in range(1, 5)]

    await stats_handlers.dispatch_day_digest_messages(fake_bot, 100, "2025-12-01", games)

    summary = api.sent_rich[0]
    text = body(summary)
    # Заголовок — жирный, дата и число матчей — в подвале, не в заголовке.
    assert text.startswith("<p><b>Матчи ночи</b></p>")
    # Матчи разделены пустой строкой (DIGEST_GAME_SEPARATOR); строка матча без <b>,
    # периоды — курсивом.
    assert rich_html(
        "1. H1 1:0 A1\n  <i>1:0, 0:0, 0:0</i>\n\n"
        "2. H2 1:0 A2\n  <i>1:0, 0:0, 0:0</i>\n\n"
        "3. H3 1:0 A3"
    ).removesuffix("</p>") + "<br>" in text
    footer = text.rsplit("<footer>", 1)[1]
    assert text.endswith("</footer>")
    assert footer.startswith("Игровой день 2025-12-01 · матчей: 4<br>Кнопка матча — полная карточка и видео голов.<br>")
    assert "Настроить подписки: /subscriptions" in footer
    rows = summary.reply_markup.inline_keyboard
    assert [len(r) for r in rows] == [3, 1]
    assert [b.text for r in rows for b in r] == [f"{n}. AAA – BBB" for n in range(1, 5)]
    assert [b.callback_data for r in rows for b in r] == [f"dg:{n}" for n in range(1, 5)]
    # Подписи — один запрос на все матчи дня, а не по запросу на кнопку.
    assert sum("JOIN teams th" in q for q, _ in digest_labels_db.executed) == 1


# ---------------------------------------------------------------------------
# dispatch_day_digest_messages — truncation marker on the multi-game summary
# (Задача 11: граница rich-сообщения 32768 отбрасывает заголовки матчей молча без
# маркера «показаны N из M»).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_day_digest_short_summary_has_no_truncation_marker(
    bot_module, fake_bot, api, digest_labels_db
):
    stats_handlers = bot_module("stats_handlers")
    games = [(1, "Матч 1 текст", []), (2, "Матч 2 текст", [])]

    await stats_handlers.dispatch_day_digest_messages(fake_bot, 100, "2025-12-01", games)

    sent_text = body(api.sent_rich[0])
    assert "Показаны" not in sent_text
    assert "1. H1 1:0 A1" in sent_text
    assert "2. H2 1:0 A2" in sent_text


@pytest.mark.asyncio
async def test_dispatch_day_digest_long_summary_marks_shown_of_total_matches(
    bot_module, fake_bot, api, fake_db_router
):
    """Сводка дня, которая не влезает в лимит Telegram, должна честно сказать,
    сколько из всех матчей дня реально попало в текст, а не молчать про
    отброшенные заголовки."""
    stats_handlers = bot_module("stats_handlers")
    # Длинные названия, а не сотни матчей: обрезка по символам должна сработать раньше
    # лимита в 500 блоков (абзацев), который она не охраняет.
    fake_db_router(_digest_db_routes(lambda gid: "Х" * 120, lambda gid: "Y" * 120))
    total_games = 200
    games = [
        (
            i + 1,
            f"Матч {i + 1}: команда А против команды Б, финальный счёт " + "X" * 40,
            [],
        )
        for i in range(total_games)
    ]

    await stats_handlers.dispatch_day_digest_messages(fake_bot, 100, "2025-12-01", games)

    sent_text = body(api.sent_rich[0])
    # Лимит rich-сообщения — на текст, а не на разметку: теги не считаются, сущность — один символ.
    plain = html.unescape(re.sub(r"<[^>]+>", "", sent_text))
    assert len(plain) <= bot_module("bot_messages").RICH_MESSAGE_MAX_LENGTH
    assert sent_text.count("<p>") <= 500, "лимит Telegram — 500 блоков"
    match = re.search(r"Показаны (\d+) из (\d+) матчей", sent_text)
    assert match is not None
    shown, total = int(match.group(1)), int(match.group(2))
    assert total == total_games
    assert 0 < shown < total_games
    # Обрезка — целыми матчами (строка периодов в <i>): ни одного незакрытого тега
    # (Telegram отверг бы HTML) и ни одного матча без строки периодов.
    # <b> — только у заголовка «Матчи ночи».
    assert sent_text.count("<b>") == sent_text.count("</b>") == 1
    # Курсив — строки периодов и сам маркер усечения.
    assert sent_text.count("<i>") == sent_text.count("</i>") == shown + 1
    assert sent_text.count("  <i>1:0, 0:0, 0:0</i>") == shown
    # Маркер усечения — первая строка подвала (служебное), подвал на месте в конце.
    assert f"<footer><i>Показаны {shown} из {total} матчей.</i><br>Игровой день " in sent_text
    assert sent_text.endswith("</footer>")


# ---------------------------------------------------------------------------
# Callback regex patterns directly — malformed/malicious shapes must not match.
# Хендлер вызывается только на данных, прошедших паттерн регистрации, поэтому
# проверка формы данных живёт здесь, а не в хендлерах.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data, expected",
    [
        ("tn:123:DET:NYR", True),
        ("tn:abc:DET:NYR", False),  # non-digit game id
        ("tn:123:DET", False),  # missing home team
        ("tn:-1:DET:NYR", False),  # negative id: \d+ does not match '-'
    ],
)
def test_tonight_game_callback_pattern_matches_only_well_formed_data(bot_module, data, expected):
    stats_handlers = bot_module("stats_handlers")
    assert bool(re.match(stats_handlers.TONIGHT_GAME_CALLBACK_PATTERN, data)) is expected


@pytest.mark.parametrize(
    "data, expected",
    [
        ("st:players_season_stats:points:0", True),
        ("sa:players_advanced_stats:sat_pct:10", True),
        ("tm:goals:20", True),
        ("st:menu", True),
        ("tm:save", True),
        ("st:players_season_stats:points:-5", False),  # \d+ rejects a leading '-'
        ("st:players_season_stats:points", False),  # missing offset
        ("tm:goals", False),  # missing offset
        ("pl:points:0", False),  # префикс убранного /leaders
        ("st:players; DROP TABLE t;--:points:0", False),  # SQL-injection-shaped garbage
        ("tm:points; DROP TABLE teams;--:0", False),
        ("st:a:b:c:0", False),  # ключ — не больше «таблица:колонка»
        ("st:not_a_table:not_a_col:0", False),  # ключ вне PLAYER_STAT_TITLES
        ("tm:procent_points:0", False),  # % очков убран из /team_stats — в /standings
        ("tm:players_season_stats:points:0", False),  # ключ игроков с префиксом команд
        ("st:goals:0", False),  # ключ команд с префиксом игроков
    ],
)
def test_stat_screen_callback_pattern_matches_only_well_formed_data(bot_module, data, expected):
    stats_handlers = bot_module("stats_handlers")
    assert bool(re.match(stats_handlers.STAT_SCREEN_CALLBACK_PATTERN, data)) is expected


@pytest.mark.parametrize(
    "pattern_name, data, expected",
    [
        ("DIGEST_EXPAND_CALLBACK_PATTERN", "dg:777", True),
        ("DIGEST_EXPAND_CALLBACK_PATTERN", "dg:not-an-int", False),
        ("GOAL_VIDEO_CALLBACK_PATTERN", "gv:2026020001:55", True),
        ("GOAL_VIDEO_CALLBACK_PATTERN", "gv:2026020001", False),  # без события
        ("GOAL_VIDEO_CALLBACK_PATTERN", "gv:x:55", False),
    ],
)
def test_other_button_patterns_match_only_well_formed_data(bot_module, pattern_name, data, expected):
    stats_handlers = bot_module("stats_handlers")
    assert bool(re.match(getattr(stats_handlers, pattern_name), data)) is expected
