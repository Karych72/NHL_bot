"""Дайджест дня: сводка матчей ночи, кнопки матчей, голы ночи.

Часть `telegram_bot/`, которая читает БД (заполненную `pipeline/`): какая ночь последняя
(`last_night_day`), её матчи (`day_digest`), сводка со счётами (`day_digest_summary_body`) и
подписи кнопок, сколько матчей влезло в лимит сообщения. Остальные экраны — в
`game_messages`, `team_messages`, `leaderboard_messages`, `country_messages`; примитивы
разметки — `rich_markup`; отправка и листание — `stats_handlers.py`.
"""

import html
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple

import config
from database import fetch_all
from game_messages import _fetch_game_score_rows, _game_score_header
from rich_markup import _telegram_cut_budget, MSK, playoff_label, truncation_marker


def night_goals(night: date) -> List[Tuple[int, int, Optional[str], Optional[int]]]:
    """Голы ночи `night` с видео: (game_id, event_id, страна автора гола или `None`, автор
    гола) по порядку игры. Голы без `event_id` и буллиты (`period >= 5` в матче с
    `is_shootouts`; в плей-офф 5+ — овертаймы) пропускаются.
    Зачем: рассылка итогов (`push_digest_job.run_game_broadcast`) и итоги канала
    (`run_channel_results`) ждут клипов всех голов матча, а в альбом подписчику страны или
    игрока (Задача 122) рассылка берёт голы этих игроков. Условие `season_id` нужно индексу
    `(season_id, day)`: без него запрос идёт полным проходом по `games` с историей (Задача 157)."""
    goals = fetch_all(
        "SELECT a.game_id, a.event_id, r.nationality, a.goal_player_id "
        "FROM all_goals a "
        "JOIN games g ON g.game_id = a.game_id "
        "LEFT JOIN rosters r ON r.player_id = a.goal_player_id AND r.season_id = g.season_id "
        "WHERE g.season_id = %s AND g.day = %s AND a.event_id IS NOT NULL "
        "AND NOT (g.is_shootouts AND a.period >= 5) "
        "ORDER BY a.game_id, a.period, a.time",
        (config.SEASON_ID, night),
        columns=["game_id", "event_id", "nationality", "scorer"],
    )
    return [
        (
            int(goals["game_id"][i]), int(goals["event_id"][i]), goals["nationality"][i],
            None if goals["scorer"][i] is None else int(goals["scorer"][i]),
        )
        for i in range(goals["count_rows"])
    ]


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


def day_digest(day=None) -> Tuple[Optional[str], List[int]]:
    """Матчи игрового дня для дайджеста: (день, `game_id` по порядку).

    Зачем: `/today`, дайджест рассылки и AI-запрос за дату показывают один и тот же
    набор матчей; карточку или сводку собирает `dispatch_day_digest_messages`.

    Аргументы: `day` — игровой день; `None` — последний день сезона с матчами в базе
    (день `None` в ответе — в базе сезона матчей нет вовсе).
    """
    if day is None:
        latest_day = fetch_all(
            "SELECT max(day) AS day FROM games WHERE season_id = %s",
            (config.SEASON_ID,),
            columns=["day"],
        )
        day = latest_day["day"][0]
        if day is None:
            return None, []
    day = str(day)
    game_ids = fetch_all(
        "SELECT DISTINCT game_id FROM games WHERE day = %s AND season_id = %s ORDER BY game_id",
        (day, config.SEASON_ID),
        ['game_id'],
    )
    return day, [int(game_id) for game_id in game_ids['game_id']]


# Пробел шириной в цифру: им сдвинут счёт по периодам под строкой матча —
# обычные пробелы в начале строки клиенты Telegram могут съесть.
_FIGURE_SPACE = "\u2007"

# Между матчами сводки — пустая строка: подряд идущие пары строк сливались.
DIGEST_GAME_SEPARATOR = "\n\n"


def day_digest_summary_body(game_ids: Sequence[int]) -> List[str]:
    """Сжатая сводка дайджеста: по элементу на матч, номер совпадает с номером
    на кнопке матча (`digest_game_button_labels`).

    Зачем так: матч — «1. Хозяева 3:2 Гости» обычным шрифтом по левому краю (жирный —
    только заголовок сводки), счёт по периодам — курсивом строкой ниже с отступом, у плей-офф
    там же пометка раунда (`playoff_label`). Моноширинное выравнивание
    команд по счёту на телефоне выглядело криво (отзыв 2026-10-03), а в одной
    строке с командами счёт по периодам не влезает в ширину экрана.

    Возвращает: по элементу на матч (две строки через `\n`); элементы склеиваются
    через `DIGEST_GAME_SEPARATOR`, обрезка под лимит Telegram
    (`digest_shown_match_count`) отбрасывает матч целиком.

    Аргументы:
        game_ids: матчи дня в порядке сводки, непустой список.
    """
    lines = []
    for i, gid in enumerate(game_ids, start=1):
        h = _game_score_header(*_fetch_game_score_rows(gid))
        label = playoff_label(gid)
        playoff = f" · {label}" if label else ""
        lines.append(
            f"{i}. {html.escape(h['home'].strip())} {html.escape(h['home_score'])}:"
            f"{html.escape(h['away_score'])} {html.escape(h['away'].strip())}\n"
            f"{_FIGURE_SPACE * 2}<i>{html.escape(h['period_scores'] + h['extra'])}{playoff}</i>"
        )
    return lines


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
