"""Сбор сырого слоя матчей 1917/18–2008/09 в кэш ``all_data/raw`` (Задача 155).

Список завершённых матчей сезона — ``fetch_final_games`` загрузчика, протоколы —
``fetch_game_json`` (он же пишет и читает кэш). В базу ничего не пишется. Сезоны
идут от новых к старым; уже лежащий в кэше полный ответ не запрашивается заново,
поэтому повторный запуск докачивает только недостающее. В конце сезона — сверка
числа файлов каждого типа со списком матчей; итог дописывается в ``SUMMARY``.
Запуск — ``sh scripts/history_raw/collect.sh``.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.load_season_modern import (  # noqa: E402
    _GAME_ENDPOINT_SUFFIXES,
    RAW_CACHE_DIR,
    ModernNhlLoader,
)

FIRST_SEASON_START = 1917
LAST_SEASON_START = 2008
SUMMARY = RAW_CACHE_DIR / "history_collect_summary.tsv"


def collect_game(loader: ModernNhlLoader, game_id: int) -> List[str]:
    """Кладёт в кэш три протокола матча; возвращает эндпоинты, не легшие в кэш.

    Зачем: неполный ответ (``payload_complete``) загрузчик не кэширует, а сбой
    сети после всех его повторов — не повод бросать сбор на сутки: такие матчи
    называются в сверке сезона и докачиваются следующим запуском.

    Args:
        loader: загрузчик с выставленным ``season_id``.
        game_id: id матча NHL.
    """
    missed = []
    for endpoint, suffix in _GAME_ENDPOINT_SUFFIXES.items():
        try:
            loader.fetch_game_json(game_id, endpoint)
        except RuntimeError as exc:
            print(f"  {game_id} {endpoint}: {exc}", flush=True)
        if not (RAW_CACHE_DIR / str(loader.season_id) / f"{game_id}.{suffix}.json.gz").exists():
            missed.append(endpoint)
    return missed


def collect_season(season_id: int, sample: Optional[int], workers: int) -> bool:
    """Собирает сезон и сверяет кэш со списком матчей; True — сошлось.

    Args:
        season_id: сезон вида ``19971998``.
        sample: только первые N матчей сезона (проба), ``None`` — все.
        workers: число параллельных потоков запросов.
    """
    loader = ModernNhlLoader()
    loader.season_id = season_id
    loader.start_date, loader.end_date = "1900-01-01", "2100-12-31"
    games = sorted(g["id"] for g in loader.fetch_final_games())
    if sample is not None:
        games = games[:sample]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        missed = dict(zip(games, pool.map(lambda gid: collect_game(loader, gid), games)))
    missed = {gid: eps for gid, eps in missed.items() if eps}

    season_dir = RAW_CACHE_DIR / str(season_id)
    counts = {
        suffix: sum(1 for gid in games if (season_dir / f"{gid}.{suffix}.json.gz").exists())
        for suffix in _GAME_ENDPOINT_SUFFIXES.values()
    }
    size_kb = sum(p.stat().st_size for p in season_dir.glob("*.json.gz")) // 1024 if season_dir.exists() else 0
    ok = not missed
    print(
        f"{season_id}: матчей {len(games)}, pbp {counts['pbp']}, box {counts['box']}, "
        f"landing {counts['landing']}, {size_kb} КБ, {time.monotonic() - started:.0f} с"
        + ("" if ok else f" — НЕ ЛЕГЛИ: {missed}"),
        flush=True,
    )
    if sample is None:
        with SUMMARY.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{season_id}\t{len(games)}\t{counts['pbp']}\t{counts['box']}\t"
                f"{counts['landing']}\t{size_kb}\t{missed or ''}\n"
            )
    return ok


def main(argv: Optional[List[str]] = None) -> None:
    """CLI: сезоны ``--from``..``--to`` от новых к старым; код выхода 1 — есть расхождения."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="newest", type=int, default=LAST_SEASON_START,
                        help="первый год самого нового сезона (по умолчанию 2008)")
    parser.add_argument("--to", dest="oldest", type=int, default=FIRST_SEASON_START,
                        help="первый год самого старого сезона (по умолчанию 1917)")
    parser.add_argument("--seasons", type=int, nargs="*", help="только эти сезоны (первый год)")
    parser.add_argument("--sample", type=int, help="проба: первые N матчей сезона")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    years = args.seasons or range(args.newest, args.oldest - 1, -1)
    results = [collect_season(y * 10000 + y + 1, args.sample, args.workers) for y in years]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
