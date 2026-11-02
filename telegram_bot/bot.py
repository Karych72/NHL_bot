"""Точка входа Telegram-бота: сборка `Dispatcher` aiogram и регистрация хендлеров.

Роль в пайплайне: `bot.py` собирает `Dispatcher` aiogram 3.31 из двух роутеров —
команды и кнопки, затем свободный текст, — вешает троттлинг нажатий (`throttle.py`) и
журнал действий (`bot_actions`, Задача 99) и запускает long polling, а рядом — тикер
расписаний экранов (Задача 107). Меню и листание таблиц статистики живут в
`stats_handlers.py`; здесь — команды верхнего уровня, подписки, расписания, свободный текст
и сборка `Dispatcher`.
"""

import asyncio
import html
import logging
import re
import zoneinfo
from datetime import datetime, time as dtime, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

import psycopg2
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.magic_filter import MagicFilter

import ai_router
import ai_stats
import config
import premium
import schedule_repo
import subscription_repo
from database import get_connection
from leaderboard_specs import ADVANCED_METRICS, PLAYER_STAT_TITLES
from push_digest_job import throttled
from throttle import enforce_callback_rate_limit
from bot_messages import (
    COUNTRY_LABELS,
    MSK,
    country_rankings,
    day_digest,
    rich_footer,
    rich_list,
    rich_message,
    last_night_day,
    season_teams_by_division,
    team_list_text,
    team_full_stats,
    team_profile,
    team_table,
    truncate_telegram_text,
)
from nhl_scoreboard import (
    ScoreboardFetchError,
    fetch_score,
    league_today,
    slate_games_sorted,
    tonight_match_button_label,
    tonight_reply_intro,
)
from help_text import (
    BOT_COMMANDS,
    HELP_MESSAGE,
    START_MESSAGE,
    STATS_COMMAND_INTRO,
    TEAM_STATS_COMMAND_INTRO,
)
from keyboards import build_menu, callback_message
from stats_handlers import (
    COUNTRY_CALLBACK_PATTERN,
    DIGEST_EXPAND_CALLBACK_PATTERN,
    GOAL_VIDEO_CALLBACK_PATTERN,
    STAT_SCREEN_CALLBACK_PATTERN,
    TONIGHT_GAME_CALLBACK_PATTERN,
    callback_country,
    callback_expand_digest_game,
    callback_stat_screen,
    callback_tonight_game,
    country_page_reply,
    country_rankings_reply,
    dispatch_day_digest_messages,
    handle_goal_video,
    send_game_card_message,
    stat_page_reply,
    stats_menu_keyboard,
    team_stats_keyboard,
)

logger = logging.getLogger(__name__)

# Лимит Bot API на число кнопок в одном inline-сообщении.
_TELEGRAM_INLINE_BUTTON_CAP = 100
# Как у кнопок матчей в /today.
_TONIGHT_BUTTON_COLUMNS = 3

# `/BOS`, `/WSH`… из списка `/team` и `/BOS_FULL` из конца профиля клуба: три
# латинские буквы (+ `_FULL`) — ни одна служебная команда бота под этот шаблон
# не попадает. Вариант `/BOS@bot` не принимается: regex не знает имени бота и
# отвечал бы на команды, адресованные чужим ботам.
TEAM_COMMAND_PATTERN = r"^/([A-Za-z]{3})(_[Ff][Uu][Ll][Ll])?$"


def build_tonight_match_keyboard(games) -> Optional[InlineKeyboardMarkup]:
    """Вертикальный список кнопок матчей: по нажатию — карточка из БД или сезонное сравнение команд."""
    buttons: List[InlineKeyboardButton] = []
    for g in games:
        gid = g.get("id")
        away = ((g.get("awayTeam") or {}).get("abbrev") or "?").strip()
        home = ((g.get("homeTeam") or {}).get("abbrev") or "?").strip()
        if gid is None:
            continue
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            continue
        cb = f"tn:{gid_int}:{away}:{home}"
        if len(cb.encode("utf-8")) > 64:
            continue
        label = tonight_match_button_label(g)
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append(InlineKeyboardButton(text=label, callback_data=cb))
    if not buttons:
        return None
    if len(buttons) > _TELEGRAM_INLINE_BUTTON_CAP:
        buttons = buttons[:_TELEGRAM_INLINE_BUTTON_CAP]
    rows = build_menu(buttons, n_cols=_TONIGHT_BUTTON_COLUMNS)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Кнопки приветствия ведут в меню `/subscriptions` (`callback_subscriptions`): нажатие
# перерисовывает приветствие экраном подписок. «Утренний дайджест» — переключатель `sub:d`:
# у подписанного нажатие дайджест выключит, подсказка нажатия об этом скажет.
START_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="⭐ Выбрать команды", callback_data="sub:T"),
        InlineKeyboardButton(text="🌍 Выбрать страны", callback_data="sub:C"),
    ],
    [InlineKeyboardButton(text="☕ Утренний дайджест", callback_data="sub:d")],
])


async def cmd_start(message: Message, bot: Bot, command: CommandObject) -> None:
    """`/start`: приветствие с кнопками подписки; deep link `?start=game_<id>` из карточки
    матча открывает карточку, битый id — приветствие."""
    args = (command.args or "").split()
    if args and args[0].startswith("game_") and args[0][5:].isdecimal():
        await send_game_card_message(bot, message.chat.id, int(args[0][5:]))
        return
    await message.answer_rich(rich_message(START_MESSAGE), reply_markup=START_KEYBOARD)


async def show_help(bot: Bot, chat_id: int) -> None:
    """`/help`: список команд бота."""
    await _send(bot, chat_id, HELP_MESSAGE)

_PREMIUM_PERKS = (
    f"Что даёт премиум: ИИ-вопросы — до {ai_router.PREMIUM_DAILY_LIMIT} в сутки вместо недельного лимита, "
    "прогноз модели в превью матчей."
)
_PREMIUM_CANCEL_HINT = (
    "Если ещё не отменили автопродление, это можно сделать в настройках Telegram: "
    "Настройки → Stars → Подписки. Доступ остаётся до конца оплаченного срока."
)
_PREMIUM_GROUP_TEXT = "Оплатить премиум можно только в личном чате с ботом: откройте его и отправьте /premium."
_PAYSUPPORT_TEXT = "Вопросы по оплате и возвраты — напишите @karych_72."


