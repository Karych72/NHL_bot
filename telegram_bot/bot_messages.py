"""Сборка текстов и HTML-разметки для сообщений бота: карточки матчей,
дайджест дня, лидерборды игроков/команд, турнирная таблица.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`) и
превращает строки в готовый Telegram-текст: построчный HTML с таблицами
`_table`, который `rich_message()` превращает в rich-сообщение (Bot API 10.1+);
сама отправка, листание страниц и клавиатуры живут в `stats_handlers.py`.
"""

import html
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Collection, Dict, List, Optional, Sequence, Tuple, Union

from aiogram.types import InputRichMessage
from psycopg2 import sql

import config
from database import cached_fetch_all, fetch_all, validate_table, validate_column
from leaderboard_specs import STAT_COLUMN_LABELS
from template_funcs import output_text

# Лимит текста rich-сообщения Telegram (символов). Считается по исходному HTML: он не
# короче видимого текста, так что сообщение в лимите заведомо проходит. Второй лимит —
# 500 блоков (абзац, строка таблицы) — обрезкой не охраняется: самый длинный экран
# (/standings, ~70 блоков) далёк от него, а длинные списки листаются страницами.
RICH_MESSAGE_MAX_LENGTH = 32768


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


def _table(
    rows: Sequence[Sequence[str]],
    align: str,
    *,
    playoff_line_after: Optional[int] = None,
    bold: Collection[Tuple[int, int]] = (),
) -> str:
    """Таблица rich-сообщения (Bot API 10.3): первая строка `rows` — шапка, остальные — тело.

    Зачем: колонки rich-таблицы Telegram раскладывает сам по ширине экрана — без
    моноширинного `<pre>`, выравнивания пробелами и выбрасывания колонок под телефон.
    `compact` — меньшие отступы в ячейках; `striped` (чередование строк) — только у
    таблиц длиннее `_STRIPED_MIN_ROWS` строк.
    Таблица — одна строка текста: `rich_message()` делает её отдельным блоком.

    Аргументы:
        rows: ячейки (сырые строки, без HTML); у всех строк одно число колонок.
        align: по символу на колонку — `l` (влево) или `r` (вправо).
        playoff_line_after: после стольких строк тела — пунктир `_PLAYOFF_LINE` на всю
            ширину (турнирная таблица: топ-3 дивизиона, две путёвки Wild Card).
        bold: ячейки тела жирным — пары (строка тела, колонка), с нуля; то, на что
            экран отвечает (лучшее значение превью матча).
    """
    def row(cells: Sequence[str], tag: str, r: int = -1) -> str:
        def cell(c: str, a: str, col: int) -> str:
            text = f"<b>{html.escape(c)}</b>" if (r, col) in bold else html.escape(c)
            return f'<{tag} align="right">{text}</{tag}>' if a == "r" else f"<{tag}>{text}</{tag}>"

        return "<tr>" + "".join(cell(c, a, col) for col, (c, a) in enumerate(zip(cells, align))) + "</tr>"

    head, *body = rows
    parts = [row(head, "th")]
    for i, cells in enumerate(body):
        if i == playoff_line_after:
            parts.append(f'<tr><td colspan="{len(align)}" align="center">{_PLAYOFF_LINE}</td></tr>')
        parts.append(row(cells, "td", i))
    attrs = "compact striped" if len(body) > _STRIPED_MIN_ROWS else "compact"
    return f"<table {attrs}>" + "".join(parts) + "</table>"


# Линия плей-офф: рамок у отдельных строк rich-таблица не знает, поэтому черта — строка
# пунктира на всю ширину; длина — чтобы не раздвигать таблицу на телефоне.
_PLAYOFF_LINE = "┄" * 28

# Полосы — только у таблиц длиннее десятка строк: на длинной они ведут глаз по строке,
# на короткой — лишний шум (spike Задачи 88, `docs/telegram_bot.md` §«Стиль сообщений»).
_STRIPED_MIN_ROWS = 10

# Видимый зазор между абзацами: граница `<p>` в клиентах Telegram отступа не даёт,
# а пустой `<p></p>` схлопывается (spike Задачи 88).
_PARAGRAPH_GAP = "<p>&nbsp;</p>"


# Строки-блоки, которые `rich_message()` не вкладывает в абзац: таблица, свёрнутый блок,
# список, подвал — каждый собран своим хелпером в одну строку.
_BLOCK_TAGS = ("<table", "<details", "<ul", "<footer")


