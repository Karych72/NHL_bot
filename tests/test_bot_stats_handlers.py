"""Unit tests for telegram_bot/stats_handlers.py: parsing of inline-keyboard
callback_data strings and the pure keyboard builders that produce them.

Handlers are exercised through the conftest fake Update/CallbackContext
fixtures (no real PTB Update/CallbackQuery objects, no Telegram network call).
Where a handler calls into bot_messages for DB-backed rendering, that
collaborator function is patched on stats_handlers' own namespace (the same
boundary style already used for bot_messages.fetch_all elsewhere in this
suite) so what's actually under test — callback_data parsing, guard clauses,
offset arithmetic, BadRequest handling — is isolated from database access.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from telegram.error import BadRequest


# ---------------------------------------------------------------------------
# Pure keyboard builders — callback_data shape and offset math
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "builder_name, args, prefix, footer_kind, parent_attr",
    [
        (
            "conversation_player_stat_keyboard",
            ("players_season_stats", "points"),
            "st:players_season_stats:points:",
            "menu_nav",
            "PLAYER_FIELD",
        ),
        (
            "conversation_team_stat_keyboard",
            ("procent_points",),
            "tm:procent_points:",
            "menu_nav",
            "TEAM_STATS",
        ),
        (
            "standalone_player_stat_keyboard",
            ("players_season_stats", "points"),
            "sa:players_season_stats:points:",
            "close",
            None,
        ),
    ],
)
def test_pagination_keyboard_offsets_and_prev_floor(
    bot_module, builder_name, args, prefix, footer_kind, parent_attr
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    builder = getattr(stats_handlers, builder_name)

    markup = builder(*args, 20, has_prev=True, has_next=True)
    nav_row = markup.inline_keyboard[0]
    assert [b.callback_data for b in nav_row] == [f"{prefix}10", f"{prefix}30"]

    # prev never goes negative, even near the start of the list
    markup_floor = builder(*args, 5, has_prev=True, has_next=False)
    assert markup_floor.inline_keyboard[0][0].callback_data == f"{prefix}0"

    footer = markup.inline_keyboard[-1]
    if footer_kind == "menu_nav":
        # «« Назад»» на родительское подменю, «В начало» на корень, «Готово»
        # на выход: родитель, если он есть, а не корень.
        assert [b.callback_data for b in footer] == [
            str(getattr(dialog_states, parent_attr)),
            str(dialog_states.CHOOSE_STATS),
            str(dialog_states.END_CONVERSATION),
        ]
    else:
        assert [b.callback_data for b in footer] == ["sa:close"]


@pytest.mark.parametrize(
    "builder_name, args",
    [
        ("conversation_player_stat_keyboard", ("players_season_stats", "points")),
        ("conversation_team_stat_keyboard", ("procent_points",)),
    ],
)
def test_pagination_keyboard_omits_nav_row_on_single_page(bot_module, builder_name, args):
    stats_handlers = bot_module("stats_handlers")
    builder = getattr(stats_handlers, builder_name)

    markup = builder(*args, 0, has_prev=False, has_next=False)

    # Only the footer row («« Назад»» / «В начало» / «Готово») remains — no
    # empty pagination row.
    assert len(markup.inline_keyboard) == 1


# ---------------------------------------------------------------------------
# «« Назад»» страницы стата игрока/вратаря — родитель выводится из
# (table, column), а не хранится: callback_data пагинации
# (`st:{table}:{column}:{offset}`) не оставляет места для лишнего поля.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "table, column, expected_parent_attr",
    [
        ("players_season_stats", "points", "PLAYER_FIELD"),
        ("players_season_stats", "time_on_ice_per_game", "PLAYER_FIELD"),
        # shootout_pct физически лежит в players_season_stats, но в меню
        # (script_bot.bot_player_advanced_menu) он в advanced-подменю, не в
        # полевых — правило деривации должно это отражать.
        ("players_season_stats", "shootout_pct", "PLAYER_ADVANCED_SUBMENU"),
        ("players_advanced_stats", "sat_pct", "PLAYER_ADVANCED_SUBMENU"),
        ("players_shot_types", "goals_wrist", "PLAYER_ADVANCED_SUBMENU"),
        ("goalies_season_stats", "wins", "PLAYER_GOALIE"),
    ],
)
def test_conversation_player_stat_keyboard_back_targets_correct_submenu(
    bot_module, table, column, expected_parent_attr
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")

    markup = stats_handlers.conversation_player_stat_keyboard(
        table, column, 0, has_prev=False, has_next=False
    )
    [footer] = markup.inline_keyboard
    assert footer[0].callback_data == str(getattr(dialog_states, expected_parent_attr))


def test_leaders_category_keyboard_has_fixed_three_categories(bot_module):
    stats_handlers = bot_module("stats_handlers")
    markup = stats_handlers.leaders_category_keyboard()
    [row] = markup.inline_keyboard
    assert [b.callback_data for b in row] == ["pl:pick:points", "pl:pick:goals", "pl:pick:assists"]


def test_leaderboard_nav_keyboard_prepends_pagination_row(bot_module):
    stats_handlers = bot_module("stats_handlers")
    markup = stats_handlers.leaderboard_nav_keyboard("goals", 20, has_prev=True, has_next=True)
    nav_row = markup.inline_keyboard[0]
    assert [b.callback_data for b in nav_row] == ["pl:goals:10", "pl:goals:30"]
    assert markup.inline_keyboard[-1][0].callback_data == "pl:pick:points"


# ---------------------------------------------------------------------------
# callback_stats_player_page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_player_page_ignores_missing_query_data(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    update = make_callback_update(None)

    result = await stats_handlers.callback_stats_player_page(update, fake_context)

    assert result == dialog_states.SECOND
    assert update.callback_query.answers == []


@pytest.mark.asyncio
async def test_stats_player_page_ignores_data_not_matching_pattern(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("garbage")

    await stats_handlers.callback_stats_player_page(update, fake_context)

    assert update.callback_query.answers == []
    assert update.callback_query.edited_texts == []


@pytest.mark.asyncio
async def test_stats_player_page_answers_without_rendering_for_unknown_stat(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("st:not_a_table:not_a_col:0")

    await stats_handlers.callback_stats_player_page(update, fake_context)

    assert update.callback_query.answers == [None]
    assert update.callback_query.edited_texts == []


@pytest.mark.asyncio
async def test_stats_player_page_parses_offset_and_renders_known_stat(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("st:players_season_stats:points:20")

    with patch.object(
        stats_handlers, "player_stat_leaderboard_page", return_value=("PAGE TEXT", True, True)
    ) as mock_page:
        await stats_handlers.callback_stats_player_page(update, fake_context)

    mock_page.assert_called_once_with("Лучшие бомбардиры", "players_season_stats", "points", 20)
    [edit] = update.callback_query.edited_texts
    assert edit["text"] == "PAGE TEXT"
    assert edit["parse_mode"] == "HTML"
    nav_row = edit["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in nav_row] == [
        "st:players_season_stats:points:10",
        "st:players_season_stats:points:30",
    ]


# ---------------------------------------------------------------------------
# callback_stats_team_page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_team_page_ignores_data_not_matching_pattern(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tm:missing-offset")

    await stats_handlers.callback_stats_team_page(update, fake_context)

    assert update.callback_query.answers == []
    assert update.callback_query.edited_texts == []


@pytest.mark.asyncio
async def test_stats_team_page_answers_without_rendering_for_unknown_column(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tm:not_a_column:0")

    await stats_handlers.callback_stats_team_page(update, fake_context)

    assert update.callback_query.answers == [None]
    assert update.callback_query.edited_texts == []


@pytest.mark.asyncio
async def test_stats_team_page_parses_column_and_offset(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tm:power_play_percentage:30")

    with patch.object(
        stats_handlers, "team_stat_leaderboard_page", return_value=("TEAM PAGE", True, False)
    ) as mock_page:
        await stats_handlers.callback_stats_team_page(update, fake_context)

    mock_page.assert_called_once_with("Статистика большинства", "power_play_percentage", 30)
    assert update.callback_query.edited_texts[0]["text"] == "TEAM PAGE"


# ---------------------------------------------------------------------------
# callback_standalone_sa
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standalone_sa_close_clears_markup(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("sa:close")

    await stats_handlers.callback_standalone_sa(update, fake_context)

    assert update.callback_query.answers == [None]
    assert update.callback_query.edited_markups == [None]


@pytest.mark.asyncio
async def test_standalone_sa_ignores_malformed_data(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("sa:whatever")

    await stats_handlers.callback_standalone_sa(update, fake_context)

    assert update.callback_query.answers == [None]
    assert update.callback_query.edited_texts == []


@pytest.mark.asyncio
async def test_standalone_sa_answers_without_rendering_unknown_stat(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("sa:not_a_table:not_a_col:0")

    await stats_handlers.callback_standalone_sa(update, fake_context)

    assert update.callback_query.answers == [None]
    assert update.callback_query.edited_texts == []


@pytest.mark.asyncio
async def test_standalone_sa_renders_known_stat_with_close_only_footer(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("sa:players_season_stats:goals:0")

    with patch.object(
        stats_handlers, "player_stat_leaderboard_page", return_value=("GOALS PAGE", False, True)
    ):
        await stats_handlers.callback_standalone_sa(update, fake_context)

    edit = update.callback_query.edited_texts[0]
    assert edit["text"] == "GOALS PAGE"
    assert [b.callback_data for b in edit["reply_markup"].inline_keyboard[-1]] == ["sa:close"]


# ---------------------------------------------------------------------------
# BadRequest("message is not modified") is swallowed; anything else re-raises
# ---------------------------------------------------------------------------

# All five handlers below render via the same (text, has_prev, has_next) shape;
# only the handler under test and its callback_data/collaborator vary.
_RENDER_RETURN = ("t", False, False)

_BAD_REQUEST_CASES = [
    ("callback_stats_player_page", "st:players_season_stats:points:0", "player_stat_leaderboard_page"),
    ("callback_stats_team_page", "tm:procent_points:0", "team_stat_leaderboard_page"),
    ("callback_standalone_sa", "sa:players_season_stats:points:0", "player_stat_leaderboard_page"),
    ("callback_leaders_pick", "pl:pick:points", "stat_leaderboard_for_kind"),
    ("callback_leaderboard_page", "pl:points:0", "stat_leaderboard_for_kind"),
]


@pytest.mark.parametrize("handler_name, data, patched_func", _BAD_REQUEST_CASES)
@pytest.mark.asyncio
async def test_callback_swallows_message_not_modified(
    bot_module, make_callback_update, fake_context, handler_name, data, patched_func
):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update(data)
    handler = getattr(stats_handlers, handler_name)

    async def raise_not_modified(*args, **kwargs):
        raise BadRequest("Message is not modified")

    update.callback_query.edit_message_text = raise_not_modified

    with patch.object(stats_handlers, patched_func, return_value=_RENDER_RETURN):
        await handler(update, fake_context)  # must not raise

    # The handler ran its normal course up to the swallowed edit — it didn't
    # bail out earlier (e.g. on a guard clause) before ever reaching it.
    assert update.callback_query.answers == [None]


@pytest.mark.parametrize("handler_name, data, patched_func", _BAD_REQUEST_CASES)
@pytest.mark.asyncio
async def test_callback_reraises_other_bad_request(
    bot_module, make_callback_update, fake_context, handler_name, data, patched_func
):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update(data)
    handler = getattr(stats_handlers, handler_name)

    async def raise_other(*args, **kwargs):
        raise BadRequest("Chat not found")

    update.callback_query.edit_message_text = raise_other

    with patch.object(stats_handlers, patched_func, return_value=_RENDER_RETURN):
        with pytest.raises(BadRequest, match="Chat not found"):
            await handler(update, fake_context)


# ---------------------------------------------------------------------------
# bot_league_standings создаёт НОВОЕ сообщение (`send_message`, не
# `edit_message_text`) с клавиатурой меню (« Главное меню» → CHOOSE_STATS) —
# его message_id должен попасть в user_data так же, как у остальных мест,
# создающих новое сообщение с живой FSM-клавиатурой.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_league_standings_records_new_message_id_for_cancel(
    bot_module, make_callback_update, fake_context
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    update = make_callback_update(str(dialog_states.LEAGUE_STANDINGS))

    with patch.object(stats_handlers, "team_table", return_value="TABLE"):
        result = await stats_handlers.bot_league_standings(update, fake_context)

    assert result == dialog_states.FIRST
    sent = fake_context.bot.sent_messages[0]
    assert sent["text"] == "TABLE"
    [row] = sent["reply_markup"].inline_keyboard
    assert row[0].callback_data == str(dialog_states.CHOOSE_STATS)
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


# ---------------------------------------------------------------------------
# callback_tonight_game
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tonight_game_ignores_non_tn_prefixed_data(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("other:1:2:3")

    await stats_handlers.callback_tonight_game(update, fake_context)

    assert fake_context.bot.sent_messages == []
    assert update.callback_query.answers == []


@pytest.mark.asyncio
async def test_tonight_game_reports_malformed_button(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tn:1:DET")  # missing the home-team part

    await stats_handlers.callback_tonight_game(update, fake_context)

    assert fake_context.bot.sent_messages[0]["text"] == "Некорректная кнопка."


@pytest.mark.asyncio
async def test_tonight_game_reports_non_integer_game_id(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tn:abc:DET:NYR")

    await stats_handlers.callback_tonight_game(update, fake_context)

    assert fake_context.bot.sent_messages[0]["text"] == "Некорректная кнопка."


@pytest.mark.asyncio
async def test_tonight_game_opens_card_when_game_already_in_db(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tn:555:DET:NYR")

    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "send_game_card_message") as mock_send_card:
        await stats_handlers.callback_tonight_game(update, fake_context)

    mock_send_card.assert_awaited_once_with(fake_context, 100, 555)


@pytest.mark.asyncio
async def test_tonight_game_shows_season_preview_when_game_not_yet_in_db(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("tn:555:DET:NYR")

    with patch.object(stats_handlers, "game_exists", return_value=False), \
            patch.object(stats_handlers, "matchup_season_preview", return_value="PREVIEW") as mock_preview, \
            patch.object(stats_handlers, "truncate_telegram_text", side_effect=lambda text, **kw: text):
        await stats_handlers.callback_tonight_game(update, fake_context)

    mock_preview.assert_called_once_with("DET", "NYR")
    assert fake_context.bot.sent_messages[0]["text"] == "PREVIEW"
    assert fake_context.bot.sent_messages[0]["parse_mode"] == "HTML"


# ---------------------------------------------------------------------------
# callback_expand_digest_game
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expand_digest_game_ignores_wrong_prefix(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("other:1")

    await stats_handlers.callback_expand_digest_game(update, fake_context)

    assert fake_context.bot.sent_messages == []


@pytest.mark.asyncio
async def test_expand_digest_game_reports_non_integer_id(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("dg:not-an-int")

    await stats_handlers.callback_expand_digest_game(update, fake_context)

    assert fake_context.bot.sent_messages[0]["text"] == "Некорректная ссылка на матч."


@pytest.mark.asyncio
async def test_expand_digest_game_opens_card_for_valid_id(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("dg:777")

    with patch.object(stats_handlers, "send_game_card_message") as mock_send:
        await stats_handlers.callback_expand_digest_game(update, fake_context)

    mock_send.assert_awaited_once_with(fake_context, 100, 777)


# ---------------------------------------------------------------------------
# callback_leaders_pick / callback_leaderboard_page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leaders_pick_ignores_category_outside_fixed_set(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("pl:pick:wins")  # not points|goals|assists

    with patch.object(stats_handlers, "stat_leaderboard_for_kind") as mock_kind:
        await stats_handlers.callback_leaders_pick(update, fake_context)

    mock_kind.assert_not_called()
    assert update.callback_query.answers == []


@pytest.mark.asyncio
async def test_leaders_pick_renders_chosen_category_from_offset_zero(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("pl:pick:goals")

    with patch.object(
        stats_handlers, "stat_leaderboard_for_kind", return_value=("GOALS PAGE", False, True)
    ) as mock_kind:
        await stats_handlers.callback_leaders_pick(update, fake_context)

    mock_kind.assert_called_once_with("goals", 0)
    assert update.callback_query.edited_texts[0]["text"] == "GOALS PAGE"


@pytest.mark.asyncio
async def test_leaderboard_page_parses_kind_and_offset_from_data(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("pl:assists:30")

    with patch.object(
        stats_handlers, "stat_leaderboard_for_kind", return_value=("PAGE", True, False)
    ) as mock_kind:
        await stats_handlers.callback_leaderboard_page(update, fake_context)

    mock_kind.assert_called_once_with("assists", 30)
    nav = update.callback_query.edited_texts[0]["reply_markup"].inline_keyboard[0]
    assert nav[0].callback_data == "pl:assists:20"


@pytest.mark.asyncio
async def test_leaderboard_page_ignores_unmatched_data(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("pl:invalid_kind:0")

    with patch.object(stats_handlers, "stat_leaderboard_for_kind") as mock_kind:
        await stats_handlers.callback_leaderboard_page(update, fake_context)

    mock_kind.assert_not_called()
    assert update.callback_query.answers == []


# ---------------------------------------------------------------------------
# callback_standalone_adv
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standalone_adv_close_clears_markup(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("adv:close")

    await stats_handlers.callback_standalone_adv(update, fake_context)

    assert update.callback_query.edited_markups == [None]


@pytest.mark.asyncio
async def test_standalone_adv_ignores_unknown_key(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("adv:zzz")

    with patch.object(stats_handlers, "player_stat_leaderboard_page") as mock_page:
        await stats_handlers.callback_standalone_adv(update, fake_context)

    mock_page.assert_not_called()
    assert update.callback_query.answers == []


@pytest.mark.asyncio
async def test_standalone_adv_resolves_known_key_to_its_stat(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("adv:sat")

    with patch.object(
        stats_handlers, "player_stat_leaderboard_page", return_value=("PAGE", False, True)
    ) as mock_page:
        await stats_handlers.callback_standalone_adv(update, fake_context)

    mock_page.assert_called_once_with(
        "Лидеры по Corsi (SAT %)", "players_advanced_stats", "sat_pct", 0
    )
    assert update.callback_query.edited_texts[0]["text"] == "PAGE"


# ---------------------------------------------------------------------------
# send_game_card_message — shared by callback_tonight_game and
# callback_expand_digest_game, but only ever exercised there via a mock; test
# its own branching directly so a regression here isn't hidden by the mocks.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_game_card_message_reports_missing_game(bot_module, fake_context):
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=False), \
            patch.object(stats_handlers, "game_message") as mock_game_message:
        await stats_handlers.send_game_card_message(fake_context, chat_id=42, game_id=999)

    mock_game_message.assert_not_called()
    [sent] = fake_context.bot.sent_messages
    assert sent["text"] == "Такого матча нет в базе бота."


@pytest.mark.asyncio
async def test_send_game_card_message_builds_goal_video_buttons_when_no_markup_given(bot_module, fake_context):
    stats_handlers = bot_module("stats_handlers")
    goals_meta = [
        {"label": "1:0 Ovechkin 5:30", "game_id": 999, "event_id": 1},
        {"label": "2:0 Ovechkin 10:00", "game_id": 999, "event_id": 2},
    ]
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("GAME TEXT", goals_meta)):
        await stats_handlers.send_game_card_message(fake_context, chat_id=42, game_id=999)

    [sent] = fake_context.bot.sent_messages
    assert sent["text"] == "GAME TEXT"
    assert sent["chat_id"] == 42
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["gv:999:1", "gv:999:2"]


@pytest.mark.asyncio
async def test_send_game_card_message_uses_no_markup_when_no_goals(bot_module, fake_context):
    stats_handlers = bot_module("stats_handlers")
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("GAME TEXT", [])):
        await stats_handlers.send_game_card_message(fake_context, chat_id=42, game_id=999)

    [sent] = fake_context.bot.sent_messages
    assert sent["reply_markup"] is None


@pytest.mark.asyncio
async def test_send_game_card_message_prefers_explicit_reply_markup(bot_module, fake_context):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    stats_handlers = bot_module("stats_handlers")
    custom_markup = InlineKeyboardMarkup([[InlineKeyboardButton("custom", callback_data="x")]])
    goals_meta = [{"label": "1:0", "game_id": 999, "event_id": 1}]
    with patch.object(stats_handlers, "game_exists", return_value=True), \
            patch.object(stats_handlers, "game_message", return_value=("GAME TEXT", goals_meta)):
        await stats_handlers.send_game_card_message(
            fake_context, chat_id=42, game_id=999, reply_markup=custom_markup
        )

    [sent] = fake_context.bot.sent_messages
    assert sent["reply_markup"] is custom_markup


# ---------------------------------------------------------------------------
# handle_goal_video
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_video_reports_unavailable_when_download_fails(bot_module, make_callback_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    update = make_callback_update("gv:100:200")

    with patch.object(stats_handlers, "download_goal_video", return_value=None) as mock_download:
        result = await stats_handlers.handle_goal_video(update, fake_context)

    mock_download.assert_called_once_with(100, 200)
    assert result == stats_handlers.SECOND
    assert fake_context.bot.sent_messages[0]["text"] == "Видео пока недоступно."
    assert update.callback_query.answers == ["Загружаю видео гола..."]


@pytest.mark.asyncio
async def test_goal_video_sends_video_with_hints_and_deletes_local_files(
    bot_module, make_callback_update, fake_context, tmp_path
):
    stats_handlers = bot_module("stats_handlers")
    video_replay = bot_module("video_replay")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-mp4")
    thumb_path = tmp_path / "thumb.jpg"
    thumb_path.write_bytes(b"fake-jpg")
    delivery = video_replay.GoalVideoDelivery(
        path=str(video_path), width=640, height=360, duration=5, thumb_path=str(thumb_path)
    )
    update = make_callback_update("gv:100:200")

    with patch.object(stats_handlers, "download_goal_video", return_value=delivery):
        result = await stats_handlers.handle_goal_video(update, fake_context)

    assert result == stats_handlers.SECOND
    [sent] = fake_context.bot.sent_videos
    assert sent["supports_streaming"] is True
    assert sent["width"] == 640
    assert sent["height"] == 360
    assert sent["duration"] == 5
    assert "thumbnail" in sent
    assert not video_path.exists(), "clip must be deleted after sending"
    assert not thumb_path.exists(), "thumbnail must be deleted after sending"


@pytest.mark.asyncio
async def test_goal_video_reports_error_and_still_cleans_up_on_send_failure(
    bot_module, make_callback_update, fake_context, tmp_path
):
    stats_handlers = bot_module("stats_handlers")
    video_replay = bot_module("video_replay")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-mp4")
    delivery = video_replay.GoalVideoDelivery(path=str(video_path))
    update = make_callback_update("gv:100:200")

    async def raise_send(**kwargs):
        raise RuntimeError("network down")

    fake_context.bot.send_video = raise_send

    with patch.object(stats_handlers, "download_goal_video", return_value=delivery):
        result = await stats_handlers.handle_goal_video(update, fake_context)

    assert result == stats_handlers.SECOND
    assert fake_context.bot.sent_messages[-1]["text"] == "Ошибка при отправке видео."
    assert not video_path.exists(), "clip must be deleted even when send_video() raises"


# ---------------------------------------------------------------------------
# bot_digest_custom_date — free-text date parsing (not a callback, but the
# same module's input-parsing surface feeding dispatch_day_digest_messages)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_custom_date_rejects_bad_format(bot_module, make_message_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    update = make_message_update("not-a-date")

    result = await stats_handlers.bot_digest_custom_date(update, fake_context)

    assert result == stats_handlers.THIRD
    reply = update.message.replies[0]
    assert "YYYY-MM-DD" in reply["text"]
    # Дно без клавиатуры — тупик (CLAUDE.md, Задача 5): пользователь должен
    # иметь кнопку выхода, а не только текстовое упоминание /cancel.
    buttons = [b for row in reply["reply_markup"].inline_keyboard for b in row]
    assert buttons[0].callback_data == stats_handlers.DIGEST_BACK_FROM_DATE_CALLBACK
    # Это НОВОЕ сообщение (reply_text) — его id записан, чтобы /cancel мог
    # снять клавиатуру с него.
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


@pytest.mark.asyncio
async def test_digest_custom_date_dispatches_digest_for_valid_date(bot_module, make_message_update, fake_context):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    update = make_message_update("2025-12-01")

    with patch.object(
        stats_handlers, "day_digest", return_value=("2025-12-01", [(1, "text", [])])
    ) as mock_digest:
        result = await stats_handlers.bot_digest_custom_date(update, fake_context)

    mock_digest.assert_called_once_with("2025-12-01")
    assert result == stats_handlers.SECOND
    # dispatch_day_digest_messages ran for real (not mocked) — assert on what
    # it actually sent, not just that something was sent.
    sent = fake_context.bot.sent_messages[0]
    assert sent["text"] == "text"
    assert sent["chat_id"] == 100
    # Родитель результата дайджеста — меню дайджеста (DAY_DIGEST), не корень;
    # сообщение новое (send_message) — id записан для /cancel. Единственный
    # реальный матч без кнопок видео гола рендерится build_menu(..., n_cols=1)
    # — каждая кнопка на своей строке.
    nav_rows = sent["reply_markup"].inline_keyboard[-3:]
    assert [row[0].callback_data for row in nav_rows] == [
        str(dialog_states.DAY_DIGEST),
        str(dialog_states.CHOOSE_STATS),
        str(dialog_states.END_CONVERSATION),
    ]
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


# ---------------------------------------------------------------------------
# dispatch_day_digest_messages — the two branches not covered by the
# bot_digest_custom_date test above: "no real games" (gid == 0, built at
# stats_handlers.py:473) and multi-game (2+ real games, built starting at
# stats_handlers.py:515). Each is a physically separate code path — its own
# nav_markup/send_message call — but both attach the same 3-button footer and
# record the same way, so one parametrized test asserts both.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "games",
    [
        pytest.param([(0, "Матчей не найдено", [])], id="no_real_games"),
        pytest.param(
            [(1, "Матч 1 текст", []), (2, "Матч 2 текст", [])], id="multi_game"
        ),
    ],
)
async def test_dispatch_day_digest_nav_targets_digest_menu_and_records_id(
    bot_module, fake_context, games
):
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")

    await stats_handlers.dispatch_day_digest_messages(
        fake_context, 100, "2025-12-01", games, attach_conv_nav_on_last=True,
    )

    sent = fake_context.bot.sent_messages[0]
    nav_row = sent["reply_markup"].inline_keyboard[-1]
    assert [b.callback_data for b in nav_row] == [
        str(dialog_states.DAY_DIGEST),
        str(dialog_states.CHOOSE_STATS),
        str(dialog_states.END_CONVERSATION),
    ]
    assert fake_context.user_data[dialog_states.LAST_MENU_MESSAGE_ID_KEY] == 1


@pytest.mark.asyncio
async def test_dispatch_day_digest_standalone_call_does_not_record_menu_message_id(
    bot_module, fake_context
):
    """`/day_games` и `/today` вызывают с `attach_conv_nav_on_last=False` — вне
    диалога `/stats`, никакая FSM-клавиатура не рисуется, поэтому и записывать
    в `user_data` нечего (иначе /cancel начал бы снимать клавиатуру с чужого,
    не диалогового сообщения)."""
    stats_handlers = bot_module("stats_handlers")
    dialog_states = bot_module("dialog_states")
    games = [(1, "Матч 1 текст", []), (2, "Матч 2 текст", [])]

    await stats_handlers.dispatch_day_digest_messages(
        fake_context, 100, "2025-12-01", games, attach_conv_nav_on_last=False,
    )

    assert dialog_states.LAST_MENU_MESSAGE_ID_KEY not in fake_context.user_data


# ---------------------------------------------------------------------------
# Callback regex patterns directly — malformed/malicious shapes must not match
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
        ("st:players_season_stats:points:-5", False),  # \d+ rejects a leading '-'
        ("st:players_season_stats:points", False),  # missing offset
        ("st:players; DROP TABLE t;--:points:0", False),  # SQL-injection-shaped garbage
    ],
)
def test_stat_page_callback_pattern_matches_only_well_formed_data(bot_module, data, expected):
    stats_handlers = bot_module("stats_handlers")
    assert bool(re.match(stats_handlers.STAT_PAGE_CALLBACK_PATTERN, data)) is expected


@pytest.mark.parametrize(
    "data, expected",
    [
        ("tm:procent_points:0", True),
        ("tm:procent_points:-5", False),  # \d+ rejects a leading '-'
        ("tm:procent_points", False),  # missing offset
        ("tm:points; DROP TABLE teams;--:0", False),  # SQL-injection-shaped garbage
    ],
)
def test_team_page_callback_pattern_matches_only_well_formed_data(bot_module, data, expected):
    stats_handlers = bot_module("stats_handlers")
    assert bool(re.match(stats_handlers.TEAM_PAGE_CALLBACK_PATTERN, data)) is expected
