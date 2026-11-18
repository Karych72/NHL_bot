"""Экраны клуба: профиль, полная статистика, список команд и составы.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`): форма и серии команды
(`_current_streak`, `_last_n_form_record` — их берёт и превью матча), места в лиге, профиль
`team_profile` и полная статистика `team_full_stats` по шаблону `messages/team_profile.txt`,
список команд, клубы по дивизионам и составы для меню подписки на игрока.
"""

import html
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import config
from database import cached_fetch_all, fetch_all
from leaderboard_messages import (
    _BY_TEAM,
    _GROUP_LEGEND,
    _MMSS_SECONDS_SQL,
    _player_group_table,
    _STANDINGS_DIV_EAST,
    _STANDINGS_DIV_WEST,
    _standings_sort_key,
    PLAYER_GROUPS,
)
from rich_markup import (
    _fmt_num_max2,
    _fmt_pct,
    _format_leader_value,
    _record_str,
    _season_footer,
    _table,
    plural_word,
    rich_details,
    rich_footer,
)
from template_funcs import output_text


# Команда текущего сезона по коду: `SELECT <колонка> ` + это, параметры (сезон, код в верхнем регистре).
_TEAM_BY_ABBREV_SQL = (
    "FROM teams WHERE season_id = %s AND upper(trim(COALESCE(abbreviation, ''))) = %s LIMIT 1"
)


def _team_id_for_abbrev(abbrev_u: str) -> Optional[int]:
    row = cached_fetch_all(
        "SELECT team_id " + _TEAM_BY_ABBREV_SQL,
        (config.SEASON_ID, abbrev_u),
        columns=["team_id"],
    )
    if row["count_rows"] < 1 or row["team_id"][0] is None:
        return None
    return int(row["team_id"][0])


def team_name_for_abbrev(abbrev: str) -> str:
    """Полное название `teams.name` («Minnesota Wild») по коду команды текущего сезона.

    Зачем: экраны, где команда известна только кодом (подпись к видео гола), пишут её
    полным названием (Задача 98). Команда уже есть в `teams` (матч из БД), поэтому её
    отсутствие — ошибка данных, а не случай для показа кода.

    Аргументы:
        abbrev: код команды (`teams.abbreviation` = `abbrev` NHL API), регистр не важен;
            в SQL — только параметром.

    Raises:
        LookupError: команды или её названия нет в `teams` сезона.
    """
    code = abbrev.strip().upper()
    row = cached_fetch_all(
        "SELECT name " + _TEAM_BY_ABBREV_SQL, (config.SEASON_ID, code), columns=["name"],
    )
    if not row["count_rows"] or not row["name"][0]:
        raise LookupError(f"Команды {code} нет в teams сезона {config.SEASON_ID}")
    return str(row["name"][0])


_TEAM_RECENT_GAMES_COLUMNS = [
    "winner_id",
    "is_overtime",
    "is_shootouts",
    "ot_empty_net_win",
]

# Колонка `ot_empty_net_win` для `_team_game_outcome()` (правило NHL 84.2) по игре `g`.
_OT_EMPTY_NET_WIN_SQL = (
    "EXISTS (SELECT 1 FROM all_goals a WHERE a.game_id = g.game_id "
    "AND a.winner_goal AND a.empty_net AND a.period >= 4) AS ot_empty_net_win "
)

# В сезоне у команды не больше 82 игр — с запасом на предмет ошибок в данных.
_CURRENT_STREAK_LOOKBACK_LIMIT = 100


def _team_game_outcome(
    team_id: int,
    winner_id: Optional[int],
    is_overtime: bool,
    is_shootouts: bool,
    ot_empty_net_win: bool,
) -> str:
    """Разряд исхода одной завершённой игры (`games.winner_id IS NOT NULL`)
    для команды `team_id`: победа (`"W"`), поражение с очком (`"OTL"`) или
    поражение без очка (`"L"`).

    Общий классификатор для `_recent_team_outcomes()` (и через него — для
    `_last_n_form_record()` и `_current_streak()`) — без него правило NHL
    84.2 (см. ниже) пришлось бы поддерживать в двух копиях.

    Правило NHL 84.2: команда, снявшая вратаря в овертайме и пропустившая
    победный гол в пустые ворота, поражения "по овертайму" не получает —
    это обычное поражение, хотя `games.is_overtime = true`. Поэтому одного
    `is_overtime` недостаточно, чтобы отличить ПО от поражения: нужен
    `ot_empty_net_win` — признак «победный гол забит в пустые ворота в
    периоде >= 4», посчитанный в SQL через `EXISTS` по `all_goals`
    (буллитное поражение, `is_shootouts`, этой оговорки не касается — оно
    всегда ПО).

    Аргументы:
        team_id: команда, для которой определяется исход.
        winner_id: `games.winner_id`.
        is_overtime: `games.is_overtime`.
        is_shootouts: `games.is_shootouts`.
        ot_empty_net_win: см. правило 84.2 выше.

    Возвращает: `"W"`, `"L"` или `"OTL"`.
    """
    if winner_id == team_id:
        return "W"
    if is_shootouts:
        return "OTL"
    if is_overtime and not ot_empty_net_win:
        return "OTL"
    return "L"