def _rich_html(text: str) -> str:
    """Построчный HTML → rich-HTML блоков; правила — у `rich_message()`."""
    blocks: List[str] = []
    paragraph: List[str] = []
    gap = False

    def flush() -> None:
        nonlocal gap
        if not paragraph:
            return
        if gap and blocks and blocks[-1].startswith("<p>"):
            blocks.append(_PARAGRAPH_GAP)
        blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
        paragraph.clear()
        gap = False

    for line in text.split("\n"):
        if line.startswith(_BLOCK_TAGS):
            flush()
            blocks.append(line)
        elif line.strip():
            paragraph.append(line)
        else:
            flush()
            gap = True
    flush()
    return "".join(blocks)


def rich_message(text: str) -> InputRichMessage:
    """Текст бота → rich-сообщение (Bot API 10.1): блок строк — абзац `<p>` с переносами
    `<br>`, пустая строка — граница абзацев с видимым зазором, строка-блок (`_table`,
    `rich_details`, `rich_list`, `rich_footer`) — свой блок.

    Зачем: в rich-HTML перевод строки — пробел, поэтому строки соединяются `<br>`;
    пустые строки, которыми шаблоны и сборщики делят сообщение на разделы, остаются
    зазором. Тег не должен пересекать пустую строку: блоки — законченные фрагменты
    разметки, иначе Telegram отвергнет сообщение.

    Аргументы: `text` — построчный HTML, как его собирают функции этого модуля.
    """
    return InputRichMessage(html=_rich_html(text))


def rich_footer(text: str) -> str:
    """Подвал сообщения — строка-блок `<footer>` для конца текста `rich_message()`.

    Зачем: служебный хвост (ссылки на настройки и соседние экраны, сезон, «данные на …»)
    клиенты рисуют мельче и серее содержания (`docs/telegram_bot.md` §«Стиль сообщений»,
    правило 3).

    Аргументы: `text` — построчный HTML подвала; строки соединяются `<br>`. Подвал без
    строк (превью матча, команды которого не сопоставлены) — пустая строка, не `<footer></footer>`.
    """
    lines = [line for line in text.split("\n") if line.strip()]
    return "<footer>" + "<br>".join(lines) + "</footer>" if lines else ""


def rich_details(summary: str, text: str) -> str:
    """Свёрнутый блок `<details>` — строка-блок для текста `rich_message()`.

    Зачем: легенды обозначений и расшифровки метрик нужны не при каждом открытии экрана —
    по умолчанию свёрнуты, кто не знает — раскроет (правило 4 стиля).

    Аргументы:
        summary: подпись свёрнутого блока (уже экранированная).
        text: построчный HTML содержимого — по тем же правилам, что у `rich_message()`.
    """
    return f"<details><summary>{summary}</summary>{_rich_html(text)}</details>"


def _season_footer() -> str:
    """Подвал справочного экрана, у которого из служебного — только сезон."""
    return rich_footer(f"Сезон {html.escape(str(config.CURRENT_SEASON))}")


