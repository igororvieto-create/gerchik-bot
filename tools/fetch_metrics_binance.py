"""Снимки позиционирования для замера XI: открытый интерес и лонги/шорты.

Binance публикует их только ДНЕВНЫМИ файлами (5-минутные строки), и качать
все дни всех символов — сотни тысяч запросов. Замеру нужны снимки только на
понедельник 00:00 UTC (момент решения) и на понедельник неделей раньше
(изменение за 7 дней), и только для монет, которые на эту дату входят во
вселенную. Снимок на понедельник 00:00 — последняя строка воскресного
файла (create_time 23:55 <= t): данные, известные к моменту решения.

Запуск (после tools/fetch_universe_binance.py):
    python3 -m tools.fetch_metrics_binance
"""
import argparse
import asyncio
import csv
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fetch_universe_binance import _get                    # noqa: E402
from tools.xsec import _DAY_MS, rebalance_dates                   # noqa: E402
from tools.xsec_universe import universe_at                       # noqa: E402

_DUMPS = "https://data.binance.vision/data/futures/um/daily/metrics"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_snapshot(blob: bytes, t: int) -> Optional[Dict[str, Optional[float]]]:
    """Последняя строка с create_time <= t. Пустые поля — None, не ноль."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    best: Optional[Tuple[int, Dict[str, Optional[float]]]] = None
    for row in csv.DictReader(io.StringIO(text)):
        try:
            ts = int(datetime.strptime(row["create_time"], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except (KeyError, ValueError):
            continue
        if ts > t:
            continue

        def f(key: str) -> Optional[float]:
            try:
                v = float(row.get(key) or "")
                return v if v > 0 else None
            except ValueError:
                return None
        snap = {"oi": f("sum_open_interest"),
                "top_pos": f("sum_toptrader_long_short_ratio"),
                "acc": f("count_long_short_ratio")}
        if best is None or ts > best[0]:
            best = (ts, snap)
    return best[1] if best else None


def needed(coins: Dict[str, Dict], start: int, end: int) -> Dict[str, Set[int]]:
    """{символ: моменты t}, для которых нужен снимок: t и t − 7 дней."""
    out: Dict[str, Set[int]] = {}
    for t in rebalance_dates(start, end):
        for s in universe_at(coins, t):
            out.setdefault(s, set()).update({t, t - 7 * _DAY_MS})
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join(_ROOT, "data", "universe"))
    ap.add_argument("--out", default=os.path.join(_ROOT, "data", "metrics"))
    args = ap.parse_args()
    with open(os.path.join(args.hist, "_meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    coins: Dict[str, Dict] = {}
    for sym in meta["symbols"]:
        p = os.path.join(args.hist, f"{sym}.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                coins[sym] = json.load(fh)
    want = needed(coins, int(meta["start_ms"]), int(meta["end_ms"]))
    os.makedirs(args.out, exist_ok=True)
    total = sum(len(v) for v in want.values())
    print(f"символов: {len(want)}, снимков: {total}")

    async with aiohttp.ClientSession(trust_env=True) as session:
        done = 0
        for i, (sym, ts_set) in enumerate(sorted(want.items()), 1):
            path = os.path.join(args.out, f"{sym}.json")
            have: Dict[str, Optional[Dict]] = {}
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    have = json.load(fh).get("snap", {})
            todo = [t for t in sorted(ts_set) if str(t) not in have]

            async def one(t: int):
                day = datetime.fromtimestamp((t - _DAY_MS) / 1000, timezone.utc)
                ds = day.strftime("%Y-%m-%d")
                blob = await _get(session, f"{_DUMPS}/{sym}/{sym}-metrics-{ds}.zip")
                return t, (parse_snapshot(blob, t) if blob else None)
            for t, snap in await asyncio.gather(*[one(t) for t in todo]):
                # None записывается тоже: «снимка нет» — факт, повторно не качаем
                have[str(t)] = snap
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"symbol": sym, "snap": have}, fh)
            done += len(ts_set)
            if i % 20 == 0:
                print(f"[{i}/{len(want)}] снимков {done}/{total}")
    with open(os.path.join(args.out, "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"symbols": sorted(want), "source": "binance um daily metrics, "
                   "снимок на понедельник 00:00 UTC"}, fh)
    print("готово")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
