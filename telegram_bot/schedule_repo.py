"""Расписания экранов бота (таблица bot_schedules, Задача 107).

Слой данных тикера в `bot.py` и списка «⏰ Расписания» в `/subscriptions`: расчёт следующей
отправки по частоте, времени и часовому поясу (`next_run_at`, stdlib `zoneinfo`), создание,
чтение, удаление расписаний, выборка тех, кому пора, и сдвиг на следующий слот.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json

from database import get_connection


class Schedule(NamedTuple):
    """Активное расписание; `oldest` — самое старое активное у своего чата (лимит без премиума)."""

    id: int
    chat_id: int
    screen: str
    params: Dict[str, Any]
    freq: str
    weekday: Optional[int]
    month_day: Optional[int]
    local_time: time
    timezone: str
    oldest: bool


# Активные расписания с отметкой самого старого в чате: окно — до фильтра по времени, иначе
# «старейшим» окажется старейшее из тех, кому пора. Поэтому `due` читает все активные строки:
# при единицах расписаний на чат это дешевле, чем второй запрос.
_ACTIVE = (
    "SELECT id, chat_id, screen, params, freq, weekday, month_day, local_time, timezone, "
    "id = min(id) OVER (PARTITION BY chat_id) AS oldest, next_run_at "
    "FROM bot_schedules WHERE active"
)


def _is_run_day(freq: str, weekday: Optional[int], month_day: Optional[int], day: date) -> bool:
    """Выпадает ли отправка на дату `day`: каждый день, ISO-день недели или число месяца
    (0 — последний день)."""
    if freq == "day":
        return True
    if freq == "week":
        return day.isoweekday() == weekday
    if month_day == 0:
        return day.day == calendar.monthrange(day.year, day.month)[1]
    return day.day == month_day


def next_run_at(
    freq: str,
    weekday: Optional[int],
    month_day: Optional[int],
    local_time: time,
    tz: str,
    after: datetime,
) -> datetime:
    """Ближайший момент отправки строго после `after`, в UTC.

    Зачем: тикер берёт расписания по `next_run_at` в БД, а после отправки сдвигает его
    сюда. Локальное время, которого нет (переход на летнее), сдвигается вперёд на величину
    перехода; неоднозначное (обратный переход) — первое вхождение: так `datetime` трактует
    `fold=0`.

    Аргументы: `freq` — 'day' / 'week' / 'month'; `weekday` — ISO-день недели у 'week';
    `month_day` — число 1–28 или 0 (последний день) у 'month'; `local_time` — время в поясе
    `tz` (IANA); `after` — aware-момент, от которого ищется следующий слот.
    """
    zone = ZoneInfo(tz)
    day = after.astimezone(zone).date()
    # Любой допустимый слот наступает за 32 дня; дольше — частота и день не согласованы.
    for _ in range(32):
        if _is_run_day(freq, weekday, month_day, day):
            moment = datetime.combine(day, local_time, tzinfo=zone).astimezone(timezone.utc)
            if moment > after:
                return moment
        day += timedelta(days=1)
    raise ValueError(f"Нет дня отправки: freq={freq} weekday={weekday} month_day={month_day}")


def create(
    chat_id: int,
    screen: str,
    params: Dict[str, Any],
    freq: str,
    weekday: Optional[int],
    month_day: Optional[int],
    local_time: time,
    tz: str,
    now: datetime,
) -> int:
    """Сохраняет расписание с первой отправкой после `now` и возвращает его `id`.

    Зачем: точка входа формы создания (Задача 108); согласованность частоты, дня и времени
    проверяют CHECK-и таблицы.

    Raises:
        ValueError: `tz` — не IANA-зона или у частоты нет дня отправки.
    """
    try:
        first = next_run_at(freq, weekday, month_day, local_time, tz, now)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс: {tz}") from exc
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_schedules (chat_id, screen, params, freq, weekday, month_day, "
                "local_time, timezone, next_run_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (chat_id, screen, Json(params), freq, weekday, month_day, local_time, tz, first),
            )
            (schedule_id,) = cur.fetchone()
        conn.commit()
    return int(schedule_id)


def _select(where: str, params: tuple) -> List[Schedule]:
    """Активные расписания из `_ACTIVE`, отобранные условием `where` (с сортировкой)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ({_ACTIVE}) s WHERE {where}", params)
            rows = cur.fetchall()
    return [Schedule(*row[:-1]) for row in rows]


def list_for_chat(chat_id: int) -> List[Schedule]:
    """Активные расписания чата от старых к новым — список «⏰ Расписания» в `/subscriptions`."""
    return _select("chat_id = %s ORDER BY id", (chat_id,))


def due(now: datetime) -> List[Schedule]:
    """Активные расписания, чья отправка наступила к `now`, — работа тикера на эту минуту."""
    return _select("next_run_at <= %s ORDER BY next_run_at, id", (now,))


def delete(chat_id: int, schedule_id: int) -> bool:
    """Стирает расписание `schedule_id` чата `chat_id`; чужое не найдётся. Возвращает,
    было ли что стирать."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bot_schedules WHERE id = %s AND chat_id = %s",
                (schedule_id, chat_id),
            )
            deleted = cur.rowcount == 1
        conn.commit()
    return deleted


def advance(schedule: Schedule, now: datetime) -> None:
    """Переносит отправку `schedule` на первый слот после `now`.

    Зачем: отсчёт от `now`, а не от прошлого слота — после простоя бота уходит одна
    пропущенная отправка, а не все накопившиеся.
    """
    nxt = next_run_at(
        schedule.freq, schedule.weekday, schedule.month_day, schedule.local_time,
        schedule.timezone, now,
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bot_schedules SET next_run_at = %s WHERE id = %s", (nxt, schedule.id))
        conn.commit()
