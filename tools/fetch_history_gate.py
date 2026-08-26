"""Скачивание истории через публичный API Gate.io.

ПОЧЕМУ НЕ BYBIT. api.bybit.com и api.bytick.com отдают 403 из дата-центра,
где работает сессия: CloudFront Bybit блокирует страну выхода. Это
блокировка биржи, не политика окружения, и allowlist её не снимает.
Публичный архив Bybit доступен, но в нём НЕТ open interest, а без него
oi_change = 0 у каждого сигнала — это минус до 30 очков из ~64 и другой
набор типов в _classify_type, то есть измерялась бы другая стратегия.
Дампы Binance содержат всё нужное, но требуют тысяч запросов на мелкие
дневные файлы: только ради рейтинга по обороту пришлось бы опросить 832
символа. Gate отдаёт то же самое десятком запросов на символ.

Gate отдаёт свечи, open interest и фандинг большими кусками: ~10 запросов
на символ вместо сотен.

ЧЕСТНО О ПОДМЕНЕ. Это данные Gate, а бот торгует на Bybit. Цена и объём по
одному и тому же перпу между площадками держатся арбитражем и почти
совпадают — уровни, ATR и VSA меряются нормально. Open interest и фандинг
у каждой биржи СВОИ: близкие по смыслу, но не те же числа. Значит прогон
отвечает на вопрос «есть ли преимущество у ядра стратегии на этих
инструментах», а не «сколько именно заработал бы бот на Bybit».

Вселенная символов ограничена теми, что РЕАЛЬНО торгуются на Bybit (список
берётся из public.bybit.com): у Gate есть ещё перпы на акции и золото,
которые боевой сканер никогда бы не увидел.

Выход — та же схема, что у tools/fetch_history.py, поэтому tools/replay.py
и его 15 тестов работают без изменений.

Запуск:
    python3 -m tools.fetch_history_gate --days 60 --symbols 25
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

GATE = "https://api.gateio.ws/api/v4/futures/usdt"
BYBIT_ARCHIVE = "https://public.bybit.com/trading/"
HIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "history")
_H4 = 4 * 3600

# ВАЖНО: trust_env=True при создании сессии. aiohttp НЕ читает HTTPS_PROXY
# сам, в отличие от curl и urllib, — без этого флага запросы идут мимо
# прокси окружения и возвращают 403 на любой хост. Полдня ушло на ложный
# диагноз «лимит частоты биржи», пока сравнение curl и aiohttp не показало
# разницу. Частота всё равно ограничивается глобально: пауза внутри
# семафора задаёт не заданную частоту, а (слоты / пауза).
_SEM = asyncio.Semaphore(3)
_MIN_GAP = 1.0 / 5.0
_LOCK = asyncio.Lock()
_NEXT_AT = 0.0


async def _pace() -> None:
    global _NEXT_AT
    loop = asyncio.get_running_loop()
    async with _LOCK:
        now = loop.time()
        wait = max(0.0, _NEXT_AT - now)
        _NEXT_AT = max(now, _NEXT_AT) + _MIN_GAP
    if wait:
        await asyncio.sleep(wait)


async def _get(session, url: str, params: Optional[Dict] = None) -> Optional[Any]:
    for delay in (2, 5, 12):
        await _pace()
        async with _SEM:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        return await r.json()
                    status = r.status
            except Exception as e:
                status = type(e).__name__
        await asyncio.sleep(delay)
    print(f"  сдались ({status}): {url} {params}", file=sys.stderr)
    return None


async def bybit_symbols(session) -> set:
    """Символы, реально торгуемые на Bybit — из её публичного архива."""
    await _pace()
    async with session.get(BYBIT_ARCHIVE,
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        html = await r.text()
    return set(re.findall(r'href="([A-Z0-9]+USDT)/"', html))


async def candles(session, contract: str, interval: str, mult: float,
                  start_s: int, end_s: int) -> List[Dict]:
    """Свечи Gate -> схема боевого клиента (ts в МИЛЛИсекундах, как у Bybit)."""
    step = {"4h": _H4, "1h": 3600, "15m": 900}[interval]
    got: Dict[int, Dict] = {}
    cur = start_s
    while cur < end_s:
        chunk = min(end_s, cur + step * 1900)
        rows = await _get(session, f"{GATE}/candlesticks",
                          {"contract": contract, "interval": interval,
                           "from": cur, "to": chunk})
        if not rows:
            break
        for r in rows:
            t = int(r["t"])
            got[t] = {"ts": t * 1000, "open": float(r["o"]), "high": float(r["h"]),
                      "low": float(r["l"]), "close": float(r["c"]),
                      # v — размер в КОНТРАКТАХ; боевой код читает базовый
                      # объём, поэтому домножаем на quanto_multiplier.
                      "volume": float(r["v"]) * mult,
                      "turnover": float(r.get("sum") or 0)}
        cur = chunk
    return [got[k] for k in sorted(got)]


async def open_interest(session, contract: str, mult: float,
                        start_s: int, end_s: int) -> List[Dict]:
    got: Dict[int, Dict] = {}
    cur = start_s
    while cur < end_s:
        rows = await _get(session, f"{GATE}/contract_stats",
                          {"contract": contract, "interval": "4h",
                           "from": cur, "limit": 100})
        if not rows:
            break
        for r in rows:
            t = int(r["time"])
            if r.get("open_interest") is not None:
                got[t] = {"ts": t * 1000, "oi": float(r["open_interest"]) * mult}
        nxt = max(int(r["time"]) for r in rows) + _H4
        if nxt <= cur:
            break
        cur = nxt
    return [got[k] for k in sorted(got) if start_s <= k <= end_s]


async def funding(session, contract: str, start_s: int, end_s: int) -> List[Dict]:
    rows = await _get(session, f"{GATE}/funding_rate",
                      {"contract": contract, "limit": 1000})
    if not rows:
        return []
    out = {int(r["t"]): {"ts": int(r["t"]) * 1000, "rate": float(r["r"])}
           for r in rows if start_s <= int(r["t"]) <= end_s}
    return [out[k] for k in sorted(out)]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=int, default=25)
    ap.add_argument("--out", default=HIST_DIR)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    end = datetime.now(timezone.utc) - timedelta(hours=6)
    start = end - timedelta(days=args.days)
    start_s, end_s = int(start.timestamp()), int(end.timestamp())

    async with aiohttp.ClientSession(trust_env=True) as s:
        on_bybit = await bybit_symbols(s)
        print(f"символов на Bybit: {len(on_bybit)}", file=sys.stderr)

        tickers = await _get(s, f"{GATE}/tickers") or []
        contracts = await _get(s, f"{GATE}/contracts") or []
        meta_by_name = {c["name"]: c for c in contracts}

        cand = []
        for t in tickers:
            name = t.get("contract", "")
            if not name.endswith("_USDT"):
                continue
            # Отсекаем перпы на акции/сырьё: боевой сканер их не видит.
            if name.replace("_", "") not in on_bybit:
                continue
            m = meta_by_name.get(name)
            if not m or m.get("in_delisting"):
                continue
            # По БАЗОВОМУ объёму, а не долларовому: боевой scan_all
            # сортирует тикеры по volume24h, и у Bybit это объём в базовой
            # монете. Из-за этого MIN_VOL_24H=2_000_000 отсекает BTC (у него
            # ~43 тыс. BTC в сутки) и пропускает дешёвые монеты с миллиардами
            # единиц. Вселенная прогона обязана повторять этот перекос,
            # иначе мы померяем не ту популяцию, которую бот торгует.
            cand.append((name, float(t.get("volume_24h_base") or 0)))
        cand.sort(key=lambda x: -x[1])
        picked = cand[:args.symbols]
        print(f"общих с Bybit: {len(cand)}, берём {len(picked)}", file=sys.stderr)
        for n, v in picked:
            print(f"  {n:<16} {v/1e6:>12.1f} млн единиц / 24ч", file=sys.stderr)

        ok = []
        for i, (name, _) in enumerate(picked, 1):
            m = meta_by_name[name]
            mult = float(m.get("quanto_multiplier") or 1) or 1.0
            k4, k1, k15, oi, fund = await asyncio.gather(
                candles(s, name, "4h", mult, start_s, end_s),
                candles(s, name, "1h", mult, start_s, end_s),
                candles(s, name, "15m", mult, start_s, end_s),
                open_interest(s, name, mult, start_s, end_s),
                funding(s, name, start_s, end_s),
            )
            if not k4 or not k15:
                print(f"[{i}/{len(picked)}] {name}: нет свечей — пропуск", file=sys.stderr)
                continue
            sym = name.replace("_", "")
            payload = {"symbol": sym,
                       # launch_time от самой биржи: гейт возраста листинга —
                       # часть измеряемой стратегии, подставлять начало окна
                       # нельзя (все монеты стали бы «свежими»).
                       "launch_ms": float(m.get("launch_time") or 0) * 1000,
                       "k4": k4, "k1": k1, "k15": k15, "oi": oi, "funding": fund}
            with open(os.path.join(args.out, f"{sym}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f)
            ok.append(sym)
            print(f"[{i}/{len(picked)}] {sym}: 4h={len(k4)} 1h={len(k1)} "
                  f"15m={len(k15)} oi={len(oi)} fund={len(fund)}", file=sys.stderr)

        with open(os.path.join(args.out, "_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"days": args.days, "symbols": ok,
                       "start_ms": start_s * 1000, "end_ms": end_s * 1000,
                       "source": "gate.io futures/usdt (не Bybit — см. шапку модуля)"},
                      f, indent=2)
    print(f"\nГотово: {len(ok)} символов", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
