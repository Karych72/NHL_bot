"""Сборка текстов и HTML-разметки для сообщений бота: карточки матчей,
дайджест дня, лидерборды игроков/команд, турнирная таблица.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`) и
превращает строки в готовый Telegram-текст; сама отправка, листание
страниц и клавиатуры живут в `stats_handlers.py`/`script_bot.py`.
"""

import html
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from psycopg2 import sql

import config
from database import cached_fetch_all, fetch_all, validate_table, validate_column
from leaderboard_specs import STAT_COLUMN_LABELS
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


# Колонки времени «м:сс» (varchar, пишет `optional_seconds_to_mmss` загрузчика): строкой
# «9:57» > «25:00», поэтому сортировать их можно только по секундам (Задача 44).
_MMSS_COLUMNS = frozenset({"time_on_ice_per_game"})
# Секунды из «м:сс»; `{col}` — колонка из белого списка или литерал модуля. NULL → NULL.
_MMSS_SECONDS_SQL = "(split_part({col}, ':', 1)::int * 60 + split_part({col}, ':', 2)::int)"


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


def _pss_join_sql(table_name: str) -> sql.Composable:
    """JOIN players_season_stats под алиасом `pss` — нужен вторичной сортировке
    по колонке pss (`_second_order_table_alias`) и «хвосту» строки лидерборда
    (games/shifts, Задача 18). Для players_advanced_stats и players_shot_types
    join всегда присутствует (не только когда second_order сам на pss) —
    games/shifts в хвосте нужны независимо от того, чем сортируется страница.
    """
    if table_name == "players_advanced_stats":
        # Порог 20 игр отсекает шум долей на малой выборке, но в начале сезона
        # не пропускает никого — тогда порог половина игр команды-лидера по
        # числу матчей (1 игра сыграна → порог 1).
        return sql.SQL(
            "INNER JOIN players_season_stats pss ON pl.player_id = pss.player_id "
            "AND pss.season_id = pl.season_id AND pss.games >= LEAST(20, ("
            "SELECT CEIL(MAX(ts.games_played) / 2.0) FROM teams_stats ts "
            "WHERE ts.season_id = pl.season_id)) "
        )
    if table_name == "players_shot_types":
        return sql.SQL(
            "LEFT JOIN players_season_stats pss ON pl.player_id = pss.player_id "
            "AND pss.season_id = pl.season_id "
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


def _goal_situation_suffix(
    is_ppg: Optional[bool],
    is_shg: Optional[bool],
    empty_net: Optional[bool],
    winner_goal: Optional[bool],
) -> str:
    """Пометки гола в карточке: ПВ/МБ/ББ (пустые ворота, меньшинство,
    большинство — одна, по приоритету) и ПШ (победная шайба). Раньше
    победную отмечала «★», которую путали со звёздами матча."""
    parts: List[str] = []
    if empty_net:
        parts.append("ПВ")
    elif is_shg:
        parts.append("МБ")
    elif is_ppg:
        parts.append("ББ")
    if winner_goal:
        parts.append("ПШ")
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


_RUSSIAN_NATIONALITY = "RUS"


def _goal_player_name(lastname: Optional[str], nationality: Optional[str]) -> str:
    """Фамилия игрока в строке гола; русский игрок (`rosters.nationality`)
    пишется капсом — так его видно в списке голов."""
    name = lastname or "Unknown"
    return name.upper() if nationality == _RUSSIAN_NATIONALITY else name


def _aligned_columns(rows: Sequence[Sequence[str]], align: str, sep: str = " ") -> List[str]:
    """Строки таблицы: каждая колонка добита пробелами до самой широкой ячейки.

    Зачем: Telegram выравнивает пробелами только моноширинный текст (`<pre>`) —
    общий рендер для списка голов карточки, сравнения в превью матча и таблиц
    (`_pre_table`, `_pre_table_fit`).

    Аргументы:
        rows: ячейки (сырые строки, без HTML); у всех строк одно число колонок.
        align: по символу на колонку — `l` (влево) или `r` (вправо).
        sep: разделитель колонок; таблицы передают `_TABLE_SEP`.
    """
    widths = [max(len(r[c]) for r in rows) for c in range(len(align))]
    return [
        sep.join(
            cell.rjust(w) if a == "r" else cell.ljust(w)
            for cell, w, a in zip(r, widths, align)
        ).rstrip()
        for r in rows
    ]


# Ширина фамилии в выровненных таблицах (лидерборды, страны): строка в `<pre>`
# не длиннее ~36 символов — столько помещается на экране телефона без переноса.
_LEADER_NAME_WIDTH = 10


def _clip_cell(text: str, width: int) -> str:
    """Ячейка таблицы не шире `width`: длинное обрезается с «…» на конце."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _pre_block(lines: Sequence[str]) -> str:
    """Строки (сырые, без HTML) одним экранированным блоком `<pre>`."""
    body = "\n".join(lines)
    return f"<pre>{html.escape(body)}</pre>"


# Разделитель колонок `<pre>`-таблиц: на месте пробела, ширину строки не меняет,
# а соседние числа («Знач И Смен») больше не сливаются (отзыв 2026-10-03).
_TABLE_SEP = "|"


def _pre_table(rows: Sequence[Sequence[str]], align: str) -> str:
    """`_aligned_columns()` с разделителем `_TABLE_SEP` одним блоком `<pre>`."""
    return _pre_block(_aligned_columns(rows, align, _TABLE_SEP))


# Столько моноширинных символов помещается в строку `<pre>` на экране телефона.
_MOBILE_PRE_WIDTH = 36


def _pre_table_fit(rows: Sequence[Sequence[str]], align: str, drop_order: Sequence[int]) -> str:
    """`_pre_table()`, ужатая под экран телефона.

    Зачем: строка шире `_MOBILE_PRE_WIDTH` переносится и ломает колонки — вместо
    переноса таблица теряет наименее важные колонки.

    Аргументы:
        rows, align: как у `_pre_table()`.
        drop_order: индексы колонок, которые можно убрать, — от наименее важной;
            колонки убираются по одной, пока самая длинная строка не влезет.
    """
    keep = list(range(len(align)))
    for col in [None, *drop_order]:
        if col is not None:
            keep.remove(col)
        lines = _aligned_columns(
            [[r[c] for c in keep] for r in rows], "".join(align[c] for c in keep), _TABLE_SEP
        )
        if max(len(line) for line in lines) <= _MOBILE_PRE_WIDTH:
            break
    return _pre_block(lines)


def _record_str(
    wins: Union[int, float, Decimal, None],
    losses: Union[int, float, Decimal, None],
    ot: Union[int, float, Decimal, None],
) -> str:
    """Баланс «W-L-OT» (`7-3-0`) — общий для профиля клуба, превью матча и вратарей."""
    return "-".join(_format_leader_value(v) for v in (wins, losses, ot))


def _format_three_star_line(
    star: int,
    lastname: Optional[str],
    position: Optional[str],
    abbreviation: Optional[str],
    goals: Optional[int],
    assists: Optional[int],
    saves: Optional[int],
    shots: Optional[int],
    save_percentage: Optional[float],
) -> str:
    """Одна строка блока «Звёзды матча»: `★1 Kaprizov (MIN) — 1+0` для полевого
    игрока, `★1 Thompson (VGK) — 33/34, 97.1%` для вратаря. Различение — по
    позиции из `rosters` (`G` → вратарь). Отсутствующая фамилия → `Unknown`
    (как для голов, `bot_messages.py:187`); отсутствующая статистика (NULL) —
    строка без хвоста после «—». Каждое поле из БД проходит `html.escape()`.
    """
    name = html.escape(str(lastname or "Unknown"))
    abbr = html.escape(str(abbreviation or ""))
    star_str = html.escape(str(star))
    is_goalie = position == "G"
    tail = ""
    if is_goalie:
        if saves is not None and shots is not None:
            tail = f"{html.escape(str(saves))}/{html.escape(str(shots))}"
            if save_percentage is not None:
                pct = round(float(save_percentage), 2)
                tail += f", {html.escape(str(pct))}%"
    elif goals is not None and assists is not None:
        tail = f"{html.escape(str(goals))}+{html.escape(str(assists))}"

    line = f"★{star_str} {name} ({abbr})"
    return f"{line} — {tail}" if tail else line


_GAME_STATS_COLUMNS = [
    'goals', 'pim', 'blocks', 'hits', 'shots', 'is_overtime',
    'is_shootouts', 'field', 'team_name',
]
_GOALS_GAME_COLUMNS = [
    "scorer",
    "scorer_position",
    "scorer_nationality",
    "assist_1",
    "assist_1_nationality",
    "assist_2",
    "assist_2_nationality",
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
]


def _fetch_game_score_rows(game_id: int) -> Tuple[Dict, Dict]:
    """Строки `get_game_stats` (хозяева, гости) и `get_goals_game` матча —
    всё, из чего считается счёт: для карточки (`game_message`) и сводки дня
    (`day_digest_summary_body`)."""
    game_stats = fetch_all(
        "SELECT * FROM get_game_stats(%s)", (game_id,), _GAME_STATS_COLUMNS,
    )
    game_goals = fetch_all(
        "SELECT * FROM get_goals_game(%s)", (game_id,), _GOALS_GAME_COLUMNS,
    )
    return game_stats, game_goals


def _running_scores(game_goals: Dict) -> List[Tuple[int, int]]:
    """Счёт после каждого гола; пропуск счёта в строке (NULL) — счёт
    предыдущего гола."""
    scores = []
    h, a = 0, 0
    for i in range(game_goals['count_rows']):
        if game_goals['home_score'][i] is not None:
            h = game_goals['home_score'][i]
        if game_goals['away_score'][i] is not None:
            a = game_goals['away_score'][i]
        scores.append((h, a))
    return scores


def _game_score_header(game_stats: Dict, game_goals: Dict) -> Dict[str, str]:
    """Счёт матча по частям, сырыми строками (без HTML): `home`, `away`,
    `home_score`, `away_score`, `extra` (« (OT)»/« (Б)»/пусто),
    `period_scores` («2:1, 4:2, 2:1»).

    Единственная точка расчёта шапки карточки матча и строки сводки /today —
    иначе две копии счёта по периодам разойдутся.
    """
    is_overtime = game_stats['is_overtime'][0]
    is_shootouts = game_stats['is_shootouts'][0]
    if not is_overtime:
        extra = ''
    elif not is_shootouts:
        extra = ' (OT)'
    else:
        extra = ' (Б)'

    period_home: dict[int, int] = defaultdict(int)
    period_away: dict[int, int] = defaultdict(int)
    prev_h, prev_a = 0, 0
    for i, (h, a) in enumerate(_running_scores(game_goals)):
        p = game_goals['period'][i]
        if p is not None:
            period_home[p] += h - prev_h
            period_away[p] += a - prev_a
        prev_h, prev_a = h, a
    num_periods = 4 if is_overtime and not is_shootouts else 3
    parts = [f"{period_home[p]}:{period_away[p]}" for p in range(1, num_periods + 1)]
    return {
        'home': str(game_stats['team_name'][0] or ""),
        'away': str(game_stats['team_name'][1] or ""),
        'home_score': str(game_stats['goals'][0]),
        'away_score': str(game_stats['goals'][1]),
        'extra': extra,
        'period_scores': ', '.join(parts),
    }


def _goals_pre_block(goal_rows: List[List[str]], assists: List[str]) -> str:
    """Голы карточки матча одним `<pre>` в ширину телефона: строка гола —
    время, счёт, автор с позицией и пометками; ассистенты — следующей строкой
    под автором (в одну строку с ними гол на телефоне переносится).

    Аргументы:
        goal_rows: [время, счёт, автор] по голу, сырые строки без HTML.
        assists: ассистенты того же гола через запятую; пусто — строки нет.
    """
    indent = " " * (max(len(r[0]) for r in goal_rows) + max(len(r[1]) for r in goal_rows) + 2)
    lines: List[str] = []
    for line, names in zip(_aligned_columns(goal_rows, "rll"), assists):
        lines.append(line)
        if names:
            lines.append(indent + names)
    return _pre_block(lines)


def game_message(game_id: int) -> Tuple[str, List[Dict]]:
    """Return (rendered_text, goal_video_metadata)."""
    game_stats, game_goals = _fetch_game_score_rows(game_id)
    header = _game_score_header(game_stats, game_goals)
    teams_row = fetch_all(
        "SELECT home_team_id, away_team_id, day::text FROM games WHERE game_id = %s LIMIT 1",
        (game_id,),
        columns=["home_team_id", "away_team_id", "day"],
    )
    game_day = teams_row["day"][0] if teams_row["count_rows"] else None
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
    game_goalies = fetch_all(
        "SELECT * FROM get_goalies_game(%s)", (game_id,),
        ['shots', 'saves', 'timeonice', 'lastname',
         'save_percentage', 'is_home'],
    )
    game_three_stars = fetch_all(
        "SELECT * FROM get_three_stars_game(%s)", (game_id,),
        ['star', 'lastname', 'player_position', 'abbreviation',
         'goals', 'assists', 'saves', 'shots', 'save_percentage'],
    )

    goal_rows: List[List[str]] = []
    goal_assists: List[str] = []
    goals_meta = []
    scorer_counts: Dict[str, int] = defaultdict(int)
    for i, (h, a) in enumerate(_running_scores(game_goals)):
        p = game_goals['period'][i]
        scorer = _goal_player_name(game_goals['scorer'][i], game_goals['scorer_nationality'][i])
        pos_raw = game_goals['scorer_position'][i]
        pos = (str(pos_raw).strip() if pos_raw else "") or ""
        scorer_counts[scorer] += 1
        situation = _goal_situation_suffix(
            game_goals['is_ppg'][i],
            game_goals['is_shg'][i],
            game_goals['empty_net'][i],
            game_goals['winner_goal'][i],
        )

        # Время от начала матча; период в строке не пишется — он виден по минуте.
        time_str = game_goals['goal_time'][i]
        if p is not None and time_str:
            t_m = str((p - 1) * 20 + int(time_str.split(':')[0]))
            t_all = t_m + ':' + time_str.split(':')[1]
        else:
            t_all = '?:??'
        assist_names = [
            _goal_player_name(game_goals[key][i], game_goals[f"{key}_nationality"][i])
            for key in ("assist_1", "assist_2")
            if game_goals[key][i] is not None
        ]
        score = f"{h}:{a}"
        goal_rows.append([t_all, score, scorer + (f" [{pos}]" if pos else "") + situation])
        goal_assists.append(", ".join(assist_names))

        evt = game_goals['goal_event_id'][i]
        if evt is not None:
            goals_meta.append({
                'game_id': game_goals['goal_game_id'][i],
                'event_id': evt,
                'label': f"▶ {score} {scorer} {t_all}",
            })
    goals_block = _goals_pre_block(goal_rows, goal_assists) if goal_rows else ""

    hat_lines = [
        f"<b>Хет-трик</b>: {html.escape(name)} (×{n})"
        for name, n in scorer_counts.items()
        if n >= 3
    ]
    hat_tricks = "\n".join(hat_lines) if hat_lines else ""

    away_abbr = (game_stats['team_name'][1] or "").strip()
    home_abbr = (game_stats['team_name'][0] or "").strip()
    # Форма — до этого матча: игры строго раньше его игрового дня.
    form_away = (
        _last_n_form_record(away_tid, 5, before_day=game_day) if away_tid is not None else "—"
    )
    form_home = (
        _last_n_form_record(home_tid, 5, before_day=game_day) if home_tid is not None else "—"
    )
    # Порядок команд — как в шапке карточки (хозяева первыми).
    recent_form = (
        "<i>Форма (5 игр, W-L-OTL)</i>:\n"
        f"{html.escape(home_abbr)} {html.escape(form_home)} · "
        f"{html.escape(away_abbr)} {html.escape(form_away)}"
        if away_abbr and home_abbr
        else ""
    )

    # Вратарь — своей строкой (хозяева первыми): в одну строку двое не влезают
    # в ширину телефона.
    goalie_lines: List[str] = []
    for i in range(game_goalies['count_rows']):
        toi = game_goalies['timeonice'][i]
        # Both NULL and the legacy "00:00" sentinel mean "this goalie did not
        # play in this game" — skip rendering.
        if toi is None or toi == '00:00':
            continue
        sv_pct = game_goalies['save_percentage'][i]
        sv_pct_str = f"{round(sv_pct, 2)}%" if sv_pct is not None else "—"
        saves = game_goalies['saves'][i]
        shots = game_goalies['shots'][i]
        saves_str = saves if saves is not None else "—"
        shots_str = shots if shots is not None else "—"
        raw_last = game_goalies["lastname"][i]
        ln_esc = html.escape(str(raw_last or ""))
        goalie_lines.append(
            f"{ln_esc} — "
            f"{html.escape(str(saves_str))}/{html.escape(str(shots_str))}, "
            f"{html.escape(str(sv_pct_str))}, "
            f"{html.escape(str(toi))}"
        )

    three_stars = "\n".join(
        _format_three_star_line(
            game_three_stars['star'][i],
            game_three_stars['lastname'][i],
            game_three_stars['player_position'][i],
            game_three_stars['abbreviation'][i],
            game_three_stars['goals'][i],
            game_three_stars['assists'][i],
            game_three_stars['saves'][i],
            game_three_stars['shots'][i],
            game_three_stars['save_percentage'][i],
        )
        for i in range(game_three_stars['count_rows'])
    )

    to_template = {
        'team_home': html.escape(header['home']),
        'team_away': html.escape(header['away']),
        'home_score': html.escape(header['home_score']),
        'away_score': html.escape(header['away_score']),
        'home_shots': html.escape(str(game_stats['shots'][0])),
        'away_shots': html.escape(str(game_stats['shots'][1])),
        'home_penalties': html.escape(str(game_stats['pim'][0])),
        'away_penalties': html.escape(str(game_stats['pim'][1])),
        'goals_block': goals_block,
        'goalkeepers': "\n".join(goalie_lines),
        'extra': html.escape(header['extra']),
        'period_scores': html.escape(header['period_scores']),
        'hat_tricks': hat_tricks,
        'recent_form': recent_form,
        'three_stars': three_stars,
    }
    return output_text('messages/game_message.txt', to_template), goals_meta


def game_exists(game_id: int) -> bool:
    """Проверяет, есть ли матч с таким `game_id` в таблице `games` — быстрый
    guard перед построением карточки матча (`game_message`), чтобы не тратить
    остальные запросы на несуществующий id."""
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


def _fmt_pct_points(val: Union[int, float, Decimal, None]) -> str:
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
    row = cached_fetch_all(
        "SELECT team_id FROM teams WHERE season_id = %s "
        "AND upper(trim(COALESCE(abbreviation, ''))) = %s LIMIT 1",
        (config.SEASON_ID, abbrev_u),
        columns=["team_id"],
    )
    if row["count_rows"] < 1 or row["team_id"][0] is None:
        return None
    return int(row["team_id"][0])


_TEAM_RECENT_GAMES_COLUMNS = [
    "winner_id",
    "is_overtime",
    "is_shootouts",
    "ot_empty_net_win",
]

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


def _recent_team_outcomes(
    team_id: int, limit: int, before_day: Optional[str] = None
) -> List[str]:
    """Разряды (`_team_game_outcome()`) последних `limit` завершённых игр
    сезона команды `team_id`, от новой игры к старой.

    Единственное место, где строится запрос «последние игры команды» и
    разбирается его строка — общее для `_last_n_form_record()` (`limit=5`,
    запись формы `W-L-OTL`) и `_current_streak()`
    (`limit=_CURRENT_STREAK_LOOKBACK_LIMIT`, текущая серия). Учитывает
    правило NHL 84.2 через `_team_game_outcome()`, поэтому форма и серия не
    могут разойтись в подсчёте ПО.

    Аргументы:
        team_id: команда, для которой отбираются игры.
        limit: сколько последних игр взять (`ORDER BY day DESC NULLS LAST, game_id DESC`).
        before_day: только игры раньше этого игрового дня (`YYYY-MM-DD`) —
            форма «до матча» в его карточке; None — все игры сезона.

    Возвращает: список `"W"`/`"L"`/`"OTL"` длиной `min(limit, сыграно игр)`;
    пустой список, если в текущем сезоне у команды нет завершённых игр.
    """
    row = cached_fetch_all(
        "SELECT winner_id, is_overtime, is_shootouts, "
        "EXISTS (SELECT 1 FROM all_goals a WHERE a.game_id = g.game_id "
        "AND a.winner_goal AND a.empty_net AND a.period >= 4) AS ot_empty_net_win "
        "FROM games g WHERE season_id = %s AND winner_id IS NOT NULL "
        "AND (home_team_id = %s OR away_team_id = %s) "
        "AND (%s::date IS NULL OR day < %s::date) "
        "ORDER BY day DESC NULLS LAST, game_id DESC LIMIT %s",
        (config.SEASON_ID, team_id, team_id, before_day, before_day, limit),
        columns=_TEAM_RECENT_GAMES_COLUMNS,
    )
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


def _last_n_form_record(team_id: int, n: int = 5, before_day: Optional[str] = None) -> str:
    """Формат W-L-OTL по последним n завершённым играм (как в таблице очков);
    без игр — `0-0-0` (первый матч сезона в карточке).

    Игры и их разряды берёт `_recent_team_outcomes()` (правило NHL 84.2 —
    см. `_team_game_outcome()`), чтобы форма и текущая серия
    (`_current_streak()`) не могли разойтись в подсчёте ПО. `before_day` —
    см. `_recent_team_outcomes()`.
    """
    outcomes = _recent_team_outcomes(team_id, n, before_day)
    w = outcomes.count("W")
    losses = outcomes.count("L")
    otl = outcomes.count("OTL")
    return f"{w}-{losses}-{otl}"


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


def _h2h_season_wins(tid_a: int, tid_b: int) -> Optional[Tuple[int, int]]:
    """Победы команд `tid_a` и `tid_b` в личных встречах сезона или None,
    если они ещё не встречались."""
    row = cached_fetch_all(
        "SELECT winner_id FROM games WHERE season_id = %s AND winner_id IS NOT NULL "
        "AND ((home_team_id = %s AND away_team_id = %s) "
        "     OR (home_team_id = %s AND away_team_id = %s))",
        (config.SEASON_ID, tid_a, tid_b, tid_b, tid_a),
        columns=["winner_id"],
    )
    if row["count_rows"] == 0:
        return None
    winners = row["winner_id"][: row["count_rows"]]
    return winners.count(tid_a), winners.count(tid_b)


def _points_word(points: Union[int, float, Decimal, None]) -> str:
    """«очко»/«очка»/«очков» под число очков (1 очко, 2 очка, 5 очков)."""
    n = int(points or 0)
    if n % 10 == 1 and n % 100 != 11:
        return "очко"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "очка"
    return "очков"


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
    ppct = _fmt_pct_points(procent_points)
    return f"{rec}, {_format_leader_value(points)} {_points_word(points)} ({ppct})"


def _model_prediction_line(game_id: int, esc_home: str) -> Optional[str]:
    """Строка модельной оценки победы хозяев для превью или None, если её нет.

    Читает `game_predictions` (Задача 22B) по точному `game_id`; строки там есть
    только у пары, прошедшей гейт качества, поэтому статус модели бот не проверяет.
    Запрос без кэша: публикация может удалить строку после провала гейта.
    """
    row = fetch_all(
        "SELECT probability FROM game_predictions WHERE game_id = %s AND task = 'home_win'",
        (game_id,),
        columns=["probability"],
    )
    if row["count_rows"] == 0:
        return None
    pct = round(float(row["probability"][0]) * 100)
    return f"🤖 Модельная оценка (не совет): победа {esc_home} — {pct}%"


def _fmt_signed(val: Union[int, float, None]) -> str:
    """Число со знаком («+1», «-0.5», «0») — разница шайб, чтобы минус не
    сливался с тире-разделителем колонок."""
    s = _fmt_num_max2(val)
    return f"+{s}" if val is not None and float(val) > 0 else s


def _matchup_compare_table(
    away_abbr: str,
    home_abbr: str,
    ra: Dict[str, Union[int, float, None]],
    rh: Dict[str, Union[int, float, None]],
) -> str:
    """Таблица превью матча `<pre>`: шапка «Сравнение команд: EDM VS VAN»,
    затем строки «• метрика: гости — хозяева» с тире в одной колонке —
    очки, баланс, форма и серия, личные встречи (если были), метрики сезона.
    """
    def diff(r: Dict[str, Union[int, float, None]]) -> Optional[float]:
        gf, ga = r["goals_per_game"], r["goals_against_per_game"]
        return None if gf is None or ga is None else round(float(gf) - float(ga), 2)

    tid_a = _team_id_for_abbrev(away_abbr)
    tid_h = _team_id_for_abbrev(home_abbr)
    tids = (tid_a, tid_h)
    form = [_last_n_form_record(t, 5) if t is not None else "—" for t in tids]
    streak = [_current_streak(t) if t is not None else "—" for t in tids]
    pairs: List[Tuple[str, str, str]] = [
        ("Очки", _format_leader_value(ra["points"]), _format_leader_value(rh["points"])),
        ("% очков", _fmt_pct_points(ra["procent_points"]), _fmt_pct_points(rh["procent_points"])),
        (
            "Баланс W-L-OT",
            _record_str(ra["wins"], ra["losses"], ra["ot"]),
            _record_str(rh["wins"], rh["losses"], rh["ot"]),
        ),
        ("Форма (5 игр)", form[0], form[1]),
        ("Серия", streak[0], streak[1]),
    ]
    h2h = _h2h_season_wins(tid_a, tid_h) if tid_a is not None and tid_h is not None else None
    if h2h:
        pairs.append(("Личные встречи", str(h2h[0]), str(h2h[1])))
    pairs += [
        ("Голы за игру", _fmt_num_max2(ra["goals_per_game"]), _fmt_num_max2(rh["goals_per_game"])),
        (
            "Пропущенные голы за игру",
            _fmt_num_max2(ra["goals_against_per_game"]),
            _fmt_num_max2(rh["goals_against_per_game"]),
        ),
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
        ("Броски за игру", _fmt_num_max2(ra["shots_per_game"]), _fmt_num_max2(rh["shots_per_game"])),
        (
            "Вбрасывания",
            _fmt_pct_stat(ra["face_off_win_percentage"]),
            _fmt_pct_stat(rh["face_off_win_percentage"]),
        ),
        ("Разница шайб за игру", _fmt_signed(diff(ra)), _fmt_signed(diff(rh))),
    ]
    rows = [["• Сравнение команд:", away_abbr, "—", home_abbr]]
    rows += [[f"• {label}:", left, "—", right] for label, left, right in pairs]
    lines = _aligned_columns(rows, "lrll")
    # «VS» шире тире: в колонке тире он раздвигал бы все строки двойным пробелом.
    lines[0] = lines[0].replace(" — ", " VS ", 1)
    return _pre_block(lines)


def matchup_season_preview(game_id: int, away_abbr: str, home_abbr: str) -> str:
    """
    Сезонное сравнение по teams.abbreviation (= abbrev из NHL API).

    `game_id` — игра из расписания: по нему в конец превью добавляется строка
    модельной оценки (если она опубликована; при раннем выходе, когда обеих
    команд нет в teams_stats, строки нет).

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

    stats = cached_fetch_all(
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
        rec_line = _team_record_line(
            row["wins"], row["losses"], row["ot"], row["points"], row["procent_points"]
        )
        return f"<b>{eab}</b> — {rec_line}"

    ra, rh = by_abbr.get(a), by_abbr.get(h)
    if ra is None and rh is None:
        return (
            header
            + f"Команд <b>{esc_a}</b> и <b>{esc_h}</b> нет в базе бота для этого сезона "
            f"({season_esc})."
        )

    parts: List[str] = [header.rstrip("\n")]
    if ra is None or rh is None:
        parts.append(_one_line_team_html(away_abbr, ra))
        parts.append(_one_line_team_html(home_abbr, rh))
    else:
        parts.append(_matchup_compare_table(a, h, ra, rh))

    prediction = _model_prediction_line(game_id, esc_h)
    if prediction:
        parts.append("")
        parts.append(prediction)

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
) -> Tuple[str, int, int]:
    """Страница лидерборда игроков/вратарей (HTML) плюс размер полной выборки.

    Зачем: страница отдаёт `LIMIT`/`OFFSET`-подмножество строк, но для маркера
    «показаны N из M» (Задача 11) вызывающему коду нужен и полный размер
    выборки — берём его окном `COUNT(*) OVER ()` в том же запросе, без
    отдельного похода в БД.

    Аргументы:
        name_stats: заголовок блока (пусто — без заголовка).
        table_name: таблица статистики, из белого списка `validate_table`.
        column_name: колонка сортировки, из белого списка `validate_column`.
        count: сколько строк вернуть (размер страницы).
        offset: сдвиг страницы.
        secondary_sort: вторичная сортировка при равенстве `column_name`.

    Строки — выровненная таблица в `<pre>` (читается на телефоне) с постоянным
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
    second_order = _resolve_secondary_sort(table_name, secondary_sort)
    validate_column(second_order)

    join_pss = _pss_join_sql(table_name)
    second_alias = _second_order_table_alias(table_name, second_order)
    pl_col = sql.SQL(".").join([sql.Identifier("pl"), sql.Identifier(column_name)])
    order_col = (
        sql.SQL(_MMSS_SECONDS_SQL).format(col=pl_col) if column_name in _MMSS_COLUMNS else pl_col
    )
    second_col = sql.SQL(".").join(
        [sql.Identifier(second_alias), sql.Identifier(second_order)]
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
        "WHERE pl.season_id = %s "
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
        name = _clip_cell(stats['lastname'][i] or "Unknown", _LEADER_NAME_WIDTH)
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

    # Конец сезона («1353/1483», «100.») не влезает в телефон — первым уходит
    # хвост (сейвы/смены), затем игры.
    body = _pre_table_fit(rows, "rllrrr", drop_order=(5, 4)) if stats['count_rows'] else ""
    if name_stats:
        body = f"<b>{html.escape(name_stats)}</b>\n\n{body}"
    return body, stats["count_rows"], total


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
    )[0]


def _rank_range_label(offset: int, rows: int) -> str:
    if rows <= 0:
        return "нет данных"
    lo = offset + 1
    hi = offset + rows
    if lo == hi:
        return str(lo)
    return f"{lo}–{hi}"


def _leaderboard_page_text(
    heading_html: str, body: str, n: int, total: int, offset: int
) -> Tuple[str, bool, bool]:
    """Общий хвост страницы лидерборда: шапка с маркером «Показаны N из M» и
    флаги перелистывания.

    Аргументы:
        heading_html: уже экранированный заголовок (без тегов).
        body: строки страницы; при `n == 0` заменяются текстом «нет данных».
        n: число строк на странице; total: размер полной выборки; offset: сдвиг.

    Возвращает: (текст, has_prev, has_next).
    """
    span = _rank_range_label(offset, n)
    if n == 0:
        body = "<i>Нет данных в этом диапазоне.</i>"
        marker = f"<i>{html.escape(span)}</i>"
    else:
        marker = truncation_marker(html.escape(span), total, item_word="строк")
    text = (
        f"<b>{heading_html}</b> ({html.escape(str(config.CURRENT_SEASON))}) "
        f"— {marker}\n\n{body}"
    )
    # total, а не «страница пришла полной» — иначе последняя полная страница
    # рисует кнопку «вперёд» на пустой диапазон, противореча маркеру.
    return text, offset > 0, offset + n < total


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
    body, n, total = player_stats_with_count(
        "",
        table_name,
        column_name,
        count=LEADERBOARD_PAGE_SIZE,
        offset=offset,
        secondary_sort=secondary_sort,
    )
    return _leaderboard_page_text(html.escape(heading), body, n, total, offset)


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
    row = cached_fetch_all(
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
    return f"{fv:5.1f}"


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
    return _TABLE_SEP.join([f"{nm:<{name_w}}", f"{pts:>3}", f"{gp:>3}", pct])


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
    hdr = _standings_header(name_w)
    lines.append(hdr)
    lines.append("-" * len(hdr))
    # Гонка за Wild Card — две путёвки и ближайшие претенденты, не вся конференция.
    for i, t in enumerate(remaining[:_WILD_CARD_SHOWN]):
        if i == 2:
            # Две путёвки Wild Card: ниже черты — претенденты, а не участники плей-офф.
            lines.append(_standings_cut_line(hdr))
        label = f"WC{i + 1}" if i < 2 else f"{i + 1:>2}."
        lines.append(label + _TABLE_SEP + row_fmt(t))
    return lines


# Строк в блоке Wild Card: WC1, WC2 и три ближайших претендента под чертой.
_WILD_CARD_SHOWN = 5


def _standings_cut_line(hdr: str) -> str:
    """Пунктир «линия плей-офф» шириной в шапку блока: выше — места в плей-офф."""
    return "- - линия плей-офф " + "- " * ((len(hdr) - 19) // 2)


def _standings_header(name_w: int) -> str:
    """Шапка блока таблицы; первые 4 символа — под колонку места (« 1.|», «WC1|»)."""
    return _TABLE_SEP.join(["   ", f"{'Команда':<{name_w}}", "Очк", "Игр", f"{'%очк':>5}"])


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

    # Секция — заголовок вплотную к своему блоку; между секциями пустая строка.
    def append_conference(title: str, div_order: Tuple[str, ...], wc_title: str) -> None:
        chunks.append(title_fmt(title))
        for div in div_order:
            label = _STANDINGS_DIV_LABEL.get(div, div.upper())
            div_teams = sorted(by_division.get(div) or [], key=_standings_sort_key)
            if div_teams:
                hdr = _standings_header(name_w)
                block_lines = [hdr, "-" * len(hdr)]
                for rank, t in enumerate(div_teams, start=1):
                    if rank == 4:
                        # Топ-3 дивизиона проходят в плей-офф напрямую.
                        block_lines.append(_standings_cut_line(hdr))
                    block_lines.append(f"{rank:>2}." + _TABLE_SEP + row_fmt(t))
            else:
                # Дивизион без единой сыгранной игры (частичный сезон,
                # Задача 36) — текст-причина вместо заголовков пустой
                # таблицы, тем же стилем, что и заглушка Wild Card ниже.
                block_lines = ["    (в дивизионе ещё никто не сыграл)"]
            chunks.append(title_fmt(label) + "\n" + block_fmt(block_lines))
        chunks.append(title_fmt(wc_title) + "\n" + block_fmt(_wild_card_lines(
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
    """Турнирная таблица всех команд текущего сезона (`config.SEASON_ID`):
    секции по конференциям/дивизионам, отсортированные по очкам, плюс секции
    Wild Card — HTML-текст с шапкой сезона и даты (шаблон
    `messages/league_table.txt`) для команды `/standings` (`bot.py`) и пункта
    «Турнирная таблица» диалога `/stats` (`stats_handlers.py`).

    Пустой `teams_stats` (сезон ещё не начался — загрузчик не пишет туда
    строк, пока в сезоне нет ни одной завершённой игры, см. Задача 36)
    отдаёт текст с причиной вместо таблицы с одними заголовками секций и
    без единой строки команды."""
    stats = cached_fetch_all(
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

    if stats['count_rows'] == 0:
        season_esc = html.escape(str(config.CURRENT_SEASON))
        return (
            f"<b>Турнирная таблица NHL</b> — сезон {season_esc}\n\n"
            "Сезон ещё не начался: в базе нет статистики команд."
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


# GROUP BY для сводок по конференциям/дивизионам (Задача 41, Фаза B) — ровно
# два статических варианта, не пользовательский ввод: `_team_group_summary()`
# принимает только эти литералы, третьего варианта нет и не планируется (YAGNI).
_CONFERENCE_GROUP_BY = "t.conference_name"
_DIVISION_GROUP_BY = "t.conference_name, t.division_name"

_GROUP_BY_COLUMNS = {
    _CONFERENCE_GROUP_BY: ["conference_name"],
    _DIVISION_GROUP_BY: ["conference_name", "division_name"],
}


def _team_group_summary(group_by: str, heading: str) -> str:
    """Общий скелет сводки по конференциям/дивизионам сезона `config.SEASON_ID`:
    число команд и средние командные метрики (`teams_stats` ⋈ `teams` по
    `(team_id, season_id)`) — очки на команду, голы за игру, % большинства/
    меньшинства. Только то, что уже лежит в `teams_stats` — без вычисляемых
    «рейтингов». Не дублирует турнирную таблицу `/standings` (`team_table()`): там
    строка на каждую команду, здесь — усреднение по группе. Строка группы —
    выровненная таблица в ширину телефона (`_pre_table_fit`: первой уходит число
    команд). Пустой сезон — текст с причиной, а не пустая таблица (Задача 36).

    Args:
        group_by: `_CONFERENCE_GROUP_BY` или `_DIVISION_GROUP_BY` — единственные
            допустимые значения (ключ `_GROUP_BY_COLUMNS`), подставляются в
            `GROUP BY`/`ORDER BY` как есть, т.к. это фиксированные литералы
            модуля, а не значения от пользователя. Имя группы в таблице —
            последняя колонка группировки (конференция или дивизион).
        heading: заголовок сообщения («Сводка по конференциям»/«...дивизионам»).
    """
    group_columns = _GROUP_BY_COLUMNS[group_by]
    season_esc = html.escape(str(config.CURRENT_SEASON))
    select_group = ", ".join(f"t.{col}" for col in group_columns)
    stats = cached_fetch_all(
        f"SELECT {select_group}, COUNT(*) AS team_count, "
        "AVG(ts.goals_per_game) AS avg_goals_per_game, "
        "AVG(ts.power_play_percentage) AS avg_power_play_percentage, "
        "AVG(ts.penalty_kill_percentage) AS avg_penalty_kill_percentage, "
        "AVG(ts.points) AS avg_points "
        "FROM teams_stats ts "
        "JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s "
        f"GROUP BY {group_by} "
        f"ORDER BY {group_by}",
        (config.SEASON_ID,),
        columns=group_columns + [
            "team_count", "avg_goals_per_game", "avg_power_play_percentage",
            "avg_penalty_kill_percentage", "avg_points",
        ],
    )
    if stats["count_rows"] == 0:
        return (
            f"<b>{heading}</b> ({season_esc})\n"
            "В базе нет командной статистики для этого сезона."
        )
    # Средние — один знак после запятой: с двумя «Metropolitan 91.38 3.08 21.46%
    # 79.88%» — 37 символов, шире экрана телефона.
    def avg(val: Union[int, float, Decimal, None], unit: str = "") -> str:
        return "—" if val is None else f"{float(val):.1f}{unit}"

    rows = [["", "Ком", "О", "Г/и", "Бол", "Мен"]] + [
        [
            (stats[group_columns[-1]][i] or "—").strip(),
            _format_leader_value(stats["team_count"][i]),
            avg(stats["avg_points"][i]),
            avg(stats["avg_goals_per_game"][i]),
            avg(stats["avg_power_play_percentage"][i], "%"),
            avg(stats["avg_penalty_kill_percentage"][i], "%"),
        ]
        for i in range(stats["count_rows"])
    ]
    return (
        f"<b>{heading}</b> ({season_esc})\n\n"
        + _pre_table_fit(rows, "lrrrrr", drop_order=(1,))
        + "\n<i>Средние по команде: О — очки, Г/и — голы за игру, Бол — реализация "
        "большинства, Мен — игра в меньшинстве; Ком — число команд.</i>"
    )


def conference_summary() -> str:
    """Сводка по конференциям — тонкая обёртка над `_team_group_summary()`
    с группировкой по конференции; см. докстринг `_team_group_summary()`."""
    return _team_group_summary(_CONFERENCE_GROUP_BY, "Сводка по конференциям")


def division_summary() -> str:
    """Сводка по дивизионам — тонкая обёртка над `_team_group_summary()` с
    группировкой по конференции+дивизиону (дивизион однозначно лежит в одной
    конференции, но `GROUP BY` требует явного столбца); см. докстринг
    `_team_group_summary()`."""
    return _team_group_summary(_DIVISION_GROUP_BY, "Сводка по дивизионам")


def season_team_abbrevs() -> List[str]:
    """Отсортированный список аббревиатур команд текущего сезона (`config.SEASON_ID`).

    Источник клавиатуры выбора команды профиля (Задача 41, Фаза D,
    `bot_team_profile_pick` в `stats_handlers.py`).
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


def team_list_text() -> str:
    """Ответ `/team`: команды сезона по дивизионам, каждая — кликабельная
    команда `/ABBR` (её разбирает `cmd_team_profile` в `bot.py`), открывающая
    `team_profile()`.

    В таблице `teams` сезона только команды, уже сыгравшие хотя бы матч
    (загрузчик строит её из `team/summary`), — в начале сезона список неполный.
    """
    season_esc = html.escape(str(config.CURRENT_SEASON))
    rows = cached_fetch_all(
        "SELECT trim(abbreviation), name, division_name FROM teams "
        "WHERE season_id = %s AND NULLIF(trim(abbreviation), '') IS NOT NULL "
        "ORDER BY division_name, name",
        (config.SEASON_ID,),
        columns=["abbr", "name", "division"],
    )
    if rows["count_rows"] == 0:
        return (
            f"<b>Команды сезона</b> ({season_esc})\n"
            "В базе пока нет списка команд для этого сезона."
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
        f"<b>Команды сезона</b> ({season_esc}) — нажмите на команду, чтобы открыть "
        "её статистику.\n\n" + "\n\n".join(blocks)
    )


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
    "<i>И — игры, Г — голы, П — передачи, О — очки, ВП — среднее время на льду за игру; "
    "вратари: В-П-ОТ — победы, поражения, поражения в овертайме, %ОБ — процент "
    "отражённых бросков, КН — коэффициент надёжности.</i>"
)


