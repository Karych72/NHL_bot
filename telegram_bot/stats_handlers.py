"""Клавиатуры пагинации и callback-хендлеры статистики: меню и таблицы `/stats`
(с подменю расширенной статистики) и `/team_stats`, дайджест дня, `/countries`, кнопка матча `/tonight`.

Часть `telegram_bot/`, который читает БД (заполненную `pipeline/`) и рисует меню поверх
неё: здесь — листание длинных таблиц статистики через inline-кнопки и отправка карточек
матчей и дайджестов, не помещающихся в одно сообщение целиком.
"""

import asyncio
import html
import logging
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaVideo,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
    ReplyParameters,
    RichBlock,
    RichBlockVideo,
    Video,
)

from bot_messages import (
    PLAYER_GROUPS,
    COUNTRY_LABELS,
    DIGEST_GAME_SEPARATOR,
    LEADERBOARD_PAGE_SIZE,
    MODEL_PREDICTION_TEASER,
    country_game_block,
    country_page,
    country_rankings,
    day_digest_summary_body,
    digest_game_button_labels,
    digest_shown_match_count,
    game_exists,
    game_goal_events,
    game_message,
    goal_clip_media_id,
    matchup_season_preview,
    player_stat_leaderboard_page,
    rich_footer,
    rich_message,
    team_stat_leaderboard_page,
    truncate_telegram_text,
    truncation_marker,
    with_model_prediction,
)
import premium
from keyboards import build_menu, callback_message
from help_text import (
    ADVANCED_COMMAND_INTRO,
    STATS_COMMAND_INTRO,
    SUBSCRIPTIONS_FOOTER,
    TEAM_STATS_COMMAND_INTRO,
)
from subscription_repo import get_chat_subscriptions
from leaderboard_specs import (
    ADVANCED_METRICS,
    ADVANCED_ROWS,
    MAIN_STAT_ROWS,
    PLAYER_STAT_TITLES,
    TEAM_STAT_ROWS,
    TEAM_STAT_SPECS,
)
from video_replay import cached_goal_clip, download_goal_video, save_goal_clip

logger = logging.getLogger(__name__)

DIGEST_EXPAND_PREFIX = "dg:"
DIGEST_EXPAND_CALLBACK_PATTERN = rf"^{DIGEST_EXPAND_PREFIX}(\d+)$"
# /tonight: кнопка матча tn:<game_id>:<away>:<home>
TONIGHT_GAME_CALLBACK_PATTERN = r"^tn:(\d+):([^:]+):([^:]+)$"

# Статистика по странам (/countries, Задача 49): cn:<CODE>:<F|D|G>:<offset> —
# страница группы игроков страны, cn:list — назад к рейтингу стран
COUNTRY_CALLBACK_PATTERN = r"^cn:(?:list|([A-Z]{3}):([FDG]):(\d+))$"

# Таблицы /stats (st), его подменю расширенной статистики (sa) и /team_stats (tm): <префикс>:<ключ>:<offset> — страница,
# <префикс>:menu — меню команды, <префикс>:save — копия страницы ниже. Ключ у st и sa —
# <таблица>:<колонка> из PLAYER_STAT_TITLES, у tm — ключ TEAM_STAT_SPECS (lookbehind по префиксу).
_PLAYER_STAT_KEYS = "|".join(f"{table}:{column}" for table, column in PLAYER_STAT_TITLES)
_TEAM_STAT_KEYS = "|".join(TEAM_STAT_SPECS)
STAT_SCREEN_CALLBACK_PATTERN = (
    rf"^(st|sa|tm):(?:(menu|save)|((?<=st:|sa:)(?:{_PLAYER_STAT_KEYS})|(?<=tm:)(?:{_TEAM_STAT_KEYS})):(\d+))$"
)

# Bot API принимает не больше 50 МБ файлов в одном запросе: альбом из 10 клипов по 8–19 МБ
# Telegram отверг, и голы ночи 2026-10-13 не ушли (Задача 75). Бюджет — с запасом на
# multipart-обвязку; клип из кэша (``file_id``) не загружается и бюджет не тратит.
_UPLOAD_BUDGET_BYTES = 45_000_000


