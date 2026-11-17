"""Экраны стран: рейтинг, страница и профиль страны, блоки игроков ночи.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`): рейтинг стран
`/countries`, страница и профиль страны (`/RUS`), блоки игроков страны и подписанных игроков
за ночь и за матч — их собирает рассылка `push_digest_job` и карточка в `stats_handlers`.
"""

import html
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import config
from database import cached_fetch_all, fetch_all
from leaderboard_messages import _BY_COUNTRY, _COUNTRY_FROM, _GROUP_LEGEND, _player_group_table, PLAYER_GROUPS
from rich_markup import (
    _leaderboard_page_text,
    _table,
    country_label,
    country_name,
    LEADERBOARD_PAGE_SIZE,
    plural_word,
    rich_details,
    rich_footer,
    rich_list,
)


# Страна с меньшим числом полевых игроков сезона в рейтинг и на кнопки не
# попадает: у 1–2 игроков «сумма очков страны» — это просто их личная статистика.
COUNTRY_MIN_PLAYERS = 3
COUNTRY_NOT_FOUND = "Страна не найдена в рейтинге этого сезона."


def _country_ranking() -> List[Tuple[str, int, int, int]]:
    """Страны сезона `config.SEASON_ID`, прошедшие порог `COUNTRY_MIN_PLAYERS`,
    в порядке рейтинга: (код, игроков, очков, голов). Единственный источник
    списка допустимых кодов страны для остальных экранов (Задача 42)."""
    stats = cached_fetch_all(
        "SELECT r.nationality, COUNT(*), SUM(p.points), SUM(p.goals) "
        + _COUNTRY_FROM
        + "WHERE p.season_id = %s AND r.position <> 'G' AND r.nationality IS NOT NULL "
        "GROUP BY r.nationality HAVING COUNT(*) >= %s "
        "ORDER BY SUM(p.points) DESC, SUM(p.goals) DESC, r.nationality",
        (config.SEASON_ID, COUNTRY_MIN_PLAYERS),
        columns=["code", "players", "points", "goals"],
    )
    return [
        (stats["code"][i], int(stats["players"][i]),
         int(stats["points"][i] or 0), int(stats["goals"][i] or 0))
        for i in range(stats["count_rows"])
    ]


def country_rankings() -> Tuple[str, List[str]]:
    """Рейтинг стран по сумме очков полевых игроков текущего сезона (Задача 42).

    Зачем: экран выбора страны — текст рейтинга и коды для кнопок.

    Правило учёта: игрок без строки `rosters` на сезон не учитывается
    (INNER JOIN); игрок с `rosters.nationality IS NULL` в рейтинг не входит, их
    число выводится отдельной сноской. Вратари исключены; страны с числом
    игроков меньше `COUNTRY_MIN_PLAYERS` не показываются. Страна — национальность
    игрока по NHL (`nationalityCode` отчётов bios, одна на игрока, Задача 62).

    Возвращает: (текст, коды стран в порядке рейтинга — только прошедшие порог).
    Пустой сезон — текст с причиной и пустой список кодов (Задача 36).
    """
    season_esc = html.escape(str(config.CURRENT_SEASON))
    ranking = _country_ranking()
    if not ranking:
        # Простой текст: причина уходит и в «Подписки → Страны» (`bot.py`), а там не rich.
        return "В базе нет данных по странам для этого сезона.", []
    unknown = cached_fetch_all(
        "SELECT COUNT(*) " + _COUNTRY_FROM
        + "WHERE p.season_id = %s AND r.position <> 'G' AND r.nationality IS NULL",
        (config.SEASON_ID,),
        columns=["n"],
    )["n"][0]
    rows = [["", "Страна", "Игр", "О", "Г", "О/и"]] + [
        [f"{i}.", country_name(code), str(players), str(points), str(goals),
         f"{points / players:.1f}"]
        for i, (code, players, points, goals) in enumerate(ranking, start=1)
    ]
    legend = (
        f"Полевые игроки; страна — национальность игрока по NHL (одна на игрока); "
        f"страны от {COUNTRY_MIN_PLAYERS} игроков. Игр — игроков, О — очки, Г — голы, "
        f"О/и — очков на игрока."
    )
    footer = f"Сезон {season_esc}"
    if unknown:
        footer += f"\nСтрана не указана в данных NHL у игроков: {unknown} (не учтены)."
    text = "\n".join([
        "<b>Рейтинг стран</b>",
        _table(rows, "rlrrrr"),
        rich_details("Обозначения", legend),
        rich_footer(footer),
    ])
    return text, [code for code, _, _, _ in ranking]


