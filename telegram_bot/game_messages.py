"""Экран матча: карточка сыгранного матча и превью предстоящего.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`): карточка `game_message`
(шапка-табло, голы, три звезды, видео голов) по шаблону `messages/game_message.txt`, голы и
клипы для неё (`game_goal_events`, `goal_clip_media_id`), превью `matchup_season_preview` —
сравнение команд таблицей. Форма и места команд — из `team_messages`, вёрстка — `rich_markup`.
"""

import html
from collections import defaultdict
from typing import Collection, Dict, List, Optional, Tuple, Union

import config
from database import cached_fetch_all, fetch_all
from rich_markup import (
    _fmt_num_max2,
    _fmt_pct,
    _fmt_signed,
    _format_leader_value,
    _record_str,
    _table,
    playoff_label,
    rich_details,
)
from team_messages import (
    _current_streak,
    _last_n_form_record,
    _league_standing_rows,
    _team_id_for_abbrev,
    _team_places,
    _team_record_line,
)
from template_funcs import output_text


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


# Россия и Беларусь — игроки, ради которых бота читают: в карточке матча их видно сразу.
_HIGHLIGHT_NATIONALITIES = frozenset({"RUS", "BLR"})


def _card_player_name(lastname: Optional[str], nationality: Optional[str]) -> str:
    """Фамилия игрока в карточке матча (голы, ассистенты, вратари) — экранированный HTML;
    игрок из России или Беларуси (`rosters.nationality`) — жирным капсом, чтобы его было
    видно в списке. Отсутствующая фамилия → `Unknown`."""
    name = lastname or "Unknown"
    if nationality in _HIGHLIGHT_NATIONALITIES:
        return f"<b>{html.escape(name.upper())}</b>"
    return html.escape(name)


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
    """Одна строка блока «Звёзды матча»: `⭐ Kaprizov (MIN) — 1+0` для полевого
    игрока, `⭐⭐ Thompson (VGK) — 33/34, 97.1%` для вратаря — номер звезды числом ⭐
    (`docs/telegram_bot.md` §«Стиль сообщений», правило 5). Различение — по
    позиции из `rosters` (`G` → вратарь). Отсутствующая фамилия → `Unknown`
    (как для голов, `_card_player_name`); отсутствующая статистика (NULL) —
    строка без хвоста после «—». Каждое поле из БД проходит `html.escape()`.
    """
    name = html.escape(str(lastname or "Unknown"))
    abbr = html.escape(str(abbreviation or ""))
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

    line = f"{'⭐' * star} {name} ({abbr})"
    return f"{line} — {tail}" if tail else line


