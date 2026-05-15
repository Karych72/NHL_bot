"""Хранение подписок на рассылку (таблица bot_subscriptions)."""

from __future__ import annotations

from typing import List, Optional

import config
from database import fetch_all, get_connection


def upsert_morning_digest(chat_id: int, timezone: Optional[str] = None) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = TRUE, "
                "timezone = COALESCE(%s, timezone), updated_at = now() "
                "WHERE chat_id = %s AND kind = 'morning_digest'",
                (timezone, chat_id),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO bot_subscriptions (chat_id, kind, team_id, timezone, active) "
                    "VALUES (%s, 'morning_digest', NULL, %s, TRUE)",
                    (chat_id, timezone),
                )
        conn.commit()


def deactivate_morning_digest(chat_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'morning_digest'",
                (chat_id,),
            )
        conn.commit()


def resolve_team_id_by_abbrev(abbrev: str) -> Optional[int]:
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
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = FALSE, updated_at = now() "
                "WHERE chat_id = %s AND kind = 'team_scores' AND team_id = %s",
                (chat_id, team_id),
            )
        conn.commit()


def list_active_morning_digest_chat_ids() -> List[int]:
    row = fetch_all(
        "SELECT chat_id FROM bot_subscriptions WHERE kind = 'morning_digest' AND active = TRUE",
        None,
        columns=["chat_id"],
    )
    return [int(row["chat_id"][i]) for i in range(row["count_rows"])]


def list_active_team_scores_rows() -> List[tuple]:
    """Кортежи (chat_id, team_id)."""
    row = fetch_all(
        "SELECT chat_id, team_id FROM bot_subscriptions "
        "WHERE kind = 'team_scores' AND active = TRUE AND team_id IS NOT NULL",
        None,
        columns=["chat_id", "team_id"],
    )
    return [
        (int(row["chat_id"][i]), int(row["team_id"][i]))
        for i in range(row["count_rows"])
    ]


def mark_subscription_inactive_by_chat_kind_team(
    chat_id: int, kind: str, team_id: Optional[int] = None
) -> None:
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
