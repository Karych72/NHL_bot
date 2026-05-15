"""NHL Web API scoreboard (unofficial): live slate for /tonight."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

NHL_API_BASE = "https://api-web.nhle.com/v1"
SCORE_NOW_PATH = "/score/now"

# US/Eastern — один согласованный пояс для времени старта в выводе.
_DISPLAY_TZ = ZoneInfo("America/New_York")


class ScoreboardFetchError(Exception):
    """Не удалось получить или разобрать ответ score API."""


def fetch_score_now() -> dict:
    """GET /v1/score/now. Редиректы включены (requests по умолчанию)."""
    url = NHL_API_BASE + SCORE_NOW_PATH
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        raise ScoreboardFetchError(str(e)) from e
    if resp.status_code != 200:
        raise ScoreboardFetchError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise ScoreboardFetchError("invalid JSON") from e


def _parse_start_utc(raw: Optional[str]) -> Tuple[datetime, str]:
    """Возвращает aware UTC и строку времени в ET; при ошибке — минимальный ключ сортировки."""
    if not raw:
        return (datetime.min.replace(tzinfo=ZoneInfo("UTC")), "—")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        local = dt.astimezone(_DISPLAY_TZ)
        return (dt, local.strftime("%H:%M ET"))
    except (ValueError, TypeError):
        return (datetime.min.replace(tzinfo=ZoneInfo("UTC")), "—")


def slate_games_sorted(payload: dict) -> List[Dict[str, Any]]:
    """
    Игры слейта на currentDate, отсортированные по startTimeUTC.
    См. tonight_reply_intro / tonight_match_button_label — правило gameDate.
    """
    current = payload.get("currentDate")
    games_raw = payload.get("games") or []

    filtered: List[Dict[str, Any]] = []
    for g in games_raw:
        if not isinstance(g, dict):
            continue
        gd = g.get("gameDate")
        if gd is not None and gd != current:
            continue
        filtered.append(g)

    filtered.sort(key=lambda g: _parse_start_utc(g.get("startTimeUTC"))[0])
    return filtered


def msk_hours_ahead_of_et_now() -> float:
    """На сколько часов московское время впереди восточного США (ET) в текущий момент."""
    now = datetime.now(timezone.utc)
    msk_off = now.astimezone(ZoneInfo("Europe/Moscow")).utcoffset()
    et_off = now.astimezone(ZoneInfo("America/New_York")).utcoffset()
    if msk_off is None or et_off is None:
        return 8.0
    dh = (msk_off - et_off).total_seconds() / 3600.0
    return round(dh, 1)


def _format_msk_ahead_hours(dh: float) -> str:
    if abs(dh - round(dh)) < 0.05:
        return str(int(round(dh)))
    s = f"{dh:.1f}"
    return s.rstrip("0").rstrip(".")


def tonight_reply_intro(payload: dict) -> str:
    """
    Текст ответа /tonight без списка матчей (матчи только кнопками).
    Разметка HTML (Telegram): <b>жирный</b>; дата и числа из API — через html.escape.
    """
    raw_current = payload.get("currentDate") or "—"
    current_esc = html.escape(str(raw_current))
    games = slate_games_sorted(payload)
    if not games:
        return f"На {current_esc} в расписании NHL матчей нет."

    n = len(games)
    dh = msk_hours_ahead_of_et_now()
    dh_s = _format_msk_ahead_hours(dh)
    n_esc = html.escape(str(n))
    dh_esc = html.escape(dh_s)
    return (
        f"Матчи NHL на календарный день лиги <b>{current_esc}</b> "
        f"(актуальное расписание с сайта NHL, не из базы бота).\n\n"
        f"Ниже <b>{n_esc}</b> кнопок — выберите матч: откроется карточка из базы "
        f"(если игра уже загружена) или сравнение сезонных показателей команд.\n\n"
        f"На кнопках время старта в <b>ET</b> (восточное время США). "
        f"<b>Москва сейчас на {dh_esc} ч впереди ET</b> "
        f"(разница меняется при переводе часов в США; в России постоянно UTC+3)."
    )


def tonight_match_button_label(game: Dict[str, Any]) -> str:
    """Подпись кнопки: AWAY @ HOME · HH:MM ET (лимит Telegram 64 символа)."""
    away = ((game.get("awayTeam") or {}).get("abbrev") or "?").strip()
    home = ((game.get("homeTeam") or {}).get("abbrev") or "?").strip()
    _, time_et = _parse_start_utc(game.get("startTimeUTC"))
    return f"{away} @ {home} · {time_et}"
