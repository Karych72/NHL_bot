"""Хранение подписок на рассылку (таблица bot_subscriptions).

Слой данных меню `/subscriptions` в `bot.py` и рассылки `push_digest_job.py`: чтение,
включение и отключение подписок (дайджест, команда, страна) и отметка
о последней отправленной ночи, защищающая от дублей при получасовых прогонах sync.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Set, Tuple

from database import fetch_all, get_connection


def upsert_morning_digest(chat_id: int) -> None:
    """Включает подписку чата на утренний дайджест (`bot_subscriptions`,
    kind='morning_digest'): обновляет существующую запись независимо от её
    текущего состояния, реактивируя погашенную (active = TRUE), иначе
    вставляет новую — идемпотентно при повторном вызове."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = TRUE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'morning_digest'",
                (chat_id,),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO bot_subscriptions (chat_id, kind, team_id, active) "
                    "VALUES (%s, 'morning_digest', NULL, TRUE)",
                    (chat_id,),
                )
        conn.commit()


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


def get_chat_subscriptions(chat_id: int) -> Tuple[bool, Set[int], List[str]]:
    """Активные подписки чата для меню `/subscriptions`: (подписан ли на дайджест,
    множество `team_id` команд, коды стран по алфавиту)."""
    row = fetch_all(
        "SELECT kind, team_id, country FROM bot_subscriptions "
        "WHERE chat_id = %s AND active = TRUE ORDER BY country",
        (chat_id,),
        columns=["kind", "team_id", "country"],
    )
    kinds = row["kind"]
    return (
        "morning_digest" in kinds,
        {int(t) for k, t in zip(kinds, row["team_id"]) if k == "team_scores"},
        [c for k, c in zip(kinds, row["country"]) if k == "country_players"],
    )


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


def list_active_morning_digest_rows() -> List[Tuple[int, Optional[date]]]:
    """Активные подписчики утреннего дайджеста — источник рассылки для
    `push_digest_job.py`: кортежи (chat_id, last_sent_night).
    Пустой список, если подписчиков нет."""
    row = fetch_all(
        "SELECT chat_id, last_sent_night FROM bot_subscriptions "
        "WHERE kind = 'morning_digest' AND active = TRUE",
        None,
        columns=["chat_id", "last_sent_night"],
    )
    return [(int(row["chat_id"][i]), row["last_sent_night"][i]) for i in range(row["count_rows"])]


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


def upsert_country(chat_id: int, country: str) -> None:
    """Включает (реактивирует) подписку чата на страну `country`; идемпотентно."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_subscriptions (chat_id, kind, country, active) "
                "VALUES (%s, 'country_players', %s, TRUE) "
                "ON CONFLICT (chat_id, country) WHERE kind = 'country_players' "
                "DO UPDATE SET active = TRUE, updated_at = now()",
                (chat_id, country),
            )
        conn.commit()


def list_active_country_rows() -> List[Tuple[int, str, Optional[date]]]:
    """Активные подписки на страны — источник рассылки для `push_digest_job.py`:
    кортежи (chat_id, country, last_sent_night)."""
    row = fetch_all(
        "SELECT chat_id, country, last_sent_night FROM bot_subscriptions "
        "WHERE kind = 'country_players' AND active = TRUE ORDER BY chat_id, country",
        None,
        columns=["chat_id", "country", "last_sent_night"],
    )
    return [
        (int(row["chat_id"][i]), row["country"][i], row["last_sent_night"][i])
        for i in range(row["count_rows"])
    ]


def mark_night_sent(
    chat_id: int, kind: str, team_id: Optional[int], night: date, country: Optional[str] = None
) -> None:
    """Отмечает, что рассылка `kind` за ночь `night` чату уже ушла.

    Зачем: `push_digest_job.py` запускается после каждого получасового
    прогона sync — без отметки дайджест уходил бы каждые полчаса.

    Args:
        team_id: `None` для morning_digest и country_players; команда — для team_scores.
        night: игровая дата ночи (`push_digest_job.night_of`).
        country: код страны для country_players, иначе `None`.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET last_sent_night = %s, updated_at = now() "
                "WHERE chat_id = %s AND kind = %s AND team_id IS NOT DISTINCT FROM %s "
                "AND country IS NOT DISTINCT FROM %s",
                (night, chat_id, kind, team_id, country),
            )
        conn.commit()


def mark_subscription_inactive_by_chat_kind_team(
    chat_id: int, kind: str, team_id: Optional[int] = None, country: Optional[str] = None
) -> None:
    """Гасит подписку по (chat_id, kind[, team_id][, country]) — общий деактиватор
    для произвольного `kind`, в отличие от `deactivate_morning_digest`/
    `deactivate_team_scores`, жёстко привязанных к своему kind.

    Args:
        team_id: `None` ищет запись без команды (напр. morning_digest);
            иначе фильтрует по конкретной команде (team_scores).
        country: то же для кода страны (country_players).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                "WHERE chat_id = %s AND kind = %s AND team_id IS NOT DISTINCT FROM %s "
                "AND country IS NOT DISTINCT FROM %s",
                (chat_id, kind, team_id, country),
            )
        conn.commit()