def _recent_team_outcomes(team_id: int, limit: int) -> List[str]:
    """Разряды (`_team_game_outcome()`) последних `limit` завершённых игр
    регулярного сезона команды `team_id`, от новой игры к старой (плей-офф —
    `game_type = 3` — в форму и серию не входит, Задача 102).

    Единственное место, где строится запрос «последние игры команды» и
    разбирается его строка — общее для `_last_n_form_record()` (`limit=5`,
    запись формы `W-L-OTL`) и `_current_streak()`
    (`limit=_CURRENT_STREAK_LOOKBACK_LIMIT`, текущая серия). Учитывает
    правило NHL 84.2 через `_team_game_outcome()`, поэтому форма и серия не
    могут разойтись в подсчёте ПО.

    Аргументы:
        team_id: команда, для которой отбираются игры.
        limit: сколько последних игр взять (`ORDER BY day DESC NULLS LAST, game_id DESC`).

    Возвращает: список `"W"`/`"L"`/`"OTL"` длиной `min(limit, сыграно игр)`;
    пустой список, если в текущем сезоне у команды нет завершённых игр.
    """
    row = cached_fetch_all(
        "SELECT winner_id, is_overtime, is_shootouts, " + _OT_EMPTY_NET_WIN_SQL
        + "FROM games g WHERE season_id = %s AND game_type = 2 AND winner_id IS NOT NULL "
        "AND (home_team_id = %s OR away_team_id = %s) "
        "ORDER BY day DESC NULLS LAST, game_id DESC LIMIT %s",
        (config.SEASON_ID, team_id, team_id, limit),
        columns=_TEAM_RECENT_GAMES_COLUMNS,
    )
    return _row_outcomes(team_id, row)


def _row_outcomes(team_id: int, row: Dict) -> List[str]:
    """Разряды (`_team_game_outcome()`) строк выборки с колонками
    `_TEAM_RECENT_GAMES_COLUMNS` — общий разбор для формы и игр `/BOS_FULL`."""
    return [
        _team_game_outcome(
            team_id,
            row["winner_id"][i],
            bool(row["is_overtime"][i]),
            bool(row["is_shootouts"][i]),
            bool(row["ot_empty_net_win"][i]),
        )
        for i in range(row["count_rows"])
    ]


def _outcomes_record(outcomes: List[str]) -> str:
    """Запись W-L-OTL из разрядов `_team_game_outcome()`; без игр — `0-0-0`."""
    return f"{outcomes.count('W')}-{outcomes.count('L')}-{outcomes.count('OTL')}"


def _last_n_form_record(team_id: int, n: int = 5) -> str:
    """Формат W-L-OTL по последним n завершённым играм (как в таблице очков);
    без игр — `0-0-0` (команда ещё не играла в сезоне).

    Игры и их разряды берёт `_recent_team_outcomes()` (правило NHL 84.2 —
    см. `_team_game_outcome()`), чтобы форма и текущая серия
    (`_current_streak()`) не могли разойтись в подсчёте ПО.
    """
    return _outcomes_record(_recent_team_outcomes(team_id, n))


def _current_streak(team_id: int) -> str:
    """Текущая серия команды: сколько подряд последних завершённых игр
    сезона закончились одним разрядом (`_team_game_outcome()`), и каким.

    Зачем: превью ещё не сыгранного матча (`matchup_season_preview()`)
    показывает серию рядом с формой W-L-OTL. Игры и их разряды берёт тот же
    `_recent_team_outcomes()`, что и `_last_n_form_record()`, поэтому
    учитывает правило NHL 84.2 — победный гол в пустые ворота в овертайме
    (период >= 4) для проигравшей команды это поражение (L), а не
    поражение "по овертайму" (OTL), хотя `games.is_overtime = true`.

    Аргументы:
        team_id: команда, для которой считается серия.

    Возвращает: строку вида `"W3"`, `"L2"`, `"OTL1"`; `"—"`, если в текущем
    сезоне у команды нет завершённых игр.
    """
    outcomes = _recent_team_outcomes(team_id, _CURRENT_STREAK_LOOKBACK_LIMIT)
    if not outcomes:
        return "—"
    streak_outcome = outcomes[0]
    streak_len = 0
    for outcome in outcomes:
        if outcome != streak_outcome:
            break
        streak_len += 1
    return f"{streak_outcome}{streak_len}"


