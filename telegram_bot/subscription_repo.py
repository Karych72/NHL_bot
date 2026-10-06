"""Хранение подписок на рассылку (таблица bot_subscriptions).

Слой данных команд подписки в `bot.py` и рассылки `push_digest_job.py`: включение
и отключение подписок, время доставки дайджеста (МСК) и отметка о последней
отправленной ночи, защищающая от дублей при получасовых прогонах sync.
"""

from __future__ import annotations

from datetime import date, time
from typing import List, Optional, Tuple

import config
from database import fetch_all, get_connection

# Самое позднее время дайджеста (МСК) = последний ночной прогон sync
# (``scheduled_sync.NIGHT_END_HOUR_UTC``): крайний срок ночи в рассылке, конец выбора в
# ``/digest_time`` и время новой подписки (прежнее общее расписание).
LATEST_DIGEST_TIME = time(11, 0)


def upsert_morning_digest(chat_id: int) -> time:
    """Включает подписку чата на утренний дайджест (`bot_subscriptions`,
    kind='morning_digest'): обновляет существующую запись независимо от её
    текущего состояния, реактивируя погашенную (active = TRUE), иначе
    вставляет новую со временем `LATEST_DIGEST_TIME` — идемпотентно при
    повторном вызове. Выбранное раньше время сохраняется.

    Returns:
        Время доставки подписки по МСК — чтобы ответ команды его назвал.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = TRUE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'morning_digest' RETURNING digest_time",
                (chat_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO bot_subscriptions (chat_id, kind, team_id, digest_time, active) "
                    "VALUES (%s, 'morning_digest', NULL, %s, TRUE)",
                    (chat_id, LATEST_DIGEST_TIME),
                )
        conn.commit()
    return LATEST_DIGEST_TIME if row is None else row[0]


def set_digest_time(chat_id: int, digest_time: time) -> bool:
    """Меняет время доставки активной подписки чата на дайджест.

    Зачем: подписчик сам выбирает, когда получать дайджест (`/digest_time`);
    рассылка придёт в это время или позже, когда загрузится вся ночь.

    Args:
        digest_time: время по МСК.

    Returns:
        `False`, если у чата нет активной подписки — менять нечего.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET digest_time = %s, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'morning_digest' AND active = TRUE",
                (digest_time, chat_id),
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def get_digest_time(chat_id: int) -> Optional[time]:
    """Время доставки (МСК) активной подписки чата на дайджест.

    Зачем: `/digest_time` показывает текущее время и отмечает его на кнопках.

    Returns:
        `None`, если активной подписки нет.
    """
    row = fetch_all(
        "SELECT digest_time FROM bot_subscriptions "
        "WHERE chat_id = %s AND kind = 'morning_digest' AND active = TRUE",
        (chat_id,),
        columns=["digest_time"],
    )
    return row["digest_time"][0] if row["count_rows"] else None


def deactivate_morning_digest(chat_id: int) -> None:
    """Гасит подписку чата на утренний дайджест (active = FALSE), саму запись
    не удаляет. Для чата без такой подписки — no-op."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'morning_digest'",
                (chat_id,),
            )
        conn.commit()


def resolve_team_id_by_abbrev(abbrev: str) -> Optional[int]:
    """Переводит трёхбуквенный код команды (`TOR`) в `team_id` из БД текущего
    сезона (`config.SEASON_ID`).

    Пользователь подписывается на команду по аббревиатуре, а таблицы подписок
    хранят числовой id — эта функция мост между ними.

    Args:
        abbrev: триграмма из callback_data, регистр значения не имеет.

    Returns:
        `None`, если строка пустая или аббревиатура не найдена в сезоне.
    """
    ab = abbrev.strip().upper()
    if not ab:
        return None
    row = fetch_all(
        "SELECT team_id FROM teams WHERE season_id = %s AND "
        "UPPER(trim(COALESCE(abbreviation, ''))) = %s LIMIT 1",
        (config.SEASON_ID, ab),
        columns=["team_id"],
    )
    if not row["count_rows"] or row["team_id"][0] is None:
        return None
    return int(row["team_id"][0])


def upsert_team_scores(chat_id: int, team_id: int) -> None:
    """Включает подписку чата на счёт матчей команды `team_id`
    (kind='team_scores'): обновляет существующую запись пары (chat_id,
    team_id) независимо от её состояния, реактивируя погашенную, иначе
    вставляет новую — идемпотентно."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = TRUE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'team_scores' AND team_id = %s",
                (chat_id, team_id),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO bot_subscriptions (chat_id, kind, team_id, active) "
                    "VALUES (%s, 'team_scores', %s, TRUE)",
                    (chat_id, team_id),
                )
        conn.commit()


