"""Таблицы лидеров и турнирная таблица.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`): страницы лидербордов
игроков и команд (`/stats`, `/team_stats`) с полным размером выборки, турнирная таблица
`/standings` по шаблону `messages/league_table.txt` и таблица группы игроков
(`_player_group_table`) — её берут экраны клуба и страны.
"""

import html
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Union

from psycopg2 import sql

import config
from database import cached_fetch_all, validate_column, validate_table
from leaderboard_specs import STAT_COLUMN_LABELS
from rich_markup import (
    _fmt_num_max2,
    _fmt_pct,
    _fmt_signed,
    _format_leader_value,
    _leaderboard_page_text,
    _record_str,
    _table,
    LEADERBOARD_PAGE_SIZE,
    rich_footer,
)
from template_funcs import output_text


# Колонки времени «м:сс» (varchar, пишет `optional_seconds_to_mmss` загрузчика): строкой
# «9:57» > «25:00», поэтому сортировать их можно только по секундам (Задача 44).
_MMSS_COLUMNS = frozenset({"time_on_ice_per_game"})
# Секунды из «м:сс»; `{col}` — колонка из белого списка или литерал модуля. NULL → NULL.
_MMSS_SECONDS_SQL = "(split_part({col}, ':', 1)::int * 60 + split_part({col}, ':', 2)::int)"


def _pss_join_sql(table_name: str) -> sql.Composable:
    """JOIN players_season_stats под алиасом `pss` — нужен вторичной сортировке
    по `pss.goals` и «хвосту» строки лидерборда
    (games/shifts, Задача 18). Для players_advanced_stats и players_shot_types
    join всегда присутствует: games/shifts в хвосте нужны независимо от того,
    чем сортируется страница.
    """
    if table_name == "players_advanced_stats":
        # Порог 20 игр отсекает шум долей на малой выборке, но в начале сезона
        # не пропускает никого — тогда порог половина игр команды-лидера по
        # числу матчей (1 игра сыграна → порог 1).
        return sql.SQL(
            "INNER JOIN players_season_stats pss ON pl.player_id = pss.player_id "
            "AND pss.season_id = pl.season_id AND pss.game_type = pl.game_type "
            "AND pss.games >= LEAST(20, ("
            "SELECT CEIL(MAX(ts.games_played) / 2.0) FROM teams_stats ts "
            "WHERE ts.season_id = pl.season_id AND ts.game_type = pl.game_type)) "
        )
    if table_name == "players_shot_types":
        return sql.SQL(
            "LEFT JOIN players_season_stats pss ON pl.player_id = pss.player_id "
            "AND pss.season_id = pl.season_id AND pss.game_type = pl.game_type "
        )
    return sql.SQL("")


def _leaderboard_tail_columns(table_name: str) -> Tuple[sql.Composable, List[str]]:
    """SQL-колонки и ключи для постоянного «хвоста» строки лидерборда (Задача
    18, «включить спящие данные»): вратарям (goalies_season_stats) — игры,
    сейвы, броски против (всё уже в самой таблице); полевым
    игрокам (players_season_stats и, через JOIN на `pss`, players_advanced_stats
    / players_shot_types) — игры и смены. Любое из значений может быть `NULL`
    («заполнены не везде») — рендер обязан пережить это через
    `_format_leader_value`, а не считать колонку гарантированно заполненной.

    Возвращает: (SQL-фрагмент SELECT-колонок через запятую, список ключей
    результата в том же порядке — передаётся в `columns=` у `fetch_all`).
    """
    cols: Tuple[str, ...]
    if table_name == "goalies_season_stats":
        cols = ("games", "saves", "shots_against")
        keys = ["tail_games", "tail_saves", "tail_shots_against"]
        alias = "pl"
    else:
        cols = ("games", "shifts")
        keys = ["tail_games", "tail_shifts"]
        alias = "pl" if table_name == "players_season_stats" else "pss"
    select_sql = sql.SQL(", ").join(
        sql.SQL(".").join([sql.Identifier(alias), sql.Identifier(c)]) for c in cols
    )
    return select_sql, keys


