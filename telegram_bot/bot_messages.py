from collections import defaultdict
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

# Telegram message text limit (UTF-16 length can differ; stay under safe byte-ish budget)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

from psycopg2 import sql

import config
from database import fetch_all, validate_table, validate_column
from template_funcs import output_text


# Subsets of columns allowed in dynamic SQL; keep in sync with ALLOWED_COLUMNS
# in database.py for these tables (plus player_id for alias resolution).
ADVANCED_STATS_COLUMNS = frozenset({
    "sat_pct", "usat_pct", "goals_pct", "oz_start_pct", "dz_start_pct",
    "nz_start_pct", "on_ice_shooting_pct", "ev_goals_for", "ev_goals_against",
    "ev_goals_for_pct", "pp_goals_for", "pp_goals_against", "sh_goals_for",
    "sh_goals_against", "player_id",
})

SHOT_TYPES_COLUMNS = frozenset({
    "player_id",
    "goals_wrist", "shots_wrist", "goals_slap", "shots_slap", "goals_snap",
    "shots_snap", "goals_backhand", "shots_backhand", "goals_tip_in",
    "shots_tip_in", "goals_deflected", "shots_deflected", "goals_wrap_around",
    "shots_wrap_around",
})


def _format_leader_value(value: Union[int, float, Decimal, str, None]) -> str:
    """Plain-text display for leaderboard cells (no monospace padding)."""
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        fv = float(value)
        if abs(fv - round(fv)) < 1e-9:
            return str(int(round(fv)))
        s = f"{fv:.3f}".rstrip("0").rstrip(".")
        return s
    return str(value)


def _resolve_secondary_sort(table_name: str, secondary_sort: Optional[str]) -> str:
    if secondary_sort is not None:
        return secondary_sort
    if table_name == "players_season_stats":
        return "goals"
    if table_name == "goalies_season_stats":
        return "save_percentage"
    if table_name in ("players_advanced_stats", "players_shot_types"):
        return "goals"
    return "goals"


def _second_order_table_alias(table_name: str, second_order: str) -> str:
    if table_name == "players_advanced_stats":
        return "pl" if second_order in ADVANCED_STATS_COLUMNS else "pss"
    if table_name == "players_shot_types":
        return "pl" if second_order in SHOT_TYPES_COLUMNS else "pss"
    return "pl"


def _pss_join_sql(table_name: str, second_order: str) -> sql.Composable:
    if table_name == "players_advanced_stats":
        return sql.SQL(
            "INNER JOIN players_season_stats pss ON pl.player_id = pss.player_id "
            "AND pss.season_id = pl.season_id AND pss.games >= 20 "
        )
    if (
        table_name == "players_shot_types"
        and _second_order_table_alias(table_name, second_order) == "pss"
    ):
        return sql.SQL(
            "LEFT JOIN players_season_stats pss ON pl.player_id = pss.player_id "
            "AND pss.season_id = pl.season_id "
        )
    return sql.SQL("")