async def cmd_premium(message: Message, bot: Bot) -> None:
    """`/premium`: статус премиума, что он даёт и кнопка оплаты подпиской Stars (Задача 97).

    Оплата только в личке (`chat_id` лички = `user_id`); в группе — текст без кнопки. При
    действующей подписке Stars кнопки нет (вторая подписка шла бы параллельно) — вместо неё
    подсказка, как отменить автопродление. Вручную выданному премиуму (Задача 96) кнопка
    остаётся, подсказки нет.
    """
    if message.chat.type != "private":
        await message.answer(_PREMIUM_GROUP_TEXT)
        return
    until = premium.premium_until(message.chat.id)
    subscribed = premium.has_paid_subscription(message.chat.id)
    status = f"Премиум до {until.astimezone(MSK):%d.%m}" if until else "Премиума нет"
    lines = [status, "", _PREMIUM_PERKS, f"Подписка {premium.PRICE_STARS} ⭐ за 30 дней, продлевается сама."]
    if subscribed:
        await message.answer_rich(rich_message("\n".join(lines + ["", _PREMIUM_CANCEL_HINT])))
        return
    link = await bot.create_invoice_link(
        title="Премиум на 30 дней",
        description=_PREMIUM_PERKS,
        payload="premium",
        currency="XTR",
        prices=[LabeledPrice(label="Премиум, 30 дней", amount=premium.PRICE_STARS)],
        subscription_period=premium.PERIOD_SECONDS,
    )
    button = InlineKeyboardButton(text=f"Оплатить {premium.PRICE_STARS} ⭐", url=link)
    await message.answer_rich(
        rich_message("\n".join(lines)), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]])
    )


async def cmd_paysupport(message: Message, bot: Bot) -> None:
    """`/paysupport`: куда писать по вопросам оплаты и возвратам (обязательна для Stars)."""
    await message.answer(_PAYSUPPORT_TEXT)


async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Подтверждает оплату без похода в БД: Telegram ждёт ответа считаные секунды."""
    await query.answer(ok=True)


async def on_successful_payment(message: Message, bot: Bot) -> None:
    """Платёж Stars прошёл (первый или автопродление): продлевает премиум и пишет срок.

    Ошибка записи не глушится: деньги уже списаны, поэтому сначала в лог идут данные платежа
    (выдать премиум придётся вручную), затем исключение уходит дальше.
    """
    payment = message.successful_payment
    assert payment is not None
    try:
        until = premium.extend_premium(
            message.chat.id,
            payment.telegram_payment_charge_id,
            payment.total_amount,
            bool(payment.is_recurring),
        )
    except Exception:
        logger.exception(
            "premium payment not recorded: charge_id=%s chat_id=%s amount=%s",
            payment.telegram_payment_charge_id, message.chat.id, payment.total_amount,
        )
        raise
    await message.answer(f"Спасибо! Премиум до {until.astimezone(MSK):%d.%m}.")


async def _send(bot: Bot, chat_id: int, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Экран новым сообщением в чат `chat_id` — так его шлют команда, ИИ и расписание."""
    await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message(text), reply_markup=markup)


# Экраны команд — функции от (бот, чат), без `Message`: их зовут и хендлер команды, и ответ
# ИИ, и тикер расписаний (Задача 107). Хендлер — `_command_handler(экран)`.


async def show_tonight(bot: Bot, chat_id: int) -> None:
    """`/tonight`: расписание сегодняшнего дня из NHL API плюс кнопки матчей."""
    try:
        payload = await asyncio.to_thread(fetch_score, league_today())
    except ScoreboardFetchError:
        logger.exception("NHL score request failed")
        await _send(bot, chat_id, "Сейчас не удалось загрузить расписание NHL. Попробуйте чуть позже.")
        return
    games = slate_games_sorted(payload)
    # Проза без счётных элементов — сноска без чисел приходит из хелпера
    # по умолчанию (truncate_telegram_text), а не из литерала здесь.
    text = truncate_telegram_text(tonight_reply_intro(payload))
    await _send(bot, chat_id, text, build_tonight_match_keyboard(games) if games else None)


async def show_standings(bot: Bot, chat_id: int) -> None:
    """`/standings`: турнирная таблица лиги."""
    await _send(bot, chat_id, team_table())


async def show_today(bot: Bot, chat_id: int) -> None:
    """`/today`: дайджест матчей прошедшей ночи по Москве (`last_night_day()`).

    Ночь без матчей (выходной день лиги или данные ещё не загружены) — честная
    пометка и последний игровой день в базе, а не голое «матчей не найдено»:
    иначе последние результаты нечем посмотреть.
    """
    night = last_night_day()
    day_label, games = day_digest(night)
    if all(gid == 0 for gid, _text, _meta in games):
        await _send(
            bot, chat_id, f"Прошедшей ночью ({night}) матчей в базе нет — показываю последний игровой день."
        )
        day_label, games = day_digest()
    await dispatch_day_digest_messages(bot, chat_id, day_label, games)


async def show_team(bot: Bot, chat_id: int) -> None:
    """`/team`: команды сезона кликабельными командами `/ABBR`."""
    await _send(bot, chat_id, team_list_text())


def team_profile_text(abbrev: str, full: bool) -> str:
    """Ответ `/BOS` — статистика клуба (`team_profile()`), при `full` (`/BOS_FULL`) — все
    его игроки (`team_full_stats()`); неизвестную аббревиатуру обе функции сами
    превращают в текст с причиной, а ссылку на `/team` ставят в подвал."""
    body = team_full_stats(abbrev) if full else team_profile(abbrev)
    return truncate_telegram_text(body)


async def cmd_team_profile(message: Message, match: re.Match) -> None:
    """`/BOS` и т.п.: профиль клуба, `/BOS_FULL` — все его игроки (`team_profile_text`);
    `match` — совпадение `TEAM_COMMAND_PATTERN`."""
    abbrev, full = match.group(1), match.group(2)
    assert message.bot is not None
    await _send(message.bot, message.chat.id, team_profile_text(abbrev, bool(full)))


async def show_stats(bot: Bot, chat_id: int) -> None:
    """`/stats`: лидеры игроков — все метрики полевых и вратарей кнопками одного экрана."""
    await _send(bot, chat_id, STATS_COMMAND_INTRO, stats_menu_keyboard())


async def show_team_stats(bot: Bot, chat_id: int) -> None:
    """`/team_stats`: рейтинги команд — голы, броски, спецбригады, вбрасывания."""
    await _send(bot, chat_id, TEAM_STATS_COMMAND_INTRO, team_stats_keyboard())


async def show_countries(bot: Bot, chat_id: int) -> None:
    """`/countries`: рейтинг стран сезона и кнопки стран (Задача 49)."""
    await _send(bot, chat_id, *country_rankings_reply())


_DELIVERY_NOTE = (
    "Итоги ночи приходят, когда загружены все её матчи, не позже 11:00 МСК.\n"
    "Превью матчей дня — с 15:30 МСК, по одному раз в полчаса."
)
_SUBSCRIPTIONS_DB_ERROR = "Подписки недоступны: выполните make db-migrate на вашей БД PostgreSQL."