def player_stats_with_count(
    table_name: str,
    column_name: str,
    count: int = 10,
    offset: int = 0,
) -> Tuple[str, int, int]:
    """Страница лидерборда игроков/вратарей (HTML) плюс размер полной выборки.

    Зачем: страница отдаёт `LIMIT`/`OFFSET`-подмножество строк, но для маркера
    «показаны N из M» (Задача 11) вызывающему коду нужен и полный размер
    выборки — берём его окном `COUNT(*) OVER ()` в том же запросе, без
    отдельного похода в БД.

    Аргументы:
        table_name: таблица статистики, из белого списка `validate_table`.
        column_name: колонка сортировки, из белого списка `validate_column`.
        count: сколько строк вернуть (размер страницы).
        offset: сдвиг страницы.

    При равенстве `column_name` строки упорядочены по голам (у вратарей — по % отражённых):
    `pss.goals` для players_advanced_stats/players_shot_types, иначе колонка самой таблицы.

    Строки — таблица rich-сообщения (`_table`) с постоянным
    «хвостом» уже загруженных полей (Задача 18): для вратарей — игры и
    сейвы/броски против, для остальных игровых таблиц — игры/смены (через JOIN
    на players_season_stats для players_advanced_stats/players_shot_types).
    Сортировка от этого не меняется — хвост не влияет на ORDER BY.

    Возвращает: (текст, число строк на странице, полный размер выборки).
    """
    validate_table(table_name)
    validate_column(column_name)
    if offset < 0:
        offset = 0
    join_pss = _pss_join_sql(table_name)
    if table_name == "goalies_season_stats":
        second_alias, second_order = "pl", "save_percentage"
    elif table_name in ("players_advanced_stats", "players_shot_types"):
        second_alias, second_order = "pss", "goals"
    else:
        second_alias, second_order = "pl", "goals"
    second_col = sql.SQL(".").join([sql.Identifier(second_alias), sql.Identifier(second_order)])
    pl_col = sql.SQL(".").join([sql.Identifier("pl"), sql.Identifier(column_name)])
    order_col = (
        sql.SQL(_MMSS_SECONDS_SQL).format(col=pl_col) if column_name in _MMSS_COLUMNS else pl_col
    )
    tail_select, tail_keys = _leaderboard_tail_columns(table_name)

    q = sql.SQL(
        "SELECT r.lastname, r.position AS roster_position, {pl_col}, t.abbreviation AS team, "
        "{tail_select}, "
        "COUNT(*) OVER () AS total "
        "FROM {table} pl "
        "{join_pss}"
        "LEFT JOIN rosters r ON pl.player_id = r.player_id AND r.season_id = pl.season_id "
        "LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = pl.season_id "
        "WHERE pl.season_id = %s AND pl.game_type = 2 "
        # NULLS LAST keeps unranked players (no value reported) at the bottom of
        # leaderboards instead of at the top under DESC's default NULLS FIRST.
        "ORDER BY {order_col} DESC NULLS LAST, {second_col} DESC NULLS LAST "
        "LIMIT %s OFFSET %s"
    ).format(
        pl_col=pl_col,
        order_col=order_col,
        table=sql.Identifier(table_name),
        join_pss=join_pss,
        second_col=second_col,
        tail_select=tail_select,
    )
    stats = cached_fetch_all(
        q, (config.SEASON_ID, count, offset),
        ['lastname', 'roster_position', 'points', 'team', *tail_keys, 'total'],
    )

    # COUNT(*) OVER () не возвращает строк вовсе для пустой выборки — total
    # в этом случае 0, а не отсутствующее значение.
    total = int(stats['total'][0]) if stats['count_rows'] else 0

    is_goalie = table_name == "goalies_season_stats"
    # Вратарская таблица — позиция [G] у всех, колонка лишняя; время на льду
    # тоже: игр достаточно (отзыв по UI, Задача 47).
    rows: List[List[str]] = [
        ["", "Вратарь" if is_goalie else "Игрок", "Ком", STAT_COLUMN_LABELS[column_name], "И",
         "Сейвы" if is_goalie else "Смен"],
    ]
    for i in range(stats['count_rows']):
        name = stats['lastname'][i] or "Unknown"
        pos = str(stats['roster_position'][i] or "").strip()
        if pos and not is_goalie:
            name += f" [{pos}]"
        games = _format_leader_value(stats['tail_games'][i])
        if is_goalie:
            extra = (
                f"{_format_leader_value(stats['tail_saves'][i])}/"
                f"{_format_leader_value(stats['tail_shots_against'][i])}"
            )
        else:
            extra = _format_leader_value(stats['tail_shifts'][i])
        rows.append([
            f"{offset + i + 1}.", name, stats['team'][i] or "—",
            _format_leader_value(stats['points'][i]), games, extra,
        ])

    body = _table(rows, "rllrrr") if stats['count_rows'] else ""
    return body, stats["count_rows"], total