def _team_record_line(
    wins: Union[int, float, Decimal, None],
    losses: Union[int, float, Decimal, None],
    ot: Union[int, float, Decimal, None],
    points: Union[int, float, Decimal, None],
    procent_points: Union[int, float, Decimal, None],
) -> str:
    """Общий хвост строки команды из `teams_stats`: «W-L-OT, N очков (%очков)».

    Используется и `matchup_season_preview()` (Фаза C), и `team_profile()`
    (Фаза D) — единственная точка сборки этого формата, чтобы он не разошёлся
    между экранами.
    """
    rec = _record_str(wins, losses, ot)
    ppct = _fmt_pct(procent_points)
    word = plural_word(points, ("очко", "очка", "очков"))
    return f"{rec}, {_format_leader_value(points)} {word} ({ppct})"


def season_team_abbrevs() -> List[str]:
    """Отсортированный список аббревиатур команд текущего сезона (`config.SEASON_ID`).

    Источник перечня клубов инструмента `team_profile` свободного текста (`ai_router.py`).
    """
    stats = cached_fetch_all(
        "SELECT DISTINCT trim(COALESCE(NULLIF(trim(abbreviation), ''), short_name)) AS ab "
        "FROM teams WHERE season_id = %s ORDER BY ab",
        (config.SEASON_ID,),
        columns=["ab"],
    )
    abbrevs: List[str] = []
    for i in range(stats["count_rows"]):
        a = (stats["ab"][i] or "").strip()
        if a:
            abbrevs.append(a)
    return abbrevs


def season_divisions_text() -> str:
    """Строки «конференция · дивизион: клубы» текущего сезона (`config.SEASON_ID`) из `teams`.

    Зачем: справка ИИ о структуре лиги (`ai_router.reference_text`, Задача 114) берёт состав
    дивизионов из БД, а не из текста: перестановка клубов не требует правки справки.
    """
    stats = cached_fetch_all(
        "SELECT conference_name, division_name, "
        "string_agg(trim(COALESCE(NULLIF(trim(abbreviation), ''), short_name)), ', ' "
        "ORDER BY abbreviation) AS clubs "
        "FROM teams WHERE season_id = %s GROUP BY 1, 2 ORDER BY 1, 2",
        (config.SEASON_ID,),
        columns=["conference", "division", "clubs"],
    )
    return "\n".join(
        f"{conference} · {division}: {clubs}"
        for conference, division, clubs in zip(stats["conference"], stats["division"], stats["clubs"])
    )


def team_list_text() -> str:
    """Ответ `/team`: команды сезона по дивизионам, каждая — кликабельная
    команда `/ABBR` (её разбирает `cmd_team_profile` в `bot.py`), открывающая
    `team_profile()`.

    В таблице `teams` сезона только команды, уже сыгравшие хотя бы матч
    (загрузчик строит её из `team/summary`), — в начале сезона список неполный.
    """
    rows = cached_fetch_all(
        "SELECT trim(abbreviation), name, division_name FROM teams "
        "WHERE season_id = %s AND NULLIF(trim(abbreviation), '') IS NOT NULL "
        "ORDER BY division_name, name",
        (config.SEASON_ID,),
        columns=["abbr", "name", "division"],
    )
    if rows["count_rows"] == 0:
        return (
            "<b>Команды сезона</b>\n"
            "В базе пока нет списка команд для этого сезона.\n"
            + _season_footer()
        )
    by_division: Dict[str, List[str]] = defaultdict(list)
    for i in range(rows["count_rows"]):
        by_division[rows["division"][i] or "—"].append(
            f"/{html.escape(rows['abbr'][i])} — {html.escape(rows['name'][i] or '')}"
        )
    blocks = [
        f"<b>{html.escape(div)}</b>\n" + "\n".join(lines)
        for div, lines in by_division.items()
    ]
    return (
        "<b>Команды сезона</b>\nНажмите на команду, чтобы открыть её статистику.\n\n"
        + "\n\n".join(blocks)
        + "\n"
        + _season_footer()
    )


