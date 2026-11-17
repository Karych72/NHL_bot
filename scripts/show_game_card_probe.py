"""Витрина карточки матча на dev-боте: все виды сообщений с карточкой — кодом из рабочей копии.

Шаблон: `scripts/show_game_card.sh` вшивает сюда модули сообщений рабочей копии (`_SRCS`, в
порядке импорта; в контейнере dev-стека — код `develop`) и прогоняет в контейнере; остальное —
модули образа.
БД — `nhl_dev`, токен — dev-бота. Карточки уходят настоящей отправкой `send_game_card`, как у
подписчика: клипы не из кэша dev-бота скачиваются с NHL, и лента «Видео голов» — со всеми голами.
Аргументы: <chat_id> [<гости> <хозяева>] — главный матч; без них — последний матч с овертаймом.
"""
import asyncio
import base64
import sys
import types

_SRCS: dict = {}  # MESSAGES_BASE64: строку заменяет show_game_card.sh
for _name, _src in _SRCS.items():
    _module = types.ModuleType(_name)
    _module.__file__ = f"{_name}.py"
    sys.modules[_name] = _module
    exec(compile(base64.b64decode(_src), f"{_name}.py", "exec"), _module.__dict__)

from aiogram import Bot  # noqa: E402

import bot_messages as bm  # noqa: E402
import config  # noqa: E402
import push_digest_job  # noqa: E402
import stats_handlers  # noqa: E402
from database import fetch_all  # noqa: E402
from help_text import SUBSCRIPTIONS_FOOTER  # noqa: E402
from rich_markup import rich_footer  # noqa: E402

_GAMES = (
    "FROM games g "
    "JOIN teams h ON h.team_id = g.home_team_id AND h.season_id = g.season_id "
    "JOIN teams a ON a.team_id = g.away_team_id AND a.season_id = g.season_id "
)


def one_game(where: str, params: tuple = ()) -> int | None:
    rows = fetch_all(f"SELECT g.game_id {_GAMES} WHERE {where} ORDER BY g.day DESC, g.game_id DESC LIMIT 1",
                     params, ["game_id"])
    return int(rows["game_id"][0]) if rows["count_rows"] else None


def team_names(game_id: int) -> tuple[str, str]:
    rows = fetch_all(f"SELECT h.name, a.name {_GAMES} WHERE g.game_id = %s", (game_id,), ["home", "away"])
    return str(rows["home"][0]), str(rows["away"][0])


async def main(chat_id: int, away: str | None, home: str | None) -> None:
    main_game = (
        one_game("a.abbreviation = %s AND h.abbreviation = %s", (away, home)) if away
        else one_game("g.is_overtime AND NOT g.is_shootouts")
    )
    shootout = one_game("g.is_overtime AND g.is_shootouts")
    playoff = one_game("g.game_type = 3")
    russian = one_game(
        "EXISTS (SELECT 1 FROM all_goals x JOIN rosters r ON r.player_id = x.goal_player_id "
        "AND r.season_id = g.season_id WHERE x.game_id = g.game_id AND r.nationality = 'RUS')"
    )
    print(f"матчи: главный={main_game} буллиты={shootout} плей-офф={playoff} гол россиянина={russian}")
    bot = Bot(config.TOKEN)
    sent = 0

    async def show(label: str, game_id: int | None, tail, collapsed: bool = False) -> None:
        """Карточка `send_game_card` — как у подписчика; `tail(game_id)` — HTML после неё."""
        nonlocal sent
        if game_id is None:
            print(f"{label}: подходящего матча в nhl_dev нет — пропуск")
            return
        try:
            await stats_handlers.send_game_card(
                bot, chat_id, game_id, f"{tail(game_id)}\n{rich_footer('Витрина: ' + label)}", collapsed,
            )
            sent += 1
            print(f"{label}: OK (game_id={game_id})")
        except Exception as exc:  # витрина: печатаем отказ и идём дальше
            print(f"{label}: ОТКАЗ {exc}")
        await asyncio.sleep(1)

    def footer(*reasons: str) -> str:
        reason = f"По подписке: {' · '.join(reasons)}\n" if reasons else ""
        return rich_footer(f"{reason}{SUBSCRIPTIONS_FOOTER}")

    def country_block(game_id: int) -> str:
        block = bm.country_game_block("RUS", game_id)
        return f"\n\n{block}" if block else ""

    try:
        # Рассылка итогов матча.
        await show("1. рассылка: подписка на команду — карточка открыта", main_game,
                   lambda g: f"\n{footer(team_names(g)[0])}")
        await show("2. рассылка: только страна — карточка свёрнута, под ней блок страны", russian,
                   lambda g: f"{country_block(g)}\n{footer('Россия')}", collapsed=True)
        if russian is not None:
            goals = [(g, e) for g, e, nat in bm.night_goals(_night(russian)) if g == russian and nat == "RUS"]
            await push_digest_job._send_goal_album(bot, chat_id, goals)
            print(f"2a. альбом голов россиян: {len(goals)} гол(ов)")
        await show("3. рассылка: команда и страна — карточка открыта, блок страны под ней", russian,
                   lambda g: f"{country_block(g)}\n{footer(team_names(g)[0], 'Россия')}")
        await show("4. рассылка владельцу: матч без подписки — без «По подписке»", main_game,
                   lambda g: f"\n{footer()}")
        # Меню и /today.
        await show("5. меню / /tonight / кнопка дайджеста: карточка + блок страны подписчика", russian,
                   country_block)
        await show("6. /today, один матч за ночь: карточка + подсказка «Ещё: …»", main_game,
                   lambda g: f"\n{rich_footer(stats_handlers._DIGEST_MORE_HINT)}")
        # Особые матчи.
        await show("7. матч с буллитами: «Овертайм — Голов нет», «Буллиты» без времени", shootout,
                   lambda g: "")
        await show("8. плей-офф: раунд и игра под строкой счёта", playoff, lambda g: "")
        # Сводка ночи (/today при нескольких матчах, утренний дайджест) — «(ОТ)» кириллицей.
        if main_game is not None:
            day, ids = bm.day_digest(_night(main_game))
            await stats_handlers.dispatch_day_digest_messages(bot, chat_id, day, ids, heading="Матчи ночи")
            print(f"9. сводка ночи {day}: {len(ids)} матч(ей)")
    finally:
        await bot.session.close()
    print(f"отправлено карточек: {sent}")


def _night(game_id: int):
    rows = fetch_all("SELECT day FROM games WHERE game_id = %s", (game_id,), ["day"])
    return rows["day"][0]


args = sys.argv[1:]
asyncio.run(main(int(args[0]), *(args[1:3] if len(args) >= 3 else (None, None))))