def country_page(code: str, group: str, offset: int) -> Tuple[str, bool, bool]:
    """Страница игроков страны одной группы — выровненная таблица (Задача 49).

    Зачем: экран `/countries` после выбора страны; таблица и колонки групп —
    `_player_group_table`.

    Аргументы:
        code: код страны из `country_rankings()`; иной код — текст «Страна не
            найдена...» и (False, False), в SQL он не попадает.
        group: ключ `PLAYER_GROUPS` (F / D / G).
        offset: сдвиг страницы (`LEADERBOARD_PAGE_SIZE` строк).

    Возвращает: (текст, есть ли предыдущая страница, есть ли следующая).
    """
    if code not in [row[0] for row in _country_ranking()]:
        return COUNTRY_NOT_FOUND, False, False
    offset = max(offset, 0)
    table, n, total, _first = _player_group_table(
        _BY_COUNTRY, code, group, LEADERBOARD_PAGE_SIZE, offset, ranked=True
    )
    body = f"{table}\n" + rich_details("Обозначения", f"{_GROUP_LEGEND[group]}.")
    # Флаг — в начале заголовка: эмодзи только как метка в начале сообщения (правило 5).
    heading = f"{country_label(code)}: {PLAYER_GROUPS[group][0].lower()}"
    return _leaderboard_page_text(heading, body, n, total, offset)


# Сколько лучших игроков каждой группы показывает `/RUS`: нападающих, защитников, вратарей.
_COUNTRY_PROFILE_TOP = {"F": 10, "D": 8, "G": 5}
# Чем меряется лидер группы в сводке `/RUS` — формы слова для `plural_word`.
_COUNTRY_LEADER_UNIT = {
    "F": ("очко", "очка", "очков"), "D": ("очко", "очка", "очков"),
    "G": ("победа", "победы", "побед"),
}


def country_profile(code: str, channel_footer: Optional[str] = None) -> str:
    """Ответ `/RUS`, `/CAN`… — страна в сезоне одним сообщением.

    Зачем: быстрый взгляд на страну без листания `/countries`. Шапка — место страны
    в рейтинге `/countries`, число полевых и их очки; затем перечень групп: сколько
    игроков и лидер каждой. Ниже — таблицы с колонками экрана страны
    (`_player_group_table`): топ-10 нападающих, топ-8 защитников, топ-5 вратарей
    (`_COUNTRY_PROFILE_TOP`); у страны игроков меньше — таблица короче, пустая группа
    пропускается. В подвале — `/RUS_FULL`, экран страны из `/countries`.

    Аргументы:
        code: код страны из команды `/CODE` в верхнем регистре; вне рейтинга сезона
            (`country_rankings()`) — текст «Страна не найдена…», в SQL он не попадает.
        channel_footer: строка подвала поста в канал (ссылка на бота, Задача 120) — вместо
            команд `/RUS_FULL` и `/countries`, которые в канале не работают; `None` — экран бота.
    """
    ranking = _country_ranking()
    place = next((i for i, row in enumerate(ranking, start=1) if row[0] == code), None)
    if place is None:
        return COUNTRY_NOT_FOUND
    _code, players, points, _goals = ranking[place - 1]
    summary = []
    tables = []
    legends = []
    for group, (label, _cond) in PLAYER_GROUPS.items():
        table, n, total, first = _player_group_table(
            _BY_COUNTRY, code, group, _COUNTRY_PROFILE_TOP[group], 0, ranked=True
        )
        if first is None:
            continue
        name, team, value = first
        summary.append(
            f"{label}: {total}, лучший — <b>{html.escape(name)}</b> ({html.escape(team)}), "
            f"{value} {plural_word(value, _COUNTRY_LEADER_UNIT[group])}"
        )
        shown = f" · топ-{n} из {total}" if n < total else ""
        tables.append(f"<b>{label}</b>{shown}\n{table}")
        legends.append(f"{label}: {_GROUP_LEGEND[group]}.")
    legends.append("Полевые — по очкам, вратари — по победам.")
    season_esc = html.escape(str(config.CURRENT_SEASON))
    footer = (
        f"Сезон {season_esc} · все игроки: /{code}_FULL · все страны: /countries"
        if channel_footer is None
        else f"Сезон {season_esc}\n{channel_footer}"
    )
    return "\n".join([
        # Крупнее — капсом, как шапка карточки матча (`<h1>`–`<h6>` не используем, стиль §1);
        # пустая строка — зазор до сводки.
        f"<b>{country_label(code).upper()} В NHL</b>",
        "",
        f"{place}-е место из {len(ranking)} в рейтинге стран · {players} полевых · "
        f"{points} {plural_word(points, _COUNTRY_LEADER_UNIT['F'])}",
        rich_list(summary),
        "",
        *tables,
        rich_details("Обозначения", "\n".join(legends)),
        rich_footer(footer),
    ])