def game_message(game_id: int) -> Tuple[str, List[Dict]]:
    """Return (rendered_text, goal_video_metadata)."""
    game_stats = fetch_all(
        "SELECT * FROM get_game_stats(%s)", (game_id,),
        ['goals', 'pim', 'blocks', 'hits', 'shots', 'is_overtime',
         'is_shootouts', 'field', 'team_name'],
    )
    game_goals = fetch_all(
        "SELECT * FROM get_goals_game(%s)", (game_id,),
        ['scorer', 'assist_1', 'assist_2', 'period', 'time',
         'home_score', 'away_score', 'game_id', 'event_id'],
    )
    game_goalies = fetch_all(
        "SELECT * FROM get_goalies_game(%s)", (game_id,),
        ['shots', 'saves', 'timeonice', 'lastname',
         'save_percentage', 'is_home'],
    )

    is_overtime = game_stats['is_overtime'][0]
    is_shootouts = game_stats['is_shootouts'][0]
    if not is_overtime:
        extra = ''
    elif not is_shootouts:
        extra = '(OT)'
    else:
        extra = '(Б)'

    period_home: dict[int, int] = defaultdict(int)
    period_away: dict[int, int] = defaultdict(int)
    prev_h, prev_a = 0, 0

    goals = []
    goals_meta = []
    for i in range(game_goals['count_rows']):
        h = game_goals['home_score'][i]
        a = game_goals['away_score'][i]
        p = game_goals['period'][i]
        period_home[p] += h - prev_h
        period_away[p] += a - prev_a
        prev_h, prev_a = h, a

        scorer = game_goals['scorer'][i] or 'Unknown'
        t_m = str((p - 1) * 20 + int(game_goals['time'][i].split(':')[0]))
        t_all = t_m + ':' + game_goals['time'][i].split(':')[1]
        assists = ''
        if game_goals['assist_2'][i] is not None:
            assists = f"({game_goals['assist_1'][i]}, {game_goals['assist_2'][i]})"
        elif game_goals['assist_1'][i] is not None:
            assists = f"({game_goals['assist_1'][i]})"

        score = f"{h}:{a}"
        goals.append({
            'home_score': h,
            'away_score': a,
            'scorer': scorer + assists,
            'time': t_all,
        })

        evt = game_goals['event_id'][i]
        if evt is not None:
            goals_meta.append({
                'game_id': game_goals['game_id'][i],
                'event_id': evt,
                'label': f"{score} {scorer}{assists} {t_all}",
            })

    num_periods = 3
    if is_overtime and not is_shootouts:
        num_periods = 4
    parts = [f"{period_home[p]}:{period_away[p]}" for p in range(1, num_periods + 1)]
    period_scores = f"({', '.join(parts)})"

    goalkeepers = ''
    change_team = False
    for i in range(game_goalies['count_rows']):
        if game_goalies['timeonice'][i] == '00:00':
            continue
        if not game_goalies['is_home'][i] and not change_team:
            change_team = True
            goalkeepers += ' - '
        goalkeepers += (
            f"{game_goalies['lastname'][i]} "
            f"({game_goalies['saves'][i]}/{game_goalies['shots'][i]}, "
            f"{round(game_goalies['save_percentage'][i], 2)}%, "
            f"{game_goalies['timeonice'][i]})"
        )

    to_template = {
        'team_home': game_stats['team_name'][0],
        'team_away': game_stats['team_name'][1],
        'home_score': game_stats['goals'][0],
        'away_score': game_stats['goals'][1],
        'home_shots': game_stats['shots'][0],
        'away_shots': game_stats['shots'][1],
        'home_penalties': game_stats['pim'][0],
        'away_penalties': game_stats['pim'][1],
        'goals': goals,
        'goalkeepers': goalkeepers,
        'extra': extra,
        'period_scores': period_scores,
    }
    return output_text('messages/game_message.txt', to_template), goals_meta


def game_exists(game_id: int) -> bool:
    row = fetch_all(
        "SELECT 1 AS o FROM games WHERE game_id = %s LIMIT 1",
        (game_id,),
        columns=["o"],
    )
    return row["count_rows"] > 0


def player_stats(
    name_stats: str,
    table_name: str,
    column_name: str,
    count: int = 10,
    *,
    secondary_sort: Optional[str] = None,
) -> str:
    validate_table(table_name)
    validate_column(column_name)
    second_order = _resolve_secondary_sort(table_name, secondary_sort)
    validate_column(second_order)

    join_pss = _pss_join_sql(table_name, second_order)
    second_alias = _second_order_table_alias(table_name, second_order)
    pl_col = sql.SQL(".").join([sql.Identifier("pl"), sql.Identifier(column_name)])
    second_col = sql.SQL(".").join(
        [sql.Identifier(second_alias), sql.Identifier(second_order)]
    )

    q = sql.SQL(
        "SELECT r.lastname, {pl_col}, t.abbreviation AS team "
        "FROM {table} pl "
        "{join_pss}"
        "LEFT JOIN rosters r ON pl.player_id = r.player_id AND r.season_id = pl.season_id "
        "LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = pl.season_id "
        "WHERE pl.season_id = %s "
        "ORDER BY {pl_col} DESC, {second_col} DESC "
        "LIMIT %s"
    ).format(
        pl_col=pl_col,
        table=sql.Identifier(table_name),
        join_pss=join_pss,
        second_col=second_col,
    )
    stats = fetch_all(q, (config.SEASON_ID, count), ['lastname', 'points', 'team'])

    to_template = {'name_stats': name_stats}
    players = []
    for i in range(stats['count_rows']):
        lastname = stats['lastname'][i] or "Unknown"
        players.append({
            'rank': i + 1,
            'lastname': lastname,
            'value': _format_leader_value(stats['points'][i]),
            'team': stats['team'][i] or "—",
        })

    to_template['players'] = players
    return output_text('messages/season_leaders_players.txt', to_template)