def season_teams_by_division() -> Dict[str, List[Tuple[int, str, str]]]:
    """Команды сезона по дивизионам для меню `/subscriptions`: {дивизион: [(team_id,
    аббревиатура, полное название)]} — код для кнопок, название для текста. Названия
    и порядок дивизионов — как у `/standings` (Восток, затем Запад); внутри — по
    аббревиатуре. Все четыре дивизиона есть всегда, у пустого сезона списки пустые."""
    rows = cached_fetch_all(
        "SELECT team_id, trim(abbreviation), name, division_name FROM teams "
        "WHERE season_id = %s AND NULLIF(trim(abbreviation), '') IS NOT NULL "
        "ORDER BY trim(abbreviation)",
        (config.SEASON_ID,),
        columns=["team_id", "abbr", "name", "division"],
    )
    result: Dict[str, List[Tuple[int, str, str]]] = {
        div: [] for div in _STANDINGS_DIV_EAST + _STANDINGS_DIV_WEST
    }
    for i in range(rows["count_rows"]):
        if rows["division"][i] in result:
            result[rows["division"][i]].append(
                (int(rows["team_id"][i]), rows["abbr"][i], rows["name"][i])
            )
    return result


# Группы состава в меню подписки на игрока (Задача 122): код группы → (подпись, позиции rosters).
ROSTER_GROUPS = {"F": ("Нападающие", ("C", "L", "R")), "D": ("Защитники", ("D",)), "G": ("Вратари", ("G",))}


def team_roster_group(team_id: int, group: str) -> List[Tuple[int, str]]:
    """Игроки клуба `team_id` текущего сезона из группы `group` (`ROSTER_GROUPS`) для меню
    подписки на игрока: [(player_id, имя)] по фамилии. Имя — `player_display_name` (с инициалом
    у однофамильцев, Задача 117). Состав — из `rosters`: вызванный из фарм-клуба после последней
    загрузки ростера появится после следующей.

    Аргументы:
        team_id: клуб (`teams.team_id`).
        group: `F`, `D` или `G`.
    """
    rows = cached_fetch_all(
        "SELECT player_id, player_display_name(name, lastname, season_id) FROM rosters "
        "WHERE season_id = %s AND current_team_id = %s AND position = ANY(%s) ORDER BY lastname, name",
        (config.SEASON_ID, team_id, list(ROSTER_GROUPS[group][1])),
        columns=["player_id", "name"],
    )
    return [(int(rows["player_id"][i]), rows["name"][i]) for i in range(rows["count_rows"])]


def player_labels(player_ids: Sequence[int]) -> Dict[int, str]:
    """Подписи игроков «Имя (КОД)» по последнему сезону в `rosters`: player_id → строка.

    Зачем: сводка `/subscriptions` и подвал рассылки (Задача 122) называют подписанных игроков,
    в том числе сменивших клуб, — код клуба (`teams` по `current_team_id`; в
    `rosters.abbreviation` загрузчик пишет позицию) отличает однофамильцев из разных команд.
    У игрока без клуба в том сезоне (свободный агент) — только имя: клуба у него нет.

    Аргументы:
        player_ids: игроки подписок чата.
    """
    rows = fetch_all(
        "SELECT DISTINCT ON (r.player_id) r.player_id, "
        "player_display_name(r.name, r.lastname, r.season_id) "
        "|| coalesce(' (' || trim(t.abbreviation) || ')', '') "
        "FROM rosters r LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = r.season_id "
        "WHERE r.player_id = ANY(%s) ORDER BY r.player_id, r.season_id DESC",
        (list(player_ids),),
        columns=["player_id", "label"],
    )
    return {int(rows["player_id"][i]): rows["label"][i] for i in range(rows["count_rows"])}


_TEAM_PROFILE_TOP = 5

# Порядок полевых игроков клуба — два фиксированных литерала модуля, не ввод
# пользователя: по очкам и по среднему времени на льду («м:сс» → секунды).
_BY_POINTS = "pss.points DESC NULLS LAST, pss.goals DESC NULLS LAST, pss.games"
_BY_TOI = (
    _MMSS_SECONDS_SQL.format(col="pss.time_on_ice_per_game") + " DESC NULLS LAST, pss.games DESC"
)
_TEAM_SKATER_COLUMNS = [
    "lastname", "position", "games", "goals", "assists", "points", "plus_minus", "toi",
]
_TEAM_STATS_LEGEND = (
    "И — игры, Г — голы, П — передачи, О — очки, ВП — среднее время на льду за игру; "
    "вратари: В-П-ОТ — победы, поражения, поражения в овертайме, %ОБ — процент "
    "отражённых бросков, КН — коэффициент надёжности."
)


def _team_footer(*links: str) -> str:
    """Подвал экранов клуба (`team_profile`, `team_full_stats`): сезон, ссылки `links`
    на соседние экраны клуба и список всех команд `/team`."""
    season_esc = html.escape(str(config.CURRENT_SEASON))
    return rich_footer(" · ".join([f"Сезон {season_esc}", *links, "все команды: /team"]))