def rich_list(items: Sequence[str]) -> str:
    """Маркированный список `<ul>` — строка-блок для текста `rich_message()`.

    Зачем: перечень команд `/help` — список с отступом у переносов длинных пунктов,
    а не строки абзаца.

    Аргументы: `items` — пункты, уже готовый инлайн-HTML.
    """
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


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
    """Одна строка блока «Звёзды матча»: `1. Kaprizov (MIN) — 1+0` для полевого
    игрока, `1. Thompson (VGK) — 33/34, 97.1%` для вратаря (номер звезды — без «★»:
    символы в теле сообщения не ставим, `docs/telegram_bot.md` §«Стиль сообщений»). Различение — по
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

    line = f"{star_str}. {name} ({abbr})"
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


def _score_line(header: Dict[str, str]) -> str:
    """HTML-строка счёта «🏒 <b>Хозяева 4:2 Гости</b> (OT)» из `_game_score_header`:
    первая строка карточки матча и шапка сообщения подписки на страну."""
    return (
        f"🏒 <b>{html.escape(header['home'])} {html.escape(header['home_score'])}:"
        f"{html.escape(header['away_score'])} {html.escape(header['away'])}</b>"
        f"{html.escape(header['extra'])}"
    )


def _period_names(period: int, shootout: bool) -> Tuple[str, str]:
    """Название периода гола в карточке матча: (заголовок блока голов, коротко
    для кнопки видео) — «2-й период»/«2п», «Овертайм»/«ОТ», «2-й овертайм»/«2ОТ»,
    «Буллиты»/«Б».

    Аргументы:
        period: номер периода из `get_goals_game`; в регулярке 4 — овертайм,
            5 — серия буллитов (победный буллит лежит голом с временем 00:00).
        shootout: матч решён буллитами — иначе период 5 значит второй овертайм
            (плей-офф).
    """
    if shootout and period == 5:
        return "Буллиты", "Б"
    if period <= 3:
        return f"{period}-й период", f"{period}п"
    overtime = period - 3
    if overtime == 1:
        return "Овертайм", "ОТ"
    return f"{overtime}-й овертайм", f"{overtime}ОТ"


def game_message(game_id: int) -> Tuple[str, List[Dict]]:
    """Return (rendered_text, goal_video_metadata)."""
    game_stats, game_goals = _fetch_game_score_rows(game_id)
    header = _game_score_header(game_stats, game_goals)
    # is_shootouts читается только при is_overtime — как в `_game_score_header`.
    shootout = bool(game_stats['is_overtime'][0] and game_stats['is_shootouts'][0])
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

    # Голы обычным текстом по периодам: заголовок периода, строка гола со временем
    # внутри периода, ассистенты строкой ниже. Не `<pre>`: моноширинные колонки
    # на телефоне разъезжались от длинной фамилии или крупного шрифта.
    goal_lines: List[str] = []
    goals_meta = []
    scorer_counts: Dict[str, int] = defaultdict(int)
    prev_period = None
    for i, (h, a) in enumerate(_running_scores(game_goals)):
        p = game_goals['period'][i]
        title, short = _period_names(p, shootout)
        if p != prev_period:
            if goal_lines:
                goal_lines.append("")
            goal_lines.append(f"<b>{title}</b>")
            prev_period = p
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

        # Время внутри периода; у буллита (всегда 00:00) его нет.
        time_str = "" if shootout and p == 5 else game_goals['goal_time'][i]
        assist_names = [
            _goal_player_name(game_goals[key][i], game_goals[f"{key}_nationality"][i])
            for key in ("assist_1", "assist_2")
            if game_goals[key][i] is not None
        ]
        score = f"{h}:{a}"
        time_prefix = f"{time_str} · " if time_str else ""
        goal_lines.append(
            f"{time_prefix}{score} — "
            f"{html.escape(scorer + (f' [{pos}]' if pos else '') + situation)}"
        )
        if assist_names:
            goal_lines.append(html.escape(", ".join(assist_names)))

        evt = game_goals['goal_event_id'][i]
        if evt is not None:
            goals_meta.append({
                'game_id': game_goals['goal_game_id'][i],
                'event_id': evt,
                'label': f"▶ {score} {scorer} {short} {time_str}".rstrip(),
            })
    goals_block = "\n".join(goal_lines)

    hat_lines = [
        f"Хет-трик: {html.escape(name)} (×{n})"
        for name, n in scorer_counts.items()
        if n >= 3
    ]
    hat_tricks = "\n".join(hat_lines) if hat_lines else ""

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
        'score_line': _score_line(header),
        'home_shots': html.escape(str(game_stats['shots'][0])),
        'away_shots': html.escape(str(game_stats['shots'][1])),
        'home_penalties': html.escape(str(game_stats['pim'][0])),
        'away_penalties': html.escape(str(game_stats['pim'][1])),
        'goals_block': goals_block,
        'goalkeepers': "\n".join(goalie_lines),
        'period_scores': html.escape(header['period_scores']),
        'hat_tricks': hat_tricks,
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

    Зачем: экраны, где команда известна только кодом (прогноз модели, подпись к видео
    гола), пишут её полным названием (Задача 98). У обоих команда
    уже есть в `teams` (матч или клуб из БД), поэтому её отсутствие — ошибка данных,
    а не случай для показа кода.

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

    Возвращает: список `"W"`/`"L"`/`"OTL"` длиной `min(limit, сыграно игр)`;
    пустой список, если в текущем сезоне у команды нет завершённых игр.
    """
    row = cached_fetch_all(
        "SELECT winner_id, is_overtime, is_shootouts, " + _OT_EMPTY_NET_WIN_SQL
        + "FROM games g WHERE season_id = %s AND winner_id IS NOT NULL "
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


def _plural_word(value: Union[int, float, Decimal, None], forms: Tuple[str, str, str]) -> str:
    """Слово, согласованное с числом (1 очко, 2 очка, 5 очков): `forms` — формы
    для 1, 2 и 5; `None` считается нулём."""
    n = int(value or 0)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


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
    word = _plural_word(points, ("очко", "очка", "очков"))
    return f"{rec}, {_format_leader_value(points)} {word} ({ppct})"


# Вместо оценки модели бесплатному чату (Задача 97B): строка того же подвала.
MODEL_PREDICTION_TEASER = "🔒 Прогноз модели — в премиуме: /premium"


def with_model_prediction(footer: str, game_id: int, home_abbr: str) -> str:
    """Подвал превью (`matchup_season_preview`) со строкой оценки модели: победа хозяев и тотал
    больше 5.5; прогноза нет — подвал как есть.

    Зачем: прогноз — только премиуму (Задача 96), поэтому строку добавляет вызывающий, знающий
    чат, а не общее превью (у рассылки оно одно на все чаты). Читает `game_predictions`
    (Задачи 22B, 73) по точному `game_id`. Публикация пишет строки только для игр, где у обеих
    команд не меньше 5 игр в сезоне, и только для победы, прошедшей гейт, — поэтому ни сезон,
    ни статус модели бот не проверяет; каждой части строки может не быть отдельно. Запрос без
    кэша: ежедневная публикация заменяет строки.

    Аргументы:
        footer: подвал превью.
        game_id: матч из расписания.
        home_abbr: аббревиатура хозяев, как в превью; в строке — полное название.
    """
    esc_home = html.escape(team_name_for_abbrev(home_abbr))
    rows = fetch_all(
        "SELECT task, probability FROM game_predictions "
        "WHERE game_id = %s AND task IN ('home_win', 'over_5_5')",
        (game_id,),
        columns=["task", "probability"],
    )
    pct = {task: round(float(p) * 100) for task, p in zip(rows["task"], rows["probability"])}
    parts = []
    if "home_win" in pct:
        parts.append(f"победа {esc_home} — {pct['home_win']}%")
    if "over_5_5" in pct:
        parts.append(f"тотал больше 5.5 — {pct['over_5_5']}%")
    if not parts:
        return footer
    return f"{footer}\nОценка модели (не совет): " + ", ".join(parts)


def _fmt_signed(val: Union[int, float, None]) -> str:
    """Число со знаком («+1», «-0.5», «0») — разница шайб: плюс виден так же
    явно, как минус."""
    s = _fmt_num_max2(val)
    return f"+{s}" if val is not None and float(val) > 0 else s


# Метка строки пропущенных голов превью: по ней `_matchup_compare_table` выделяет
# меньшее значение, а не большее.
_GOALS_AGAINST_LABEL = "Пропущено/игра"


def _matchup_compare_table(
    away_abbr: str,
    home_abbr: str,
    away_name: str,
    home_name: str,
    ra: Dict[str, Union[int, float, None]],
    rh: Dict[str, Union[int, float, None]],
) -> str:
    """Таблица превью матча (`_table`) в три колонки «метрика · гости · хозяева» с
    полными названиями в шапке (коды — для поиска `team_id`) — очки, баланс, форма
    и серия, личные встречи (если были), метрики сезона. Лучшее из двух значений
    метрики — жирным (у пропущенных голов — меньшее); баланс, форма и серия и равные
    значения — без выделения.
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
            _GOALS_AGAINST_LABEL,
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
        ("Разница шайб", _fmt_signed(diff(ra)), _fmt_signed(diff(rh))),
    ]

    def number(value: str) -> Optional[float]:
        try:
            return float(value.rstrip("%"))
        except ValueError:  # «—» — значения нет; баланс, форма и серия — не числа
            return None

    bold = []
    for r, (label, left, right) in enumerate(pairs):
        lv, rv = number(left), number(right)
        if lv is None or rv is None or lv == rv:
            continue
        if label == _GOALS_AGAINST_LABEL:  # единственная метрика, где меньше — лучше
            lv, rv = -lv, -rv
        bold.append((r, 1 if lv > rv else 2))
    rows = [["", away_name, home_name], *([label, left, right] for label, left, right in pairs)]
    return _table(rows, "lrr", bold=bold)


def matchup_season_preview(away_abbr: str, home_abbr: str) -> Tuple[str, str]:
    """
    Сезонное сравнение по teams.abbreviation (= abbrev из NHL API); команды в тексте —
    полным названием `teams.name`.

    Возвращает: (тело — построчный HTML для `rich_message()`, метрики — таблица
    `_table`; строки подвала для `rich_footer()` — сезон). Подвал отдельно:
    вызывающий дописывает в него свои строки (премиум-чату — оценку модели,
    `with_model_prediction`; рассылка — ссылку на подписки), а подвал у сообщения
    один и последний. Превью одно на все чаты, поэтому от чата не зависит.
    """
    a = (away_abbr or "").strip().upper()
    h = (home_abbr or "").strip().upper()
    if not a or not h or a == "?" or h == "?":
        return "Не удалось сопоставить команды с данными в базе.", ""

    season_esc = html.escape(str(config.CURRENT_SEASON))

    stats = cached_fetch_all(
        "SELECT upper(trim(COALESCE(t.abbreviation, ''))) AS abbr, t.name, ts.games_played, "
        "ts.wins, ts.losses, ts.ot, "
        "ts.points, ts.procent_points, ts.goals_per_game, ts.goals_against_per_game, "
        "ts.power_play_percentage, ts.penalty_kill_percentage, "
        "ts.shots_per_game, ts.face_off_win_percentage "
        "FROM teams_stats ts "
        "INNER JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s AND upper(trim(COALESCE(t.abbreviation, ''))) = ANY(%s)",
        (config.SEASON_ID, [a, h]),
        columns=[
            "abbr",
            "name",
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
    # Полное название — для показа (Задача 98); команды нет в базе — показываем код.
    names = {a: away_abbr, h: home_abbr}
    for i in range(stats["count_rows"]):
        ab = stats["abbr"][i]
        if not ab:
            continue
        names[str(ab).strip().upper()] = stats["name"][i]
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

    esc_a, esc_h = html.escape(names[a]), html.escape(names[h])
    header = f"<b>{esc_a} @ {esc_h}</b>"
    footer = f"Сезон {season_esc}"

    def _one_line_team_html(esc_name: str, row: Optional[Dict[str, Union[int, float, None]]]) -> str:
        if not row:
            return f"{esc_name}: нет строки в базе для сезона."
        rec_line = _team_record_line(
            row["wins"], row["losses"], row["ot"], row["points"], row["procent_points"]
        )
        return f"{esc_name} — {rec_line}"

    ra, rh = by_abbr.get(a), by_abbr.get(h)
    if ra is None and rh is None:
        return f"{header}\nКоманд {esc_a} и {esc_h} нет в базе бота для этого сезона.", footer

    parts: List[str] = [header]
    if ra is None or rh is None:
        parts.append(_one_line_team_html(esc_a, ra))
        parts.append(_one_line_team_html(esc_h, rh))
    else:
        parts.append(_matchup_compare_table(a, h, names[a], names[h], ra, rh))
    return "\n".join(parts), footer


LEADERBOARD_PAGE_SIZE = 10


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
    if name_stats:
        body = f"<b>{html.escape(name_stats)}</b>\n\n{body}"
    return body, stats["count_rows"], total


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
    """Общий каркас страницы лидерборда: заголовок, тело, подвал с сезоном и маркером
    «Показаны N из M» и флаги перелистывания.

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
    text = f"<b>{heading_html}</b>\n{body}\n" + rich_footer(
        f"Сезон {html.escape(str(config.CURRENT_SEASON))} · {marker}"
    )
    # total, а не «страница пришла полной» — иначе последняя полная страница
    # рисует кнопку «вперёд» на пустой диапазон, противореча маркеру.
    return text, offset > 0, offset + n < total


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


def team_table() -> str:
    """Турнирная таблица всех команд текущего сезона (`config.SEASON_ID`):
    секции по конференциям/дивизионам, отсортированные по очкам, плюс секции
    Wild Card — HTML-текст с шапкой сезона и даты (шаблон
    `messages/league_table.txt`) для команды `/standings` (`bot.py`).

    Пустой `teams_stats` (сезон ещё не начался — загрузчик не пишет туда
    строк, пока в сезоне нет ни одной завершённой игры, см. Задача 36)
    отдаёт текст с причиной вместо таблицы с одними заголовками секций и
    без единой строки команды."""
    stats = cached_fetch_all(
        "SELECT t.name, games_played, points, procent_points, wins, "
        "       losses, ot, t.division_name, t.conference_name "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id AND ts.season_id = t.season_id "
        "WHERE ts.season_id = %s "
        "ORDER BY conference_name, division_name, points DESC",
        (config.SEASON_ID,),
        columns=['name', 'games_played', 'points', 'procent_points',
                 'wins', 'losses', 'ot', 'division_name', 'conference_name'],
    )

    if stats['count_rows'] == 0:
        return (
            "<b>Турнирная таблица NHL</b>\n"
            "Сезон ещё не начался: в базе нет статистики команд.\n"
            + _season_footer()
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
            f"Сезон {html.escape(str(config.CURRENT_SEASON))} · "
            f"данные на {html.escape(str(_standings_as_of_day()))}"
        ),
    }
    return output_text('messages/league_table.txt', to_template)


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
    """Вратари клуба сезона с хотя бы одной игрой — таблица `_table` (игры,
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
    return _table(rows, "lrrrr")

_TEAM_PROFILE_STATS_COLUMNS = [
    "team_id", "name", "short_name", "division_name", "conference_name", "games_played",
    "wins", "losses", "ot", "points", "procent_points", "goals_per_game",
    "goals_against_per_game", "shots_per_game", "shots_allowed",
    "power_play_percentage", "penalty_kill_percentage", "face_off_win_percentage",
    "power_play_goals", "power_play_opportunities", "power_play_goals_against",
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

    league = cached_fetch_all(
        "SELECT ts.team_id, t.name, t.short_name, t.division_name, t.conference_name, "
        "ts.games_played, ts.wins, ts.losses, ts.ot, ts.points, ts.procent_points, "
        "ts.goals_per_game, ts.goals_against_per_game, ts.shots_per_game, "
        "ts.shots_allowed, ts.power_play_percentage, ts.penalty_kill_percentage, "
        "ts.face_off_win_percentage, ts.power_play_goals, ts.power_play_opportunities, "
        "ts.power_play_goals_against "
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
        "pp": _fmt_pct_stat(me["power_play_percentage"])
        + place("power_play_percentage", detail=pp_detail),
        "pk": _fmt_pct_stat(me["penalty_kill_percentage"]) + place(
            "penalty_kill_percentage",
            detail=f"пропущено {fmt(me['power_play_goals_against'])}, ",
        ),
        "fo": _fmt_pct_stat(me["face_off_win_percentage"]) + place("face_off_win_percentage"),
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
    `_team_game_outcome()`, как у формы. Ближайшие — `scheduled_games` (день по Северной
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
        "WHERE g.season_id = %s AND g.winner_id IS NOT NULL "
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
    страны (`_player_group_table`). Ссылка на него стоит в конце `team_profile()`.

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
        table, n, _total = _player_group_table(_BY_TEAM, team_id, group, None, 0, ranked=False)
        if n:
            player_groups.append((label, table))
            legends.append(f"{label}: {_GROUP_LEGEND[group]}.")
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
COUNTRY_NOT_FOUND = "Страна не найдена в рейтинге этого сезона."
_COUNTRY_FROM = (
    "FROM players_season_stats p "
    "JOIN rosters r ON p.player_id = r.player_id AND p.season_id = r.season_id "
)


def country_label(code: str) -> str:
    """Метка страны для HTML: флаг + русское название из `COUNTRY_LABELS`,
    неизвестный код — сам код; результат экранирован. Флаг — метка в начале
    сообщения (заголовок экрана страны)."""
    return html.escape(COUNTRY_LABELS.get(code, code))


def country_name(code: str) -> str:
    """Название страны без флага, сырой строкой (в HTML — через `html.escape`): метка
    `COUNTRY_LABELS` — всегда «флаг название», неизвестный код — сам код.

    Зачем: эмодзи только как метка в начале сообщения (`docs/telegram_bot.md`
    §«Стиль сообщений», правило 5) — в ячейках таблиц, заголовках блоков страны и
    подвале рассылки (`push_digest_job`) флага нет.
    """
    return COUNTRY_LABELS[code].split(" ", 1)[1] if code in COUNTRY_LABELS else code


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
    """Таблица игроков одной группы (`_table`) — общая для экрана страны
    и `/BOS_FULL`.

    Зачем: у каждой группы свои главные показатели — нападающие: голы, очки,
    игры, время, силовые, броски; защитники — то же с блоками вместо бросков;
    вратари (с хотя бы одной игрой): победы, %ОБ, КН, игры, сухие.

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
        cells = [
            [fmt(stats[k][i]) for k in ("goals", "points", "games", "toi", "hits", extra_key)]
            for i in range(stats["count_rows"])
        ]
    n = stats["count_rows"]
    if not n:
        return "", 0, 0

    def prefix(i: int) -> List[str]:
        name = stats["lastname"][i] or "Unknown"
        return [f"{offset + i + 1}.", name, stats["team"][i] or "—"] if ranked else [name]

    lead = ["", name_header, "Ком"] if ranked else [name_header]
    rows = [lead + header] + [prefix(i) + cells[i] for i in range(n)]
    align = ("rll" if ranked else "l") + "r" * len(header)
    table = _table(rows, align)
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
        return COUNTRY_NOT_FOUND, False, False
    offset = max(offset, 0)
    table, n, total = _player_group_table(
        _BY_COUNTRY, code, group, LEADERBOARD_PAGE_SIZE, offset, ranked=True
    )
    body = f"{table}\n" + rich_details("Обозначения", f"{_GROUP_LEGEND[group]}.")
    # Флаг — в начале заголовка: эмодзи только как метка в начале сообщения (правило 5).
    heading = f"{country_label(code)}: {PLAYER_GROUPS[group][0].lower()}"
    return _leaderboard_page_text(heading, body, n, total, offset)