def _page_nav_rows(
    prefix: str, offset: int, has_prev: bool, has_next: bool
) -> List[List[InlineKeyboardButton]]:
    """Ряд листания таблиц: «« 1–10» (с третьей страницы и дальше), «← 11–20»,
    «31–40 →» — подпись называет места, на которые ведёт кнопка. Пустой
    список, если соседних страниц нет.

    Args:
        prefix: callback_data без смещения — кнопки несут `{prefix}:{offset}`.
        offset: смещение текущей страницы (шаг — `LEADERBOARD_PAGE_SIZE`).
        has_prev, has_next: есть ли соседняя страница в эту сторону.
    """
    size = LEADERBOARD_PAGE_SIZE

    def span(off: int) -> str:
        return f"{off + 1}–{off + size}"

    nav: List[InlineKeyboardButton] = []
    # Со второй страницы «назад» и так ведёт на 1–10 — отдельная кнопка не нужна.
    if offset >= 2 * size:
        nav.append(InlineKeyboardButton(text=f"« {span(0)}", callback_data=f"{prefix}:0"))
    if has_prev:
        prev_off = max(0, offset - size)
        nav.append(InlineKeyboardButton(text=f"← {span(prev_off)}", callback_data=f"{prefix}:{prev_off}"))
    if has_next:
        next_off = offset + size
        nav.append(InlineKeyboardButton(text=f"{span(next_off)} →", callback_data=f"{prefix}:{next_off}"))
    return [nav] if nav else []


