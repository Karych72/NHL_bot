import html
from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from psycopg2 import sql

import config
from database import fetch_all, validate_table, validate_column
from template_funcs import output_text

# Telegram message text limit (UTF-16 length can differ; stay under safe byte-ish budget)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


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


def _goal_situation_suffix(
    is_ppg: Optional[bool],
    is_shg: Optional[bool],
    empty_net: Optional[bool],
) -> str:
    parts: List[str] = []
    if empty_net:
        parts.append("ПВ")
    elif is_shg:
        parts.append("МБ")
    elif is_ppg:
        parts.append("ББ")
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


def game_message(game_id: int) -> Tuple[str, List[Dict]]:
    """Return (rendered_text, goal_video_metadata)."""
    game_stats = fetch_all(
        "SELECT * FROM get_game_stats(%s)", (game_id,),
        ['goals', 'pim', 'blocks', 'hits', 'shots', 'is_overtime',
         'is_shootouts', 'field', 'team_name'],
    )
    teams_row = fetch_all(
        "SELECT home_team_id, away_team_id FROM games WHERE game_id = %s LIMIT 1",
        (game_id,),
        columns=["home_team_id", "away_team_id"],
    )
    away_tid = (
        int(teams_row["away_team_id"][0])
        if teams_row["count_rows"] and teams_row["away_team_id"][0] is not None
        else None
    )
    home_tid = (
        int(teams_row["home_team_id"][0])
        if teams_row["count_rows"] and teams_row["home_team_id"][0] is not None
        else None
    )
    game_goals = fetch_all(
        "SELECT * FROM get_goals_game(%s)", (game_id,),
        [
            "scorer",
            "scorer_position",
            "assist_1",
            "assist_2",
            "period",
            "goal_time",
            "home_score",
            "away_score",
            "is_ppg",
            "is_shg",
            "empty_net",
            "winner_goal",
            "goal_game_id",
            "goal_event_id",
        ],
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
    scorer_counts: Dict[str, int] = defaultdict(int)
    for i in range(game_goals['count_rows']):
        h_raw = game_goals['home_score'][i]
        a_raw = game_goals['away_score'][i]
        p = game_goals['period'][i]
        h = h_raw if h_raw is not None else prev_h
        a = a_raw if a_raw is not None else prev_a
        if p is not None:
            period_home[p] += h - prev_h
            period_away[p] += a - prev_a
        prev_h, prev_a = h, a

        scorer = game_goals['scorer'][i] or 'Unknown'
        pos_raw = game_goals['scorer_position'][i]
        pos = (str(pos_raw).strip() if pos_raw else "") or ""
        scorer_counts[scorer] += 1
        situation = _goal_situation_suffix(
            game_goals['is_ppg'][i],
            game_goals['is_shg'][i],
            game_goals['empty_net'][i],
        )
        winner_mark = " ★" if game_goals['winner_goal'][i] else ""

        time_str = game_goals['goal_time'][i]
        if p is not None and time_str:
            t_m = str((p - 1) * 20 + int(time_str.split(':')[0]))
            t_all = t_m + ':' + time_str.split(':')[1]
        else:
            t_all = '?:??'
        period_label = f"P{p}" if p is not None else "?"
        assists = ''
        if game_goals['assist_2'][i] is not None:
            assists = f"({game_goals['assist_1'][i]}, {game_goals['assist_2'][i]})"
        elif game_goals['assist_1'][i] is not None:
            assists = f"({game_goals['assist_1'][i]})"

        score = f"{h}:{a}"
        scorer_line = scorer + (f" [{pos}]" if pos else "") + assists + situation + winner_mark
        goals.append({
            'home_score': h,
            'away_score': a,
            'scorer': html.escape(scorer_line),
            'time': html.escape(t_all),
            'period_label': html.escape(period_label),
        })

        evt = game_goals['goal_event_id'][i]
        if evt is not None:
            goals_meta.append({
                'game_id': game_goals['goal_game_id'][i],
                'event_id': evt,
                'label': f"{score} {scorer}{assists} {t_all}",
            })

    num_periods = 3
    if is_overtime and not is_shootouts:
        num_periods = 4
    parts = [f"{period_home[p]}:{period_away[p]}" for p in range(1, num_periods + 1)]
    period_scores = f"({', '.join(parts)})"

    hat_lines = [
        f"• {html.escape(name)}: хет-трик (×{n})"
        for name, n in scorer_counts.items()
        if n >= 3
    ]
    hat_tricks = "\n".join(hat_lines) if hat_lines else ""

    away_abbr = (game_stats['team_name'][1] or "").strip()
    home_abbr = (game_stats['team_name'][0] or "").strip()
    form_away = _last_n_form_record(away_tid, 5) if away_tid is not None else "—"
    form_home = _last_n_form_record(home_tid, 5) if home_tid is not None else "—"
    recent_form = (
        "<b>Форма до матча (последние 5)</b>: "
        f"<b>{html.escape(away_abbr)}</b> {html.escape(form_away)} — "
        f"<b>{html.escape(home_abbr)}</b> {html.escape(form_home)}"
        if away_abbr and home_abbr
        else ""
    )

    goalkeepers = ''
    change_team = False
    for i in range(game_goalies['count_rows']):
        toi = game_goalies['timeonice'][i]
        # Both NULL and the legacy "00:00" sentinel mean "this goalie did not
        # play in this game" — skip rendering.
        if toi is None or toi == '00:00':
            continue
        if not game_goalies['is_home'][i] and not change_team:
            change_team = True
            goalkeepers += ' - '
        sv_pct = game_goalies['save_percentage'][i]
        sv_pct_str = f"{round(sv_pct, 2)}%" if sv_pct is not None else "—"
        saves = game_goalies['saves'][i]
        shots = game_goalies['shots'][i]
        saves_str = saves if saves is not None else "—"
        shots_str = shots if shots is not None else "—"
        raw_last = game_goalies["lastname"][i]
        ln_esc = html.escape(str(raw_last or ""))
        goalkeepers += (
            f"{ln_esc} "
            f"({html.escape(str(saves_str))}/{html.escape(str(shots_str))}, "
            f"{html.escape(str(sv_pct_str))}, "
            f"{html.escape(str(toi))})"
        )

    to_template = {
        'team_home': html.escape(str(game_stats['team_name'][0] or "")),
        'team_away': html.escape(str(game_stats['team_name'][1] or "")),
        'home_score': html.escape(str(game_stats['goals'][0])),
        'away_score': html.escape(str(game_stats['goals'][1])),
        'home_shots': html.escape(str(game_stats['shots'][0])),
        'away_shots': html.escape(str(game_stats['shots'][1])),
        'home_penalties': html.escape(str(game_stats['pim'][0])),
        'away_penalties': html.escape(str(game_stats['pim'][1])),
        'goals': goals,
        'goalkeepers': goalkeepers,
        'extra': html.escape(extra),
        'period_scores': html.escape(period_scores),
        'hat_tricks': hat_tricks,
        'recent_form': recent_form,
    }
    return output_text('messages/game_message.txt', to_template), goals_meta


def game_exists(game_id: int) -> bool:
    row = fetch_all(
        "SELECT 1 AS o FROM games WHERE game_id = %s LIMIT 1",
        (game_id,),
        columns=["o"],
    )
    return row["count_rows"] > 0


def _fmt_num_max2(val: Union[int, float, None]) -> str:
    if val is None:
        return "—"
    r = round(float(val), 2)
    s = f"{r:.2f}"
    return s.rstrip("0").rstrip(".")


def _fmt_pct_points(val: Union[int, float, None]) -> str:
    """Доля очков в БД уже в шкале 0–100 (нормализуется в пайплайне через `optional_pct_from_ratio`)."""
    if val is None:
        return "—"
    r = round(float(val), 2)
    s = f"{r:.2f}"
    return s.rstrip("0").rstrip(".") + "%"


def _fmt_pct_stat(val: Union[int, float, None]) -> str:
    """Проценты PP/PK/вбрасываний в БД уже 0–100."""
    if val is None:
        return "—"
    r = round(float(val), 2)
    s = f"{r:.2f}"
    return s.rstrip("0").rstrip(".") + "%"


def _team_id_for_abbrev(abbrev_u: str) -> Optional[int]:
    row = fetch_all(
        "SELECT team_id FROM teams WHERE season_id = %s "
        "AND upper(trim(COALESCE(abbreviation, ''))) = %s LIMIT 1",
        (config.SEASON_ID, abbrev_u),
        columns=["team_id"],
    )
    if row["count_rows"] < 1 or row["team_id"][0] is None:
        return None
    return int(row["team_id"][0])


def _last_n_form_record(team_id: int, n: int = 5) -> str:
    """Формат W-L-OTL по последним n завершённым играм (как в таблице очков)."""
    row = fetch_all(
        "SELECT winner_id, home_team_id, away_team_id, is_overtime, is_shootouts "
        "FROM games WHERE season_id = %s AND winner_id IS NOT NULL "
        "AND (home_team_id = %s OR away_team_id = %s) "
        "ORDER BY day DESC NULLS LAST, game_id DESC LIMIT %s",
        (config.SEASON_ID, team_id, team_id, n),
        columns=[
            "winner_id",
            "home_team_id",
            "away_team_id",
            "is_overtime",
            "is_shootouts",
        ],
    )
    w = losses = otl = 0
    for i in range(row["count_rows"]):
        wid = row["winner_id"][i]
        ot = bool(row["is_overtime"][i])
        so = bool(row["is_shootouts"][i])
        if wid == team_id:
            w += 1
        elif ot or so:
            otl += 1
        else:
            losses += 1
    if row["count_rows"] == 0:
        return "—"
    return f"{w}-{losses}-{otl}"


def _matchup_aligned_compare_rows(pairs: List[Tuple[str, str, str]], min_gap: int = 4) -> List[str]:
    """Строки «• метрика: … значение — значение»; числа начинаются в одной колонке (моноширинно в Telegram)."""
    prefixes = [f"• {lbl}:" for lbl, _, _ in pairs]
    w = max(len(p) for p in prefixes)
    value_start = w + min_gap
    rows: List[str] = []
    for (lbl, left, right), p in zip(pairs, prefixes):
        gap = value_start - len(p)
        if gap < 1:
            gap = 1
        rows.append(f"{p}{' ' * gap}{left} — {right}")
    return rows


def _h2h_season_wins(
    tid_a: int, tid_b: int, abbrev_a: str, abbrev_b: str
) -> Optional[str]:
    """Строка «побед в личных встречах» или None, если матчей не было."""
    row = fetch_all(
        "SELECT winner_id FROM games WHERE season_id = %s AND winner_id IS NOT NULL "
        "AND ((home_team_id = %s AND away_team_id = %s) "
        "     OR (home_team_id = %s AND away_team_id = %s))",
        (config.SEASON_ID, tid_a, tid_b, tid_b, tid_a),
        columns=["winner_id"],
    )
    if row["count_rows"] == 0:
        return None
    wa = wb = 0
    for i in range(row["count_rows"]):
        wid = row["winner_id"][i]
        if wid == tid_a:
            wa += 1
        elif wid == tid_b:
            wb += 1
    ea, eb = html.escape(abbrev_a), html.escape(abbrev_b)
    return f"<b>{ea}</b> {wa} — {wb} <b>{eb}</b>"


def matchup_season_preview(away_abbr: str, home_abbr: str) -> str:
    """
    Сезонное сравнение по teams.abbreviation (= abbrev из NHL API).

    Разметка HTML (parse_mode=HTML): блок метрик в &lt;pre&gt; — моноширинный шрифт,
    иначе в обычном тексте Telegram пробелы не выравнивают колонки.
    """
    a = (away_abbr or "").strip().upper()
    h = (home_abbr or "").strip().upper()
    if not a or not h or a == "?" or h == "?":
        return "Не удалось сопоставить команды с данными в базе."

    esc_a = html.escape(away_abbr)
    esc_h = html.escape(home_abbr)
    season_esc = html.escape(str(config.CURRENT_SEASON))

    stats = fetch_all(
        "SELECT upper(trim(COALESCE(t.abbreviation, ''))) AS abbr, ts.games_played, ts.wins, ts.losses, ts.ot, "
        "ts.points, ts.procent_points, ts.goals_per_game, ts.goals_against_per_game, "
        "ts.power_play_percentage, ts.penalty_kill_percentage, "
        "ts.shots_per_game, ts.face_off_win_percentage "
        "FROM teams_stats ts "
        "INNER JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s AND upper(trim(COALESCE(t.abbreviation, ''))) = ANY(%s)",
        (config.SEASON_ID, [a, h]),
        columns=[
            "abbr",
            "games_played",
            "wins",
            "losses",
            "ot",
            "points",
            "procent_points",
            "goals_per_game",
            "goals_against_per_game",
            "power_play_percentage",
            "penalty_kill_percentage",
            "shots_per_game",
            "face_off_win_percentage",
        ],
    )

    by_abbr: Dict[str, Dict[str, Union[int, float, None]]] = {}
    for i in range(stats["count_rows"]):
        ab = stats["abbr"][i]
        if not ab:
            continue
        by_abbr[str(ab).strip().upper()] = {
            "games_played": stats["games_played"][i],
            "wins": stats["wins"][i],
            "losses": stats["losses"][i],
            "ot": stats["ot"][i],
            "points": stats["points"][i],
            "procent_points": stats["procent_points"][i],
            "goals_per_game": stats["goals_per_game"][i],
            "goals_against_per_game": stats["goals_against_per_game"][i],
            "power_play_percentage": stats["power_play_percentage"][i],
            "penalty_kill_percentage": stats["penalty_kill_percentage"][i],
            "shots_per_game": stats["shots_per_game"][i],
            "face_off_win_percentage": stats["face_off_win_percentage"][i],
        }

    header = f"<b>{esc_a} @ {esc_h}</b> — сезон {season_esc}\n"

    def _one_line_team_html(ab: str, row: Optional[Dict[str, Union[int, float, None]]]) -> str:
        eab = html.escape(ab)
        if not row:
            return f"<b>{eab}</b>: нет строки в базе для сезона."
        w, losses, ot = row["wins"], row["losses"], row["ot"]
        rec = f"{_format_leader_value(w)}-{_format_leader_value(losses)}-{_format_leader_value(ot)}"
        pts = row["points"]
        ppct = _fmt_pct_points(row["procent_points"])
        return f"<b>{eab}</b> — {rec}, {_format_leader_value(pts)} очков ({ppct})"

    ra, rh = by_abbr.get(a), by_abbr.get(h)
    if ra is None and rh is None:
        return (
            header
            + f"Команд <b>{esc_a}</b> и <b>{esc_h}</b> нет в базе бота для этого сезона "
            f"({season_esc})."
        )

    parts: List[str] = [
        header.rstrip("\n"),
        _one_line_team_html(away_abbr, ra),
        _one_line_team_html(home_abbr, rh),
    ]

    if ra and rh:
        gf_a = ra["goals_per_game"]
        gf_h = rh["goals_per_game"]
        ga_a = ra["goals_against_per_game"]
        ga_h = rh["goals_against_per_game"]
        diff_a = (
            None
            if gf_a is None or ga_a is None
            else round(float(gf_a) - float(ga_a), 2)
        )
        diff_h = (
            None
            if gf_h is None or ga_h is None
            else round(float(gf_h) - float(ga_h), 2)
        )

        parts.append("")
        parts.append("<b>Сравнение</b>")
        parts.append(f"<b>{esc_a} VS {esc_h}</b>")
        parts.append("")
        compare_pairs: List[Tuple[str, str, str]] = [
            ("Голы за игру", _fmt_num_max2(gf_a), _fmt_num_max2(gf_h)),
            ("Пропущенные голы за игру", _fmt_num_max2(ga_a), _fmt_num_max2(ga_h)),
            (
                "Большинство",
                _fmt_pct_stat(ra["power_play_percentage"]),
                _fmt_pct_stat(rh["power_play_percentage"]),
            ),
            (
                "Меньшинство",
                _fmt_pct_stat(ra["penalty_kill_percentage"]),
                _fmt_pct_stat(rh["penalty_kill_percentage"]),
            ),
            (
                "Броски за игру",
                _fmt_num_max2(ra["shots_per_game"]),
                _fmt_num_max2(rh["shots_per_game"]),
            ),
            (
                "Вбрасывания",
                _fmt_pct_stat(ra["face_off_win_percentage"]),
                _fmt_pct_stat(rh["face_off_win_percentage"]),
            ),
            (
                "Разница шайб за игру",
                _fmt_num_max2(diff_a),
                _fmt_num_max2(diff_h),
            ),
        ]
        pre_raw = "\n".join(_matchup_aligned_compare_rows(compare_pairs, min_gap=4))
        parts.append(f"<pre>{html.escape(pre_raw)}</pre>")

        tid_a = _team_id_for_abbrev(a)
        tid_h = _team_id_for_abbrev(h)
        parts.append("")
        parts.append("<b>Форма (последние 5 игр)</b>")
        fa = _last_n_form_record(tid_a, 5) if tid_a is not None else "—"
        fh = _last_n_form_record(tid_h, 5) if tid_h is not None else "—"
        parts.append(f"<b>{esc_a}</b>: {html.escape(fa)}")
        parts.append(f"<b>{esc_h}</b>: {html.escape(fh)}")

        if tid_a is not None and tid_h is not None:
            h2h = _h2h_season_wins(tid_a, tid_h, away_abbr, home_abbr)
            if h2h:
                parts.append("")
                parts.append("<b>Личные встречи в сезоне (победы)</b>")
                parts.append(h2h)

    return "\n".join(parts)


LEADERBOARD_PAGE_SIZE = 10

LEADERBOARD_KIND_POINTS = "points"
LEADERBOARD_KIND_ASSISTS = "assists"
LEADERBOARD_KIND_GOALS = "goals"


def player_stats_with_count(
    name_stats: str,
    table_name: str,
    column_name: str,
    count: int = 10,
    offset: int = 0,
    *,
    secondary_sort: Optional[str] = None,
    use_html: bool = False,
) -> Tuple[str, int]:
    validate_table(table_name)
    validate_column(column_name)
    if offset < 0:
        offset = 0
    second_order = _resolve_secondary_sort(table_name, secondary_sort)
    validate_column(second_order)

    join_pss = _pss_join_sql(table_name, second_order)
    second_alias = _second_order_table_alias(table_name, second_order)
    pl_col = sql.SQL(".").join([sql.Identifier("pl"), sql.Identifier(column_name)])
    second_col = sql.SQL(".").join(
        [sql.Identifier(second_alias), sql.Identifier(second_order)]
    )

    q = sql.SQL(
        "SELECT r.lastname, r.position AS roster_position, {pl_col}, t.abbreviation AS team "
        "FROM {table} pl "
        "{join_pss}"
        "LEFT JOIN rosters r ON pl.player_id = r.player_id AND r.season_id = pl.season_id "
        "LEFT JOIN teams t ON t.team_id = r.current_team_id AND t.season_id = pl.season_id "
        "WHERE pl.season_id = %s "
        # NULLS LAST keeps unranked players (no value reported) at the bottom of
        # leaderboards instead of at the top under DESC's default NULLS FIRST.
        "ORDER BY {pl_col} DESC NULLS LAST, {second_col} DESC NULLS LAST "
        "LIMIT %s OFFSET %s"
    ).format(
        pl_col=pl_col,
        table=sql.Identifier(table_name),
        join_pss=join_pss,
        second_col=second_col,
    )
    stats = fetch_all(
        q, (config.SEASON_ID, count, offset),
        ['lastname', 'roster_position', 'points', 'team'],
    )

    roster_positions = stats.get("roster_position")
    players = []
    for i in range(stats['count_rows']):
        lastname = stats['lastname'][i] or "Unknown"
        pos_raw = roster_positions[i] if roster_positions is not None else None
        pos = (str(pos_raw).strip() if pos_raw else "") or ""
        players.append({
            'rank': offset + i + 1,
            'lastname': lastname + (f" [{pos}]" if pos else ""),
            'value': _format_leader_value(stats['points'][i]),
            'team': stats['team'][i] or "—",
        })

    if use_html:
        lines: List[str] = []
        for p in players:
            line = (
                f"{p['rank']}. {html.escape(str(p['lastname']))} — "
                f"{html.escape(str(p['value']))} ({html.escape(str(p['team']))})"
            )
            lines.append(line)
        inner = "\n".join(lines)
        body_inner = f"<pre>{inner}</pre>" if inner else ""
        if name_stats:
            body = f"<b>{html.escape(name_stats)}</b>\n\n{body_inner}"
        else:
            body = body_inner
        return body, stats["count_rows"]

    to_template: Dict[str, Any] = {'name_stats': name_stats, 'players': players}
    return output_text('messages/season_leaders_players.txt', to_template), stats[
        "count_rows"
    ]


def player_stats(
    name_stats: str,
    table_name: str,
    column_name: str,
    count: int = 10,
    offset: int = 0,
    *,
    secondary_sort: Optional[str] = None,
) -> str:
    """Текст лидерборда; всегда HTML (экранирование имён из БД), как в /stats и /leaders."""
    return player_stats_with_count(
        name_stats,
        table_name,
        column_name,
        count=count,
        offset=offset,
        secondary_sort=secondary_sort,
        use_html=True,
    )[0]


def _rank_range_label(offset: int, rows: int) -> str:
    if rows <= 0:
        return "нет данных"
    lo = offset + 1
    hi = offset + rows
    if lo == hi:
        return str(lo)
    return f"{lo}–{hi}"


def leaders_menu_intro() -> str:
    """Текст первого шага /leaders — до выбора категории кнопкой."""
    season_esc = html.escape(str(config.CURRENT_SEASON))
    return (
        f"<b>Лидеры сезона</b> ({season_esc})\n\n"
        "Выберите категорию:"
    )


def player_stat_leaderboard_page(
    heading: str,
    table_name: str,
    column_name: str,
    offset: int,
    *,
    secondary_sort: Optional[str] = None,
) -> Tuple[str, bool, bool]:
    """Произвольная таблица лидеров игроков/вратарей: шапка + пустая строка + список."""
    if offset < 0:
        offset = 0
    body, n = player_stats_with_count(
        "",
        table_name,
        column_name,
        count=LEADERBOARD_PAGE_SIZE,
        offset=offset,
        secondary_sort=secondary_sort,
        use_html=True,
    )
    span = _rank_range_label(offset, n)
    if n == 0:
        body = "<i>Нет данных в этом диапазоне.</i>"
    text = (
        f"<b>{html.escape(heading)}</b> ({html.escape(str(config.CURRENT_SEASON))}) "
        f"— <i>{html.escape(span)}</i>\n\n{body}"
    )
    has_prev = offset > 0
    has_next = n >= LEADERBOARD_PAGE_SIZE
    return text, has_prev, has_next


def single_stat_leaderboard_page(
    heading: str,
    column_name: str,
    offset: int,
) -> Tuple[str, bool, bool]:
    """Сезонные статы полевых (players_season_stats)."""
    return player_stat_leaderboard_page(
        heading, "players_season_stats", column_name, offset
    )


def stat_leaderboard_for_kind(kind: str, offset: int) -> Tuple[str, bool, bool]:
    """Топ по очкам, голам или передачам; *kind* — points / goals / assists."""
    if kind == LEADERBOARD_KIND_POINTS:
        return player_stat_leaderboard_page(
            "Топ бомбардиров", "players_season_stats", "points", offset
        )
    if kind == LEADERBOARD_KIND_GOALS:
        return player_stat_leaderboard_page(
            "Топ снайперов", "players_season_stats", "goals", offset
        )
    if kind == LEADERBOARD_KIND_ASSISTS:
        return player_stat_leaderboard_page(
            "Топ ассистентов", "players_season_stats", "assists", offset
        )
    raise ValueError(f"unknown leaderboard kind: {kind!r}")


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


def _standings_html_pre_block(lines: List[str]) -> str:
    body = html.escape("\n".join(lines).rstrip("\n"))
    return f"<pre>{body}</pre>"


def _build_standings_table_body(
    teams: List[Dict[str, Union[int, str, float, None]]],
    *,
    use_html: bool = False,
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

    block_fmt = _standings_html_pre_block if use_html else _standings_md_code_block
    title_fmt = (lambda t: f"<b>{html.escape(t)}</b>") if use_html else (lambda t: f"*{t}*")

    def append_conference(title: str, div_order: Tuple[str, ...], wc_title: str) -> None:
        chunks.append(title_fmt(title))
        for div in div_order:
            label = _STANDINGS_DIV_LABEL.get(div, div.upper())
            hdr = f"{'Команда':<{name_w}} {'Очк':>3} {'Игр':>3} {'%очк':>6}"
            block_lines = [hdr, "-" * len(hdr)]
            for t in sorted(by_division.get(div) or [], key=_standings_sort_key):
                block_lines.append(row_fmt(t))
            chunks.append(title_fmt(label))
            chunks.append(block_fmt(block_lines))
        chunks.append(title_fmt(wc_title))
        chunks.append(block_fmt(_wild_card_lines(
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

    sep = "\n\n" if not use_html else "\n"
    return sep.join(chunks)


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

    table_body = _build_standings_table_body(teams, use_html=True)
    to_template = {
        'season': html.escape(str(config.CURRENT_SEASON)),
        'as_of': html.escape(str(_standings_as_of_day())),
        'table_body': table_body,
    }
    return output_text('messages/league_table.txt', to_template)


def season_team_abbrev_help_text() -> str:
    """Краткий список аббревиатур команд текущего сезона для /team."""
    stats = fetch_all(
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
    season_esc = html.escape(str(config.CURRENT_SEASON))
    if not abbrevs:
        return (
            f"<b>Команды сезона</b> ({season_esc})\n"
            "В базе пока нет списка команд для этого сезона."
        )
    parts = [f"<code>{html.escape(a)}</code>" for a in abbrevs]
    inner = ", ".join(parts)
    max_inner = 3500
    while len(inner) > max_inner and len(parts) > 1:
        parts = parts[:-1]
        inner = ", ".join(parts) + "…"
    if len(inner) > max_inner:
        inner = inner[: max_inner - 1] + "…"
    return (
        f"<b>Команды сезона</b> ({season_esc}) — аббревиатуры:\n{inner}\n\n"
        "Карточку матча из базы удобнее открыть через <code>/day_games</code> "
        "или <code>/stats</code> → дайджест."
    )


def team_stats_with_count(
    name_stats: str,
    column_name: str,
    count: int = 10,
    offset: int = 0,
    *,
    use_html: bool = False,
) -> Tuple[str, int]:
    validate_column(column_name)
    if offset < 0:
        offset = 0

    q = sql.SQL(
        "SELECT short_name, {col}, games_played "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s "
        "ORDER BY {col} DESC NULLS LAST, short_name "
        "LIMIT %s OFFSET %s"
    ).format(col=sql.Identifier(column_name))
    stats = fetch_all(
        q,
        (config.SEASON_ID, count, offset),
        columns=['team', 'points', 'games_played'],
    )

    teams = []
    for i in range(stats['count_rows']):
        teams.append({
            'rank': offset + i + 1,
            'name': (stats['team'][i] or "—").strip(),
            'value': _format_leader_value(stats['points'][i]),
            'games': stats['games_played'][i],
        })

    if use_html:
        lines_t: List[str] = []
        for trow in teams:
            gn = html.escape(str(trow["games"]))
            lines_t.append(
                f"{trow['rank']}. {html.escape(trow['name'])} — "
                f"{html.escape(str(trow['value']))} (игр: {gn})"
            )
        inner_t = "\n".join(lines_t)
        body_t = f"<pre>{inner_t}</pre>" if inner_t else ""
        if name_stats:
            return (
                f"<b>{html.escape(name_stats)}</b>\n\n{body_t}",
                stats["count_rows"],
            )
        return body_t, stats["count_rows"]

    to_template: Dict[str, Any] = {'name_stats': name_stats, 'teams': teams}
    return output_text('messages/team_stats.txt', to_template), stats["count_rows"]


def team_stat_leaderboard_page(
    heading: str,
    column_name: str,
    offset: int,
) -> Tuple[str, bool, bool]:
    """Топ команд по одной колонке teams_stats."""
    body, n = team_stats_with_count(
        "",
        column_name,
        count=LEADERBOARD_PAGE_SIZE,
        offset=offset,
        use_html=True,
    )
    span = _rank_range_label(offset, n)
    if n == 0:
        body = "<i>Нет данных в этом диапазоне.</i>"
    text = (
        f"<b>{html.escape(heading)}</b> ({html.escape(str(config.CURRENT_SEASON))}) "
        f"— <i>{html.escape(span)}</i>\n\n{body}"
    )
    has_prev = offset > 0
    has_next = n >= LEADERBOARD_PAGE_SIZE
    return text, has_prev, has_next


def team_stats(name_stats: str, column_name: str) -> str:
    """Совместимость: одна страница (первые 10) с заголовком в шаблоне."""
    return team_stats_with_count(
        name_stats, column_name, count=LEADERBOARD_PAGE_SIZE, offset=0
    )[0]


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


def truncate_telegram_text(
    text: str,
    max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    *,
    footer_note: Optional[str] = None,
) -> str:
    if len(text) <= max_len:
        return text
    note = (
        footer_note
        if footer_note is not None
        else (
            "\n\n<i>Текст обрезан (лимит Telegram). Подробности — кнопками «Матч N» ниже.</i>"
        )
    )
    cut = max_len - len(note) - 3
    if cut < 80:
        cut = 80
    return text[:cut] + "..." + note
