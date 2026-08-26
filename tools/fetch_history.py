"""Скачивание исторических данных Bybit для офлайн-прогона стратегии.

Отделено от прогона намеренно: скачивание требует сетевого доступа к
api.bybit.com, прогон — нет. Скачали один раз, прогоняем сколько угодно
раз без нагрузки на API и без зависимости от доступности биржи.

Запуск:
    python3 -m tools.fetch_history --days 90 --symbols 60

Результат: data/history/<SYMBOL>.json на каждый символ + _meta.json.

ЧЕГО В ИСТОРИИ НЕТ и почему это важно:
  * стакан — Bybit не отдаёт исторические снапшоты. В прогоне стакан
    нейтрален, то есть голос стакана и очки за него отсутствуют;
  * лента сделок — то же самое, flow_* в прогоне пустые.
Оба фактора в боевом скоринге дают 14-20 очков из ~64 и участвуют в
выборе направления. Значит прогон измеряет ЯДРО стратегии (цена, объём,
VSA, уровни, фандинг, OI), а не её боевую копию. Выдавать одно за другое
нельзя — это записано и в отчёте прогона.
"""
import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.bybit import BybitClient  # noqa: E402

HIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "history")

_MS_MIN = 60_000


async def _klines_range(client: BybitClient, symbol: str, interval: str,
                        start_ms: int, end_ms: int) -> List[Dict]:
    """Все свечи интервала за окно, по возрастанию времени, без дублей.

    Bybit отдаёт максимум 1000 за запрос и НОВЕЙШИЕ первыми, поэтому идём
    окном назад от end_ms. Курсор двигается по времени самой старой
    полученной свечи; если он не сдвинулся — выходим, иначе бесконечный цикл
    (та же защита, что в пагинации боевого клиента).
    """
    out: Dict[int, Dict] = {}
    cursor = end_ms
    while cursor > start_ms:
        data = await client._get("/v5/market/kline", {
            "category": "linear", "symbol": symbol, "interval": interval,
            "start": start_ms, "end": cursor, "limit": 1000,
        })
        raw = data.get("result", {}).get("list", [])
        if not raw:
            break
        for r in raw:
            ts = int(r[0])
            out[ts] = {"ts": ts, "open": float(r[1]), "high": float(r[2]),
                       "low": float(r[3]), "close": float(r[4]),
                       "volume": float(r[5]), "turnover": float(r[6])}
        oldest = min(int(r[0]) for r in raw)
        if oldest >= cursor:      # курсор не сдвинулся — выходим
            break
        cursor = oldest - 1
        await asyncio.sleep(0.15)
    return [out[k] for k in sorted(out)]


async def _oi_range(client: BybitClient, symbol: str,
                    start_ms: int, end_ms: int) -> List[Dict]:
    out: Dict[int, Dict] = {}
    cursor = end_ms
    while cursor > start_ms:
        data = await client._get("/v5/market/open-interest", {
            "category": "linear", "symbol": symbol, "intervalTime": "4h",
            "startTime": start_ms, "endTime": cursor, "limit": 200,
        })
        raw = data.get("result", {}).get("list", [])
        if not raw:
            break
        for r in raw:
            ts = int(r["timestamp"])
            out[ts] = {"ts": ts, "oi": float(r["openInterest"])}
        oldest = min(int(r["timestamp"]) for r in raw)
        if oldest >= cursor:
            break
        cursor = oldest - 1
        await asyncio.sleep(0.15)
    return [out[k] for k in sorted(out)]


async def _funding_range(client: BybitClient, symbol: str,
                         start_ms: int, end_ms: int) -> List[Dict]:
    out: Dict[int, Dict] = {}
    cursor = end_ms
    while cursor > start_ms:
        data = await client._get("/v5/market/funding/history", {
            "category": "linear", "symbol": symbol,
            "startTime": start_ms, "endTime": cursor, "limit": 200,
        })
        raw = data.get("result", {}).get("list", [])
        if not raw:
            break
        for r in raw:
            ts = int(r["fundingRateTimestamp"])
            out[ts] = {"ts": ts, "rate": float(r["fundingRate"])}
        oldest = min(int(r["fundingRateTimestamp"]) for r in raw)
        if oldest >= cursor:
            break
        cursor = oldest - 1
        await asyncio.sleep(0.15)
    return [out[k] for k in sorted(out)]


async def fetch_symbol(client: BybitClient, symbol: str,
                       start_ms: int, end_ms: int) -> Dict[str, Any]:
    info = await client.get_instrument_info(symbol)
    k4 = await _klines_range(client, symbol, "240", start_ms, end_ms)
    k1 = await _klines_range(client, symbol, "60", start_ms, end_ms)
    # 15m нужны оценщику: он судит исход по ним, и брать 4h вместо них
    # нельзя — в одной 4h-свече цена успевает задеть и стоп, и цель.
    k15 = await _klines_range(client, symbol, "15", start_ms, end_ms)
    oi = await _oi_range(client, symbol, start_ms, end_ms)
    fund = await _funding_range(client, symbol, start_ms, end_ms)
    return {
        "symbol": symbol,
        "launch_ms": float(info.get("launchTime") or 0),
        "k4": k4, "k1": k1, "k15": k15, "oi": oi, "funding": fund,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", type=int, default=60)
    ap.add_argument("--out", default=HIST_DIR)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    client = BybitClient(api_key="", secret="")
    try:
        tickers = await client.get_tickers()
        if not tickers:
            print("Не удалось получить тикеры — нет доступа к api.bybit.com?",
                  file=sys.stderr)
            return 1
        usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        usdt.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
        picked = usdt[:args.symbols]

        now_ms = int(picked[0].get("time") or 0) or None
        if not now_ms:
            import time as _t
            now_ms = int(_t.time() * 1000)
        start_ms = now_ms - args.days * 24 * 60 * _MS_MIN

        meta = {"days": args.days, "start_ms": start_ms, "end_ms": now_ms,
                "symbols": [t["symbol"] for t in picked]}
        for i, t in enumerate(picked, 1):
            sym = t["symbol"]
            try:
                payload = await fetch_symbol(client, sym, start_ms, now_ms)
            except Exception as e:
                print(f"[{i}/{len(picked)}] {sym}: ОШИБКА {e}", file=sys.stderr)
                continue
            with open(os.path.join(args.out, f"{sym}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(payload, f)
            print(f"[{i}/{len(picked)}] {sym}: 4h={len(payload['k4'])} "
                  f"1h={len(payload['k1'])} 15m={len(payload['k15'])} "
                  f"oi={len(payload['oi'])} fund={len(payload['funding'])}")
        with open(os.path.join(args.out, "_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        print(f"\nГотово: {args.out}")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