# Кнопки меню `/subscriptions` (Задача 63): `sub:m` главный экран, `sub:d` дайджест,
# `sub:u` предстоящие матчи (Задача 78), `sub:T` дивизионы, `sub:D:<i>` команды дивизиона i, `sub:t:<i>:<team_id>` команда
# (i — чтобы перерисовать тот же дивизион), `sub:C` страны, `sub:c:<CODE>` страна,
# `sub:x` «Готово», `sub:s` расписания (Задача 107), `sub:s:<id>` удалить расписание. Самая
# длинная — `sub:t:3:` + id команды, заведомо меньше 64 байт.
SUBSCRIPTIONS_CALLBACK_PATTERN = r"^sub:(m|d|u|T|C|x|s|D:[0-3]|t:[0-3]:\d+|c:[A-Z]{3}|s:\d+)$"
_DIVISION_BUTTON_COLUMNS = 2
_TEAM_BUTTON_COLUMNS = 4
_COUNTRY_BUTTON_COLUMNS = 3

_Screen = Tuple[str, InlineKeyboardMarkup]


def _sub_button(
    label: str, selected: bool, data: str, off_prefix: str = ""
) -> InlineKeyboardButton:
    """Кнопка переключателя: у включённого пункта подпись начинается с ✅, у выключенного —
    с `off_prefix` (для дайджеста «➕»)."""
    prefix = "✅ " if selected else off_prefix
    return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=data)


def _subscriptions_main_screen(
    digest: bool, upcoming: bool, team_names: List[str], countries: List[str], schedules: int
) -> _Screen:
    """Главный экран: сводка подписок, переключатели дайджеста и предстоящих матчей
    (каждый в своём ряду — вдвоём в ряд на телефоне не влезают), переходы в подменю, в том
    числе к `schedules` расписаниям чата.
    Текст — построчный HTML для `rich_message()`: подписки — список, расписание — подвал."""
    items = []
    if digest:
        items.append("Утренний дайджест")
    if upcoming:
        items.append("Предстоящие матчи")
    if team_names:
        items.append("Команды: " + ", ".join(html.escape(name) for name in team_names))
    if countries:
        items.append("Страны: " + ", ".join(countries))
    lines = ["<b>Ваши подписки</b>", rich_list(items) if items else "Пока ничего нет."]
    lines.append(rich_footer(_DELIVERY_NOTE))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_sub_button("Дайджест", digest, "sub:d", "➕ ")],
        [_sub_button("Предстоящие матчи", upcoming, "sub:u", "➕ ")],
        [
            InlineKeyboardButton(text=f"🏒 Команды ({len(team_names)})", callback_data="sub:T"),
            InlineKeyboardButton(text=f"🌍 Страны ({len(countries)})", callback_data="sub:C"),
        ],
        [InlineKeyboardButton(text=f"⏰ Расписания ({schedules})", callback_data="sub:s")],
        [InlineKeyboardButton(text="Готово", callback_data="sub:x")],
    ])
    return "\n".join(lines), markup


# Дни недели в «по пятницам», индекс — ISO-день недели минус 1.
_WEEKDAYS_DATIVE = (
    "понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям",
)
_SCHEDULE_HINT = "Создать — напишите боту, например: присылай лидеров по голам по пятницам в 10."


def _screen_label(screen: str, p: Dict[str, Any]) -> str:
    """Подпись экрана расписания: «/standings», «/WSH_FULL», «Лидеры · Лучшие снайперы»."""
    if screen == "open_command":
        return f"/{p['command']}"
    if screen == "team_profile":
        return f"/{p['team']}" + ("_FULL" if p["all_players"] else "")
    if screen == "leaders":
        return "Лидеры · " + PLAYER_STAT_TITLES[("players_season_stats", p["category"])]
    if screen == "country_players":
        return "Игроки · " + COUNTRY_LABELS[p["country"]]
    return "Лидеры · " + PLAYER_STAT_TITLES[ADVANCED_METRICS[p["metric"]]]


def _when_label(freq: str, weekday: Optional[int], month_day: Optional[int]) -> str:
    """Подпись частоты: «ежедневно», «по пятницам», «1-го числа», «в последний день месяца»."""
    if freq == "day":
        return "ежедневно"
    if freq == "week":
        assert weekday is not None
        return "по " + _WEEKDAYS_DATIVE[weekday - 1]
    return "в последний день месяца" if month_day == 0 else f"{month_day}-го числа"


def _schedule_label(schedule: schedule_repo.Schedule) -> str:
    """Подпись расписания в списке: «Лидеры · Лучшие снайперы — по пятницам 10:00 (МСК)»."""
    screen = _screen_label(schedule.screen, schedule.params)
    when = _when_label(schedule.freq, schedule.weekday, schedule.month_day)
    zone = "МСК" if schedule.timezone == "Europe/Moscow" else schedule.timezone
    return f"{screen} — {when} {schedule.local_time:%H:%M} ({zone})"