# Задачи 61, 76: игроки страны в матчах игрового дня (`g.day`) или в одном матче
# (`g.game_id`); страна — `rosters.nationality`.
_COUNTRY_PLAYERS_FROM = (
    "FROM {stats} s "
    "JOIN games g ON g.game_id = s.game_id "
    "JOIN rosters r ON r.player_id = s.player_id AND r.season_id = g.season_id "
    "LEFT JOIN teams t ON t.team_id = s.team_id AND t.season_id = g.season_id "
    "WHERE {scope} = %s AND r.nationality = %s AND "
)


def _night_skater_lines(s: Dict, i: int) -> List[str]:
    """Две строки полевого игрока `i` выборки `_country_blocks`: фамилия и
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
        parts.append(f"{blocked} {_plural_word(blocked, ('блок', 'блока', 'блоков'))}")
    else:
        hits = s["hits"][i] or 0
        parts.append(f"{hits} {_plural_word(hits, ('хит', 'хита', 'хитов'))}")
    if position == "C":
        parts.append(f"вбр. {s['fo_wins'][i] or 0}/{s['fo_taken'][i] or 0}")
    parts.append(s["toi"][i])
    return [head, " · ".join(parts)]


def _country_blocks(code: str, scope: str, value: Union[date, int]) -> Dict[int, str]:
    """Блок страны `code` по матчам выборки `scope = value` (`g.day` или `g.game_id`):
    game_id → HTML. Заголовок — страна (жирным, без флага), у игрока две строки — имя и
    статистика по позиции, между игроками пустая строка; полевые по очкам и голам,
    затем вратари. Матча, где никто из страны не играл, в словаре нет."""
    skaters = fetch_all(
        "SELECT s.game_id, r.lastname, t.name, r.position, s.goals, s.assists, s.shots, "
        "s.plus_minus, s.hits, s.blocked, s.face_off_wins, s.face_off_taken, s.time_on_ice "
        + _COUNTRY_PLAYERS_FROM.format(stats="game_player_stats", scope=scope)
        + "r.position <> 'G' AND s.time_on_ice IS NOT NULL AND s.time_on_ice <> '00:00' "
        "ORDER BY COALESCE(s.goals, 0) + COALESCE(s.assists, 0) DESC, "
        "COALESCE(s.goals, 0) DESC, r.lastname",
        (value, code),
        columns=["game_id", "lastname", "team", "position", "goals", "assists", "shots",
                 "plus_minus", "hits", "blocked", "fo_wins", "fo_taken", "toi"],
    )
    goalies = fetch_all(
        "SELECT s.game_id, r.lastname, t.name, s.saves, s.shots, s.save_percentage, "
        "s.timeonice "
        + _COUNTRY_PLAYERS_FROM.format(stats="game_goalie_stats", scope=scope)
        + "s.timeonice IS NOT NULL AND s.timeonice <> '00:00' ORDER BY r.lastname",
        (value, code),
        columns=["game_id", "lastname", "team", "saves", "shots", "save_percentage", "toi"],
    )
    players: Dict[int, List[str]] = defaultdict(list)
    for i in range(skaters["count_rows"]):
        players[int(skaters["game_id"][i])] += [""] + _night_skater_lines(skaters, i)
    for i in range(goalies["count_rows"]):
        pct = goalies["save_percentage"][i]
        players[int(goalies["game_id"][i])] += [
            "",
            f"{html.escape(goalies['lastname'][i])} ({html.escape(goalies['team'][i] or '?')})",
            f"{goalies['saves'][i] if goalies['saves'][i] is not None else '—'}/"
            f"{goalies['shots'][i] if goalies['shots'][i] is not None else '—'} · "
            f"{f'{round(pct, 2)}%' if pct is not None else '—'} · {goalies['toi'][i]}",
        ]
    heading = f"<b>{html.escape(country_name(code))}</b>"
    return {game_id: "\n".join([heading] + lines) for game_id, lines in players.items()}


def country_night_blocks(code: str, night: date) -> List[Tuple[int, str]]:
    """Матчи ночи `night` (игровой день), где играли игроки страны `code`:
    (game_id, блок страны) по порядку `game_id`; формат блока — `_country_blocks`.
    Нужно рассылке итогов по матчу (`push_digest_job.run_game_broadcast`)."""
    return sorted(_country_blocks(code, "g.day", night).items())


def country_game_block(code: str, game_id: int) -> Optional[str]:
    """Блок страны `code` в матче `game_id` или `None`, если никто из неё не играл.
    Зачем: полная карточка матча подписчику страны дополняется этим блоком (Задача 76)."""
    return _country_blocks(code, "g.game_id", game_id).get(game_id)


def game_score_line(game_id: int) -> str:
    """Строка счёта матча `game_id` — первая строка его карточки (`_score_line`).
    Нужна короткому сообщению подписки на страну (Задача 76), где полной карточки нет."""
    return _score_line(_game_score_header(*_fetch_game_score_rows(game_id)))


def night_goals(night: date) -> List[Tuple[int, int, Optional[str]]]:
    """Голы ночи `night` с видео: (game_id, event_id, страна автора гола или `None`) по
    порядку игры. Голы без `event_id` и буллиты (`period >= 5` в матче с `is_shootouts`;
    в плей-офф 5+ — овертаймы) пропускаются.
    Зачем: рассылка итогов (`push_digest_job.run_game_broadcast`) ждёт клипов всех голов
    матча, а в альбом подписчику страны берёт голы её игроков."""
    goals = fetch_all(
        "SELECT a.game_id, a.event_id, r.nationality "
        "FROM all_goals a "
        "JOIN games g ON g.game_id = a.game_id "
        "LEFT JOIN rosters r ON r.player_id = a.goal_player_id AND r.season_id = g.season_id "
        "WHERE g.day = %s AND a.event_id IS NOT NULL "
        "AND NOT (g.is_shootouts AND a.period >= 5) "
        "ORDER BY a.game_id, a.period, a.time",
        (night,),
        columns=["game_id", "event_id", "nationality"],
    )
    return [
        (int(goals["game_id"][i]), int(goals["event_id"][i]), goals["nationality"][i])
        for i in range(goals["count_rows"])
    ]


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
        "WHERE ts.season_id = %s "
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


# Москва живёт в UTC+3 без перехода на летнее время (с 2014 года) — фиксированный
# сдвиг вместо zoneinfo, которому в slim-образе может не хватить tzdata.
MSK = timezone(timedelta(hours=3))


def last_night_day(nights_back: int = 0) -> str:
    """Игровой день NHL (`games.day`, дата по Северной Америке) матчей, сыгранных
    прошедшей ночью по Москве.

    Зачем: матчи игрового дня D идут в Москве ночью на D+1, поэтому «сегодня»
    для пользователя из Москвы — это вчерашняя дата NHL, а календарное
    `date.today()` сервера показывало бы ещё не сыгранные матчи.

    Аргументы:
        nights_back: 0 — прошедшая ночь, 1 — ночь перед ней.
    """
    return (datetime.now(MSK).date() - timedelta(days=1 + nights_back)).isoformat()


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

    Зачем так: матч — «1. Хозяева 3:2 Гости» обычным шрифтом по левому краю (жирный —
    только заголовок сводки), счёт по периодам — курсивом строкой ниже с отступом. Моноширинное выравнивание
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
        f"{i}. {html.escape(h['home'].strip())} {html.escape(h['home_score'])}:"
        f"{html.escape(h['away_score'])} {html.escape(h['away'].strip())}\n"
        f"{_FIGURE_SPACE * 2}<i>{html.escape(h['period_scores'] + h['extra'])}</i>"
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
    note: str, max_len: int = RICH_MESSAGE_MAX_LENGTH
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
            `RICH_MESSAGE_MAX_LENGTH`).
    """
    return max(max_len - len(note), 80)