def _team_skaters(team_id: int, order_by: str, limit: int) -> Dict:
    """Полевые игроки клуба сезона (`rosters ⋈ players_season_stats` при
    `current_team_id = team_id`) в порядке `order_by` (`_BY_POINTS`/`_BY_TOI`);
    первые `limit`. Колонки — `_TEAM_SKATER_COLUMNS`."""
    return cached_fetch_all(
        "SELECT r.lastname, r.position, pss.games, pss.goals, pss.assists, pss.points, "
        "pss.plus_minus, pss.time_on_ice_per_game "
        "FROM rosters r "
        "JOIN players_season_stats pss "
        "  ON r.player_id = pss.player_id AND r.season_id = pss.season_id "
        f"WHERE r.current_team_id = %s AND r.season_id = %s ORDER BY {order_by} LIMIT %s",
        (team_id, config.SEASON_ID, limit),
        columns=_TEAM_SKATER_COLUMNS,
    )


def _skater_label(skaters: Dict, i: int) -> str:
    """«Forsling [D]» — фамилия и позиция i-й строки `_team_skaters()`."""
    pos = (skaters["position"][i] or "—").strip()
    return f"{skaters['lastname'][i] or 'Unknown'} [{pos}]"


def _team_goalies_table(team_id: int) -> str:
    """Вратари клуба сезона с хотя бы одной игрой — `<pre>`-таблица (игры,
    В-П-ОТ, %ОБ, КН), пустая строка, если таких нет."""
    goalies = cached_fetch_all(
        "SELECT r.lastname, gs.games, gs.wins, gs.losses, gs.ot, "
        "gs.save_percentage, gs.goal_against_average "
        "FROM rosters r "
        "JOIN goalies_season_stats gs "
        "  ON r.player_id = gs.player_id AND r.season_id = gs.season_id "
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
            _fmt_pct_stat(goalies["sv"][i]),
            _fmt_num_max2(goalies["gaa"][i]),
        ]
        for i in range(goalies["count_rows"])
    ]
    return _pre_table(rows, "lrrrr")