def _subscriptions_schedules_screen(schedules: List[schedule_repo.Schedule]) -> _Screen:
    """Расписания чата списком и кнопка 🗑 у каждого (`sub:s:<id>`); как создать — в подвале."""
    labels = [_schedule_label(s) for s in schedules]
    rows = [
        [InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"sub:s:{s.id}")]
        for s, label in zip(schedules, labels)
    ]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:m")])
    body = rich_list([html.escape(label) for label in labels]) if labels else "Пока ничего нет."
    return (
        "\n".join(["<b>Расписания</b>", body, rich_footer(_SCHEDULE_HINT)]),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _subscriptions_divisions_screen(
    teams_by_division: Dict[str, List[Tuple[int, str, str]]], team_ids: Set[int]
) -> _Screen:
    """Шаг 1 выбора команд: дивизионы с числом выбранных команд в скобках."""
    buttons = []
    for i, (division, teams) in enumerate(teams_by_division.items()):
        chosen = sum(1 for team_id, _, _ in teams if team_id in team_ids)
        label = f"{division} ({chosen})" if chosen else division
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"sub:D:{i}"))
    rows = build_menu(buttons, n_cols=_DIVISION_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:m")])
    return (
        "<b>Команды</b>\n" + rich_footer("Выберите дивизион."),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _subscriptions_teams_screen(
    teams_by_division: Dict[str, List[Tuple[int, str, str]]], team_ids: Set[int], division_index: int
) -> _Screen:
    """Шаг 2 выбора команд: команды дивизиона номер `division_index`, у выбранных ✅."""
    division, teams = list(teams_by_division.items())[division_index]
    buttons = [
        _sub_button(abbrev, team_id in team_ids, f"sub:t:{division_index}:{team_id}")
        for team_id, abbrev, _ in teams
    ]
    rows = build_menu(buttons, n_cols=_TEAM_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton(text="« К дивизионам", callback_data="sub:T")])
    return (
        f"<b>{html.escape(division)}</b>\n"
        + rich_footer("Нажмите на команду, чтобы подписаться или отписаться."),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _subscriptions_countries_screen(codes: List[str], chosen: List[str]) -> _Screen:
    """Страны рейтинга `/countries`, у выбранных ✅. Подписанная страна, выпавшая из рейтинга,
    тоже получает кнопку — иначе от неё нельзя отписаться."""
    buttons = [
        _sub_button(code, code in chosen, f"sub:c:{code}")
        for code in codes + [c for c in chosen if c not in codes]
    ]
    rows = build_menu(buttons, n_cols=_COUNTRY_BUTTON_COLUMNS)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="sub:m")])
    return (
        "<b>Страны</b>\n" + rich_footer("Нажмите на страну, чтобы подписаться или отписаться."),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _subscriptions_main_screen_for(chat_id: int) -> _Screen:
    """Главный экран по текущим подпискам чата; читает БД."""
    subs = subscription_repo.get_chat_subscriptions(chat_id)
    names = sorted(
        name
        for teams in season_teams_by_division().values()
        for team_id, _, name in teams
        if team_id in subs.team_ids
    )
    schedules = len(schedule_repo.list_for_chat(chat_id))
    return _subscriptions_main_screen(subs.digest, subs.upcoming, names, subs.countries, schedules)


async def show_subscriptions(bot: Bot, chat_id: int) -> None:
    """`/subscriptions`: меню подписок — сводка, дайджест, предстоящие матчи, команды,
    страны и расписания (Задачи 63, 78, 107)."""
    try:
        text, markup = _subscriptions_main_screen_for(chat_id)
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await _send(bot, chat_id, _SUBSCRIPTIONS_DB_ERROR)
        return
    await _send(bot, chat_id, text, markup)


def _toggle_subscription(chat_id: int, action: str, args: List[str]) -> str:
    """Переключает подписку по нажатой кнопке (`sub:d`, `sub:u`, `sub:t:…`, `sub:c:…`), пишет
    в БД и возвращает подсказку для ответа на нажатие."""
    digest, upcoming, team_ids, countries = subscription_repo.get_chat_subscriptions(chat_id)
    if action in ("d", "u"):
        kind, on = ("morning_digest", digest) if action == "d" else ("upcoming_games", upcoming)
        if on:
            subscription_repo.mark_subscription_inactive_by_chat_kind_team(chat_id, kind)
        else:
            subscription_repo.upsert_chat_kind(chat_id, kind)
        if action == "d":
            return "Дайджест " + ("выключен" if on else "включён")
        return "Предстоящие матчи " + ("выключены" if on else "включены")
    if action == "t":
        team_id = int(args[1])
        abbrev = next(
            a for teams in season_teams_by_division().values() for t, a, _ in teams if t == team_id
        )
        if team_id in team_ids:
            subscription_repo.deactivate_team_scores(chat_id, team_id)
        else:
            subscription_repo.upsert_team_scores(chat_id, team_id)
        return f"Подписка на {abbrev} " + ("отключена" if team_id in team_ids else "включена")
    code = args[0]
    if code in countries:
        subscription_repo.mark_subscription_inactive_by_chat_kind_team(
            chat_id, "country_players", None, code
        )
    else:
        subscription_repo.upsert_country(chat_id, code)
    return f"Подписка на {code} " + ("отключена" if code in countries else "включена")


async def callback_subscriptions(callback: CallbackQuery) -> None:
    """Кнопки `sub:…` меню `/subscriptions`: переключают подписку (каждое нажатие сразу
    пишется в БД) и перерисовывают то же сообщение; ✅ рисуются по данным БД после записи,
    поэтому при сбое записи отметки нет. «Готово» убирает клавиатуру. `sub:s:<id>` стирает
    расписание этого чата (чужое запрос с `chat_id` не найдёт) и пишет `schedule_deleted`."""
    message = callback_message(callback)
    chat_id = message.chat.id
    assert callback.data is not None
    _, action, *args = callback.data.split(":")
    hint = None
    try:
        if action in ("d", "u", "t", "c"):
            hint = _toggle_subscription(chat_id, action, args)
        if action == "s" and args:
            deleted = schedule_repo.delete(chat_id, int(args[0]))
            if deleted:
                log_action(chat_id, callback.from_user.id, "schedule_deleted", args[0])
            hint = "Расписание удалено" if deleted else "Расписание не найдено"
        if action == "s":
            text, markup = _subscriptions_schedules_screen(schedule_repo.list_for_chat(chat_id))
        elif action in ("m", "d", "u", "x"):
            text, markup = _subscriptions_main_screen_for(chat_id)
        elif action in ("T", "D", "t"):
            teams_by_division = season_teams_by_division()
            team_ids = subscription_repo.get_chat_subscriptions(chat_id).team_ids
            if not any(teams_by_division.values()):
                text, markup = "В базе нет списка команд для этого сезона.", InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="sub:m")]]
                )
            elif action == "T":
                text, markup = _subscriptions_divisions_screen(teams_by_division, team_ids)
            else:
                text, markup = _subscriptions_teams_screen(
                    teams_by_division, team_ids, int(args[0])
                )
        else:
            rankings_text, codes = country_rankings()
            chosen = subscription_repo.get_chat_subscriptions(chat_id).countries
            if codes or chosen:
                text, markup = _subscriptions_countries_screen(codes, chosen)
            else:
                text, markup = rankings_text, InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="sub:m")]]
                )
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await callback.answer()
        await message.edit_text(rich_message=rich_message(_SUBSCRIPTIONS_DB_ERROR))
        return
    await callback.answer(hint)
    await message.edit_text(
        rich_message=rich_message(text), reply_markup=None if action == "x" else markup
    )


# Экран без параметров — функция от (бот, чат).
_CommandScreen = Callable[[Bot, int], Awaitable[None]]


def _command_handler(show: _CommandScreen) -> Callable[[Message, Bot], Awaitable[None]]:
    """Хендлер команды aiogram (`message`, `bot`) из экрана `show`: ответ — в чат сообщения."""
    async def handler(message: Message, bot: Bot) -> None:
        await show(bot, message.chat.id)
    handler.__doc__ = show.__doc__
    return handler


cmd_help = _command_handler(show_help)
cmd_tonight = _command_handler(show_tonight)
cmd_standings = _command_handler(show_standings)
cmd_today = _command_handler(show_today)
cmd_team = _command_handler(show_team)
cmd_stats = _command_handler(show_stats)
cmd_team_stats = _command_handler(show_team_stats)
cmd_countries = _command_handler(show_countries)
cmd_subscriptions = _command_handler(show_subscriptions)


# Свободный текст (Задача 81): экран, выбранный моделью (`ai_router`), бот рисует тем же
# кодом, что команду или кнопку, но новым сообщением в ответ на вопрос.
AI_COMMANDS: Dict[str, _CommandScreen] = {
    "today": show_today,
    "tonight": show_tonight,
    "standings": show_standings,
    "team": show_team,
    "stats": show_stats,
    "team_stats": show_team_stats,
    "countries": show_countries,
    "subscriptions": show_subscriptions,
    "help": show_help,
}


async def _ai_digest_for_date(bot: Bot, chat_id: int, params: Dict[str, Any]) -> None:
    """Дайджест игрового дня `params["date"]` — как `/today`, но за выбранную дату."""
    day_label, games = day_digest(params["date"])
    await dispatch_day_digest_messages(bot, chat_id, day_label, games)