def player_stat_leaderboard_page(
    heading: str,
    table_name: str,
    column_name: str,
    offset: int,
) -> Tuple[str, bool, bool]:
    """Произвольная таблица лидеров игроков/вратарей: шапка + пустая строка + список."""
    if offset < 0:
        offset = 0
    body, n, total = player_stats_with_count(
        table_name,
        column_name,
        count=LEADERBOARD_PAGE_SIZE,
        offset=offset,
    )
    return _leaderboard_page_text(html.escape(heading), body, n, total, offset)


def _standings_as_of_day() -> str:
    """Дата «по состоянию на» у `/standings`: последний день регулярки сезона — таблица
    по плей-офф не меняется, и его дни дату не сдвигают (Задача 102)."""
    row = cached_fetch_all(
        "SELECT max(day)::text AS d FROM games WHERE season_id = %s AND game_type = 2",
        (config.SEASON_ID,),
        columns=["d"],
    )
    d = row["d"][0] if row["count_rows"] else None
    return str(d) if d else "—"


_STANDINGS_DIV_EAST: Tuple[str, ...] = ("Atlantic", "Metropolitan")
_STANDINGS_DIV_WEST: Tuple[str, ...] = ("Central", "Pacific")
_STANDINGS_DIV_LABEL = {
    "Atlantic": "ATLANTIC DIVISION",
    "Metropolitan": "METROPOLITAN DIVISION",
    "Central": "CENTRAL DIVISION",
    "Pacific": "PACIFIC DIVISION",
}


def _standings_sort_key(team: Dict[str, Union[int, str, float, None]]) -> Tuple:
    pts = int(team.get("points") or 0)
    wins = int(team.get("wins") or 0)
    losses = int(team.get("losses") or 0)
    name = str(team.get("name") or "")
    return (-pts, -wins, losses, name)


def _fmt_standings_pct(val: Union[int, float, Decimal, str, None]) -> str:
    if val is None:
        return "---"
    try:
        fv = float(val)
    except (TypeError, ValueError):
        return "---"
    return f"{fv:.1f}"


# Шапка и выравнивание таблиц дивизионов и Wild Card: место, команда, очки, игры, % очков.
_STANDINGS_HEADER = ["", "Команда", "Очк", "Игр", "%очк"]
_STANDINGS_ALIGN = "rlrrr"


def _standings_row(label: str, team: Dict[str, Union[int, str, float, None]]) -> List[str]:
    """Строка таблицы `/standings`: метка места (« 1.», «WC1») и показатели команды."""
    return [
        label,
        str(team.get("name") or "—").strip(),
        str(int(team.get("points") or 0)),
        str(int(team.get("games_played") or 0)),
        _fmt_standings_pct(team.get("procent_points")),
    ]


