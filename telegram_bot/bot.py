"""Точка входа Telegram-бота: сборка `Dispatcher` aiogram и регистрация хендлеров.

Роль в пайплайне: `bot.py` собирает `Dispatcher` aiogram 3.31 из двух роутеров —
команды и кнопки, затем свободный текст, — вешает троттлинг нажатий (`throttle.py`)
и запускает long polling. Меню и листание таблиц статистики живут в
`stats_handlers.py`; здесь — команды верхнего уровня, подписки, свободный текст и
сборка `Dispatcher`.
"""

import asyncio
import html
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import psycopg2
from aiogram import Bot, Dispatcher, F, Router
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
import subscription_repo
from leaderboard_specs import ADVANCED_METRICS
from throttle import enforce_callback_rate_limit
from bot_messages import (
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


# Команды, которые открывает и свободный текст (`AI_COMMANDS`), принимают (message, bot):
# так их одинаково зовёт и aiogram (бот подставляется по имени параметра), и `on_free_text`.


async def cmd_start(message: Message, bot: Bot, command: CommandObject) -> None:
    """`/start`, в том числе deep link `?start=game_<id>` из карточки матча."""
    args = (command.args or "").split()
    if args and args[0].startswith("game_"):
        try:
            gid = int(args[0].split("_", 1)[1])
        except (IndexError, ValueError):
            await message.answer_rich(rich_message(START_MESSAGE))
            return
        await send_game_card_message(bot, message.chat.id, gid)
        return
    await message.answer_rich(rich_message(START_MESSAGE))


async def cmd_help(message: Message, bot: Bot) -> None:
    """`/help`: список команд бота."""
    await message.answer_rich(rich_message(HELP_MESSAGE))

_PREMIUM_PERKS = (
    f"Что даёт премиум: ИИ-вопросы — до {ai_router.PREMIUM_DAILY_LIMIT} в сутки вместо недельного лимита, "
    "прогноз модели в превью матчей."
)
_PREMIUM_CANCEL_HINT = (
    "Отменить автопродление — в настройках Telegram: Настройки → Stars → Подписки. "
    "Доступ остаётся до конца оплаченного срока."
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


async def cmd_tonight(message: Message, bot: Bot) -> None:
    """`/tonight`: расписание сегодняшнего дня из NHL API плюс кнопки матчей."""
    try:
        payload = await asyncio.to_thread(fetch_score, league_today())
    except ScoreboardFetchError:
        logger.exception("NHL score request failed")
        await message.answer_rich(rich_message(
            "Сейчас не удалось загрузить расписание NHL. Попробуйте чуть позже."
        ))
        return
    games = slate_games_sorted(payload)
    # Проза без счётных элементов — сноска без чисел приходит из хелпера
    # по умолчанию (truncate_telegram_text), а не из литерала здесь.
    text = truncate_telegram_text(tonight_reply_intro(payload))
    await message.answer_rich(
        rich_message(text),
        reply_markup=build_tonight_match_keyboard(games) if games else None,
    )


async def cmd_standings(message: Message, bot: Bot) -> None:
    """`/standings`: турнирная таблица лиги."""
    await message.answer_rich(rich_message(team_table()))


async def cmd_today(message: Message, bot: Bot) -> None:
    """`/today`: дайджест матчей прошедшей ночи по Москве (`last_night_day()`).

    Ночь без матчей (выходной день лиги или данные ещё не загружены) — честная
    пометка и последний игровой день в базе, а не голое «матчей не найдено»:
    иначе последние результаты нечем посмотреть.
    """
    night = last_night_day()
    day_label, games = day_digest(night)
    if all(gid == 0 for gid, _text, _meta in games):
        await message.answer_rich(rich_message(
            f"Прошедшей ночью ({night}) матчей в базе нет — показываю последний игровой день."
        ))
        day_label, games = day_digest()
    await dispatch_day_digest_messages(bot, message.chat.id, day_label, games)


async def cmd_team(message: Message, bot: Bot) -> None:
    """`/team`: команды сезона кликабельными командами `/ABBR`."""
    await message.answer_rich(rich_message(team_list_text()))


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
    await message.answer_rich(rich_message(team_profile_text(abbrev, bool(full))))


async def cmd_stats(message: Message, bot: Bot) -> None:
    """`/stats`: лидеры игроков — все метрики полевых и вратарей кнопками одного экрана."""
    await message.answer_rich(rich_message(STATS_COMMAND_INTRO), reply_markup=stats_menu_keyboard())


async def cmd_team_stats(message: Message, bot: Bot) -> None:
    """`/team_stats`: рейтинги команд — голы, броски, спецбригады, вбрасывания."""
    await message.answer_rich(rich_message(TEAM_STATS_COMMAND_INTRO), reply_markup=team_stats_keyboard())


async def cmd_countries(message: Message, bot: Bot) -> None:
    """`/countries`: рейтинг стран сезона и кнопки стран (Задача 49)."""
    text, markup = country_rankings_reply()
    await message.answer_rich(rich_message(text), reply_markup=markup)


_DELIVERY_NOTE = (
    "Итоги ночи приходят, когда загружены все её матчи, не позже 11:00 МСК.\n"
    "Превью матчей дня — с 15:30 МСК, по одному раз в полчаса."
)
_SUBSCRIPTIONS_DB_ERROR = "Подписки недоступны: выполните make db-migrate на вашей БД PostgreSQL."

# Кнопки меню `/subscriptions` (Задача 63): `sub:m` главный экран, `sub:d` дайджест,
# `sub:u` предстоящие матчи (Задача 78), `sub:T` дивизионы, `sub:D:<i>` команды дивизиона i, `sub:t:<i>:<team_id>` команда
# (i — чтобы перерисовать тот же дивизион), `sub:C` страны, `sub:c:<CODE>` страна,
# `sub:x` «Готово». Самая длинная — `sub:t:3:` + id команды, заведомо меньше 64 байт.
SUBSCRIPTIONS_CALLBACK_PATTERN = r"^sub:(m|d|u|T|C|x|D:[0-3]|t:[0-3]:\d+|c:[A-Z]{3})$"
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
    digest: bool, upcoming: bool, team_names: List[str], countries: List[str]
) -> _Screen:
    """Главный экран: сводка подписок, переключатели дайджеста и предстоящих матчей
    (каждый в своём ряду — вдвоём в ряд на телефоне не влезают), переходы в подменю.
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
        [InlineKeyboardButton(text="Готово", callback_data="sub:x")],
    ])
    return "\n".join(lines), markup


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
    return _subscriptions_main_screen(subs.digest, subs.upcoming, names, subs.countries)


async def cmd_subscriptions(message: Message, bot: Bot) -> None:
    """`/subscriptions`: меню подписок — сводка, дайджест, предстоящие матчи, команды и
    страны (Задачи 63, 78)."""
    try:
        text, markup = _subscriptions_main_screen_for(message.chat.id)
    except psycopg2.Error:
        logger.exception("subscriptions DB error")
        await message.answer_rich(rich_message(_SUBSCRIPTIONS_DB_ERROR))
        return
    await message.answer_rich(rich_message(text), reply_markup=markup)


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
    поэтому при сбое записи отметки нет. «Готово» убирает клавиатуру."""
    message = callback_message(callback)
    chat_id = message.chat.id
    assert callback.data is not None
    _, action, *args = callback.data.split(":")
    hint = None
    try:
        if action in ("d", "u", "t", "c"):
            hint = _toggle_subscription(chat_id, action, args)
        if action in ("m", "d", "u", "x"):
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


# Свободный текст (Задача 81): экран, выбранный моделью (`ai_router`), бот рисует тем же
# кодом, что команду или кнопку, но новым сообщением в ответ на вопрос.
AI_COMMANDS: Dict[str, Callable[[Message, Bot], Awaitable[None]]] = {
    "today": cmd_today,
    "tonight": cmd_tonight,
    "standings": cmd_standings,
    "team": cmd_team,
    "stats": cmd_stats,
    "team_stats": cmd_team_stats,
    "countries": cmd_countries,
    "subscriptions": cmd_subscriptions,
    "help": cmd_help,
}


async def _ai_digest_for_date(message: Message, bot: Bot, params: Dict[str, Any]) -> None:
    """Дайджест игрового дня `params["date"]` — как `/today`, но за выбранную дату."""
    day_label, games = day_digest(params["date"])
    await dispatch_day_digest_messages(bot, message.chat.id, day_label, games)


async def _ai_team_profile(message: Message, bot: Bot, params: Dict[str, Any]) -> None:
    """Профиль клуба, как `/WSH` (`all_players=false`) или `/WSH_FULL` (`true`)."""
    text = team_profile_text(params["team"], params["all_players"])
    await message.answer_rich(rich_message(text))


def _ai_reply(screen: Callable[[Dict[str, Any]], Tuple[str, InlineKeyboardMarkup]]):
    """Экран из функции «параметры → (текст, кнопки)» ответом на вопрос пользователя."""
    async def _send(message: Message, bot: Bot, params: Dict[str, Any]) -> None:
        text, markup = screen(params)
        await message.answer_rich(rich_message(text), reply_markup=markup)
    return _send


# Инструмент модели (`ai_router.build_tools`) → функция экрана.
AI_SCREENS: Dict[str, Callable[[Message, Bot, Dict[str, Any]], Awaitable[None]]] = {
    "open_command": lambda message, bot, p: AI_COMMANDS[p["command"]](message, bot),
    "digest_for_date": _ai_digest_for_date,
    "team_profile": _ai_team_profile,
    "leaders": _ai_reply(lambda p: stat_page_reply("st", f"players_season_stats:{p['category']}", 0)),
    "country_players": _ai_reply(lambda p: country_page_reply(p["country"], "F", 0)),
    "advanced_metric": _ai_reply(
        lambda p: stat_page_reply("sa", "{}:{}".format(*ADVANCED_METRICS[p["metric"]]), 0)
    ),
}


async def on_free_text(message: Message, bot: Bot) -> None:
    """Текст в личке вне команд: вопрос модели (`ai_router.ask`).

    Лимит исчерпан (`ai_router.limit_refusal`: премиум или бесплатный, Задача 96) — отказ без
    вызова модели; модель выбрала экран — бот показывает его (`AI_SCREENS`); иначе — её текст:
    отказ, уточнение или сообщение о сбое API.

    Модель может передать вопрос ответам по статистике (`ai_stats.answer`, Задача 83); а если
    чат получил такой ответ в последние 30 минут, сообщение идёт туда сразу, без маршрутизатора.
    """
    chat_id = message.chat.id
    text = message.text or ""
    refusal = ai_router.limit_refusal(chat_id)
    if refusal is not None:
        await message.answer_rich(rich_message(refusal))
        return
    history = ai_stats.recent_pairs(chat_id)
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
    await AI_SCREENS[reply.tool](message, bot, reply.params)


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
    давая дойти до кнопок. Роутеры проверяются по порядку: standalone → хвост
    (`build_tail_router`).

    Апдейты разных пользователей обрабатываются конкурентно (polling aiogram запускает
    каждый задачей): ожидание ответа модели на свободный текст (секунды, при сбое API —
    до ~30 с) не останавливает остальных. Апдейты одного пользователя в чате — по одному
    (`SimpleEventIsolation`, как было в python-telegram-bot): иначе двойной тап обгоняет
    запись реестра `/tonight`, и карточка уходит дважды.
    """
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.callback_query.outer_middleware(enforce_callback_rate_limit)
    dispatcher.startup.register(_publish_command_menu)
    dispatcher.include_routers(build_standalone_router(), build_tail_router())
    return dispatcher


async def run_polling(token: str) -> None:
    """Long polling бота с токеном `token` (в проде `config.TOKEN`) до сигнала остановки."""
    await build_dispatcher().start_polling(Bot(token))


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