async def _ai_team_profile(bot: Bot, chat_id: int, params: Dict[str, Any]) -> None:
    """Профиль клуба, как `/WSH` (`all_players=false`) или `/WSH_FULL` (`true`)."""
    await _send(bot, chat_id, team_profile_text(params["team"], params["all_players"]))


def _ai_reply(screen: Callable[[Dict[str, Any]], Tuple[str, InlineKeyboardMarkup]]):
    """Экран из функции «параметры → (текст, кнопки)» новым сообщением в чат."""
    async def _reply(bot: Bot, chat_id: int, params: Dict[str, Any]) -> None:
        await _send(bot, chat_id, *screen(params))
    return _reply


_ParamScreen = Callable[[Bot, int, Dict[str, Any]], Awaitable[None]]

# Инструмент модели (`ai_router.build_tools`) → функция экрана (бот, чат, параметры).
AI_SCREENS: Dict[str, _ParamScreen] = {
    "open_command": lambda bot, chat_id, p: AI_COMMANDS[p["command"]](bot, chat_id),
    "digest_for_date": _ai_digest_for_date,
    "team_profile": _ai_team_profile,
    "leaders": _ai_reply(lambda p: stat_page_reply("st", f"players_season_stats:{p['category']}", 0)),
    "country_players": _ai_reply(lambda p: country_page_reply(p["country"], "F", 0)),
    "advanced_metric": _ai_reply(
        lambda p: stat_page_reply("sa", "{}:{}".format(*ADVANCED_METRICS[p["metric"]]), 0)
    ),
}

# Экраны, которые ставятся на расписание (Задача 107; набор — `ai_router.SCHEDULE_TOOLS` и
# `SCHEDULE_COMMANDS`): ключ — `bot_schedules.screen`, параметры — как у инструмента модели.
SCHEDULE_COMMANDS: Dict[str, _CommandScreen] = {
    name: AI_COMMANDS[name] for name in ai_router.SCHEDULE_COMMANDS
}
SCHEDULE_SCREENS: Dict[str, _ParamScreen] = {
    **{name: AI_SCREENS[name] for name in ai_router.SCHEDULE_TOOLS},
    "open_command": lambda bot, chat_id, p: SCHEDULE_COMMANDS[p["command"]](bot, chat_id),
}


class Draft(NamedTuple):
    """Черновик расписания на карточке (Задача 108): `day` — ISO-день недели у 'week' или
    число месяца у 'month' (0 — последний); `slot` — получас суток 0–47; None — не выбрано."""

    screen: str
    params: Dict[str, Any]
    freq: Optional[str]
    day: Optional[int]
    slot: Optional[int]
    tz: str


# Кнопки карточки (Задача 108). Состояния у бота нет: каждая кнопка несёт черновик целиком,
# каким он станет после нажатия, — `sch:<вид>:<экран>:<частота>:<день>:<слот>:<пояс>`; вид `e`
# — карточка, `z` — выбор пояса, `s` — сохранить; `sch:x` — отмена. Экран — буква инструмента и
# аргумент (`lgoals`, `tWSH_FULL`, `oteam_stats`), пусто — не выбрано, пояс — IANA-имя. Черновик
# живёт в `callback_data`, а не строкой в БД: самый длинный (`oteam_stats`, `month`, `28`, `47`,
# `America/Argentina/ComodRivadavia`) — 62 байта из 64.
SCHEDULE_CALLBACK_PATTERN = r"^sch:(x|[ezs]:[otlca]\w+:(day|week|month)?:\d{0,2}:\d{0,2}:[\w/+-]+)$"
# Инструмент → (буква, параметр-аргумент); у клуба аргумент — `WSH` или `WSH_FULL`.
_SCREEN_CODES = {
    "open_command": ("o", "command"), "team_profile": ("t", ""), "leaders": ("l", "category"),
    "country_players": ("c", "country"), "advanced_metric": ("a", "metric"),
}
# Допустимые аргументы экранов — те же наборы, что `enum` инструментов; клуб — шаблоном.
_SCREEN_ARGS = {
    "command": ai_router.SCHEDULE_COMMANDS, "category": ai_router.LEADER_CATEGORIES,
    "country": tuple(COUNTRY_LABELS), "metric": tuple(ADVANCED_METRICS),
}
_DAYS = {"day": (None,), "week": tuple(range(1, 8)), "month": tuple(range(29))}
_FREQ_BUTTONS = (("day", "День"), ("week", "Неделя"), ("month", "Месяц"))
_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_TIME_PRESETS = (18, 24, 36, 42)  # 09:00, 12:00, 18:00, 21:00
# Пояса РФ кнопкой «Сменить»; другой пояс — просьбой текстом с городом.
_RU_ZONES = {
    "Europe/Kaliningrad": "Калининград", "Europe/Moscow": "Москва", "Europe/Samara": "Самара",
    "Asia/Yekaterinburg": "Екатеринбург", "Asia/Omsk": "Омск", "Asia/Krasnoyarsk": "Красноярск",
    "Asia/Irkutsk": "Иркутск", "Asia/Yakutsk": "Якутск", "Asia/Vladivostok": "Владивосток",
}
_UNKNOWN_SCREEN_TEXT = (
    "Не понял, какой экран присылать. Например: «присылай лидеров по голам по пятницам в 10», "
    "«профиль BOS раз в месяц 1-го числа», «турнирную таблицу каждый день в 9»."
)
_SCHEDULE_LIMIT_TEXT = (
    "Без премиума — одно расписание, оно уже есть: /subscriptions. Больше расписаний — в премиуме: /premium"
)
_CARD_NOTE = "Ночь догружается до 11:00 МСК: раньше экран может прийти без последних матчей."
_ZONE_NOTE = "Другой пояс — напишите просьбу заново с городом, например: «… в 10 по Нью-Йорку»."


def _encode(view: str, d: Draft) -> str:
    """`callback_data` кнопки, которая открывает вид `view` с черновиком `d`."""
    code, key = _SCREEN_CODES[d.screen]
    arg = d.params[key] if key else d.params["team"] + ("_FULL" if d.params["all_players"] else "")
    fields = ("" if f is None else str(f) for f in (d.freq, d.day, d.slot))
    return f"sch:{view}:{code}{arg}:" + ":".join(fields) + f":{d.tz}"


def _decode(data: str) -> Tuple[str, Draft]:
    """Вид и черновик из `callback_data` кнопки карточки (обратное `_encode`).

    Raises:
        ValueError: аргумент экрана, день, слот или пояс вне допустимых — `callback_data`
            подделана (клиент MTProto шлёт любую): в `bot_schedules.params` CHECK-а нет.
    """
    _, view, screen, freq, day, slot, tz = data.split(":")
    name, key = next((n, k) for n, (c, k) in _SCREEN_CODES.items() if c == screen[0])
    arg = screen[1:]
    params: Dict[str, Any] = {key: arg} if key else {"team": arg[:3], "all_players": arg.endswith("_FULL")}
    draft = Draft(name, params, freq or None, int(day) if day else None, int(slot) if slot else None, tz)
    valid_arg = arg in _SCREEN_ARGS[key] if key else re.fullmatch(r"[A-Z]{3}(_FULL)?", arg)
    valid_day = draft.day is None or draft.day in _DAYS[freq or "day"]
    valid_slot = draft.slot is None or draft.slot < 48
    if not (valid_arg and valid_day and valid_slot and tz in zoneinfo.available_timezones()):
        raise ValueError(f"Подделанная callback_data карточки расписания: {data}")
    return view, draft