def _wild_card_block(
    by_division: Dict[str, List[Dict[str, Union[int, str, float, None]]]],
    division_order: Tuple[str, ...],
) -> str:
    """Гонка за Wild Card конференции: команды вне топ-3 своих дивизионов по очкам —
    две путёвки и ближайшие претенденты под линией плей-офф; без таких команд —
    строка-причина."""
    top3_names: set = set()
    conf_teams: List[Dict[str, Union[int, str, float, None]]] = []
    for div in division_order:
        teams = sorted(by_division.get(div) or [], key=_standings_sort_key)
        conf_teams.extend(teams)
        for t in teams[:3]:
            top3_names.add(str(t.get("name") or "").strip())
    remaining = [
        t for t in conf_teams if str(t.get("name") or "").strip() not in top3_names
    ]
    remaining.sort(key=_standings_sort_key)
    if not remaining:
        if len(conf_teams) < 8:
            return html.escape("(в выборке < 8 команд конференции — гонка WC не показана)")
        return "(нет команд вне топ-3 дивизионов)"
    # Гонка за Wild Card — две путёвки и ближайшие претенденты, не вся конференция.
    rows = [_STANDINGS_HEADER] + [
        _standings_row(f"WC{i + 1}" if i < 2 else f"{i + 1}.", t)
        for i, t in enumerate(remaining[:_WILD_CARD_SHOWN])
    ]
    return _table(rows, _STANDINGS_ALIGN, playoff_line_after=2)


# Строк в блоке Wild Card: WC1, WC2 и три ближайших претендента под чертой.
_WILD_CARD_SHOWN = 5


def _build_standings_table_body(teams: List[Dict[str, Union[int, str, float, None]]]) -> str:
    """Тело `/standings`: по конференциям — дивизионы (топ-3 над линией плей-офф) и гонка
    Wild Card; каждая секция — заголовок и таблица `_table` строкой ниже."""
    by_division: Dict[str, List[Dict[str, Union[int, str, float, None]]]] = defaultdict(
        list
    )
    for t in teams:
        div = t.get("division_name")
        if div:
            by_division[str(div)].append(t)

    chunks: List[str] = []

    # Секция — заголовок вплотную к своему блоку; между секциями пустая строка.
    def append_conference(title: str, div_order: Tuple[str, ...], wc_title: str) -> None:
        chunks.append(f"<b>{html.escape(title)}</b>")
        for div in div_order:
            label = _STANDINGS_DIV_LABEL.get(div, div.upper())
            div_teams = sorted(by_division.get(div) or [], key=_standings_sort_key)
            if div_teams:
                # Топ-3 дивизиона проходят в плей-офф напрямую.
                block = _table(
                    [_STANDINGS_HEADER] + [_standings_row(f"{rank}.", t) for rank, t in enumerate(div_teams, 1)],
                    _STANDINGS_ALIGN,
                    playoff_line_after=3,
                )
            else:
                # Дивизион без единой сыгранной игры (частичный сезон, Задача 36) —
                # текст-причина вместо таблицы из одной шапки.
                block = "(в дивизионе ещё никто не сыграл)"
            chunks.append(f"<b>{html.escape(label)}</b>\n{block}")
        chunks.append(f"<b>{html.escape(wc_title)}</b>\n{_wild_card_block(by_division, div_order)}")

    append_conference(
        "EASTERN CONFERENCE",
        _STANDINGS_DIV_EAST,
        "WILD CARD — EASTERN (вне топ-3 дивизиона, по очкам)",
    )
    append_conference(
        "WESTERN CONFERENCE",
        _STANDINGS_DIV_WEST,
        "WILD CARD — WESTERN (вне топ-3 дивизиона, по очкам)",
    )

    return "\n\n".join(chunks)


