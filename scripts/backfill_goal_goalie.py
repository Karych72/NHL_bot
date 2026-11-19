"""Backfill ``all_goals.goalie_id`` from the local play-by-play cache (Task 160).

Reads ``{raw}/{season}/{game}.pbp.json.gz`` (the loader's cache, no network) and sets
``goalie_id`` from ``details.goalieInNetId`` of every ``goal`` event, matching rows by
``(game_id, event_id)``. A goal without the field (empty net) stays NULL. Idempotent;
touches only ``goalie_id``. The DB is chosen by ``PG_HOST``/``PG_PORT``/``PG_USER``/
``PG_DATABASE`` as for the loader. Run from the checkout that holds ``all_data/raw``:

    PG_DATABASE=nhl_dev .venv/bin/python scripts/backfill_goal_goalie.py [--raw all_data/raw]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Iterator, Tuple

import psycopg2
from psycopg2.extras import execute_values

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import config  # noqa: E402

BATCH_GAMES = 500


def goalie_rows(pbp_file: Path) -> Iterator[Tuple[int, int, int]]:
    """Yield ``(game_id, event_id, goalie_id)`` for goals of one game with a goalie in net."""
    game_id = int(pbp_file.name.split(".")[0])
    with gzip.open(pbp_file, "rt", encoding="utf-8") as fh:
        plays = json.load(fh).get("plays", [])
    for play in plays:
        goalie = (play.get("details") or {}).get("goalieInNetId")
        if play.get("typeDescKey") == "goal" and goalie is not None:
            yield game_id, int(play["eventId"]), int(goalie)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", default="all_data/raw", help="каталог кэша play-by-play")
    raw = Path(parser.parse_args().raw)
    files = sorted(raw.glob("*/*.pbp.json.gz"))
    if not files:
        sys.exit(f"В {raw} нет файлов *.pbp.json.gz")

    conn = psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, user=config.PG_USER, database=config.PG_DATABASE
    )
    updated = 0
    with conn, conn.cursor() as cur:
        for start in range(0, len(files), BATCH_GAMES):
            rows = [r for f in files[start:start + BATCH_GAMES] for r in goalie_rows(f)]
            execute_values(
                cur,
                "UPDATE all_goals g SET goalie_id = v.goalie_id "
                "FROM (VALUES %s) AS v(game_id, event_id, goalie_id) "
                "WHERE g.game_id = v.game_id AND g.event_id = v.event_id",
                rows,
                template="(%s::bigint, %s::int, %s::bigint)",
                page_size=5000,
            )
            updated += cur.rowcount
            print(f"{min(start + BATCH_GAMES, len(files))}/{len(files)} игр", flush=True)
    print(f"Обновлено голов: {updated}")


if __name__ == "__main__":
    main()