def _standings_as_of_day() -> str:
    row = fetch_all(
        "SELECT max(day)::text AS d FROM games WHERE season_id = %s",
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
    name = str(team.get("short_name") or "")
    return (-pts, -wins, losses, name)


def _fmt_standings_pct(val: Union[int, float, Decimal, str, None]) -> str:
    if val is None:
        return "  ---"
    try:
        fv = float(val)
    except (TypeError, ValueError):
        return "  ---"
    return f"{fv:6.2f}"


def _standings_name_width(teams: Sequence[Dict[str, Union[int, str, float, None]]]) -> int:
    longest = 0
    for t in teams:
        nm = str(t.get("short_name") or "").strip()
        longest = max(longest, len(nm))
    return max(14, min(longest + 1, 22))


def _fmt_standings_row(
    team: Dict[str, Union[int, str, float, None]], name_w: int
) -> str:
    nm = str(team.get("short_name") or "—").strip()
    if len(nm) > name_w:
        nm = nm[: max(1, name_w - 1)] + "…"
    pts = int(team.get("points") or 0)
    gp = int(team.get("games_played") or 0)
    pct = _fmt_standings_pct(team.get("procent_points"))
    return f"{nm:<{name_w}} {pts:>3} {gp:>3} {pct}"


def _wild_card_lines(
    by_division: Dict[str, List[Dict[str, Union[int, str, float, None]]]],
    division_order: Tuple[str, ...],
    name_w: int,
    row_fmt: Callable[[Dict[str, Union[int, str, float, None]]], str],
) -> List[str]:
    top3_names: set = set()
    conf_teams: List[Dict[str, Union[int, str, float, None]]] = []
    for div in division_order:
        teams = sorted(by_division.get(div) or [], key=_standings_sort_key)
        conf_teams.extend(teams)
        for t in teams[:3]:
            top3_names.add(str(t.get("short_name") or "").strip())
    remaining = [
        t for t in conf_teams if str(t.get("short_name") or "").strip() not in top3_names
    ]
    remaining.sort(key=_standings_sort_key)
    lines: List[str] = []
    if not remaining:
        if len(conf_teams) < 8:
            lines.append("    (в выборке < 8 команд конференции — гонка WC не показана)")
        else:
            lines.append("    (нет команд вне топ-3 дивизионов)")
        return lines
    hdr = f"{'':4}{'Команда':<{name_w}} {'Очк':>3} {'Игр':>3} {'%очк':>6}"
    lines.append(hdr)
    lines.append("    " + "-" * (len(hdr) - 4))
    for i, t in enumerate(remaining):
        label = "WC1 " if i == 0 else "WC2 " if i == 1 else "    "
        lines.append(label + row_fmt(t))
    return lines


def _standings_md_code_block(lines: List[str]) -> str:
    body = "\n".join(lines).rstrip("\n")
    return f"```\n{body}\n```"


def _build_standings_table_body(
    teams: List[Dict[str, Union[int, str, float, None]]],
) -> str:
    by_division: Dict[str, List[Dict[str, Union[int, str, float, None]]]] = defaultdict(
        list
    )
    for t in teams:
        div = t.get("division_name")
        if div:
            by_division[str(div)].append(t)

    name_w = _standings_name_width(teams)

    def row_fmt(team: Dict[str, Union[int, str, float, None]]) -> str:
        return _fmt_standings_row(team, name_w)

    chunks: List[str] = []

    def append_conference(title: str, div_order: Tuple[str, ...], wc_title: str) -> None:
        chunks.append(f"*{title}*")
        for div in div_order:
            label = _STANDINGS_DIV_LABEL.get(div, div.upper())
            hdr = f"{'Команда':<{name_w}} {'Очк':>3} {'Игр':>3} {'%очк':>6}"
            block_lines = [hdr, "-" * len(hdr)]
            for t in sorted(by_division.get(div) or [], key=_standings_sort_key):
                block_lines.append(row_fmt(t))
            chunks.append(f"*{label}*")
            chunks.append(_standings_md_code_block(block_lines))
        chunks.append(f"*{wc_title}*")
        chunks.append(_standings_md_code_block(_wild_card_lines(
            by_division, div_order, name_w, row_fmt
        )))

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


def team_table() -> str:
    stats = fetch_all(
        "SELECT short_name, games_played, points, procent_points, wins, "
        "       losses, ot, t.division_name, t.conference_name "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s "
        "ORDER BY conference_name, division_name, points DESC",
        (config.SEASON_ID,),
        columns=['short_name', 'games_played', 'points', 'procent_points',
                 'wins', 'losses', 'ot', 'division_name', 'conference_name'],
    )

    teams: List[Dict[str, Union[int, str, float, None]]] = []
    for i in range(stats['count_rows']):
        teams.append({
            'short_name': (stats['short_name'][i] or '—').strip(),
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
        'season': config.CURRENT_SEASON,
        'as_of': _standings_as_of_day(),
        'table_body': table_body,
    }
    return output_text('messages/league_table.txt', to_template)


def leaders_compact() -> str:
    """Топ-5 по очкам и голам в одном сообщении (MARKDOWN)."""
    pts = player_stats('Очки', 'players_season_stats', 'points', count=5)
    gls = player_stats('Голы', 'players_season_stats', 'goals', count=5)
    return (
        f"*Лидеры сезона* ({config.CURRENT_SEASON})\n\n"
        f"{pts}\n\n{gls}"
    )


def leaders_top10_messages() -> List[str]:
    """Два сообщения: топ-10 по очкам и по голам."""
    return [
        player_stats('Лучшие бомбардиры', 'players_season_stats', 'points', count=10),
        player_stats('Лучшие снайперы', 'players_season_stats', 'goals', count=10),
    ]


def team_stats(name_stats: str, column_name: str) -> str:
    validate_column(column_name)

    q = sql.SQL(
        "SELECT short_name, {col}, games_played "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s "
        "ORDER BY {col} DESC, short_name"
    ).format(col=sql.Identifier(column_name))
    stats = fetch_all(q, (config.SEASON_ID,), columns=['team', 'points', 'games_played'])

    to_template = {'name_stats': name_stats}
    teams = []
    for i in range(stats['count_rows']):
        teams.append({
            'rank': i + 1,
            'name': (stats['team'][i] or "—").strip(),
            'value': _format_leader_value(stats['points'][i]),
            'games': stats['games_played'][i],
        })

    to_template['teams'] = teams
    return output_text('messages/team_stats.txt', to_template)


def day_digest(day=None) -> Tuple[Optional[str], List[Tuple[int, str, List[Dict]]]]:
    """Return (day_label, list of (game_id, game_text, goals_meta)).

    game_id is 0 only for synthetic error/info rows (single tuple in the list).
    """
    day_label: Optional[str] = None
    if day is None:
        latest_day = fetch_all(
            "SELECT max(day) AS day FROM games WHERE season_id = %s",
            (config.SEASON_ID,),
            columns=["day"],
        )
        day = latest_day["day"][0]
        if day is None:
            return (None, [(0, "В базе пока нет завершенных матчей.", [])])
        day = str(day)
    else:
        day = str(day)
    day_label = day

    game_ids = fetch_all(
        "SELECT DISTINCT game_id FROM games WHERE day = %s AND season_id = %s ORDER BY game_id",
        (day, config.SEASON_ID),
        ['game_id'],
    )
    if game_ids['count_rows'] == 0:
        return (day_label, [(0, f'За {day} завершенных матчей не найдено.', [])])

    results: List[Tuple[int, str, List[Dict]]] = []
    for game_id in game_ids['game_id']:
        text, goals_meta = game_message(game_id)
        if text:
            results.append((game_id, text, goals_meta))

    if not results:
        return (day_label, [(0, f'За {day} завершенных матчей не найдено.', [])])
    return (day_label, results)


def day_digest_summary_body(real_games: List[Tuple[int, str, List[Dict]]]) -> str:
    """First line of each full game card — compact header for multi-game digest."""
    lines = []
    for _gid, full_text, _ in real_games:
        header = full_text.strip().split("\n", 1)[0].strip()
        lines.append(header)
    return "\n".join(lines)


def truncate_telegram_text(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    note = "\n\n_Текст обрезан (лимит Telegram). Подробности — кнопками «Матч N» ниже._"
    cut = max_len - len(note) - 3
    if cut < 80:
        cut = 80
    return text[:cut] + "..." + note