def _team_skaters(team_id: int, order_by: str, limit: int) -> Dict:
    """Полевые игроки клуба сезона (`rosters ⋈ players_season_stats` при
    `current_team_id = team_id`) в порядке `order_by` (`_BY_POINTS`/`_BY_TOI`);
    первые `limit`. Колонки — `_TEAM_SKATER_COLUMNS`."""
    return cached_fetch_all(
        "SELECT r.lastname, r.position, pss.games, pss.goals, pss.assists, pss.points, "
        "pss.plus_minus, pss.time_on_ice_per_game "
        "FROM rosters r "
        "JOIN players_season_stats pss "
        "  ON r.player_id = pss.player_id AND r.season_id = pss.season_id AND pss.game_type = 2 "
        f"WHERE r.current_team_id = %s AND r.season_id = %s ORDER BY {order_by} LIMIT %s",
        (team_id, config.SEASON_ID, limit),
        columns=_TEAM_SKATER_COLUMNS,
    )


def _skater_label(skaters: Dict, i: int) -> str:
    """«Forsling [D]» — фамилия и позиция i-й строки `_team_skaters()`."""
    pos = (skaters["position"][i] or "—").strip()
    return f"{skaters['lastname'][i] or 'Unknown'} [{pos}]"


def _team_goalies_table(team_id: int) -> str:
    """Вратари клуба сезона с хотя бы одной игрой — таблица `_table` (игры,
    В-П-ОТ, %ОБ, КН), пустая строка, если таких нет."""
    goalies = cached_fetch_all(
        "SELECT r.lastname, gs.games, gs.wins, gs.losses, gs.ot, "
        "gs.save_percentage, gs.goal_against_average "
        "FROM rosters r "
        "JOIN goalies_season_stats gs "
        "  ON r.player_id = gs.player_id AND r.season_id = gs.season_id AND gs.game_type = 2 "
        "WHERE r.current_team_id = %s AND r.season_id = %s AND gs.games > 0 "
        "ORDER BY gs.games DESC, gs.wins DESC",
        (team_id, config.SEASON_ID),
        columns=["lastname", "games", "wins", "losses", "ot", "sv", "gaa"],
    )
    if goalies["count_rows"] == 0:
        return ""
    fmt = _format_leader_value
    rows = [["Вратарь", "И", "В-П-ОТ", "%ОБ", "КН"]] + [
        [
            goalies["lastname"][i] or "Unknown",
            fmt(goalies["games"][i]),
            _record_str(goalies["wins"][i], goalies["losses"][i], goalies["ot"][i]),
            _fmt_pct(goalies["sv"][i]),
            _fmt_num_max2(goalies["gaa"][i]),
        ]
        for i in range(goalies["count_rows"])
    ]
    return _table(rows, "lrrrr")

_TEAM_PROFILE_STATS_COLUMNS = [
    "team_id", "name", "short_name", "division_name", "conference_name", "games_played",
    "wins", "losses", "ot", "points", "procent_points", "goals_per_game",
    "goals_against_per_game", "shots_per_game", "shots_allowed",
    "power_play_percentage", "penalty_kill_percentage", "face_off_win_percentage",
    "power_play_goals", "power_play_opportunities", "power_play_goals_against",
]


def _league_standing_rows() -> List[Dict[str, Any]]:
    """Строки `teams_stats ⋈ teams` всей лиги за регулярку текущего сезона — источник мест
    команды (`_team_places`, `_league_place`) в профиле и превью матча."""
    league = cached_fetch_all(
        "SELECT ts.team_id, t.name, t.short_name, t.division_name, t.conference_name, "
        "ts.games_played, ts.wins, ts.losses, ts.ot, ts.points, ts.procent_points, "
        "ts.goals_per_game, ts.goals_against_per_game, ts.shots_per_game, "
        "ts.shots_allowed, ts.power_play_percentage, ts.penalty_kill_percentage, "
        "ts.face_off_win_percentage, ts.power_play_goals, ts.power_play_opportunities, "
        "ts.power_play_goals_against "
        "FROM teams_stats ts "
        "JOIN teams t ON t.team_id = ts.team_id AND t.season_id = ts.season_id "
        "WHERE ts.season_id = %s AND ts.game_type = 2",
        (config.SEASON_ID,),
        columns=_TEAM_PROFILE_STATS_COLUMNS,
    )
    return [
        {col: league[col][i] for col in _TEAM_PROFILE_STATS_COLUMNS}
        for i in range(league["count_rows"])
    ]