def team_table(channel_footer: Optional[str] = None) -> str:
    """Турнирная таблица всех команд текущего сезона (`config.SEASON_ID`):
    секции по конференциям/дивизионам, отсортированные по очкам, плюс секции
    Wild Card — HTML-текст с шапкой сезона и даты (шаблон
    `messages/league_table.txt`) для команды `/standings` (`bot.py`).

    Пустой `teams_stats` (сезон ещё не начался — загрузчик не пишет туда
    строк, пока в сезоне нет ни одной завершённой игры, см. Задача 36)
    отдаёт текст с причиной вместо таблицы с одними заголовками секций и
    без единой строки команды.

    Аргументы: `channel_footer` — строка подвала поста в канал (ссылка на бота, Задача 120),
    дописывается под сезоном; `None` — экран бота."""
    season = f"Сезон {html.escape(str(config.CURRENT_SEASON))}"
    channel_line = "" if channel_footer is None else f"\n{channel_footer}"
    stats = cached_fetch_all(
        "SELECT t.name, games_played, points, procent_points, wins, "
        "       losses, ot, t.division_name, t.conference_name "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s AND ts.game_type = 2 "
        "ORDER BY conference_name, division_name, points DESC",
        (config.SEASON_ID,),
        columns=['name', 'games_played', 'points', 'procent_points',
                 'wins', 'losses', 'ot', 'division_name', 'conference_name'],
    )

    if stats['count_rows'] == 0:
        return (
            "<b>Турнирная таблица NHL</b>\n"
            "Сезон ещё не начался: в базе нет статистики команд.\n"
            + rich_footer(season + channel_line)
        )

    teams: List[Dict[str, Union[int, str, float, None]]] = []
    for i in range(stats['count_rows']):
        teams.append({
            'name': (stats['name'][i] or '—').strip(),
            'games_played': stats['games_played'][i],
            'points': stats['points'][i],
            'procent_points': stats['procent_points'][i],
            'wins': stats['wins'][i],
            'losses': stats['losses'][i],
            'division_name': stats['division_name'][i],
            'conference_name': stats['conference_name'][i],
        })

    table_body = _build_standings_table_body(teams)
    to_template = {
        'table_body': table_body,
        'footer': rich_footer(
            f"{season} · данные на {html.escape(str(_standings_as_of_day()))}{channel_line}"
        ),
    }
    return output_text('messages/league_table.txt', to_template)
# Условие на `p` (регулярка, Задача 103) стоит здесь, а не в WHERE: так его получают все три
# запроса стран — рейтинг, счётчик без страны и `_GROUP_SKATERS_SQL`.
_COUNTRY_FROM = (
    "FROM players_season_stats p "
    "JOIN rosters r ON p.player_id = r.player_id AND p.season_id = r.season_id "
    "AND p.game_type = 2 "
)


# Группы игроков экрана страны и `/BOS_FULL`: код (в callback_data страны) →
# (подпись, условие на `r.position`).
PLAYER_GROUPS: Dict[str, Tuple[str, str]] = {
    "F": ("Нападающие", "r.position IN ('C', 'L', 'R')"),
    "D": ("Защитники", "r.position = 'D'"),
    "G": ("Вратари", "r.position = 'G'"),
}
# Чьи игроки в таблице группы — фиксированные литералы модуля для `{owner}`,
# значение идёт параметром запроса.
_BY_COUNTRY = "r.nationality = %s"
_BY_TEAM = "r.current_team_id = %s"
_GROUP_SKATERS_SQL = (
    "SELECT r.lastname, t.abbreviation, p.goals, p.points, p.games, p.time_on_ice_per_game, "
    "p.hits, p.shots, p.blocked, COUNT(*) OVER () "
    + _COUNTRY_FROM
    + "LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = r.season_id "
    "WHERE p.season_id = %s AND {owner} AND {group} "
    "ORDER BY p.points DESC NULLS LAST, p.goals DESC NULLS LAST, r.lastname, r.player_id "
    "LIMIT %s OFFSET %s"
)
_GROUP_GOALIES_SQL = (
    "SELECT r.lastname, t.abbreviation, g.wins, g.save_percentage, "
    "g.goal_against_average, g.games, g.shutouts, COUNT(*) OVER () "
    "FROM goalies_season_stats g "
    "JOIN rosters r ON g.player_id = r.player_id AND g.season_id = r.season_id "
    "LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = r.season_id "
    "WHERE g.season_id = %s AND g.game_type = 2 AND {owner} AND g.games > 0 "
    "ORDER BY g.wins DESC NULLS LAST, r.lastname, r.player_id "
    "LIMIT %s OFFSET %s"
)
_GROUP_LEGEND = {
    "F": "Г — голы, О — очки, И — игры, ВП — время на льду за игру, Хит — силовые, "
         "Бр — броски",
    "D": "Г — голы, О — очки, И — игры, ВП — время на льду за игру, Хит — силовые, "
         "Бл — блоки",
    "G": "В — победы, %ОБ — отражённые броски, КН — коэффициент надёжности, И — игры, "
         "Сух — сухие матчи",
}


