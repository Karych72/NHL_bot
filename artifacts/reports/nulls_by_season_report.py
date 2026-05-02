"""Build NULL-count report by season directly from PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import psycopg2
from psycopg2 import sql


REPO_ROOT = Path(__file__).resolve().parents[2]
TELEGRAM_BOT_DIR = REPO_ROOT / "telegram_bot"
if str(TELEGRAM_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(TELEGRAM_BOT_DIR))
import config  # noqa: E402


DEFAULT_MATCH_TABLES = [
    "games",
    "game_team_stats",
    "game_player_stats",
    "game_goalie_stats",
    "all_goals",
]


def _parse_csv_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _connect():
    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        user=config.PG_USER,
        database=config.PG_DATABASE,
    )


def _existing_columns(cur, table: str) -> List[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def _available_seasons(cur) -> List[int]:
    cur.execute("SELECT DISTINCT season_id FROM games WHERE season_id IS NOT NULL ORDER BY season_id")
    return [int(row[0]) for row in cur.fetchall()]


def _null_counts_for_table(
    cur,
    table: str,
    season_id: int,
    columns: Sequence[str],
) -> Dict[str, object]:
    if table == "games":
        from_clause = sql.SQL("FROM public.games t")
        where_clause = sql.SQL("WHERE t.season_id = %s")
    else:
        from_clause = sql.SQL(
            "FROM public.{table} t JOIN public.games g ON g.game_id = t.game_id"
        ).format(table=sql.Identifier(table))
        where_clause = sql.SQL("WHERE g.season_id = %s")

    exprs = [
        sql.SQL(
            "SUM(CASE WHEN t.{col} IS NULL THEN 1 ELSE 0 END)::bigint AS {alias}"
        ).format(col=sql.Identifier(col_name), alias=sql.Identifier(col_name))
        for col_name in columns
    ]
    query = sql.SQL(
        "SELECT COUNT(*)::bigint AS total_rows, {exprs} {from_clause} {where_clause}"
    ).format(exprs=sql.SQL(", ").join(exprs), from_clause=from_clause, where_clause=where_clause)
    cur.execute(query, (season_id,))
    values = cur.fetchone()
    return {
        "total_rows": int(values[0]),
        "null_counts": [int(v or 0) for v in values[1:]],
    }


def build_report_rows(cur, tables: Iterable[str], seasons: Iterable[int]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for table in tables:
        columns = _existing_columns(cur, table)
        if not columns:
            continue
        for season_id in seasons:
            stats = _null_counts_for_table(cur, table=table, season_id=season_id, columns=columns)
            total_rows = int(stats["total_rows"])
            for col_name, null_count in zip(columns, stats["null_counts"]):
                null_pct = (null_count / total_rows * 100.0) if total_rows else 0.0
                rows.append(
                    {
                        "season_id": season_id,
                        "table_name": table,
                        "column_name": col_name,
                        "total_rows": total_rows,
                        "null_count": null_count,
                        "null_pct": round(null_pct, 4),
                    }
                )
    return rows


def write_csv(path: Path, rows: Sequence[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "season_id",
                "table_name",
                "column_name",
                "total_rows",
                "null_count",
                "null_pct",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: Sequence[Dict[str, object]], tables: Sequence[str], seasons: Sequence[int]) -> None:
    for season_id in seasons:
        print(f"\n=== season_id={season_id} ===")
        season_rows = [row for row in rows if row["season_id"] == season_id]
        for table in tables:
            table_rows = [row for row in season_rows if row["table_name"] == table]
            if not table_rows:
                print(f"[{table}] no rows")
                continue
            total_rows = table_rows[0]["total_rows"]
            null_rows = [row for row in table_rows if int(row["null_count"]) > 0]
            print(f"[{table}] total_rows={total_rows}; null_columns={len(null_rows)}")
            for row in null_rows:
                print(f"  - {row['column_name']}: {row['null_count']} ({row['null_pct']}%)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count NULL values by column and season for PostgreSQL tables.",
    )
    parser.add_argument(
        "--tables",
        default=",".join(DEFAULT_MATCH_TABLES),
        help="Comma-separated table names. Default: match-level tables.",
    )
    parser.add_argument(
        "--seasons",
        default="",
        help="Comma-separated season_id list (e.g. 20242025,20252026). "
        "If empty, seasons are auto-detected from games.",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent / "nulls_by_season_report.csv"),
        help="Output CSV path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tables = _parse_csv_list(args.tables)
    if not tables:
        raise ValueError("No tables provided")

    with _connect() as conn, conn.cursor() as cur:
        if args.seasons.strip():
            seasons = [int(item) for item in _parse_csv_list(args.seasons)]
        else:
            seasons = _available_seasons(cur)
        if not seasons:
            raise ValueError("No seasons found in games table")

        rows = build_report_rows(cur, tables=tables, seasons=seasons)

    out_path = Path(args.output).resolve()
    write_csv(out_path, rows)
    print(f"saved_csv={out_path}")
    print_summary(rows, tables=tables, seasons=seasons)


if __name__ == "__main__":
    main()