def schedule_draft(chat_id: int, p: Dict[str, Any]) -> Optional[Draft]:
    """Черновик из параметров `schedule_screen`, которые выбрала модель; None — экран не понят.

    Время округляется до получаса (модель могла оставить «10:17»); пояс, которого нет в
    `zoneinfo`, или не названный — пояс последнего расписания чата, иначе МСК.
    """
    if p["screen"] is None:
        return None
    params = dict(p["screen"])
    screen = params.pop("tool")
    freq = p["freq"]
    day = p["weekday"] if freq == "week" else p["month_day"] if freq == "month" else None
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", p["time"] or "")
    # После 23:45 — 00:00 того же дня недели: пограничный случай, правится кнопкой.
    slot = round((int(match[1]) * 60 + int(match[2])) / 30) % 48 if match else None
    tz = p["timezone"]
    if tz not in zoneinfo.available_timezones():
        existing = schedule_repo.list_for_chat(chat_id)
        tz = existing[-1].timezone if existing else "Europe/Moscow"
    return Draft(screen, params, freq, day, slot, tz)


def _zone_label(tz: str) -> str:
    """«Москва (UTC+3)»; пояс не из РФ — IANA-имя со смещением на сейчас."""
    offset = datetime.now(zoneinfo.ZoneInfo(tz)).utcoffset()
    assert offset is not None
    minutes = int(offset.total_seconds()) // 60
    shift = f"{minutes // 60:+d}" + (f":{minutes % 60:02d}" if minutes % 60 else "")
    return f"{_RU_ZONES.get(tz, tz)} (UTC{shift})"