def digest_shown_match_count(header: str, lines: List[str], total: int, *, tail: str = "") -> int:
    """Сколько заголовков матчей из `lines` уместится в сводке дайджеста после
    обрезки `truncate_telegram_text` по лимиту Telegram.

    Зачем: обрезка режет текст по символам, а не по заголовкам матчей, поэтому
    число реально показанных матчей для маркера «показаны N из M» (Задача 11)
    нужно подобрать отдельно — иначе сноска солжёт о том, сколько матчей
    осталось видно.

    Аргументы:
        header: неизменяемая часть сводки перед списком заголовков матчей
            (включает завершающие переносы строк).
        tail: неизменяемая часть после списка (подвал с переносом перед ним); маркер
            «показаны N из M» в бюджете учитывается отдельно.
        lines: заголовки матчей, по одному на матч (может быть многострочным),
            в порядке отображения.
        total: всего матчей в выборке дня — M в маркере.
    """
    shown = len(lines)
    while shown > 0:
        note = "\n\n" + truncation_marker(shown, total, item_word="матчей")
        cut = _telegram_cut_budget(note)
        prefix_len = len(header) + len(DIGEST_GAME_SEPARATOR.join(lines[:shown])) + len(tail)
        if prefix_len <= cut:
            return shown
        shown -= 1
    return 0


def truncate_telegram_text(
    text: str,
    max_len: int = RICH_MESSAGE_MAX_LENGTH,
    *,
    footer_note: Optional[str] = None,
) -> str:
    """Режет `text` под лимит Telegram (по умолчанию — rich-сообщения), добавляя сноску.

    Зачем: сообщения Telegram не долетают за лимитом символов; резать нужно с
    запасом под сноску и добавлять её саму, а не отправлять оборванный текст
    без предупреждения. Режет по границе строки: строка — законченный фрагмент
    разметки rich-сообщения (`rich_message`), и её обрывок оставил бы незакрытый тег.

    Аргументы:
        text: исходный текст сообщения (построчный HTML).
        max_len: лимит символов (по умолчанию `RICH_MESSAGE_MAX_LENGTH`).
        footer_note: сноска, дописываемая после обрезки; по умолчанию — общая
            сноска о лимите Telegram без чисел (`truncation_marker()`).
    """
    if len(text) <= max_len:
        return text
    note = footer_note if footer_note is not None else ("\n\n" + truncation_marker())
    cut = _telegram_cut_budget(note, max_len)
    return text[: max(text.rfind("\n", 0, cut + 1), 0)] + note