def _team_places(
    team_id: int, rows: List[Dict[str, Any]]
) -> Tuple[Dict[str, Any], int, int, int]:
    """Строка команды и её места в дивизионе, конференции и лиге — тем же
    правилом сортировки, что и турнирная таблица (`_standings_sort_key`,
    включая разбивку равенства по `short_name`)."""
    ordered = sorted(rows, key=_standings_sort_key)
    me = next(r for r in ordered if r["team_id"] == team_id)

    def place(group: List[Dict[str, Any]]) -> int:
        return [r["team_id"] for r in group].index(team_id) + 1

    div = [r for r in ordered if r["division_name"] == me["division_name"]]
    conf = [r for r in ordered if r["conference_name"] == me["conference_name"]]
    return me, place(div), place(conf), place(ordered)


def _league_place(
    rows: List[Dict[str, Any]], me: Dict[str, Any], col: str, higher_is_better: bool = True
) -> int:
    """Место команды `me` в лиге по колонке `col` среди строк `rows` всей лиги.

    Правило: 1 + число команд со строго лучшим значением, поэтому равные делят одно место,
    а не получают порядковые номера. `higher_is_better=False` — «пропущено» и «броски
    соперника»: меньше — выше.
    """
    mine = me[col]
    if higher_is_better:
        return 1 + sum(1 for r in rows if r[col] > mine)
    return 1 + sum(1 for r in rows if r[col] < mine)


def _team_season_context(a: str, ranked: bool) -> Union[str, Tuple[int, Dict[str, Any]]]:
    """Шапка и блок «Сезон» шаблона `team_profile.txt` — общие для `/BOS` и `/BOS_FULL`.

    Зачем: оба экрана начинаются одинаково (места, баланс, форма, голы, броски, спецбригады),
    и единственный источник не даёт им разойтись. Источник — `teams_stats ⋈ teams` всей лиги
    (ради мест).

    Аргументы:
        a: аббревиатура в верхнем регистре; в SQL — только параметром.
        ranked: `/BOS_FULL` — у показателей «Сезона» место в лиге (`_league_place()`), у
            большинства реализация в штуках, у меньшинства пропущенные голы.

    Возвращает: `(team_id, значения шаблона)`; неизвестная аббревиатура или команда без
    строки в `teams_stats` (ещё не сыграла, Задача 36) — готовый текст с причиной.
    """
    team_id = _team_id_for_abbrev(a)
    if team_id is None:
        return f"<b>{html.escape(a)}</b>\nКоманда не найдена в базе для этого сезона.\n{_team_footer()}"

    rows = _league_standing_rows()
    if not any(r["team_id"] == team_id for r in rows):
        return (
            f"<b>{html.escape(a)}</b>\n"
            f"Команда ещё не сыграла в этом сезоне — статистики нет.\n{_team_footer()}"
        )
    me, div_place, conf_place, league_place = _team_places(team_id, rows)
    fmt = _format_leader_value

    def place(col: str, higher_is_better: bool = True, detail: str = "") -> str:
        if not ranked:
            return ""
        return f" ({detail}{_league_place(rows, me, col, higher_is_better)}-е)"

    pp_detail = f"{fmt(me['power_play_goals'])} из {fmt(me['power_play_opportunities'])}, "
    return team_id, {
        "team_name": html.escape(me["name"] or a),
        "division": html.escape(me["division_name"] or "—"),
        "conference": html.escape(me["conference_name"] or "—"),
        "div_place": div_place,
        "conf_place": conf_place,
        "league_place": league_place,
        "league_size": len(rows),
        "games": fmt(me["games_played"]),
        "record": _team_record_line(
            me["wins"], me["losses"], me["ot"], me["points"], me["procent_points"]
        ),
        "form": html.escape(_last_n_form_record(team_id, 5)),
        "streak": html.escape(_current_streak(team_id)),
        "gf": _fmt_num_max2(me["goals_per_game"]) + place("goals_per_game"),
        "ga": _fmt_num_max2(me["goals_against_per_game"])
        + place("goals_against_per_game", higher_is_better=False),
        "sf": _fmt_num_max2(me["shots_per_game"]) + place("shots_per_game"),
        "sa": _fmt_num_max2(me["shots_allowed"])
        + place("shots_allowed", higher_is_better=False),
        "pp": _fmt_pct(me["power_play_percentage"])
        + place("power_play_percentage", detail=pp_detail),
        "pk": _fmt_pct(me["penalty_kill_percentage"]) + place(
            "penalty_kill_percentage",
            detail=f"пропущено {fmt(me['power_play_goals_against'])}, ",
        ),
        "fo": _fmt_pct(me["face_off_win_percentage"]) + place("face_off_win_percentage"),
    }