def deactivate_team_scores(chat_id: int, team_id: int) -> None:
    """Гасит подписку чата на счёт конкретной команды (active = FALSE), саму
    запись не удаляет. Для отсутствующей подписки — no-op."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'team_scores' AND team_id = %s",
                (chat_id, team_id),
            )
        conn.commit()


def list_active_morning_digest_rows() -> List[Tuple[int, time, Optional[date]]]:
    """Активные подписчики утреннего дайджеста — источник рассылки для
    `push_digest_job.py`: кортежи (chat_id, digest_time, last_sent_night).
    Пустой список, если подписчиков нет."""
    row = fetch_all(
        "SELECT chat_id, digest_time, last_sent_night FROM bot_subscriptions "
        "WHERE kind = 'morning_digest' AND active = TRUE",
        None,
        columns=["chat_id", "digest_time", "last_sent_night"],
    )
    return [
        (int(row["chat_id"][i]), row["digest_time"][i], row["last_sent_night"][i])
        for i in range(row["count_rows"])
    ]


def list_active_team_scores_rows() -> List[Tuple[int, int, Optional[date]]]:
    """Активные подписки на команду: кортежи (chat_id, team_id, last_sent_night)."""
    row = fetch_all(
        "SELECT chat_id, team_id, last_sent_night FROM bot_subscriptions "
        "WHERE kind = 'team_scores' AND active = TRUE AND team_id IS NOT NULL",
        None,
        columns=["chat_id", "team_id", "last_sent_night"],
    )
    return [
        (int(row["chat_id"][i]), int(row["team_id"][i]), row["last_sent_night"][i])
        for i in range(row["count_rows"])
    ]


def mark_night_sent(chat_id: int, kind: str, team_id: Optional[int], night: date) -> None:
    """Отмечает, что рассылка `kind` за ночь `night` чату уже ушла.

    Зачем: `push_digest_job.py` запускается после каждого получасового
    прогона sync — без отметки дайджест уходил бы каждые полчаса.

    Args:
        team_id: `None` для morning_digest; команда — для team_scores.
        night: игровая дата ночи (`push_digest_job.night_of`).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET last_sent_night = %s, updated_at = now() "
                "WHERE chat_id = %s AND kind = %s AND team_id IS NOT DISTINCT FROM %s",
                (night, chat_id, kind, team_id),
            )
        conn.commit()


def mark_subscription_inactive_by_chat_kind_team(
    chat_id: int, kind: str, team_id: Optional[int] = None
) -> None:
    """Гасит подписку по (chat_id, kind[, team_id]) — общий деактиватор для
    произвольного `kind`, в отличие от `deactivate_morning_digest`/
    `deactivate_team_scores`, жёстко привязанных к своему kind.

    Args:
        team_id: `None` ищет запись без команды (напр. morning_digest);
            иначе фильтрует по конкретной команде (team_scores).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if team_id is None:
                cur.execute(
                    "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                    "WHERE chat_id = %s AND kind = %s AND team_id IS NULL",
                    (chat_id, kind),
                )
            else:
                cur.execute(
                    "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                    "WHERE chat_id = %s AND kind = %s AND team_id = %s",
                    (chat_id, kind, team_id),
                )
        conn.commit()