# Задачи 61, 76, 122: игроки страны (`r.nationality = %s`) или подписанные игроки
# (`s.player_id = ANY(%s)`) в матчах игрового дня (`g.day`) или в одном матче (`g.game_id`).
_COUNTRY_PLAYERS_FROM = (
    "FROM {stats} s "
    "JOIN games g ON g.game_id = s.game_id "
    "JOIN rosters r ON r.player_id = s.player_id AND r.season_id = g.season_id "
    "LEFT JOIN teams t ON t.team_id = s.team_id AND t.season_id = g.season_id "
    "WHERE {scope} = %s AND {who} AND "
)

# game_id → [(player_id, строки игрока)] в порядке блока: полевые по очкам, затем вратари.
PlayerEntries = Dict[int, List[Tuple[int, List[str]]]]


def _night_skater_lines(s: Dict, i: int) -> List[str]:
    """Две строки полевого игрока `i` выборки `_player_entries`: фамилия и
    команда (у центрального — «ц», у защитника — «защ»), под ними статистика:
    у всех голы+передачи, ±, броски и время; у нападающих — хиты, у центральных
    ещё вбрасывания, у защитников — блоки вместо хитов."""
    position = s["position"][i]
    mark = {"C": ", ц", "D": ", защ"}.get(position, "")
    head = f"{html.escape(s['lastname'][i])} ({html.escape(s['team'][i] or '?')}{mark})"
    parts = [
        f"{s['goals'][i] or 0}+{s['assists'][i] or 0}",
        f"{s['plus_minus'][i] or 0:+d}".replace("-", "−"),
        f"{s['shots'][i] or 0} бр.",
    ]
    if position == "D":
        blocked = s["blocked"][i] or 0
        parts.append(f"{blocked} {plural_word(blocked, ('блок', 'блока', 'блоков'))}")
    else:
        hits = s["hits"][i] or 0
        parts.append(f"{hits} {plural_word(hits, ('хит', 'хита', 'хитов'))}")
    if position == "C":
        parts.append(f"вбр. {s['fo_wins'][i] or 0}/{s['fo_taken'][i] or 0}")
    parts.append(s["toi"][i])
    return [head, " · ".join(parts)]