async def _edit_page(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовывает листаемую страницу (rich-сообщение) на месте сообщения с нажатой кнопкой.

    Зачем: повторное нажатие кнопки текущей страницы Telegram отвергает «message is
    not modified» — это штатный случай, он глушится; любая другая ошибка всплывает.
    Общая для всех листаемых экранов: таблиц `/stats` и его расширенной статистики, `/team_stats`, `/countries`.

    Аргументы: `callback` — нажатие, `text` — HTML страницы, `markup` — её клавиатура.
    """
    try:
        await callback_message(callback).edit_text(rich_message=rich_message(text), reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


def stats_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню `/stats`: основные метрики полевых и вратарей (`MAIN_STAT_ROWS`), кнопка —
    `st:<таблица>:<колонка>:0`; первый ряд (очки, голы, передачи) — самые частые
    вопросы, он выделен цветом — единственный акцент меню. Последняя кнопка
    `sa:menu` открывает подменю расширенной статистики (`advanced_menu_keyboard`)."""
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"st:{table}:{column}:0",
                style="primary" if i == 0 else None,
            )
            for label, (table, column) in row
        ]
        for i, row in enumerate(MAIN_STAT_ROWS)
    ]
    rows.append([InlineKeyboardButton(text="Расширенная статистика »", callback_data="sa:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def advanced_menu_keyboard() -> InlineKeyboardMarkup:
    """Подменю расширенной статистики из `/stats`: метрики и голы по типам бросков
    (`ADVANCED_ROWS`), кнопка — `sa:<таблица>:<колонка>:0`; «« Назад» (`st:menu`)
    возвращает основное меню `/stats` в том же сообщении."""
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data="sa:{}:{}:0".format(*ADVANCED_METRICS[key]))
            for label, key in row
        ]
        for row in ADVANCED_ROWS
    ]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="st:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def team_stats_keyboard() -> InlineKeyboardMarkup:
    """Меню `/team_stats` (`TEAM_STAT_ROWS`): кнопка — `tm:<ключ TEAM_STAT_SPECS>:0`."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"tm:{key}:0") for label, key in row]
        for row in TEAM_STAT_ROWS
    ])


# Префикс callback_data таблицы → текст и клавиатура меню её команды («« К списку»).
_STAT_MENUS: Dict[str, Tuple[str, Callable[[], InlineKeyboardMarkup]]] = {
    "st": (STATS_COMMAND_INTRO, stats_menu_keyboard),
    "sa": (ADVANCED_COMMAND_INTRO, advanced_menu_keyboard),
    "tm": (TEAM_STATS_COMMAND_INTRO, team_stats_keyboard),
}


def stat_page_reply(prefix: str, key: str, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Страница таблицы `/stats`, его расширенной статистики или `/team_stats` и её клавиатура.

    Зачем одна на три меню: страницы различаются только данными и меню, куда ведёт
    «« К списку»; листание (`_page_nav_rows`) и «Сохранить» у них общие.

    Аргументы:
        prefix: `st`, `sa` или `tm` — меню, из которого открыта таблица.
        key: у `st`/`sa` — `<таблица>:<колонка>` (ключ `PLAYER_STAT_TITLES`), у `tm` —
            ключ `TEAM_STAT_SPECS`.
        offset: сдвиг страницы.
    """
    if prefix == "tm":
        title, columns = TEAM_STAT_SPECS[key]
        text, has_prev, has_next = team_stat_leaderboard_page(title, columns, offset)
    else:
        table, column = key.split(":")
        text, has_prev, has_next = player_stat_leaderboard_page(
            PLAYER_STAT_TITLES[(table, column)], table, column, offset
        )
    rows = _page_nav_rows(f"{prefix}:{key}", offset, has_prev, has_next)
    rows.append([
        InlineKeyboardButton(text="« К списку", callback_data=f"{prefix}:menu"),
        InlineKeyboardButton(text="Сохранить", callback_data=f"{prefix}:save"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_stat_screen(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Кнопки таблиц `/stats`, его расширенной статистики и `/team_stats` (`match` — совпадение
    `STAT_SCREEN_CALLBACK_PATTERN`): `<префикс>:menu` — меню на месте страницы (`sa:menu` — и переход в подменю из `/stats`);
    `<префикс>:save` — страница остаётся в чате без кнопок, её копия с теми же кнопками
    уходит ниже; `<префикс>:<ключ>:<offset>` — страница таблицы (ключ уже проверен паттерном).
    """
    prefix, action, key, offset = match.groups()
    message = callback_message(callback)
    if action == "save":
        await callback.answer("Сохранено")
        # Сначала копия, потом снятие кнопок: упади отправка — страница останется живой.
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=message.reply_markup,
        )
        await message.edit_reply_markup(reply_markup=None)
        return
    await callback.answer()
    if action == "menu":
        intro, keyboard = _STAT_MENUS[prefix]
        await _edit_page(callback, intro, keyboard())
        return
    text, markup = stat_page_reply(prefix, key, int(offset))
    await _edit_page(callback, text, markup)


def country_rankings_reply() -> Tuple[str, InlineKeyboardMarkup]:
    """Рейтинг стран сезона и сетка кнопок `cn:<CODE>:F:0` — по одной на страну,
    прошедшую порог (Задача 49, команда /countries). Пустой сезон — текст без
    кнопок (Задача 36)."""
    text, codes = country_rankings()
    buttons = [
        InlineKeyboardButton(text=COUNTRY_LABELS.get(code, code), callback_data=f"cn:{code}:F:0")
        for code in codes
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=build_menu(buttons, n_cols=2))


def country_page_reply(code: str, group: str, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Экран страны `/countries`: страница группы игроков (`country_page`), листание,
    переключатель групп (нападающие / защитники / вратари) и «« Страны».

    Аргументы: `code` — код страны, `group` — ключ `PLAYER_GROUPS`, `offset` — сдвиг страницы.
    """
    text, has_prev, has_next = country_page(code, group, offset)
    rows = _page_nav_rows(f"cn:{code}:{group}", offset, has_prev, has_next)
    rows.append([
        InlineKeyboardButton(
            text=("• " if key == group else "") + label, callback_data=f"cn:{code}:{key}:0"
        )
        for key, (label, _cond) in PLAYER_GROUPS.items()
    ])
    rows.append([InlineKeyboardButton(text="« Страны", callback_data="cn:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_country(callback: CallbackQuery, match: re.Match) -> None:
    """Кнопки /countries: `cn:list` — снова рейтинг стран, `cn:<CODE>:<G>:<off>` —
    страница группы игроков страны с листанием, переключателем групп
    (нападающие / защитники / вратари) и «« Страны». Код вне рейтинга сезона
    `country_page()` сама превращает в текст «Страна не найдена…».
    `match` — совпадение `COUNTRY_CALLBACK_PATTERN`."""
    await callback.answer()
    if match.group(1) is None:
        text, markup = country_rankings_reply()
    else:
        text, markup = country_page_reply(match.group(1), match.group(2), int(match.group(3)))
    await _edit_page(callback, text, markup)


# --- Day digest: один матч — полная карточка; несколько — сводка + кнопки матчей ---

# Подсказка в подвале последнего сообщения дайджеста (из /today, ИИ и утренней рассылки).
_DIGEST_MORE_HINT = (
    "Ещё: /tonight — расписание NHL, /standings — таблица, /stats — лидеры игроков, "
    "/team_stats — статистика команд, /team — команды, /help — справка.\n"
    + SUBSCRIPTIONS_FOOTER
)

# Клип гола к отправке в карточке: видео (``file_id`` из кэша или mp4 к загрузке) и
# подпись ``goal_caption`` — она пишется в кэш вместе с ``file_id`` загруженного клипа
# (её показывает альбом голов страны в рассылке).
_Clip = Tuple[InputMediaVideo, str]

# Отправка или правка карточки: rich-сообщение → отправленное сообщение.
_Publish = Callable[[InputRichMessage], Awaitable[Message | bool]]


async def _goal_clips(
    game_id: int, events: Sequence[int], from_cache: bool = True
) -> Tuple[Dict[int, _Clip], bool]:
    """Клипы голов ``events`` матча для одной отправки карточки, по ``event_id`` в порядке
    ``events``, и признак «загружено всё, что есть».

    Клип из кэша ``bot_goal_clips`` уходит по ``file_id``; остальные скачиваются с NHL и
    грузятся в самом сообщении, пока помещаются в ``_UPLOAD_BUDGET_BYTES``. Первый не
    влезший клип прекращает скачивание — признак ``False``: его гол и следующие
    некэшированные доливает правка карточки (``send_game_card``). Клип тяжелее всего
    бюджета не влезет никогда — его гол остаётся строкой без видео.

    Аргументы: ``game_id``, ``events`` — голы (``game_goal_events``); ``from_cache`` — брать
    ли кэш (``False`` — Telegram отверг его ``file_id``, качаем заново).
    """
    clips: Dict[int, _Clip] = {}
    budget = _UPLOAD_BUDGET_BYTES
    complete = True
    for event_id in events:
        cached = cached_goal_clip(game_id, event_id) if from_cache else None
        if cached is not None:
            clips[event_id] = (InputMediaVideo(media=cached[0], supports_streaming=True), cached[1])
            continue
        if not complete:
            continue
        # В отдельном потоке: скачивание и ffmpeg блокируют event loop (см. video_replay).
        d = await asyncio.to_thread(download_goal_video, game_id, event_id)
        if d is None:
            logger.warning("No video for goal game_id=%s event_id=%s", game_id, event_id)
            continue
        try:
            data = Path(d.path).read_bytes()
            thumb = Path(d.thumb_path).read_bytes() if d.thumb_path else None
        finally:
            os.unlink(d.path)
            if d.thumb_path:
                os.unlink(d.thumb_path)
        size = len(data) + len(thumb or b"")
        if size > _UPLOAD_BUDGET_BYTES:
            logger.warning("game_id=%s event_id=%s: клип тяжелее лимита запроса, без видео", game_id, event_id)
            continue
        if size > budget:
            complete = False
            continue
        budget -= size
        clips[event_id] = (
            InputMediaVideo(
                media=BufferedInputFile(data, filename=f"{event_id}.mp4"),
                thumbnail=BufferedInputFile(thumb, filename=f"{event_id}.jpg") if thumb else None,
                width=d.width, height=d.height, duration=d.duration, supports_streaming=True,
            ),
            d.caption,
        )
    return clips, complete


def _rich_videos(blocks: Sequence[RichBlock]) -> Iterator[Video]:
    """Видео отправленного rich-сообщения в порядке текста, со вложенными блоками."""
    for block in blocks:
        if isinstance(block, RichBlockVideo):
            yield block.video
        yield from _rich_videos(getattr(block, "blocks", None) or [])


async def _publish_card(
    publish: _Publish, game_id: int, clips: Dict[int, _Clip], tail: str, collapsed: bool
) -> Message:
    """Одна отправка или правка карточки с клипами ``clips``; ``file_id`` загруженных в
    ней клипов — в кэш ``bot_goal_clips``."""
    media = [InputRichMessageMedia(id=goal_clip_media_id(evt), media=video) for evt, (video, _) in clips.items()]
    message = await publish(rich_message(f"{game_message(game_id, clips, collapsed)}{tail}", media))
    assert isinstance(message, Message) and message.rich_message is not None  # не inline: ответ — само сообщение
    # Сначала сверка всех пар (видео ответа — в порядке текста, как ``clips``), потом запись:
    # расхождение числа видео падает до того, как в кэш попал хоть один ``file_id``.
    sent = list(zip(clips.items(), _rich_videos(message.rich_message.blocks), strict=True))
    for (evt, (video, caption)), uploaded in sent:
        if not isinstance(video.media, str):
            save_goal_clip(game_id, evt, uploaded.file_id, caption)
    return message


async def _publish_with_clips(
    publish: _Publish, game_id: int, events: List[int], tail: str, collapsed: bool
) -> Tuple[Message, bool]:
    """Отправка или правка карточки с клипами, которые есть и влезают (``_goal_clips``);
    протухший ``file_id`` кэша (Telegram отверг файл) — ошибка в лог и один повтор с
    клипами, скачанными заново: клипы из кэша — первыми, чтобы бюджет достался им и кэш
    обновился. Иной отказ Telegram всплывает без повтора. Возвращает сообщение и признак
    «загружено всё»."""
    clips, complete = await _goal_clips(game_id, events)
    try:
        return await _publish_card(publish, game_id, clips, tail, collapsed), complete
    except TelegramBadRequest as exc:
        cached = [evt for evt, (video, _) in clips.items() if isinstance(video.media, str)]
        if not cached or "file" not in exc.message.lower():
            raise
        logger.error("Telegram rejected cached clips of game_id=%s, re-uploading: %s", game_id, exc)
    fresh, complete = await _goal_clips(game_id, cached + [evt for evt in events if evt not in cached], from_cache=False)
    # Порядок клипов — порядок голов в тексте: по нему сверяются видео ответа.
    clips = {evt: fresh[evt] for evt in events if evt in fresh}
    return await _publish_card(publish, game_id, clips, tail, collapsed), complete


async def send_game_card(bot: Bot, chat_id: int, game_id: int, tail: str = "", collapsed: bool = False) -> Message:
    """Шлёт карточку матча одним rich-сообщением, клипы голов — внутри строк голов.

    Зачем: одна отправка карточки для меню, ``/today`` и рассылки (Задача 119) — видео
    голов не кнопками под сообщением, а в самой карточке. Bot API принимает до 50 МБ файлов
    на запрос: клипы, не влезшие в отправку, доливаются правками того же сообщения, пока
    не загружено всё, — первый получатель (владелец, ``config.OWNER_CHAT_ID``) видит полную
    карточку, остальным клипы уходят по ``file_id``.

    Аргументы:
        bot, chat_id: куда слать.
        game_id: матч.
        tail: построчный HTML после карточки — блоки стран, подвал.
        collapsed: карточка под табло свёрнута (``game_message``).
    """
    events = game_goal_events(game_id)
    message, complete = await _publish_with_clips(
        lambda rich: bot.send_rich_message(chat_id=chat_id, rich_message=rich), game_id, events, tail, collapsed
    )
    while not complete:
        message_id = message.message_id
        message, complete = await _publish_with_clips(
            lambda rich: bot.edit_message_text(chat_id=chat_id, message_id=message_id, rich_message=rich),
            game_id, events, tail, collapsed,
        )
    return message


async def send_game_card_message(bot: Bot, chat_id: int, game_id: int) -> Message:
    """Полная карточка матча (``send_game_card``); возвращает отправленное сообщение.
    Подписчику страны под карточкой — блок игроков каждой его страны, сыгравших в матче
    (Задача 76)."""
    if not game_exists(game_id):
        return await bot.send_rich_message(
            chat_id=chat_id, rich_message=rich_message("Такого матча нет в базе бота.")
        )
    tail = ""
    for country in get_chat_subscriptions(chat_id).countries:
        block = country_game_block(country, game_id)
        if block is not None:
            tail += f"\n\n{block}"
    return await send_game_card(bot, chat_id, game_id, tail)


async def dispatch_day_digest_messages(
    bot: Bot,
    chat_id: int,
    day_label: Optional[str],
    game_ids: List[int],
    *,
    inter_message_sleep_sec: Optional[float] = None,
    heading: str = "Матчи ночи",
    summary_only: bool = False,
) -> None:
    """Дайджест дня: один матч — одна карточка; несколько — сводка + кнопки разворота.
    Подсказка «Ещё: …» и ссылка на подписки — подвал этого же сообщения, а не отдельное
    сообщение: служебное тише содержания (`docs/telegram_bot.md` §«Стиль сообщений»).

    Аргументы:
        bot, chat_id: куда слать.
        day_label, game_ids: ответ `day_digest()`.
        heading: заголовок сводки (дата и число матчей — в подвале); рассылка без уже
            присланных чату матчей — «Остальные матчи ночи» (Задача 85).
        inter_message_sleep_sec: пауза после каждого сообщения (рассылка).
        summary_only: сводка и при одном матче — рассылка матчей без подписки чата:
            карточку он не запрашивал, она раскрывается кнопкой.
    """

    async def _pause() -> None:
        if inter_message_sleep_sec is not None and inter_message_sleep_sec > 0:
            await asyncio.sleep(inter_message_sleep_sec)

    hint = rich_footer(_DIGEST_MORE_HINT)

    if not game_ids:
        empty = (
            f"За {day_label} завершенных матчей не найдено." if day_label
            else "В базе пока нет завершенных матчей."
        )
        await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message(f"{html.escape(empty)}\n{hint}"))
        await _pause()
        return

    if len(game_ids) == 1 and not summary_only:
        await send_game_card(bot, chat_id, game_ids[0], f"\n{hint}")
        await _pause()
        return

    day_str = day_label or "—"
    lines = day_digest_summary_body(game_ids)
    header = f"<b>{html.escape(heading)}</b>\n\n"
    footer = (
        f"Игровой день {html.escape(day_str)} · матчей: {len(game_ids)}\n"
        f"Кнопка матча — полная карточка и видео голов.\n{_DIGEST_MORE_HINT}"
    )
    # Матч сводки несёт HTML-теги: резать его по символам нельзя — Telegram
    # отвергнет незакрытый тег. Не влезло — оставляем целые матчи, сколько
    # уместилось, и маркер «показаны N из M» первой строкой подвала.
    total_games = len(game_ids)
    shown_games = digest_shown_match_count(header, lines, total_games, tail=f"\n{rich_footer(footer)}")
    if shown_games < total_games:
        footer = truncation_marker(shown_games, total_games, item_word="матчей") + f"\n{footer}"
    summary_text = header + DIGEST_GAME_SEPARATOR.join(lines[:shown_games]) + f"\n{rich_footer(footer)}"

    expand_buttons = [
        InlineKeyboardButton(text=label, callback_data=f"{DIGEST_EXPAND_PREFIX}{gid}")
        for label, gid in zip(digest_game_button_labels(game_ids), game_ids)
    ]
    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=rich_message(summary_text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=build_menu(expand_buttons, n_cols=3)),
    )
    await _pause()


# Ответы на кнопки матчей /tonight по чатам: chat_id → (game_id, карточка ли это) →
# [id ответа, id последней ссылки на него или None]. Превью и карточка — разные ответы:
# матч, сыгранный после превью, открывается карточкой. Только в памяти процесса: после
# рестарта бота повторное нажатие шлёт ответ заново.
_tonight_sent: Dict[int, Dict[Tuple[int, bool], List[Optional[int]]]] = {}
_TONIGHT_ALREADY_SENT = "↑ Этот матч уже открыт — нажмите на цитату, чтобы перейти к нему."


async def _delete_tonight_pointer(bot: Bot, chat_id: int, pointer_id: Optional[int]) -> None:
    """Удаляет прошлую ссылку-reply на ответ `/tonight` (`pointer_id`, None —
    ссылки не было) в чате `chat_id`."""
    if pointer_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=pointer_id)
    except TelegramBadRequest as exc:
        # «Message to delete not found» / «message can't be deleted» (старше 48 ч):
        # убирать нечего; остальные ошибки — наверх.
        text = exc.message.lower()
        if "to delete not found" not in text and "can't be deleted" not in text:
            raise


async def _point_to_tonight_answer(bot: Bot, chat_id: int, entry: List[Optional[int]]) -> bool:
    """Повторное нажатие кнопки матча `/tonight`: вместо новой копии ответа —
    короткое сообщение-ответ (reply) на уже отправленный; тап по цитате
    прокручивает чат к нему (отзыв 2026-10-03).

    Зачем удалять прошлую ссылку: иначе каждое нажатие всё равно добавляет
    сообщение в чат — так на матч остаётся не больше одной ссылки.

    Аргументы:
        bot, chat_id: как у `callback_tonight_game`.
        entry: значение `_tonight_sent[chat_id]` для матча — обновляется
            на месте id новой ссылки.

    Возвращает: False, если исходного ответа в чате уже нет (пользователь его
    удалил) — тогда ответ нужно отправить заново; ссылка на удалённый ответ
    при этом тоже удаляется.
    """
    answer_id, pointer_id = entry
    assert answer_id is not None
    try:
        pointer = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message(_TONIGHT_ALREADY_SENT),
            reply_parameters=ReplyParameters(message_id=answer_id),
        )
    except TelegramBadRequest as exc:
        # «Message to be replied not found»: ответ удалён из чата.
        if "replied not found" not in exc.message.lower():
            raise
        await _delete_tonight_pointer(bot, chat_id, pointer_id)
        return False
    await _delete_tonight_pointer(bot, chat_id, pointer_id)
    entry[1] = pointer.message_id
    return True


async def callback_tonight_game(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Обрабатывает нажатие кнопки матча `/tonight` (`tn:<game_id>:<away>:<home>`,
    `match` — совпадение `TONIGHT_GAME_CALLBACK_PATTERN`).

    Если матч уже есть в базе — шлёт полную карточку (`send_game_card_message`);
    иначе шлёт текстовое превью сезонных встреч команд (`matchup_season_preview`)
    — матч из расписания NHL API мог ещё не попасть в БД; премиум-чату — с оценкой
    модели (Задача 96). Повторное нажатие
    не шлёт копию, а ссылается на уже отправленный ответ
    (`_point_to_tonight_answer`).
    """
    assert callback.message is not None
    chat_id = callback.message.chat.id
    await callback.answer()
    game_id, away, home = int(match.group(1)), match.group(2), match.group(3)
    is_card = game_exists(game_id)
    sent = _tonight_sent.setdefault(chat_id, {})
    key = (game_id, is_card)
    if key in sent and await _point_to_tonight_answer(bot, chat_id, sent[key]):
        return
    if is_card:
        answer = await send_game_card_message(bot, chat_id, game_id)
    else:
        # Проза без счётных элементов — сноска без чисел, но текст берётся из
        # того же единственного хелпера (truncate_telegram_text по умолчанию),
        # а не из литерала, продублированного здесь и в bot.py.
        body, footer = matchup_season_preview(game_id, away, home)
        if premium.is_premium(chat_id):
            footer = with_model_prediction(footer, game_id, home)
        else:
            footer = f"{footer}\n{MODEL_PREDICTION_TEASER}"
        text = f"{truncate_telegram_text(body)}\n{rich_footer(footer)}"
        answer = await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message(text))
    sent[key] = [answer.message_id, None]


async def callback_expand_digest_game(callback: CallbackQuery, bot: Bot, match: re.Match) -> None:
    """Обрабатывает кнопку матча сжатой сводки дайджеста (`dg:<game_id>`, `match` —
    совпадение `DIGEST_EXPAND_CALLBACK_PATTERN`) — досылает полную карточку
    конкретного матча отдельным сообщением.
    """
    assert callback.message is not None
    await callback.answer()
    await send_game_card_message(bot, callback.message.chat.id, int(match.group(1)))