_GAME_STATS_COLUMNS = [
    'goals', 'pim', 'blocks', 'hits', 'shots', 'is_overtime',
    'is_shootouts', 'field', 'team_name', 'abbreviation',
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


def _game_score_header(game_stats: Dict, game_goals: Dict, away_first: bool = False) -> Dict[str, str]:
    """Счёт матча по частям, сырыми строками (без HTML): `home`, `away`,
    `home_abbr`, `away_abbr`, `home_score`, `away_score`, `extra` (« (ОТ)»/« (Б)»/пусто),
    `period_scores` («2:1, 4:2, 2:1»).

    Единственная точка расчёта шапки карточки матча и строки сводки /today —
    иначе две копии счёта по периодам разойдутся.

    Аргументы:
        game_stats, game_goals: ответ `_fetch_game_score_rows`.
        away_first: счёт по периодам «гости:хозяева» — как в шапке карточки (`_score_line`);
            сводка /today пишет хозяев первыми.
    """
    is_overtime = game_stats['is_overtime'][0]
    is_shootouts = game_stats['is_shootouts'][0]
    if not is_overtime:
        extra = ''
    elif not is_shootouts:
        extra = ' (ОТ)'
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
    parts = [
        f"{period_away[p]}:{period_home[p]}" if away_first else f"{period_home[p]}:{period_away[p]}"
        for p in range(1, num_periods + 1)
    ]
    return {
        'home': str(game_stats['team_name'][0] or ""),
        'away': str(game_stats['team_name'][1] or ""),
        'home_abbr': str(game_stats['abbreviation'][0] or ""),
        'away_abbr': str(game_stats['abbreviation'][1] or ""),
        'home_score': str(game_stats['goals'][0]),
        'away_score': str(game_stats['goals'][1]),
        'extra': extra,
        'period_scores': ', '.join(parts),
    }


# Маскоты команд в шапке карточки (`_score_line`) по `teams.abbreviation`. Эмодзи, а не
# логотипы: кастомные эмодзи требуют username с Fragment, логотипы — товарные знаки NHL.
_TEAM_MASCOTS = {
    "ANA": "🦆", "BOS": "🐻", "BUF": "⚔️", "CGY": "🔥", "CAR": "🌀", "CHI": "🪶", "COL": "🏔️",
    "CBJ": "💣", "DAL": "⭐", "DET": "🐙", "EDM": "🛢️", "FLA": "🐆", "LAK": "👑", "MIN": "🌲",
    "MTL": "🔴", "NSH": "🐯", "NJD": "😈", "NYI": "🏝️", "NYR": "🗽", "OTT": "🏛️", "PHI": "🟠",
    "PIT": "🐧", "SJS": "🦈", "SEA": "🦑", "STL": "🎵", "TBL": "⚡", "TOR": "🍁", "UTA": "🦣",
    "VAN": "🐋", "VGK": "🛡️", "WSH": "🦅", "WPG": "✈️",
}
_NO_MASCOT = "🏒"


def _score_line(header: Dict[str, str]) -> str:
    """Шапка матча из `_game_score_header` — строка жирным курсивом, названия капсом, маскот
    с обеих сторон названия: «⚔️ BUFFALO SABRES ⚔️ 2 : 3 (ОТ) 🪶 CHICAGO BLACKHAWKS 🪶».
    Первая строка карточки матча — её видно в уведомлении.

    Зачем так: строка того же кегля, что текст, одинакова на iOS и Desktop и выделяется
    капсом, жирным, курсивом и маскотами (решение человека 2026-11-13; правило 1 стиля,
    `docs/telegram_bot.md`). Гости слева.
    """
    away = _TEAM_MASCOTS.get(header['away_abbr'], _NO_MASCOT)
    home = _TEAM_MASCOTS.get(header['home_abbr'], _NO_MASCOT)
    return (
        f"<b><i>{away} {html.escape(header['away'].upper())} {away} "
        f"{html.escape(header['away_score'])} : {html.escape(header['home_score'])}{header['extra']} "
        f"{home} {html.escape(header['home'].upper())} {home}</i></b>"
    )


def _card_heading(header: Dict[str, str], game_id: int) -> str:
    """Шапка карточки матча: строка счёта (`_score_line`), курсивом под ней — раунд плей-офф
    (`playoff_label`), если это плей-офф, и счёт по периодам. Счёт остаётся первой строкой;
    шапка видна и над свёрнутой карточкой (`game_message(collapsed=True)`)."""
    label = playoff_label(game_id)
    lines = [_score_line(header)] + ([f"<i>{label}</i>"] if label else [])
    return "\n".join(lines + [f"<i>Периоды: {html.escape(header['period_scores'])}</i>"])


def _period_name(period: int, shootout: bool) -> str:
    """Заголовок блока голов периода в карточке матча: «2-й период», «Овертайм»,
    «2-й овертайм», «Буллиты».

    Аргументы:
        period: номер периода из `get_goals_game`; в регулярке 4 — овертайм,
            5 — серия буллитов (победный буллит лежит голом с временем 00:00).
        shootout: матч решён буллитами — иначе период 5 значит второй овертайм
            (плей-офф).
    """
    if shootout and period == 5:
        return "Буллиты"
    if period <= 3:
        return f"{period}-й период"
    overtime = period - 3
    if overtime == 1:
        return "Овертайм"
    return f"{overtime}-й овертайм"


def game_clock(period: int, time_in_period: str) -> str:
    """Время гола от начала матча: «2-й период, 09:37» → «29:37».

    Зачем: во всех сообщениях время гола — общее игровое, не внутри периода (решение
    человека 2026-11-13). Каждый период и овертайм плей-офф — 20 минут, поэтому период N
    начинается с (N−1)·20:00; овертайм регулярки (период 4) тоже начинается с 60:00.

    Аргументы:
        period: номер периода (1–3, 4+ — овертаймы).
        time_in_period: «ММ:СС» с начала периода, как в `all_goals.time` и `timeInPeriod`.
    """
    minutes, seconds = time_in_period.split(":")
    return f"{(period - 1) * 20 + int(minutes):02d}:{seconds}"


def goal_clip_media_id(event_id: int) -> str:
    """`id` клипа гола в поле `media` rich-сообщения — на него ссылается строка гола
    карточки (`tg://video?id=…`, `game_message`) и по нему отправитель кладёт сам клип."""
    return f"goal{event_id}"


def game_goal_events(game_id: int) -> List[int]:
    """`event_id` голов матча по порядку игры (голы без события пропущены).

    Зачем: клипы карточки готовятся до её сборки — `game_message` рисует видео только
    у голов, клип которых отправитель положит в сообщение.

    Аргументы: `game_id` — матч.
    """
    goals = fetch_all("SELECT goal_event_id FROM get_goals_game(%s)", (game_id,), ["goal_event_id"])
    return [int(evt) for evt in goals["goal_event_id"] if evt is not None]


def game_message(game_id: int, clips: Collection[int] = (), collapsed: bool = False) -> str:
    """Карточка матча: шапка со счётом, счёт по периодам, голы по периодам, броски, вратари, звёзды.

    Зачем: одна карточка для меню, `/today` и рассылки. Голы — плотным списком, как звёзды;
    клипы — под ним свёрнутым блоком «Видео голов»: лента `<tg-slideshow>`, листается
    свайпом, без подписей — гол виден в списке выше (решение человека 2026-11-14; столбик
    «строка гола + видео» растягивал карточку на экраны). Кнопок видео под сообщением нет.
    Счёт везде «гости:хозяева», как в шапке.

    Аргументы:
        game_id: матч.
        clips: `event_id` голов, клипы которых отправитель кладёт в `media` сообщения
            под `goal_clip_media_id`; у остальных голов видео нет.
        collapsed: всё, кроме шапки, — в свёрнутом блоке «Голы и статистика» (сообщение
            подписки только на страну: на виду счёт и блоки стран).
    """
    game_stats, game_goals = _fetch_game_score_rows(game_id)
    header = _game_score_header(game_stats, game_goals, away_first=True)
    # is_shootouts читается только при is_overtime — как в `_game_score_header`.
    shootout = bool(game_stats['is_overtime'][0] and game_stats['is_shootouts'][0])
    game_goalies = fetch_all(
        "SELECT * FROM get_goalies_game(%s)", (game_id,),
        ['shots', 'saves', 'timeonice', 'lastname',
         'save_percentage', 'is_home', 'nationality'],
    )
    game_three_stars = fetch_all(
        "SELECT * FROM get_three_stars_game(%s)", (game_id,),
        ['star', 'lastname', 'player_position', 'abbreviation',
         'goals', 'assists', 'saves', 'shots', 'save_percentage'],
    )

    # Голы одним абзацем по периодам: заголовок периода, строка гола со временем от начала
    # матча (`game_clock`) и ассистентами в скобках. Не `<pre>`: моноширинные колонки на
    # телефоне разъезжались от длинной фамилии или крупного шрифта.
    goals_by_period: Dict[int, List[str]] = defaultdict(list)
    clip_videos: List[str] = []
    scorer_counts: Dict[str, int] = defaultdict(int)
    for i, (h, a) in enumerate(_running_scores(game_goals)):
        p = game_goals['period'][i]
        scorer = _card_player_name(game_goals['scorer'][i], game_goals['scorer_nationality'][i])
        pos_raw = game_goals['scorer_position'][i]
        pos = (str(pos_raw).strip() if pos_raw else "") or ""
        scorer_counts[scorer] += 1
        situation = _goal_situation_suffix(
            game_goals['is_ppg'][i],
            game_goals['is_shg'][i],
            game_goals['empty_net'][i],
            game_goals['winner_goal'][i],
        )

        # Время от начала матча; у буллита (всегда 00:00) его нет.
        time_str = "" if shootout and p == 5 else game_clock(p, game_goals['goal_time'][i])
        assist_names = [
            _card_player_name(game_goals[key][i], game_goals[f"{key}_nationality"][i])
            for key in ("assist_1", "assist_2")
            if game_goals[key][i] is not None
        ]
        time_prefix = f"{time_str} · " if time_str else ""
        assists = f" ({', '.join(assist_names)})" if assist_names else ""
        line = (
            f"{time_prefix}{a}:{h} — {scorer}"
            + html.escape((f' [{pos}]' if pos else '') + situation)
            + assists
        )
        goals_by_period[p].append(line)
        evt = game_goals['goal_event_id'][i]
        if evt is not None and int(evt) in clips:
            clip_videos.append(f'<video src="tg://video?id={goal_clip_media_id(int(evt))}"></video>')
    # Каждый сыгранный период — и без голов: три основных, овертайм, если был (в матче с
    # буллитами он безголевой), дальше — до последнего периода с голом. Между периодами —
    # пустая строка внутри абзаца: граница абзацев дала бы зазор больше строки.
    played = max(4 if game_stats['is_overtime'][0] else 3, *goals_by_period)
    goal_lines: List[str] = []
    for p in range(1, played + 1):
        if goal_lines:
            goal_lines.append("&nbsp;")
        goal_lines += [f"<b>{_period_name(p, shootout)}</b>", *(goals_by_period[p] or ["Голов нет"])]
    goals_block = "\n".join(goal_lines)
    if clip_videos:
        goals_block += "\n" + rich_details("Видео голов", f"<tg-slideshow>{''.join(clip_videos)}</tg-slideshow>")

    hat_lines = [
        f"Хет-трик: {name} (×{n})"
        for name, n in scorer_counts.items()
        if n >= 3
    ]
    hat_tricks = "\n".join(hat_lines) if hat_lines else ""

    # Вратарь — своей строкой (гости первыми, как в шапке): в одну строку двое не влезают
    # в ширину телефона.
    goalie_lines: List[str] = []
    for i in sorted(range(game_goalies['count_rows']), key=lambda i: bool(game_goalies['is_home'][i])):
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
        name = _card_player_name(game_goalies["lastname"][i], game_goalies["nationality"][i])
        goalie_lines.append(
            f"{name} — "
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
        'home_shots': html.escape(str(game_stats['shots'][0])),
        'away_shots': html.escape(str(game_stats['shots'][1])),
        'home_penalties': html.escape(str(game_stats['pim'][0])),
        'away_penalties': html.escape(str(game_stats['pim'][1])),
        'goals_block': goals_block,
        'goalkeepers': "\n".join(goalie_lines),
        'hat_tricks': hat_tricks,
        'three_stars': three_stars,
    }
    body = output_text('messages/game_message.txt', to_template)
    if collapsed:
        body = rich_details("Голы и статистика", body)
    return f"{_card_heading(header, game_id)}\n{body}"


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


def _h2h_season_wins(tid_a: int, tid_b: int) -> Optional[Tuple[int, int]]:
    """Победы команд `tid_a` и `tid_b` в личных встречах регулярного сезона или None,
    если они ещё не встречались; серия плей-офф в счёт не идёт (Задача 102)."""
    row = cached_fetch_all(
        "SELECT winner_id FROM games WHERE season_id = %s AND game_type = 2 "
        "AND winner_id IS NOT NULL AND ((home_team_id = %s AND away_team_id = %s) "
        "     OR (home_team_id = %s AND away_team_id = %s))",
        (config.SEASON_ID, tid_a, tid_b, tid_b, tid_a),
        columns=["winner_id"],
    )
    if row["count_rows"] == 0:
        return None
    winners = row["winner_id"][: row["count_rows"]]
    return winners.count(tid_a), winners.count(tid_b)


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
    полными названиями в шапке (коды — для поиска `team_id`) — места в дивизионе и
    конференции, очки, баланс, форма и серия, личные встречи (если были), метрики сезона.
    Лучшее из двух значений метрики — жирным (у пропущенных голов — меньшее); места,
    баланс, форма и серия и равные значения — без выделения: номера мест команд из разных
    групп не сравниваются.
    """
    def diff(r: Dict[str, Union[int, float, None]]) -> Optional[float]:
        gf, ga = r["goals_per_game"], r["goals_against_per_game"]
        return None if gf is None or ga is None else round(float(gf) - float(ga), 2)

    def places(team_id: Optional[int]) -> Tuple[str, str]:
        if team_id is None:
            return "—", "—"
        _, division, conference, _ = _team_places(team_id, _league_standing_rows())
        return f"{division}-е", f"{conference}-е"

    tid_a = _team_id_for_abbrev(away_abbr)
    tid_h = _team_id_for_abbrev(home_abbr)
    tids = (tid_a, tid_h)
    form = [_last_n_form_record(t, 5) if t is not None else "—" for t in tids]
    streak = [_current_streak(t) if t is not None else "—" for t in tids]
    place_a, place_h = places(tid_a), places(tid_h)
    pairs: List[Tuple[str, str, str]] = [
        ("Место в дивизионе", place_a[0], place_h[0]),
        ("Место в конференции", place_a[1], place_h[1]),
        ("Очки", _format_leader_value(ra["points"]), _format_leader_value(rh["points"])),
        ("% очков", _fmt_pct(ra["procent_points"]), _fmt_pct(rh["procent_points"])),
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
            _fmt_pct(ra["power_play_percentage"]),
            _fmt_pct(rh["power_play_percentage"]),
        ),
        (
            "Меньшинство",
            _fmt_pct(ra["penalty_kill_percentage"]),
            _fmt_pct(rh["penalty_kill_percentage"]),
        ),
        ("Броски за игру", _fmt_num_max2(ra["shots_per_game"]), _fmt_num_max2(rh["shots_per_game"])),
        (
            "Вбрасывания",
            _fmt_pct(ra["face_off_win_percentage"]),
            _fmt_pct(rh["face_off_win_percentage"]),
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


def matchup_season_preview(
    game_id: int, away_abbr: str, home_abbr: str, collapsed: bool = False
) -> Tuple[str, str]:
    """
    Сезонное сравнение по teams.abbreviation (= abbrev из NHL API); команды в тексте —
    полным названием `teams.name`. У матча плей-офф под заголовком — пометка раунда
    (`playoff_label` по `game_id`); сравнение и тогда по регулярке. `collapsed` — таблица
    в свёрнутом блоке (`rich_details`): рассылка шлёт превью без запроса, а `/tonight`
    открывает его нажатием, и там таблица развёрнута.

    Возвращает: (тело — построчный HTML для `rich_message()`, метрики — таблица
    `_table`; строки подвала для `rich_footer()` — сезон). Подвал отдельно:
    вызывающий дописывает в него свои строки (рассылка — ссылку на подписки), а подвал
    у сообщения один и последний. Превью одно на все чаты, поэтому от чата не зависит.
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
        "WHERE ts.season_id = %s AND ts.game_type = 2 "
        "AND upper(trim(COALESCE(t.abbreviation, ''))) = ANY(%s)",
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
    label = playoff_label(game_id)
    if label is not None:
        header += f"\n<i>{label}</i>"
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
        table = _matchup_compare_table(a, h, names[a], names[h], ra, rh)
        parts.append(rich_details("Сравнение команд", table) if collapsed else table)
    return "\n".join(parts), footer