_TEAM_PROFILE_STATS_COLUMNS = [
    "team_id", "name", "short_name", "division_name", "conference_name", "games_played",
    "wins", "losses", "ot", "points", "procent_points", "goals_per_game",
    "goals_against_per_game", "shots_per_game", "shots_allowed",
    "power_play_percentage", "penalty_kill_percentage", "face_off_win_percentage",
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


def team_profile(abbrev: str) -> str:
    """Статистика клуба сезона `config.SEASON_ID`: место в дивизионе/конференции/
    лиге, баланс и форма, голы и броски за игру, спецбригады, лучшие бомбардиры
    и вратари.

    Зачем: ответ на `/BOS` из списка `/team` и экран «Профиль команды» меню
    `/stats` — главное о клубе одним сообщением. Источники: `teams_stats`
    (вся лига — ради мест), `rosters ⋈ players_season_stats` и
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
    season_esc = html.escape(str(config.CURRENT_SEASON))
    team_id = _team_id_for_abbrev(a)
    if team_id is None:
        return f"<b>{html.escape(a)}</b> ({season_esc})\nКоманда не найдена в базе для этого сезона."

    league = cached_fetch_all(
        "SELECT ts.team_id, t.name, t.short_name, t.division_name, t.conference_name, "
        "ts.games_played, ts.wins, ts.losses, ts.ot, ts.points, ts.procent_points, "
        "ts.goals_per_game, ts.goals_against_per_game, ts.shots_per_game, "
        "ts.shots_allowed, ts.power_play_percentage, ts.penalty_kill_percentage, "
        "ts.face_off_win_percentage "
        "FROM teams_stats ts "
        "JOIN teams t ON t.team_id = ts.team_id AND t.season_id = ts.season_id "
        "WHERE ts.season_id = %s",
        (config.SEASON_ID,),
        columns=_TEAM_PROFILE_STATS_COLUMNS,
    )
    rows = [
        {col: league[col][i] for col in _TEAM_PROFILE_STATS_COLUMNS}
        for i in range(league["count_rows"])
    ]
    if not any(r["team_id"] == team_id for r in rows):
        return (
            f"<b>{html.escape(a)}</b> ({season_esc})\n"
            "Команда ещё не сыграла в этом сезоне — статистики нет."
        )
    me, div_place, conf_place, league_place = _team_places(team_id, rows)

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
            "team_name": html.escape(me["name"] or a),
            "abbrev": html.escape(a),
            "season": season_esc,
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
            "gf": _fmt_num_max2(me["goals_per_game"]),
            "ga": _fmt_num_max2(me["goals_against_per_game"]),
            "sf": _fmt_num_max2(me["shots_per_game"]),
            "sa": _fmt_num_max2(me["shots_allowed"]),
            "pp": _fmt_pct_stat(me["power_play_percentage"]),
            "pk": _fmt_pct_stat(me["penalty_kill_percentage"]),
            "fo": _fmt_pct_stat(me["face_off_win_percentage"]),
            "scorers_table": _pre_table(scorer_rows, "llrrrr") if scorers["count_rows"] else "",
            "toi_table": _pre_table(toi_rows, "llrr") if toi_leaders["count_rows"] else "",
            "goalies_table": _team_goalies_table(team_id),
            "legend": _TEAM_STATS_LEGEND,
            "full_command": f"/{html.escape(a)}_FULL",
        },
    )


def team_full_stats(abbrev: str) -> str:
    """Ответ `/BOS_FULL`: все игроки клуба сезона `config.SEASON_ID` тремя
    таблицами — нападающие, защитники, вратари — с теми же колонками, что на
    экране страны (`_player_group_table`); ссылка на него стоит в конце
    `team_profile()`.

    Аргументы:
        abbrev: аббревиатура команды из команды `/ABBR_FULL`; неизвестная —
            текст с причиной.
    """
    a = abbrev.strip().upper()
    season_esc = html.escape(str(config.CURRENT_SEASON))
    team_id = _team_id_for_abbrev(a)
    if team_id is None:
        return f"<b>{html.escape(a)}</b> ({season_esc})\nКоманда не найдена в базе для этого сезона."
    parts = [f"<b>{html.escape(a)}: все игроки</b> · сезон {season_esc}"]
    for group, (label, _cond) in PLAYER_GROUPS.items():
        table, n, _total = _player_group_table(_BY_TEAM, team_id, group, None, 0, ranked=False)
        if n:
            parts += ["", f"<b>{label}</b>", table, f"<i>{_GROUP_LEGEND[group]}.</i>"]
    if len(parts) == 1:
        return parts[0] + "\n\nУ команды пока нет статистики игроков в этом сезоне."
    parts += ["", f"Профиль клуба: /{html.escape(a)}"]
    return "\n".join(parts)


# Задача 42: страна игрока — rosters.nationality (трёхбуквенный код NHL, одна на
# игрока). Справочник покрывает все коды, встречающиеся в БД за все сезоны;
# неизвестный код показывается как есть.
COUNTRY_LABELS: Dict[str, str] = {
    "CAN": "🇨🇦 Канада", "USA": "🇺🇸 США", "SWE": "🇸🇪 Швеция", "RUS": "🇷🇺 Россия",
    "FIN": "🇫🇮 Финляндия", "CZE": "🇨🇿 Чехия", "CHE": "🇨🇭 Швейцария",
    "DEU": "🇩🇪 Германия", "SVK": "🇸🇰 Словакия", "DNK": "🇩🇰 Дания",
    "BLR": "🇧🇾 Беларусь", "LVA": "🇱🇻 Латвия", "AUT": "🇦🇹 Австрия",
    "FRA": "🇫🇷 Франция", "GBR": "🇬🇧 Великобритания", "NOR": "🇳🇴 Норвегия",
    "AUS": "🇦🇺 Австралия", "NLD": "🇳🇱 Нидерланды", "BGR": "🇧🇬 Болгария",
    "SVN": "🇸🇮 Словения", "UZB": "🇺🇿 Узбекистан", "KAZ": "🇰🇿 Казахстан",
    "POL": "🇵🇱 Польша", "BEL": "🇧🇪 Бельгия", "EST": "🇪🇪 Эстония",
}
# Страна с меньшим числом полевых игроков сезона в рейтинг и на кнопки не
# попадает: у 1–2 игроков «сумма очков страны» — это просто их личная статистика.
COUNTRY_MIN_PLAYERS = 3
_COUNTRY_NOT_FOUND = "Страна не найдена в рейтинге этого сезона."
_COUNTRY_FROM = (
    "FROM players_season_stats p "
    "JOIN rosters r ON p.player_id = r.player_id AND p.season_id = r.season_id "
)


def _country_label(code: str) -> str:
    """Метка страны для HTML: флаг + русское название из `COUNTRY_LABELS`,
    неизвестный код — сам код; результат экранирован."""
    return html.escape(COUNTRY_LABELS.get(code, code))


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
    игроков меньше `COUNTRY_MIN_PLAYERS` не показываются. Страна — поле
    nationality ростера NHL (одна на игрока).

    Возвращает: (текст, коды стран в порядке рейтинга — только прошедшие порог).
    Пустой сезон — текст с причиной и пустой список кодов (Задача 36).
    """
    season_esc = html.escape(str(config.CURRENT_SEASON))
    ranking = _country_ranking()
    if not ranking:
        return "В базе нет данных по странам для этого сезона.", []
    unknown = cached_fetch_all(
        "SELECT COUNT(*) " + _COUNTRY_FROM
        + "WHERE p.season_id = %s AND r.position <> 'G' AND r.nationality IS NULL",
        (config.SEASON_ID,),
        columns=["n"],
    )["n"][0]
    rows = [["", "Страна", "Игр", "О", "Г", "О/и"]] + [
        [f"{i}.", COUNTRY_LABELS.get(code, code), str(players), str(points), str(goals),
         f"{points / players:.1f}"]
        for i, (code, players, points, goals) in enumerate(ranking, start=1)
    ]
    text = (
        f"<b>Рейтинг стран</b> ({season_esc})\n"
        f"<i>Полевые игроки; страна — поле nationality ростера NHL (одна на игрока); "
        f"страны от {COUNTRY_MIN_PLAYERS} игроков. Игр — игроков, О — очки, Г — голы, "
        f"О/и — очков на игрока.</i>\n\n"
        # Наименее важное — голы, затем очки на игрока.
        + _pre_table_fit(rows, "rlrrrr", drop_order=(4, 5))
    )
    if unknown:
        text += f"\n\n<i>Страна не указана в данных NHL у игроков: {unknown} (не учтены).</i>"
    return text, [code for code, _, _, _ in ranking]


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
    "WHERE g.season_id = %s AND {owner} AND g.games > 0 "
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
) -> Tuple[str, int, int]:
    """Выровненная `<pre>`-таблица игроков одной группы — общая для экрана страны
    и `/BOS_FULL`.

    Зачем: у каждой группы свои главные показатели — нападающие: голы, очки,
    игры, время, силовые, броски; защитники — то же с блоками вместо бросков;
    вратари (с хотя бы одной игрой): победы, %ОБ, КН, игры, сухие. Не влезающие
    в ширину телефона колонки убираются с наименее важной (`_pre_table_fit`).

    Аргументы:
        owner: `_BY_COUNTRY` или `_BY_TEAM` — чьи игроки.
        owner_value: код страны или `team_id`; в SQL — только параметром.
        group: ключ `PLAYER_GROUPS` (F / D / G).
        limit, offset: страница выборки; `limit=None` — все строки.
        ranked: колонки места и команды (экран страны); без них — `/BOS_FULL`.

    Возвращает: (таблица — пусто при 0 строк, строк в таблице, полный размер выборки).
    """
    params = (config.SEASON_ID, owner_value, limit, offset)
    fmt = _format_leader_value
    if group == "G":
        cols = ["lastname", "team", "wins", "sv", "gaa", "games", "shutouts", "total"]
        stats = cached_fetch_all(_GROUP_GOALIES_SQL.format(owner=owner), params, columns=cols)
        name_header, header = "Вратарь", ["В", "%ОБ", "КН", "И", "Сух"]
        drop_order: Tuple[int, ...] = (4, 2, 3)  # сухие, КН, игры — игры важнее (Задача 50)
        cells = [
            [fmt(stats["wins"][i]), _fmt_pct_stat(stats["sv"][i]),
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
        # Важность — из отзыва: голы, очки, игры, время, силовые; броски/блоки — дополнение.
        drop_order = (5, 4, 3)
        cells = [
            [fmt(stats[k][i]) for k in ("goals", "points", "games", "toi", "hits", extra_key)]
            for i in range(stats["count_rows"])
        ]
    n = stats["count_rows"]
    if not n:
        return "", 0, 0

    def prefix(i: int) -> List[str]:
        name = _clip_cell(stats["lastname"][i] or "Unknown", _LEADER_NAME_WIDTH)
        return [f"{offset + i + 1}.", name, stats["team"][i] or "—"] if ranked else [name]

    lead = ["", name_header, "Ком"] if ranked else [name_header]
    rows = [lead + header] + [prefix(i) + cells[i] for i in range(n)]
    align = ("rll" if ranked else "l") + "r" * len(header)
    table = _pre_table_fit(rows, align, [len(lead) + c for c in drop_order])
    return table, n, int(stats["total"][0])


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
        return _COUNTRY_NOT_FOUND, False, False
    offset = max(offset, 0)
    table, n, total = _player_group_table(
        _BY_COUNTRY, code, group, LEADERBOARD_PAGE_SIZE, offset, ranked=True
    )
    body = f"{table}\n<i>{_GROUP_LEGEND[group]}.</i>"
    heading = f"{PLAYER_GROUPS[group][0]}: {_country_label(code)}"
    return _leaderboard_page_text(heading, body, n, total, offset)


def team_stats_with_count(
    name_stats: str,
    column_name: str,
    count: int = 10,
    offset: int = 0,
) -> Tuple[str, int, int]:
    """Страница лидерборда команд (HTML) плюс размер полной выборки.

    Зачем: как `player_stats_with_count` — маркер «показаны N из M» (Задача 11)
    нуждается в полном размере выборки, который берём окном `COUNT(*) OVER ()`
    в этом же запросе, а не отдельным `SELECT count(*)`.

    Аргументы: заголовок (пусто — без заголовка), колонка сортировки (из белого
    списка `validate_column`), размер и сдвиг страницы.

    Строки — выровненная таблица в `<pre>` с постоянным «хвостом» уже
    загруженных полей (Задача 18): игры, баланс `wins`-`losses`-`ot` и `points`
    из `teams_stats`. Команда — аббревиатурой: полное имя не помещается в
    строку на экране телефона. Сортировка от хвоста не меняется.

    Возвращает: (текст, число строк на странице, полный размер выборки).
    """
    validate_column(column_name)
    if offset < 0:
        offset = 0

    q = sql.SQL(
        "SELECT t.abbreviation, {col}, games_played, wins, losses, ot, points, "
        "COUNT(*) OVER () AS total "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s "
        "ORDER BY {col} DESC NULLS LAST, t.abbreviation "
        "LIMIT %s OFFSET %s"
    ).format(col=sql.Identifier(column_name))
    stats = cached_fetch_all(
        q,
        (config.SEASON_ID, count, offset),
        columns=[
            'team', 'points', 'games_played',
            'wins', 'losses', 'ot', 'record_points', 'total',
        ],
    )

    total = int(stats['total'][0]) if stats['count_rows'] else 0

    rows: List[List[str]] = [["", "Ком", STAT_COLUMN_LABELS[column_name], "И", "В-П-ОТ", "О"]]
    for i in range(stats['count_rows']):
        rows.append([
            f"{offset + i + 1}.",
            (stats['team'][i] or "—").strip(),
            _format_leader_value(stats['points'][i]),
            _format_leader_value(stats['games_played'][i]),
            _record_str(stats['wins'][i], stats['losses'][i], stats['ot'][i]),
            _format_leader_value(stats['record_points'][i]),
        ])

    body = _pre_table(rows, "rlrrrr") if stats['count_rows'] else ""
    if name_stats:
        body = f"<b>{html.escape(name_stats)}</b>\n\n{body}"
    return body, stats["count_rows"], total


def team_stat_leaderboard_page(
    heading: str,
    column_name: str,
    offset: int,
) -> Tuple[str, bool, bool]:
    """Топ команд по одной колонке teams_stats."""
    body, n, total = team_stats_with_count(
        "",
        column_name,
        count=LEADERBOARD_PAGE_SIZE,
        offset=offset,
    )
    return _leaderboard_page_text(html.escape(heading), body, n, total, offset)


# Москва живёт в UTC+3 без перехода на летнее время (с 2014 года) — фиксированный
# сдвиг вместо zoneinfo, которому в slim-образе может не хватить tzdata.
_MSK = timezone(timedelta(hours=3))


def last_night_day(nights_back: int = 0) -> str:
    """Игровой день NHL (`games.day`, дата по Северной Америке) матчей, сыгранных
    прошедшей ночью по Москве.

    Зачем: матчи игрового дня D идут в Москве ночью на D+1, поэтому «сегодня»
    для пользователя из Москвы — это вчерашняя дата NHL, а календарное
    `date.today()` сервера показывало бы ещё не сыгранные матчи.

    Аргументы:
        nights_back: 0 — прошедшая ночь, 1 — ночь перед ней.
    """
    return (datetime.now(_MSK).date() - timedelta(days=1 + nights_back)).isoformat()


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


# Пробел шириной в цифру: им сдвинут счёт по периодам под строкой матча —
# обычные пробелы в начале строки клиенты Telegram могут съесть.
_FIGURE_SPACE = "\u2007"

# Между матчами сводки — пустая строка: подряд идущие пары строк сливались.
DIGEST_GAME_SEPARATOR = "\n\n"


def day_digest_summary_body(game_ids: Sequence[int]) -> List[str]:
    """Сжатая сводка дайджеста: по элементу на матч, номер совпадает с номером
    на кнопке матча (`digest_game_button_labels`).

    Зачем так: матч — «1. <b>Хозяева 3:2 Гости</b>» обычным шрифтом по левому
    краю, счёт по периодам — строкой ниже с отступом. Моноширинное выравнивание
    команд по счёту на телефоне выглядело криво (отзыв 2026-10-03), а в одной
    строке с командами счёт по периодам не влезает в ширину экрана.

    Возвращает: по элементу на матч (две строки через `\n`); элементы склеиваются
    через `DIGEST_GAME_SEPARATOR`, обрезка под лимит Telegram
    (`digest_shown_match_count`) отбрасывает матч целиком.

    Аргументы:
        game_ids: матчи дня в порядке сводки, непустой список.
    """
    headers = [_game_score_header(*_fetch_game_score_rows(gid)) for gid in game_ids]
    return [
        f"{i}. <b>{html.escape(h['home'].strip())} {html.escape(h['home_score'])}:"
        f"{html.escape(h['away_score'])} {html.escape(h['away'].strip())}</b>\n"
        f"{_FIGURE_SPACE * 2}{html.escape(h['period_scores'] + h['extra'])}"
        for i, h in enumerate(headers, start=1)
    ]


def digest_game_button_labels(game_ids: Sequence[int]) -> List[str]:
    """Подписи кнопок матчей сводки дайджеста: «1. PHI – PIT» (хозяева первыми,
    как в шапке карточки).

    Зачем: на кнопке «Матч N» не видно, какие команды играли. Одним запросом
    на все матчи дня, а не по запросу на кнопку.

    Аргументы:
        game_ids: матчи в порядке сводки (`day_digest_summary_body`).
    """
    rows = fetch_all(
        "SELECT g.game_id, th.abbreviation, ta.abbreviation FROM games g "
        "JOIN teams th ON th.team_id = g.home_team_id AND th.season_id = g.season_id "
        "JOIN teams ta ON ta.team_id = g.away_team_id AND ta.season_id = g.season_id "
        "WHERE g.game_id = ANY(%s)",
        (list(game_ids),),
        columns=["game_id", "home", "away"],
    )
    matchup = {
        rows["game_id"][i]: f"{(rows['home'][i] or '?').strip()} – {(rows['away'][i] or '?').strip()}"
        for i in range(rows["count_rows"])
    }
    return [f"{n}. {matchup[gid]}" for n, gid in enumerate(game_ids, start=1)]


def truncation_marker(
    shown: Optional[Union[int, str]] = None,
    total: Optional[int] = None,
    *,
    item_word: str = "",
) -> str:
    """Единственная точка, где собирается курсивная HTML-сноска об усечении.

    Зачем: обрезка по лимиту Telegram (`truncate_telegram_text`) и обрезка
    выборки (страницы лидербордов, список аббревиатур команд) не должны каждая
    заново собирать текст сноски — иначе формат расползается по веткам
    (Задача 11).

    Экранирование: `shown` вставляется в HTML как есть — экранировать его
    (если это строка из непроверенного источника) обязана вызывающая сторона
    до передачи сюда, как делают лидерборды (`html.escape(span)`).

    Аргументы:
        shown: сколько показано — число или диапазон («1–10»); None — сноска
            без чисел, для обрезки прозаического текста, где считать элементы
            нечем (нет счётных элементов, N/M выдумывать не из чего).
        total: всего элементов в исходной выборке; обязателен, если задан
            `shown` — иначе неоднозначно, что означает «показаны N из ничего».
        item_word: существительное в родительном падеже множественного числа
            («строк», «матчей», «команд»); пусто, если числа сами по себе ясны.

    Исключения:
        ValueError: если `shown` задан, а `total` — нет.
    """
    if shown is None:
        return "<i>Текст обрезан (лимит Telegram).</i>"
    if total is None:
        raise ValueError("truncation_marker: total обязателен, если задан shown")
    word = f" {item_word}" if item_word else ""
    return f"<i>Показаны {shown} из {total}{word}.</i>"


def _telegram_cut_budget(
    note: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> int:
    """Сколько символов исходного текста остаётся под обрезку `truncate_telegram_text`
    при данной сноске `note`.

    Зачем: `digest_shown_match_count` предсказывает результат
    `truncate_telegram_text` заранее (чтобы посчитать N для маркера), поэтому
    обе функции обязаны считать бюджет одной и той же формулой — раздельные
    копии рассинхронизируются при правке одной из них и заставят маркер
    молча врать про число показанных элементов.

    Аргументы:
        note: сноска, которая будет дописана после обрезки (с учётом
            разделителя "\\n\\n", если он нужен).
        max_len: лимит символов сообщения (по умолчанию
            `TELEGRAM_MAX_MESSAGE_LENGTH`).
    """
    cut = max_len - len(note) - 3
    return max(cut, 80)


def digest_shown_match_count(header: str, lines: List[str], total: int) -> int:
    """Сколько заголовков матчей из `lines` уместится в сводке дайджеста после
    обрезки `truncate_telegram_text` по лимиту Telegram.

    Зачем: обрезка режет текст по символам, а не по заголовкам матчей, поэтому
    число реально показанных матчей для маркера «показаны N из M» (Задача 11)
    нужно подобрать отдельно — иначе сноска солжёт о том, сколько матчей
    осталось видно.

    Аргументы:
        header: неизменяемая часть сводки перед списком заголовков матчей
            (включает завершающие переносы строк).
        lines: заголовки матчей, по одному на матч (может быть многострочным),
            в порядке отображения.
        total: всего матчей в выборке дня — M в маркере.
    """
    shown = len(lines)
    while shown > 0:
        note = "\n\n" + truncation_marker(shown, total, item_word="матчей")
        cut = _telegram_cut_budget(note)
        prefix_len = len(header) + len(DIGEST_GAME_SEPARATOR.join(lines[:shown]))
        if prefix_len <= cut:
            return shown
        shown -= 1
    return 0


def truncate_telegram_text(
    text: str,
    max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    *,
    footer_note: Optional[str] = None,
) -> str:
    """Режет `text` под лимит Telegram (по умолчанию 4096), добавляя сноску.

    Зачем: сообщения Telegram не долетают за лимитом символов; резать нужно с
    запасом под сноску и добавлять её саму, а не отправлять оборванный текст
    без предупреждения.

    Аргументы:
        text: исходный текст сообщения.
        max_len: лимит символов (по умолчанию `TELEGRAM_MAX_MESSAGE_LENGTH`).
        footer_note: сноска, дописываемая после обрезки; по умолчанию — общая
            сноска о лимите Telegram без чисел (`truncation_marker()`).
    """
    if len(text) <= max_len:
        return text
    note = footer_note if footer_note is not None else ("\n\n" + truncation_marker())
    cut = _telegram_cut_budget(note, max_len)
    return text[:cut] + "..." + note
