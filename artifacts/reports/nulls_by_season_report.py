"""Build NULL-count report by season directly from PostgreSQL.

Outputs:
    * CSV: one row per (season_id, table, column) with null_count / null_pct.
    * HTML (default): self-contained page with per-season summary cards,
      a grouped bar chart of NULL share per table, and per-table detail tables
      with charts of the most NULL-heavy columns. Pure inline SVG, no CDN.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

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


# ---------------------------------------------------------------------------
# HTML rendering (self-contained: inline CSS + inline SVG, no CDN).
# ---------------------------------------------------------------------------


def _html_escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _season_label(season_id: int) -> str:
    text = str(season_id)
    if len(text) == 8 and text.isdigit():
        return f"{text[2:4]}/{text[6:8]}"
    return text


def _truncate(value: str, limit: int = 26) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _compute_table_overview(
    rows: Sequence[Dict[str, object]],
    tables: Sequence[str],
    seasons: Sequence[int],
) -> Dict[Tuple[str, int], Dict[str, float]]:
    """For each (table, season_id), aggregate row/cell/NULL totals."""
    overview: Dict[Tuple[str, int], Dict[str, float]] = {}
    for table in tables:
        for season_id in seasons:
            relevant = [
                r for r in rows
                if r["table_name"] == table and int(r["season_id"]) == int(season_id)
            ]
            if not relevant:
                continue
            total_rows = int(relevant[0]["total_rows"])
            null_cells = sum(int(r["null_count"]) for r in relevant)
            total_cells = total_rows * len(relevant)
            null_pct = (null_cells / total_cells * 100.0) if total_cells else 0.0
            overview[(table, int(season_id))] = {
                "total_rows": total_rows,
                "total_columns": len(relevant),
                "null_columns": sum(1 for r in relevant if int(r["null_count"]) > 0),
                "null_cells": null_cells,
                "total_cells": total_cells,
                "null_pct": null_pct,
            }
    return overview


def _nice_ceiling(value: float) -> float:
    """Round value up to a tidy axis maximum (1 / 2 / 5 × 10^n)."""
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 2, 2.5, 5, 10):
        candidate = mult * base
        if candidate >= value:
            return candidate
    return 10 * base


def render_grouped_bar_chart(
    *,
    title: str,
    categories: Sequence[str],
    series: Sequence[Tuple[str, Sequence[float]]],
    y_label: str = "%",
    width: int = 920,
    height: int = 380,
    label_max_chars: int = 26,
) -> str:
    margin_top, margin_right, margin_bottom, margin_left = 56, 24, 130, 70
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    if not categories or not series:
        return (
            f'<svg width="{width}" height="80" class="chart" '
            'xmlns="http://www.w3.org/2000/svg">'
            f'<text x="10" y="40">No data to plot — {_html_escape(title)}</text></svg>'
        )

    all_vals = [float(v) for _, vals in series for v in vals]
    max_y = _nice_ceiling(max(all_vals) if all_vals else 0.0)

    n_series = len(series)
    n_cats = len(categories)
    group_w = plot_w / n_cats
    bar_w = (group_w * 0.72) / n_series
    group_pad = group_w * 0.14

    palette = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2", "#ca8a04"]

    out: List[str] = []
    out.append(
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'xmlns="http://www.w3.org/2000/svg" class="chart">'
    )
    out.append(
        '<style>'
        '.chart text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;'
        'font-size:11px;fill:#333}'
        '.chart .title{font-size:14px;font-weight:600;fill:#111}'
        '.chart .axis{stroke:#888;stroke-width:1}'
        '.chart .grid{stroke:#eee;stroke-width:1}'
        '.chart .ylab{font-size:11px;fill:#666}'
        '</style>'
    )
    out.append(f'<text class="title" x="{width / 2}" y="22" text-anchor="middle">{_html_escape(title)}</text>')

    legend_x = margin_left
    legend_y = 42
    swatch = 11
    cur_x = legend_x
    for idx, (label, _) in enumerate(series):
        color = palette[idx % len(palette)]
        out.append(f'<rect x="{cur_x:.1f}" y="{legend_y - swatch:.1f}" width="{swatch}" height="{swatch}" fill="{color}" />')
        out.append(f'<text x="{cur_x + swatch + 5:.1f}" y="{legend_y:.1f}">{_html_escape(label)}</text>')
        cur_x += swatch + 8 + len(str(label)) * 7 + 18

    n_grid = 5
    for i in range(n_grid + 1):
        y = margin_top + plot_h - (plot_h * i / n_grid)
        v = max_y * i / n_grid
        out.append(
            f'<line class="grid" x1="{margin_left}" y1="{y:.1f}" '
            f'x2="{margin_left + plot_w}" y2="{y:.1f}" />'
        )
        text_label = f"{v:.0f}" if max_y >= 10 else f"{v:.2f}"
        out.append(f'<text x="{margin_left - 8:.1f}" y="{y + 3:.1f}" text-anchor="end">{text_label}</text>')

    out.append(
        f'<line class="axis" x1="{margin_left}" y1="{margin_top}" '
        f'x2="{margin_left}" y2="{margin_top + plot_h}" />'
    )
    out.append(
        f'<line class="axis" x1="{margin_left}" y1="{margin_top + plot_h}" '
        f'x2="{margin_left + plot_w}" y2="{margin_top + plot_h}" />'
    )

    y_label_x = margin_left - 50
    y_label_y = margin_top + plot_h / 2
    out.append(
        f'<text class="ylab" x="{y_label_x}" y="{y_label_y}" text-anchor="middle" '
        f'transform="rotate(-90 {y_label_x},{y_label_y})">{_html_escape(y_label)}</text>'
    )

    for ci, cat in enumerate(categories):
        gx = margin_left + group_w * ci + group_pad
        for si, (series_label, vals) in enumerate(series):
            v = float(vals[ci]) if ci < len(vals) else 0.0
            bh = (v / max_y) * plot_h if max_y else 0.0
            x = gx + bar_w * si
            y = margin_top + plot_h - bh
            color = palette[si % len(palette)]
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}">'
                f'<title>{_html_escape(series_label)} | {_html_escape(cat)}: {v:.2f}</title>'
                f'</rect>'
            )
        cx = margin_left + group_w * ci + group_w / 2
        baseline_y = margin_top + plot_h + 14
        display = _truncate(str(cat), label_max_chars)
        out.append(
            f'<text x="{cx:.1f}" y="{baseline_y:.1f}" text-anchor="end" '
            f'transform="rotate(-40 {cx:.1f},{baseline_y:.1f})">'
            f'<title>{_html_escape(cat)}</title>{_html_escape(display)}</text>'
        )

    out.append('</svg>')
    return "".join(out)


def render_html(
    rows: Sequence[Dict[str, object]],
    tables: Sequence[str],
    seasons: Sequence[int],
    *,
    top_columns_per_table: int = 12,
) -> str:
    overview = _compute_table_overview(rows, tables, seasons)

    overview_categories = [
        table for table in tables if any((table, s) in overview for s in seasons)
    ]
    overview_series: List[Tuple[str, List[float]]] = []
    for season_id in seasons:
        per_table_pct = [
            round(overview.get((table, season_id), {"null_pct": 0.0})["null_pct"], 2)
            for table in overview_categories
        ]
        overview_series.append((_season_label(season_id), per_table_pct))
    overview_chart = render_grouped_bar_chart(
        title="Overall NULL share per table (% of cells)",
        categories=overview_categories,
        series=overview_series,
        y_label="% NULL cells",
        label_max_chars=40,
    )

    parts: List[str] = []
    parts.append(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>NHL DB — NULL counts by season</title>'
        '<style>'
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;'
        'margin:24px;color:#1a1a1a;max-width:1200px;}'
        'h1{margin-top:0}'
        'h2{margin-top:32px;border-bottom:1px solid #ddd;padding-bottom:6px;font-size:18px}'
        'h2 .small{font-size:13px;color:#666;font-weight:400;margin-left:8px}'
        '.muted{color:#666}'
        '.cards{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px}'
        '.card{flex:1 1 240px;background:#f9fafb;border:1px solid #e5e5e5;'
        'border-radius:8px;padding:12px}'
        '.card h3{margin:0 0 8px 0;font-size:14px}'
        '.card .stat{display:flex;justify-content:space-between;font-size:12px;padding:2px 0}'
        '.bar{background:#eee;height:8px;border-radius:4px;overflow:hidden;margin-top:4px}'
        '.bar>span{display:block;height:100%;background:#dc2626}'
        'svg.chart{display:block;margin:12px 0;max-width:100%;height:auto}'
        'table.detail{border-collapse:collapse;width:100%;font-size:13px;margin-top:12px}'
        'table.detail th,table.detail td{border:1px solid #e5e5e5;padding:6px 8px;text-align:right}'
        'table.detail th{background:#f5f5f5;text-align:center;font-weight:600}'
        'table.detail td.col{text-align:left;font-family:ui-monospace,Menlo,Consolas,monospace}'
        'table.detail tr:nth-child(even) td{background:#fafafa}'
        'table.detail td.hi{background:#fef2f2!important;color:#991b1b;font-weight:600}'
        '.legend{font-size:12px;color:#555;margin-top:6px}'
        '.legend code{background:#f1f5f9;padding:1px 5px;border-radius:3px}'
        '</style></head><body>'
    )
    parts.append('<h1>NHL DB — NULL counts by season</h1>')

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    season_text = ", ".join(f"{_season_label(s)} ({s})" for s in seasons)
    parts.append(
        f'<p class="muted">Generated at {_html_escape(generated_at)}'
        f' &middot; Seasons: {_html_escape(season_text)}'
        f' &middot; Tables: {_html_escape(", ".join(tables))}</p>'
    )
    parts.append(
        '<p class="legend">'
        'For per-game tables (everything except <code>games</code>) rows are joined to '
        '<code>games</code> on <code>game_id</code>, so the season filter follows '
        '<code>games.season_id</code>.'
        '</p>'
    )

    parts.append('<h2>Summary by season</h2><div class="cards">')
    for season_id in seasons:
        per_table = [
            (table, overview[(table, season_id)])
            for table in tables
            if (table, season_id) in overview
        ]
        rows_total = sum(int(o["total_rows"]) for _, o in per_table)
        cells_total = sum(int(o["total_cells"]) for _, o in per_table)
        nulls_total = sum(int(o["null_cells"]) for _, o in per_table)
        null_pct = (nulls_total / cells_total * 100.0) if cells_total else 0.0
        bar_pct = min(null_pct, 100.0)
        parts.append(
            f'<div class="card"><h3>{_html_escape(_season_label(season_id))} '
            f'<span class="muted">({season_id})</span></h3>'
            f'<div class="stat"><span>Rows across tracked tables</span><b>{rows_total:,}</b></div>'
            f'<div class="stat"><span>Cells inspected</span><b>{cells_total:,}</b></div>'
            f'<div class="stat"><span>NULL cells</span><b>{nulls_total:,}</b></div>'
            f'<div class="stat"><span>NULL share</span><b>{null_pct:.2f}%</b></div>'
            f'<div class="bar"><span style="width:{bar_pct:.2f}%"></span></div>'
            '</div>'
        )
    parts.append('</div>')

    parts.append('<h2>Overall NULL share per table</h2>')
    parts.append(overview_chart)

    for table in tables:
        per_col: Dict[str, Dict[int, Dict[str, object]]] = {}
        for r in rows:
            if r["table_name"] != table:
                continue
            per_col.setdefault(str(r["column_name"]), {})[int(r["season_id"])] = r
        if not per_col:
            parts.append(
                f'<h2>{_html_escape(table)}</h2>'
                '<p class="muted">No rows for this table in any of the selected seasons.</p>'
            )
            continue

        cols_with_nulls = [
            col for col, by_season in per_col.items()
            if any(int(by_season.get(s, {"null_count": 0})["null_count"]) > 0 for s in seasons)
        ]

        info_chunks: List[str] = []
        for season_id in seasons:
            ov = overview.get((table, season_id))
            if not ov:
                continue
            info_chunks.append(
                f'<b>{_html_escape(_season_label(season_id))}</b>: '
                f'{int(ov["total_rows"]):,} rows, '
                f'{int(ov["null_columns"])} of {int(ov["total_columns"])} cols with NULLs, '
                f'{ov["null_pct"]:.2f}% NULL cells'
            )
        parts.append(
            f'<h2>{_html_escape(table)}'
            f'<span class="small">{int(len(per_col))} columns total</span></h2>'
        )
        parts.append('<p>' + ' &nbsp;|&nbsp; '.join(info_chunks) + '</p>')

        if not cols_with_nulls:
            parts.append('<p class="muted">No NULL values in any column for the selected seasons.</p>')
            continue

        def _max_pct(col_name: str) -> float:
            return max(
                float(per_col[col_name].get(s, {"null_pct": 0.0})["null_pct"]) for s in seasons
            )

        cols_sorted = sorted(cols_with_nulls, key=_max_pct, reverse=True)
        top_cols = cols_sorted[:top_columns_per_table]

        chart_series = [
            (
                _season_label(season_id),
                [float(per_col[c].get(season_id, {"null_pct": 0.0})["null_pct"]) for c in top_cols],
            )
            for season_id in seasons
        ]
        parts.append(
            render_grouped_bar_chart(
                title=f"Top {len(top_cols)} columns by NULL% — {table}",
                categories=top_cols,
                series=chart_series,
                y_label="% NULL",
            )
        )

        parts.append('<table class="detail"><thead><tr><th rowspan="2">Column</th>')
        for season_id in seasons:
            parts.append(
                f'<th colspan="2">{_html_escape(_season_label(season_id))} '
                f'<span class="muted">({season_id})</span></th>'
            )
        parts.append('</tr><tr>')
        for _ in seasons:
            parts.append('<th>NULLs</th><th>%</th>')
        parts.append('</tr></thead><tbody>')
        for col in cols_sorted:
            parts.append(f'<tr><td class="col">{_html_escape(col)}</td>')
            for season_id in seasons:
                row = per_col[col].get(season_id)
                if row is None:
                    parts.append('<td>—</td><td>—</td>')
                    continue
                pct = float(row["null_pct"])
                pct_class = ' class="hi"' if pct >= 50.0 else ''
                parts.append(
                    f'<td>{int(row["null_count"]):,}</td>'
                    f'<td{pct_class}>{pct:.2f}</td>'
                )
            parts.append('</tr>')
        parts.append('</tbody></table>')

    parts.append('</body></html>')
    return "".join(parts)


def write_html(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


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
    parser.add_argument(
        "--html",
        default=None,
        help="Output HTML path (default: <output>.html next to the CSV).",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip HTML generation (CSV-only mode).",
    )
    parser.add_argument(
        "--top-columns",
        type=int,
        default=12,
        help="Top-N columns by max NULL%% to plot per table (default: 12).",
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

    if not args.no_html:
        html_path = Path(args.html).resolve() if args.html else out_path.with_suffix(".html")
        html = render_html(
            rows,
            tables=tables,
            seasons=seasons,
            top_columns_per_table=args.top_columns,
        )
        write_html(html_path, html)
        print(f"saved_html={html_path}")

    print_summary(rows, tables=tables, seasons=seasons)


if __name__ == "__main__":
    main()