_TEAM_RECENT_GAMES_SHOWN = 5
_TEAM_UPCOMING_GAMES_SHOWN = 3
_TEAM_SEASON_GAMES_COLUMNS = [
    "day", "is_home", "opponent", "gf", "ga", "gf1", "gf2", "gf3", "ga1", "ga2", "ga3",
    *_TEAM_RECENT_GAMES_COLUMNS,
]


def _team_games_context(team_id: int) -> Dict[str, str]:
    """Игровые блоки `/BOS_FULL`: дома и в гостях, голы по периодам, последние и ближайшие
    матчи — значения `season_extra`, `recent_games`, `upcoming_games` шаблона `team_profile.txt`.

    Зачем: то, чего нет в коротком `/BOS`. Сыгранные — `games` ⋈ `game_team_stats` своих и
    соперника (счёт — `goals`, как в карточке матча: победный буллит в нём уже учтён), исход —
    `_team_game_outcome()`, как у формы; только регулярка — как форма и блок «Сезон»
    (Задача 102). Ближайшие — `scheduled_games` (день по Северной
    Америке, времени начала в БД нет). Нет игр или расписания — блок пустой и не выводится.

    Аргументы:
        team_id: клуб текущего сезона; в SQL — только параметром.
    """
    played = cached_fetch_all(
        "SELECT g.day, g.home_team_id = %s, t.abbreviation, own.goals, opp.goals, "
        "own.fst_period_goals, own.snd_period_goals, own.trd_period_goals, "
        "opp.fst_period_goals, opp.snd_period_goals, opp.trd_period_goals, "
        "g.winner_id, g.is_overtime, g.is_shootouts, " + _OT_EMPTY_NET_WIN_SQL
        + "FROM games g "
        "JOIN game_team_stats own ON own.game_id = g.game_id AND own.team_id = %s "
        "JOIN game_team_stats opp ON opp.game_id = g.game_id AND opp.team_id <> own.team_id "
        "JOIN teams t ON t.team_id = opp.team_id AND t.season_id = g.season_id "
        "WHERE g.season_id = %s AND g.game_type = 2 AND g.winner_id IS NOT NULL "
        "ORDER BY g.day DESC NULLS LAST, g.game_id DESC",
        (team_id, team_id, config.SEASON_ID),
        columns=_TEAM_SEASON_GAMES_COLUMNS,
    )
    upcoming = cached_fetch_all(
        "SELECT s.day, s.home_team_id = %s, t.abbreviation FROM scheduled_games s "
        "JOIN teams t ON t.season_id = s.season_id AND t.team_id = "
        "CASE WHEN s.home_team_id = %s THEN s.away_team_id ELSE s.home_team_id END "
        "WHERE s.season_id = %s AND %s IN (s.home_team_id, s.away_team_id) "
        "ORDER BY s.day, s.game_id LIMIT %s",
        (team_id, team_id, config.SEASON_ID, team_id, _TEAM_UPCOMING_GAMES_SHOWN),
        columns=["day", "is_home", "opponent"],
    )

    def where(is_home: bool) -> str:
        return "дома" if is_home else "в гостях"

    outcomes = _row_outcomes(team_id, played)
    games = [
        ({col: played[col][i] for col in _TEAM_SEASON_GAMES_COLUMNS}, outcomes[i])
        for i in range(played["count_rows"])
    ]
    season_extra = ""
    if games:

        def periods(side: str) -> str:
            return " · ".join(
                f"{p}-й {sum(g[f'{side}{p}'] for g, _ in games)}" for p in (1, 2, 3)
            )

        home = _outcomes_record([o for g, o in games if g["is_home"]])
        away = _outcomes_record([o for g, o in games if not g["is_home"]])
        season_extra = (
            f"Дома: {home} · в гостях: {away}\n"
            f"Голы по периодам (без овертайма): забито {periods('gf')}; "
            f"пропущено {periods('ga')}"
        )

    def recent_line(g: Dict[str, Any], outcome: str) -> str:
        extra = (" Б" if g["is_shootouts"] else " ОТ") if g["is_overtime"] else ""
        return (
            f"{g['day']:%d.%m} · {where(g['is_home'])} · {html.escape(g['opponent'])} · "
            f"{g['gf']}:{g['ga']}{extra} · {'В' if outcome == 'W' else 'П'}"
        )

    return {
        "season_extra": season_extra,
        "recent_games": "\n".join(
            recent_line(g, o) for g, o in games[:_TEAM_RECENT_GAMES_SHOWN]
        ),
        "upcoming_games": "\n".join(
            f"{upcoming['day'][i]:%d.%m} · {where(upcoming['is_home'][i])} · "
            f"{html.escape(upcoming['opponent'][i])}"
            for i in range(upcoming["count_rows"])
        ),
    }