def _player_group_table(
    owner: str,
    owner_value: Union[int, str],
    group: str,
    limit: Optional[int],
    offset: int,
    *,
    ranked: bool,
) -> Tuple[str, int, int, Optional[Tuple[str, str, int]]]:
    """Таблица игроков одной группы (`_table`) — общая для экрана страны,
    `/RUS` и `/BOS_FULL`.

    Зачем: у каждой группы свои главные показатели — нападающие: голы, очки,
    игры, время, силовые, броски; защитники — то же с блоками вместо бросков;
    вратари (с хотя бы одной игрой): победы, %ОБ, КН, игры, сухие.

    Аргументы:
        owner: `_BY_COUNTRY` или `_BY_TEAM` — чьи игроки.
        owner_value: код страны или `team_id`; в SQL — только параметром.
        group: ключ `PLAYER_GROUPS` (F / D / G).
        limit, offset: страница выборки; `limit=None` — все строки.
        ranked: колонки места и команды (экран страны); без них — `/BOS_FULL`.

    Возвращает: (таблица — пусто при 0 строк, строк в таблице, полный размер выборки,
    первая строка — (фамилия, команда, очки полевого / победы вратаря), None при 0 строк).
    """
    params = (config.SEASON_ID, owner_value, limit, offset)
    fmt = _format_leader_value
    if group == "G":
        cols = ["lastname", "team", "wins", "sv", "gaa", "games", "shutouts", "total"]
        stats = cached_fetch_all(_GROUP_GOALIES_SQL.format(owner=owner), params, columns=cols)
        name_header, header = "Вратарь", ["В", "%ОБ", "КН", "И", "Сух"]
        cells = [
            [fmt(stats["wins"][i]), _fmt_pct(stats["sv"][i]),
             _fmt_num_max2(stats["gaa"][i]), fmt(stats["games"][i]), fmt(stats["shutouts"][i])]
            for i in range(stats["count_rows"])
        ]
    else:
        cols = ["lastname", "team", "goals", "points", "games", "toi", "hits", "shots",
                "blocked", "total"]
        sql_text = _GROUP_SKATERS_SQL.format(owner=owner, group=PLAYER_GROUPS[group][1])
        stats = cached_fetch_all(sql_text, params, columns=cols)
        extra_key, extra_label = ("shots", "Бр") if group == "F" else ("blocked", "Бл")
        name_header, header = "Игрок", ["Г", "О", "И", "ВП", "Хит", extra_label]
        cells = [
            [fmt(stats[k][i]) for k in ("goals", "points", "games", "toi", "hits", extra_key)]
            for i in range(stats["count_rows"])
        ]
    n = stats["count_rows"]
    if not n:
        return "", 0, 0, None

    def prefix(i: int) -> List[str]:
        name = stats["lastname"][i] or "Unknown"
        return [f"{offset + i + 1}.", name, stats["team"][i] or "—"] if ranked else [name]

    lead = ["", name_header, "Ком"] if ranked else [name_header]
    rows = [lead + header] + [prefix(i) + cells[i] for i in range(n)]
    align = ("rll" if ranked else "l") + "r" * len(header)
    table = _table(rows, align)
    first = (
        stats["lastname"][0] or "Unknown",
        stats["team"][0] or "—",
        int(stats["wins" if group == "G" else "points"][0] or 0),
    )
    return table, n, int(stats["total"][0]), first


