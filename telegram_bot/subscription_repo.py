"""Хранение подписок на рассылку (таблица bot_subscriptions).

Слой данных меню `/subscriptions` в `bot.py` и рассылки `push_digest_job.py`: чтение,
включение и отключение подписок (дайджест, предстоящие матчи, команда, страна) и отметки
об отправке — ночи (дайджест, команда) и матча (страна, предстоящие матчи,
`bot_game_sent`), — защищающие от дублей при получасовых прогонах sync.
"""

from __future__ import annotations

from datetime import date
from typing import List, NamedTuple, Optional, Set, Tuple

from database import fetch_all, get_connection

# Подписки «одна на чат», без команды и страны: включаются переключателем главного экрана.
CHAT_KINDS = ("morning_digest", "upcoming_games")


def upsert_chat_kind(chat_id: int, kind: str) -> None:
    """Включает подписку чата вида `kind` из `CHAT_KINDS` (дайджест или предстоящие матчи):
    обновляет существующую запись независимо от её текущего состояния, реактивируя
    погашенную (active = TRUE), иначе вставляет новую — идемпотентно при повторном вызове."""
    assert kind in CHAT_KINDS, kind
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_subscriptions SET active = TRUE, updated_at = now() "
                "WHERE chat_id = %s AND kind = %s",
                (chat_id, kind),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO bot_subscriptions (chat_id, kind, team_id, active) "
                    "VALUES (%s, %s, NULL, TRUE)",
                    (chat_id, kind),
                )
        conn.commit()


class ChatSubscriptions(NamedTuple):
    """Активные подписки чата (`get_chat_subscriptions`)."""

    digest: bool
    upcoming: bool
    team_ids: Set[int]
    countries: List[str]


def get_chat_subscriptions(chat_id: int) -> ChatSubscriptions:
    """Активные подписки чата одним запросом.

    Зачем: меню `/subscriptions` рисует сводку и ✅ по этим данным, поэтому они всегда
    отражают БД, а не последнее нажатие.

    Аргументы: `chat_id` — чат, чьи подписки читаем.

    Возвращает: `ChatSubscriptions` — подписан ли на дайджест и на предстоящие матчи,
    множество `team_id` команд, коды стран по алфавиту.
    """
    row = fetch_all(
        "SELECT kind, team_id, country FROM bot_subscriptions "
        "WHERE chat_id = %s AND active = TRUE ORDER BY country",
        (chat_id,),
        columns=["kind", "team_id", "country"],
    )
    kinds = row["kind"]
    return ChatSubscriptions(
        "morning_digest" in kinds,
        "upcoming_games" in kinds,
        {int(t) for k, t in zip(kinds, row["team_id"]) if k == "team_scores"},
        [c for k, c in zip(kinds, row["country"]) if k == "country_players"],
    )


def upsert_team_scores(chat_id: int, team_id: int) -> None:
    """Включает подписку чата на карточки матчей команды `team_id`
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
    """Гасит подписку чата на матчи конкретной команды (active = FALSE), саму
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


def list_active_country_rows() -> List[Tuple[int, int, str]]:
    """Активные подписки на страны — источник рассылки для `push_digest_job.py`:
    кортежи (id подписки, chat_id, country)."""
    row = fetch_all(
        "SELECT id, chat_id, country FROM bot_subscriptions "
        "WHERE kind = 'country_players' AND active = TRUE ORDER BY chat_id, country",
        None,
        columns=["id", "chat_id", "country"],
    )
    return [
        (int(row["id"][i]), int(row["chat_id"][i]), row["country"][i])
        for i in range(row["count_rows"])
    ]


def list_active_upcoming_rows() -> List[Tuple[int, int]]:
    """Активные подписки на предстоящие матчи — источник рассылки превью для
    `push_digest_job.py`: кортежи (id подписки, chat_id)."""
    row = fetch_all(
        "SELECT id, chat_id FROM bot_subscriptions "
        "WHERE kind = 'upcoming_games' AND active = TRUE ORDER BY chat_id",
        None,
        columns=["id", "chat_id"],
    )
    return [(int(row["id"][i]), int(row["chat_id"][i])) for i in range(row["count_rows"])]


def sent_games(game_ids: List[int]) -> Set[Tuple[int, int]]:
    """Какие из матчей `game_ids` уже ушли подпискам с рассылкой по матчу (страна,
    предстоящие матчи): пары (id подписки, game_id) из `bot_game_sent`.

    Зачем: эти рассылки идут по матчу (Задачи 76, 78) и запускаются после каждого
    получасового прогона sync — без отметки матч уходил бы каждые полчаса."""
    row = fetch_all(
        "SELECT subscription_id, game_id FROM bot_game_sent WHERE game_id = ANY(%s)",
        (game_ids,),
        columns=["subscription_id", "game_id"],
    )
    return {
        (int(row["subscription_id"][i]), int(row["game_id"][i]))
        for i in range(row["count_rows"])
    }


def mark_game_sent(subscription_id: int, game_id: int) -> None:
    """Отмечает, что матч `game_id` подписке `subscription_id` уже ушёл
    (см. `sent_games`); повторная отметка — no-op."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_game_sent (subscription_id, game_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (subscription_id, game_id),
            )
        conn.commit()


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
    chat_id: int, kind: str, team_id: Optional[int] = None, country: Optional[str] = None
) -> None:
    """Гасит подписку по (chat_id, kind[, team_id][, country]) — общий деактиватор
    для произвольного `kind`, в отличие от `deactivate_team_scores`, привязанного к
    команде; гасит и подписки из `CHAT_KINDS`.

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