def _player_entries(scope: str, value: Union[date, int], who: str, who_value: Any) -> PlayerEntries:
    """Строки игроков, вышедших на лёд, по матчам выборки `scope = value` (`g.day` или
    `g.game_id`), отобранных условием `who` (`r.nationality = %s` или `s.player_id = ANY(%s)`)
    со значением `who_value`. У игрока две строки — имя и статистика по позиции; полевые по
    очкам и голам, затем вратари. Матча без таких игроков в словаре нет."""
    skaters = fetch_all(
        "SELECT s.game_id, s.player_id, player_display_name(r.name, r.lastname, r.season_id), t.name, "
        "r.position, s.goals, s.assists, s.shots, "
        "s.plus_minus, s.hits, s.blocked, s.face_off_wins, s.face_off_taken, s.time_on_ice "
        + _COUNTRY_PLAYERS_FROM.format(stats="game_player_stats", scope=scope, who=who)
        + "r.position <> 'G' AND s.time_on_ice IS NOT NULL AND s.time_on_ice <> '00:00' "
        "ORDER BY COALESCE(s.goals, 0) + COALESCE(s.assists, 0) DESC, "
        "COALESCE(s.goals, 0) DESC, r.lastname",
        (value, who_value),
        columns=["game_id", "player_id", "lastname", "team", "position", "goals", "assists", "shots",
                 "plus_minus", "hits", "blocked", "fo_wins", "fo_taken", "toi"],
    )
    goalies = fetch_all(
        "SELECT s.game_id, s.player_id, player_display_name(r.name, r.lastname, r.season_id), t.name, "
        "s.saves, s.shots, s.save_percentage, "
        "s.timeonice "
        + _COUNTRY_PLAYERS_FROM.format(stats="game_goalie_stats", scope=scope, who=who)
        + "s.timeonice IS NOT NULL AND s.timeonice <> '00:00' ORDER BY r.lastname",
        (value, who_value),
        columns=["game_id", "player_id", "lastname", "team", "saves", "shots", "save_percentage", "toi"],
    )
    players: PlayerEntries = defaultdict(list)
    for i in range(skaters["count_rows"]):
        players[int(skaters["game_id"][i])].append(
            (int(skaters["player_id"][i]), _night_skater_lines(skaters, i))
        )
    for i in range(goalies["count_rows"]):
        pct = goalies["save_percentage"][i]
        players[int(goalies["game_id"][i])].append((int(goalies["player_id"][i]), [
            f"{html.escape(goalies['lastname'][i])} ({html.escape(goalies['team'][i] or '?')})",
            f"{goalies['saves'][i] if goalies['saves'][i] is not None else '—'}/"
            f"{goalies['shots'][i] if goalies['shots'][i] is not None else '—'} · "
            f"{f'{round(pct, 2)}%' if pct is not None else '—'} · {goalies['toi'][i]}",
        ]))
    return dict(players)


def players_block(heading: str, entries: List[Tuple[int, List[str]]]) -> str:
    """Блок игроков под карточкой: заголовок `heading` (HTML), между игроками пустая строка.

    Зачем: один формат у блока страны и у блока «Ваши игроки» подписки на игрока (Задача 122).

    Аргументы:
        heading: заголовок блока, уже HTML.
        entries: (player_id, строки игрока) одного матча, как в `PlayerEntries`.
    """
    return "\n".join([heading] + [line for _, lines in entries for line in ["", *lines]])


def country_heading(code: str) -> str:
    """Заголовок блока страны: флаг и название жирным."""
    return f"<b>{country_label(code)}</b>"


def _country_blocks(code: str, scope: str, value: Union[date, int]) -> Dict[int, str]:
    """Блок страны `code` по матчам выборки `scope = value`: game_id → HTML
    (`players_block` под заголовком страны). Матча, где никто из страны не играл, нет."""
    entries = _player_entries(scope, value, "r.nationality = %s", code)
    return {game_id: players_block(country_heading(code), rows) for game_id, rows in entries.items()}


def country_night_blocks(code: str, night: date) -> List[Tuple[int, str]]:
    """Матчи ночи `night` (игровой день), где играли игроки страны `code`:
    (game_id, блок страны) по порядку `game_id`; формат блока — `_country_blocks`.
    Нужно итогам канала (`run_channel_results`, Задача 118)."""
    return sorted(_country_blocks(code, "g.day", night).items())


def country_night_entries(code: str, night: date) -> PlayerEntries:
    """Игроки страны `code`, вышедшие на лёд в матчах ночи `night`, — строками по игроку.

    Зачем: рассылка итогов (`push_digest_job.subscribed_games`) собирает блок страны чату
    без игроков, которые у него уже в «Ваших игроках» (Задача 122).

    Аргументы:
        code: код страны (`rosters.nationality`).
        night: игровая дата ночи.
    """
    return _player_entries("g.day", night, "r.nationality = %s", code)


def players_night_entries(player_ids: Sequence[int], night: date) -> PlayerEntries:
    """Подписанные игроки, вышедшие на лёд в матчах ночи `night`, — строками по игроку
    (Задача 122). Игрока в запасе, травмированного или без матча в выборке нет.

    Аргументы:
        player_ids: игроки подписок `player_scores`.
        night: игровая дата ночи.
    """
    return _player_entries("g.day", night, "s.player_id = ANY(%s)", list(player_ids))


def country_game_block(code: str, game_id: int) -> Optional[str]:
    """Блок страны `code` в матче `game_id` или `None`, если никто из неё не играл.
    Зачем: полная карточка матча подписчику страны дополняется этим блоком (Задача 76)."""
    return _country_blocks(code, "g.game_id", game_id).get(game_id)