def _slot_time(slot: int) -> dtime:
    """Время получасового слота суток 0–47."""
    return dtime(slot // 2, slot % 2 * 30)


def _complete(d: Draft) -> bool:
    """Заполнено ли всё, что нужно для сохранения."""
    return d.freq is not None and (d.freq == "day" or d.day is not None) and d.slot is not None


def _card_button(label: str, selected: bool, d: Draft, view: str = "e") -> InlineKeyboardButton:
    """Кнопка карточки: у выбранного значения — «•»."""
    return InlineKeyboardButton(text=("•" if selected else "") + label, callback_data=_encode(view, d))


def _schedule_card(d: Draft) -> _Screen:
    """Карточка-черновик: экран, частота, день, время и пояс; ряд дней зависит от частоты,
    «Сохранить» — только когда заполнено всё."""
    pick = "<i>выбрать</i>"
    when = pick if d.day is None else _when_label(d.freq or "", d.day, d.day)
    lines = [
        "<b>⏰ Новое расписание</b>",
        "Экран: " + html.escape(_screen_label(d.screen, d.params)),
        "Частота: " + (dict(_FREQ_BUTTONS)[d.freq].lower() if d.freq else pick),
        *(["Когда: " + when] if d.freq in ("week", "month") else []),
        "Время: " + (f"{_slot_time(d.slot):%H:%M}" if d.slot is not None else pick),
        "Пояс: " + html.escape(_zone_label(d.tz)),
        rich_footer(_CARD_NOTE),
    ]
    rows = [[
        _card_button(label, d.freq == f, d._replace(freq=f, day=d.day if d.freq == f else None))
        for f, label in _FREQ_BUTTONS
    ]]
    if d.freq == "week":
        rows.append([_card_button(n, d.day == i, d._replace(day=i))
                     for i, n in enumerate(_WEEKDAYS_SHORT, 1)])
    if d.freq == "month":
        days = [_card_button(str(i), d.day == i, d._replace(day=i)) for i in range(1, 29)]
        rows += build_menu(days, n_cols=7)
        rows.append([_card_button("Последний день", d.day == 0, d._replace(day=0))])
    if d.slot is None:
        rows.append([_card_button(f"{_slot_time(s):%H:%M}", False, d._replace(slot=s)) for s in _TIME_PRESETS])
    else:
        rows.append([
            _card_button("−1ч", False, d._replace(slot=(d.slot - 2) % 48)),
            _card_button("+1ч", False, d._replace(slot=(d.slot + 2) % 48)),
            _card_button(":00/:30", False, d._replace(slot=d.slot ^ 1)),
        ])
    rows.append([_card_button("Сменить пояс", False, d, "z")])
    last = [InlineKeyboardButton(text="✖ Отмена", callback_data="sch:x")]
    if _complete(d):
        last.insert(0, InlineKeyboardButton(text="✅ Сохранить", callback_data=_encode("s", d)))
    rows.append(last)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _zone_picker(d: Draft) -> _Screen:
    """Выбор пояса из поясов РФ; «« Назад» — к карточке без изменений."""
    buttons = [_card_button(name, d.tz == tz, d._replace(tz=tz)) for tz, name in _RU_ZONES.items()]
    rows = build_menu(buttons, n_cols=3)
    rows.append([_card_button("« Назад", False, d)])
    return "<b>Часовой пояс</b>\n" + rich_footer(_ZONE_NOTE), InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_schedule_card(callback: CallbackQuery) -> None:
    """Кнопки `sch:…` карточки-черновика: перерисовка карточки или выбора пояса, отмена и
    сохранение (`schedule_repo.create`, журнал `schedule_created`).

    Лимит 107 проверяется в момент нажатия «Сохранить»: бесплатному чату с активным
    расписанием — отказ новым сообщением со ссылкой на `/premium`, карточка остаётся.
    """
    message = callback_message(callback)
    chat_id = message.chat.id
    assert callback.data is not None
    if callback.data == "sch:x":
        await callback.answer()
        await message.edit_text(rich_message=rich_message("Расписание не создано."))
        return
    view, d = _decode(callback.data)
    if view == "s":
        assert _complete(d) and d.freq is not None and d.slot is not None
        if not premium.is_premium(chat_id) and schedule_repo.list_for_chat(chat_id):
            await callback.answer()
            await message.answer_rich(rich_message(_SCHEDULE_LIMIT_TEXT))
            return
        weekday = d.day if d.freq == "week" else None
        month_day = d.day if d.freq == "month" else None
        local = _slot_time(d.slot)
        schedule_id = schedule_repo.create(chat_id, d.screen, d.params, d.freq, weekday, month_day,
                                           local, d.tz, datetime.now(timezone.utc))
        log_action(chat_id, callback.from_user.id, "schedule_created", f"{schedule_id}:{d.screen}")
        label = _schedule_label(schedule_repo.Schedule(
            schedule_id, chat_id, d.screen, d.params, d.freq, weekday, month_day, local, d.tz, False
        ))
        await callback.answer("Расписание сохранено")
        await message.edit_text(rich_message=rich_message(
            f"Готово: {html.escape(label)}\n" + rich_footer("Список и удаление — /subscriptions")
        ))
        return
    text, markup = _zone_picker(d) if view == "z" else _schedule_card(d)
    await callback.answer()
    await message.edit_text(rich_message=rich_message(text), reply_markup=markup)


async def send_due_schedules(bot: Bot, now: datetime) -> None:
    """Шлёт экраны расписаний, чья отправка наступила к `now`, и сдвигает их на следующий слот.

    Зачем: один проход тикера (`run_schedule_ticker`). Без премиума чат получает только самое
    старое активное расписание, остальные пропускают слот (лимит, Задача 107). Заблокировавший
    бота чат (`TelegramForbiddenError`) теряет подписки и расписания (`deactivate_chat`). Сбой
    одного расписания логируется с его `id` и не мешает остальным чатам получить своё; слот
    сдвигается в любом случае, иначе сломанное расписание слалось бы каждую минуту.

    Аргументы: `bot` — бот процесса; `now` — момент прохода (aware).
    """
    blocked: Set[int] = set()
    for schedule in await asyncio.to_thread(schedule_repo.due, now):
        chat_id = schedule.chat_id
        try:
            if chat_id not in blocked and (
                schedule.oldest or await asyncio.to_thread(premium.is_premium, chat_id)
            ):
                await throttled(
                    partial(SCHEDULE_SCREENS[schedule.screen], bot, chat_id, schedule.params), chat_id
                )
                await asyncio.to_thread(
                    log_action, chat_id, None, "schedule_sent", f"{schedule.id}:{schedule.screen}"
                )
        except TelegramForbiddenError:
            logger.info("chat_id=%s blocked bot; deactivate chat", chat_id)
            blocked.add(chat_id)
            await asyncio.to_thread(subscription_repo.deactivate_chat, chat_id)
        except Exception:
            logger.exception("schedule id=%s failed", schedule.id)
        await asyncio.to_thread(schedule_repo.advance, schedule, now)


async def run_schedule_ticker(bot: Bot) -> None:
    """«Будильник» расписаний: раз в минуту, в начале минуты, — `send_due_schedules`.

    Зачем в процессе бота, а не в сервисе `sync`: sync запускает рассылки только по своей сетке
    (ночь и день с 12:30 UTC), а расписанию нужно любое время суток (решение человека,
    Задача 107). Сбой прохода (например, БД недоступна) логируется, следующий проход — через
    минуту: упавшая задача asyncio молча остановила бы все расписания при живом боте.
    """
    while True:
        try:
            await send_due_schedules(bot, datetime.now(timezone.utc))
        except Exception:
            logger.exception("schedule tick failed")
        await asyncio.sleep(60 - datetime.now(timezone.utc).second)


# Отказ по лимиту → вид лимита в журнале `bot_actions`: премиум (сутки), неделя, всего.
_AI_LIMIT_KINDS = {
    ai_router.LIMIT_TEXT: "day",
    ai_router.WEEK_LIMIT_TEXT: "week",
    ai_router.TOTAL_LIMIT_TEXT: "total",
}


# Просьба о расписании в окне продолжения 83 идёт маршрутизатору, а не ответам по статистике:
# глаголы регулярной отправки и «каждый день/неделю/…».
_SCHEDULE_REQUEST = re.compile(
    r"присыла|отправля|напомина|расписани|ежедневн|еженедельн|ежемесячн"
    r"|кажд(ый|ую|ое) (день|недел|месяц|утро|вечер|понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)",
    re.IGNORECASE,
)


async def on_free_text(message: Message, bot: Bot) -> None:
    """Текст в личке вне команд: вопрос модели (`ai_router.ask`).

    Лимит исчерпан (`ai_router.limit_refusal`: премиум или бесплатный, Задача 96) — отказ без
    вызова модели, вид лимита — в журнал `bot_actions`; модель выбрала экран — бот показывает его (`AI_SCREENS`); иначе — её текст:
    отказ, уточнение или сообщение о сбое API.

    Модель может передать вопрос ответам по статистике (`ai_stats.answer`, Задача 83); а если
    чат получил такой ответ в последние 30 минут, сообщение идёт туда сразу, без маршрутизатора,
    — кроме просьбы о расписании (`_SCHEDULE_REQUEST`). Её модель разбирает инструментом
    `schedule_screen`, бот отвечает карточкой-черновиком (`_schedule_card`, Задача 108).
    """
    chat_id = message.chat.id
    text = message.text or ""
    refusal = ai_router.limit_refusal(chat_id)
    if refusal is not None:
        assert message.from_user is not None
        await asyncio.to_thread(
            log_action, chat_id, message.from_user.id, "ai_limit", _AI_LIMIT_KINDS[refusal]
        )
        await message.answer_rich(rich_message(refusal))
        return
    history = [] if _SCHEDULE_REQUEST.search(text) else ai_stats.recent_pairs(chat_id)
    if history:
        await _answer_stats(message, text, history)
        return
    reply = await ai_router.ask(chat_id, text)
    if reply.tool == "stats_question":
        await _answer_stats(message, text, history)
        return
    if reply.tool is None:
        # Текст модели — не разметка: `<` в нём не должен ломать rich-HTML.
        await message.answer_rich(rich_message(html.escape(reply.text or ai_router.UNSUPPORTED_TEXT)))
        return
    if reply.tool == "schedule_screen":
        draft = schedule_draft(chat_id, reply.params)
        if draft is None:
            await message.answer_rich(rich_message(_UNKNOWN_SCREEN_TEXT))
            return
        card, markup = _schedule_card(draft)
        await message.answer_rich(rich_message(card), reply_markup=markup)
        return
    await AI_SCREENS[reply.tool](bot, chat_id, reply.params)


async def _answer_stats(message: Message, question: str, history: List[Dict[str, Any]]) -> None:
    """Ответ на статистический вопрос (`ai_stats.answer`); текст модели экранируется."""
    reply = await ai_stats.answer(message.chat.id, question, history)
    await message.answer_rich(rich_message(html.escape(reply)))


def is_plain_text(message: Message) -> bool:
    """Текстовое сообщение, не начинающееся с команды бота.

    Зачем: свободный текст ловит только обычный текст — команда, даже неизвестная
    боту, к модели не уходит. Команда — как у Telegram: сущность `bot_command` в
    самом начале текста.
    """
    if message.text is None:
        return False
    entities = message.entities or []
    return not (entities and entities[0].type == "bot_command" and entities[0].offset == 0)


def log_action(chat_id: int, user_id: Optional[int], kind: str, data: Optional[str]) -> None:
    """Строка журнала `bot_actions` (Задача 99).

    Зачем: по журналу видно, сколько у бота пользователей и кто возвращается, какие экраны
    открывают и что делают до лимита ИИ — без него действия вне ИИ следа в БД не оставляют.

    Аргументы: `chat_id` и `user_id` — чат и тот, кто действовал (у `schedule_sent` никого:
    шлёт тикер); `kind` — `command`, `callback`, `ai_limit`, `schedule_sent`,
    `schedule_deleted` (Задача 107) или `schedule_created` (Задача 108); `data` — имя команды,
    `callback_data`, вид лимита, `<id>:<экран>` отправленного или созданного или `<id>`
    удалённого расписания.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_actions (chat_id, user_id, kind, data) VALUES (%s, %s, %s, %s)",
                (chat_id, user_id, kind, data),
            )
        conn.commit()


async def log_user_action(
    handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
    event: Any,
    data: Dict[str, Any],
) -> Any:
    """Outer-middleware `message` и `callback_query`: пишет команду или нажатие в журнал
    (`log_action`) и передаёт апдейт дальше.

    Зачем: outer-middleware видит апдейт раньше фильтров, поэтому в журнал попадает и
    неизвестная команда. Команда — имя без `/` и `@бот` в нижнем регистре; свободный текст не
    пишется (он в `ai_questions` / `ai_stats_log`). У нажатий стоит после троттлинга —
    отброшенные им до журнала не доходят.

    Аргументы: `handler` — следующий шаг цепочки aiogram, `event` — сообщение или нажатие,
    `data` — контекст апдейта: `event_chat` и `event_from_user` берутся из него.
    """
    if isinstance(event, CallbackQuery):
        kind, value = "callback", event.data
    elif event.text is not None and not is_plain_text(event):
        kind, value = "command", event.text[1:event.entities[0].length].split("@")[0].lower()
    else:
        return await handler(event, data)
    await asyncio.to_thread(
        log_action, data["event_chat"].id, data["event_from_user"].id, kind, value
    )
    return await handler(event, data)


def _command(name: str) -> Command:
    """Фильтр команды `/name` (и `/name@бот`) без учёта регистра."""
    return Command(name, ignore_case=True)


def _callback(pattern: str) -> MagicFilter:
    """Фильтр callback_data по regex `pattern`; совпадение уходит хендлеру аргументом `match`."""
    return F.data.regexp(pattern).as_("match")


def build_standalone_router() -> Router:
    """Команды верхнего уровня и inline-кнопки — первым в `Dispatcher`, раньше свободного текста."""
    router = Router(name="standalone")
    router.message.register(cmd_start, CommandStart(ignore_case=True))
    for name, handler in (
        ("help", cmd_help),
        ("today", cmd_today),
        ("tonight", cmd_tonight),
        ("standings", cmd_standings),
        ("team", cmd_team),
    ):
        router.message.register(handler, _command(name))
    router.message.register(cmd_team_profile, F.text.regexp(TEAM_COMMAND_PATTERN).as_("match"))
    for name, handler in (
        ("stats", cmd_stats),
        ("team_stats", cmd_team_stats),
        ("countries", cmd_countries),
        ("subscriptions", cmd_subscriptions),
        ("premium", cmd_premium),
        ("paysupport", cmd_paysupport),
    ):
        router.message.register(handler, _command(name))
    router.pre_checkout_query.register(on_pre_checkout)
    router.message.register(on_successful_payment, F.successful_payment)
    for pattern, callback in (
        (STAT_SCREEN_CALLBACK_PATTERN, callback_stat_screen),
        (DIGEST_EXPAND_CALLBACK_PATTERN, callback_expand_digest_game),
        (SUBSCRIPTIONS_CALLBACK_PATTERN, callback_subscriptions),
        (TONIGHT_GAME_CALLBACK_PATTERN, callback_tonight_game),
        (COUNTRY_CALLBACK_PATTERN, callback_country),
        (GOAL_VIDEO_CALLBACK_PATTERN, handle_goal_video),
        (SCHEDULE_CALLBACK_PATTERN, callback_schedule_card),
    ):
        router.callback_query.register(callback, _callback(pattern))
    return router


def build_tail_router() -> Router:
    """Последний роутер: свободный текст (Задача 81), только в личке.

    Стоит после команд: в aiogram апдейт берёт первый подошедший хендлер.
    """
    router = Router(name="tail")
    router.message.register(on_free_text, F.chat.type == "private", is_plain_text)
    return router


async def _publish_command_menu(bot: Bot) -> None:
    """Публикует список команд в Telegram (`set_my_commands`) при старте бота.

    Зачем: без него кнопка «Меню» клиента пуста или показывает устаревший
    список — пользователь не видит, какие команды есть. Источник —
    `help_text.BOT_COMMANDS`, тот же, что у `/help`.
    """
    await bot.set_my_commands(
        [BotCommand(command=cmd, description=desc) for cmd, desc in BOT_COMMANDS]
    )


def build_dispatcher() -> Dispatcher:
    """Собирает `Dispatcher` со всеми хендлерами бота.

    Зачем: единственная точка сборки, которую тест может построить без сети и
    проверить маршрутизацию апдейтов (`Dispatcher.feed_update`), не поднимая polling.

    Троттлинг нажатий (`throttle.py`) — outer-middleware `callback_query`: он видит
    нажатие раньше фильтров любого роутера и при превышении лимита обрывает его, не
    давая дойти до кнопок. Журнал действий (`log_user_action`) — outer-middleware
    `message` и `callback_query`, у нажатий вторым: aiogram зовёт их в порядке регистрации. Роутеры проверяются по порядку: standalone → хвост
    (`build_tail_router`).

    Апдейты разных пользователей обрабатываются конкурентно (polling aiogram запускает
    каждый задачей): ожидание ответа модели на свободный текст (секунды, при сбое API —
    до ~30 с) не останавливает остальных. Апдейты одного пользователя в чате — по одному
    (`SimpleEventIsolation`, как было в python-telegram-bot): иначе двойной тап обгоняет
    запись реестра `/tonight`, и карточка уходит дважды.
    """
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.callback_query.outer_middleware(enforce_callback_rate_limit)
    dispatcher.callback_query.outer_middleware(log_user_action)
    dispatcher.message.outer_middleware(log_user_action)
    dispatcher.startup.register(_publish_command_menu)
    dispatcher.include_routers(build_standalone_router(), build_tail_router())
    return dispatcher


async def run_polling(token: str) -> None:
    """Long polling бота с токеном `token` (в проде `config.TOKEN`) до сигнала остановки;
    рядом — тикер расписаний (`run_schedule_ticker`), он останавливается вместе с polling."""
    bot = Bot(token)
    ticker = asyncio.create_task(run_schedule_ticker(bot))
    try:
        await build_dispatcher().start_polling(bot)
    finally:
        ticker.cancel()


def main() -> None:
    """Точка входа `python bot.py`: проверка окружения, затем long polling
    до сигнала остановки."""
    config.validate_env("ANTHROPIC_API_KEY")
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
    )
    logger.info("Starting bot")
    asyncio.run(run_polling(config.TOKEN))


if __name__ == '__main__':
    main()
