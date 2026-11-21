"""Print ``game_id<TAB>event_id<TAB>goalie_id`` of every goal in the local play-by-play cache (Task 160).

Reads ``{raw}/{season}/{game}.pbp.json.gz`` (the loader's cache, no network, no DB) and writes
one line per ``goal`` event that has ``details.goalieInNetId``; empty-net goals have none and
are not printed. ``scripts/backfill_goal_goalie.sh`` pipes the output into ``all_goals.goalie_id``.

    .venv/bin/python scripts/backfill_goal_goalie.py [--raw all_data/raw] > goals.tsv
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Iterator, Tuple


def goalie_rows(pbp_file: Path) -> Iterator[Tuple[int, int, int]]:
    """Yield ``(game_id, event_id, goalie_id)`` for goals of one game with a goalie in net.

    Why: ``all_goals`` rows are matched by ``(game_id, event_id)``; empty-net goals carry no
    ``goalieInNetId`` and are skipped, so their ``goalie_id`` stays NULL.

    Args:
        pbp_file: cached ``{game_id}.pbp.json.gz``; the game id is the file-name prefix.
    """
    game_id = int(pbp_file.name.split(".")[0])
    with gzip.open(pbp_file, "rt", encoding="utf-8") as fh:
        plays = json.load(fh).get("plays", [])
    for play in plays:
        goalie = (play.get("details") or {}).get("goalieInNetId")
        if play.get("typeDescKey") == "goal" and goalie is not None:
            yield game_id, int(play["eventId"]), int(goalie)


def main() -> None:
    """Print the TSV of the whole cache to stdout; the file count goes to stderr."""
    parser = argparse.ArgumentParser(description="Вратари голов из кэша play-by-play (TSV в stdout)")
    parser.add_argument("--raw", default="all_data/raw", help="каталог кэша play-by-play")
    raw = Path(parser.parse_args().raw)
    files = sorted(raw.glob("*/*.pbp.json.gz"))
    if not files:
        sys.exit(f"В {raw} нет файлов *.pbp.json.gz")
    for pbp_file in files:
        for game_id, event_id, goalie_id in goalie_rows(pbp_file):
            print(f"{game_id}\t{event_id}\t{goalie_id}")
    print(f"Игр в кэше: {len(files)}", file=sys.stderr)


if __name__ == "__main__":
    main()