def team_stats_with_count(
    columns: Sequence[str],
    count: int = 10,
    offset: int = 0,
) -> Tuple[str, int, int]:
    """Страница лидерборда команд (HTML) плюс размер полной выборки.

    Зачем: как `player_stats_with_count` — маркер «показаны N из M» (Задача 11)
    нуждается в полном размере выборки, который берём окном `COUNT(*) OVER ()`
    в этом же запросе, а не отдельным `SELECT count(*)`.

    Аргументы:
        columns: колонки `teams_stats` из белого списка `validate_column` — одна
            (рейтинг по ней) или две, «за» и «против» в среднем за игру (рейтинг
            по разнице, `TEAM_STAT_SPECS` в `leaderboard_specs.py`).
        count, offset: размер и сдвиг страницы.

    Строки — таблица rich-сообщения (`_table`), команда — аббревиатурой: полное имя
    не помещается в строку на экране телефона. У одной колонки — постоянный «хвост»
    уже загруженных полей (Задача 18): игры, баланс `wins`-`losses`-`ot` и `points`;
    у пары — «за», «против», разница и игры: шире строка на телефоне не помещается.

    Возвращает: (текст, число строк на странице, полный размер выборки).
    """
    for column in columns:
        validate_column(column)
    offset = max(offset, 0)
    ids = [sql.Identifier(column) for column in columns]
    sort = ids[0] if len(ids) == 1 else sql.SQL("({} - {})").format(*ids)
    values = [f"v{i}" for i in range(len(ids))]

    q = sql.SQL(
        "SELECT t.abbreviation, {cols}, {sort}, games_played, wins, losses, ot, points, "
        "COUNT(*) OVER () AS total "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s AND ts.game_type = 2 "
        "ORDER BY {sort} DESC NULLS LAST, t.abbreviation "
        "LIMIT %s OFFSET %s"
    ).format(cols=sql.SQL(", ").join(ids), sort=sort)
    stats = cached_fetch_all(
        q,
        (config.SEASON_ID, count, offset),
        columns=[
            'team', *values, 'sort', 'games_played',
            'wins', 'losses', 'ot', 'record_points', 'total',
        ],
    )

    total = int(stats['total'][0]) if stats['count_rows'] else 0

    if len(columns) == 1:
        rows: List[List[str]] = [["", "Ком", STAT_COLUMN_LABELS[columns[0]], "И", "В-П-ОТ", "О"]]
        for i in range(stats['count_rows']):
            rows.append([
                f"{offset + i + 1}.",
                (stats['team'][i] or "—").strip(),
                _format_leader_value(stats['v0'][i]),
                _format_leader_value(stats['games_played'][i]),
                _record_str(stats['wins'][i], stats['losses'][i], stats['ot'][i]),
                _format_leader_value(stats['record_points'][i]),
            ])
    else:
        rows = [["", "Ком", "За", "Пр", "Разн", "И"]]
        for i in range(stats['count_rows']):
            rows.append([
                f"{offset + i + 1}.",
                (stats['team'][i] or "—").strip(),
                _fmt_num_max2(stats['v0'][i]),
                _fmt_num_max2(stats['v1'][i]),
                _fmt_signed(stats['sort'][i]),
                _format_leader_value(stats['games_played'][i]),
            ])

    body = _table(rows, "rlrrrr") if stats['count_rows'] else ""
    return body, stats["count_rows"], total


def team_stat_leaderboard_page(
    heading: str,
    columns: Sequence[str],
    offset: int,
) -> Tuple[str, bool, bool]:
    """Страница рейтинга команд `/team_stats`: заголовок `heading`, колонки `columns`
    (см. `team_stats_with_count`), сдвиг `offset`; возвращает (текст, has_prev, has_next)."""
    body, n, total = team_stats_with_count(
        columns,
        count=LEADERBOARD_PAGE_SIZE,
        offset=offset,
    )
    return _leaderboard_page_text(html.escape(heading), body, n, total, offset)