def team_profile(abbrev: str) -> str:
    """Статистика клуба сезона `config.SEASON_ID`: место в дивизионе/конференции/
    лиге, баланс и форма, голы и броски за игру, спецбригады, лучшие бомбардиры
    и вратари.

    Зачем: ответ на `/BOS` из списка `/team` и экран «Профиль команды» меню
    `/stats` — главное о клубе одним сообщением. Шапка и «Сезон» —
    `_team_season_context()`; игроки — `rosters ⋈ players_season_stats` и
    `rosters ⋈ goalies_season_stats` по `(player_id, season_id)` при
    `current_team_id = team_id`.

    Пустые случаи (Задача 36): неизвестная аббревиатура и команда без строки
    в `teams_stats` (ещё не сыграла в сезоне) — текст с причиной, не исключение.

    Аргументы:
        abbrev: аббревиатура команды (кнопка `tp:<ABBR>` или команда `/ABBR`);
            резолвится в `team_id` через `_team_id_for_abbrev()` — в SQL
            попадает только как параметр.
    """
    a = abbrev.strip().upper()
    season = _team_season_context(a, ranked=False)
    if isinstance(season, str):
        return season
    team_id, context = season

    fmt = _format_leader_value
    scorers = _team_skaters(team_id, _BY_POINTS, _TEAM_PROFILE_TOP)
    toi_leaders = _team_skaters(team_id, _BY_TOI, _TEAM_PROFILE_TOP)
    scorer_rows = [["", "Игрок", "И", "Г", "П", "О"]] + [
        [f"{i + 1}.", _skater_label(scorers, i)]
        + [fmt(scorers[k][i]) for k in ("games", "goals", "assists", "points")]
        for i in range(scorers["count_rows"])
    ]
    toi_rows = [["", "Игрок", "И", "ВП"]] + [
        [f"{i + 1}.", _skater_label(toi_leaders, i), fmt(toi_leaders["games"][i]),
         fmt(toi_leaders["toi"][i])]
        for i in range(toi_leaders["count_rows"])
    ]

    return output_text(
        "messages/team_profile.txt",
        {
            **context,
            "scorers_table": _table(scorer_rows, "llrrrr") if scorers["count_rows"] else "",
            "toi_table": _table(toi_rows, "llrr") if toi_leaders["count_rows"] else "",
            "goalies_table": _team_goalies_table(team_id),
            "legend": rich_details("Обозначения", _TEAM_STATS_LEGEND),
            "footer": _team_footer(f"все игроки: /{html.escape(a)}_FULL"),
        },
    )


def team_full_stats(abbrev: str) -> str:
    """Ответ `/BOS_FULL` — полная версия `team_profile()` (Задача 100).

    Зачем: всё о клубе одним сообщением. Те же шапка и «Сезон», но с местами в лиге у
    показателей и спецбригадами в штуках (`_team_season_context(ranked=True)`); затем
    дом/выезд, голы по периодам, последние и ближайшие матчи (`_team_games_context()`) и все
    игроки клуба тремя таблицами — нападающие, защитники, вратари — с колонками экрана
    страны (`_player_group_table`) и +/− у полевых. Ссылка на него стоит в конце
    `team_profile()`.

    Аргументы:
        abbrev: аббревиатура команды из команды `/ABBR_FULL`; неизвестная или ещё не
            сыгравшая — текст с причиной.
    """
    a = abbrev.strip().upper()
    season = _team_season_context(a, ranked=True)
    if isinstance(season, str):
        return season
    team_id, context = season
    player_groups = []
    legends = []
    for group, (label, _cond) in PLAYER_GROUPS.items():
        table, n, _total, _first = _player_group_table(
            _BY_TEAM, team_id, group, None, 0, ranked=False
        )
        if n:
            player_groups.append((label, table))
            extra = ", +/− — плюс/минус" if group != "G" else ""
            legends.append(f"{label}: {_GROUP_LEGEND[group]}{extra}.")
    return output_text(
        "messages/team_profile.txt",
        {
            **context,
            **_team_games_context(team_id),
            "player_groups": player_groups,
            "legend": rich_details("Обозначения", "\n".join(legends)) if legends
            else "У команды пока нет статистики игроков в этом сезоне.",
            "footer": _team_footer(f"профиль клуба: /{html.escape(a)}"),
        },
    )
