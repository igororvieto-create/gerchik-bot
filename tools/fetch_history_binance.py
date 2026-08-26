"""Скачивание истории из публичных дампов Binance USDT-M — один источник.

ПОЧЕМУ НЕ BYBIT. api.bybit.com и api.bytick.com отдают 403 из дата-центра
сессии: CloudFront Bybit блокирует страну выхода. Это блокировка биржи, не
политика окружения. Публичный архив Bybit доступен, но в нём нет open
interest, а без него oi_change = 0 у каждого сигнала — минус до 30 очков
из ~64 и другой набор типов, то есть другая стратегия.

ПОЧЕМУ НЕ GATE (пробовали, отпало). Свечи Gate ограничены 10 000 точками
на интервал: по 15-минуткам это 104 дня. Фандинг — 90 записей (~30 дней)
независимо от limit. На первом сборе это дало тихий провал: истории
фандинга не хватало на 70% окна, значение подставлялось нулём, и голос
фандинга не срабатывал НИ РАЗУ на 394 сделках. Прогон мерил обрубок
стратегии, и по отчёту это было не видно.

У Binance история не ограничена, и все три ряда лежат в одном месте.

ЧЕСТНО О ПОДМЕНЕ: это данные Binance, а бот торгует на Bybit. Цену и
объём по одному и тому же перпу держит арбитраж — уровни, ATR и VSA
меряются нормально. Open interest и фандинг у каждой биржи свои: близкие,
но не те же. Прогон отвечает «есть ли преимущество у ядра стратегии на
этих инструментах», а не «сколько заработал бы бот на Bybit».

Вселенная берётся из тикеров Gate, пересечённых со списком реально
торгуемых на Bybit (из её архива): у бирж есть перпы на акции и золото,
которых боевой сканер не видит. Ранжирование — по БАЗОВОМУ объёму, как в
боевом scan_all.

Выход — та же схема, что у tools/fetch_history.py, поэтому tools/replay.py
и его тесты работают без изменений.

Запуск:
    python3 -m tools.fetch_history_binance --days 100 --symbols 40
"""
import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

DUMP = "https://data.binance.vision/data/futures/um"
GATE = "https://api.gateio.ws/api/v4/futures/usdt"
BYBIT_ARCHIVE = "https://public.bybit.com/trading/"
HIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "history")
_H4_MS = 4 * 3600 * 1000

# Частота ограничивается ГЛОБАЛЬНО, а не паузой внутри семафора: пауза в
# слоте даёт частоту (слоты / пауза). И trust_env=True при создании сессии —
# aiohttp НЕ читает HTTPS_PROXY сам, без этого всё возвращает 403.
_SEM = asyncio.Semaphore(4)
_MIN_GAP = 1.0 / 6.0
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


async def _get(session, url: str, params: Optional[Dict] = None,
               as_json: bool = False) -> Any:
    for delay in (2, 5, 12):
        await _pace()
        async with _SEM:
            try:
                async with session.get(
                        url, params=params,
                        timeout=aiohttp.ClientTimeout(total=90)) as r:
                    if r.status == 404:
                        return None          # норма: нет такого месяца/дня
                    if r.status == 200:
                        return await (r.json() if as_json else r.read())
                    status = r.status
            except Exception as e:
                status = type(e).__name__
        await asyncio.sleep(delay)
    print(f"  сдались ({status}): {url}", file=sys.stderr)
    return None


def _rows(blob: bytes) -> List[List[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    out = list(csv.reader(io.StringIO(text)))
    # Заголовок есть не во всех файлах — слепой пропуск первой строки терял
    # бы свечу. Отбрасываем только если первое поле не число.
    if out and out[0] and not out[0][0].replace(".", "", 1).isdigit():
        out = out[1:]
    return [r for r in out if r]


def _months(a: datetime, b: datetime) -> List[str]:
    out, cur = [], a.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= b:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=8)).replace(day=1)
    return out


def _days(a: datetime, b: datetime) -> List[str]:
    out, cur = [], a
    while cur <= b:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


async def klines(session, sym: str, interval: str,
                 a: datetime, b: datetime) -> List[Dict]:
    got: Dict[int, Dict] = {}
    first_of_month = b.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    urls = [f"{DUMP}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{m}.zip"
            for m in _months(a, b)]
    # Дневные нужны ТОЛЬКО если окно кончается посреди месяца. Сейчас оно
    # всегда кончается закрытым месяцем, и без этой проверки на каждый
    # символ уходило по 93 лишних запроса (31 день x 3 интервала).
    month_end = (b + timedelta(seconds=2)).day == 1
    if not month_end:
        urls += [f"{DUMP}/daily/klines/{sym}/{interval}/{sym}-{interval}-{d}.zip"
                 for d in _days(max(first_of_month, a), b)]
    for blob in await asyncio.gather(*[_get(session, u) for u in urls]):
        if not blob:
            continue
        for r in _rows(blob):
            try:
                ts = int(r[0])
            except (ValueError, IndexError):
                continue
            got[ts] = {"ts": ts, "open": float(r[1]), "high": float(r[2]),
                       "low": float(r[3]), "close": float(r[4]),
                       "volume": float(r[5]), "turnover": float(r[7])}
    lo, hi = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    return [got[k] for k in sorted(got) if lo <= k <= hi]


async def funding(session, sym: str, a: datetime, b: datetime) -> List[Dict]:
    got: Dict[int, Dict] = {}
    urls = [f"{DUMP}/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip"
            for m in _months(a, b)]
    for blob in await asyncio.gather(*[_get(session, u) for u in urls]):
        if not blob:
            continue
        for r in _rows(blob):
            try:
                got[int(r[0])] = {"ts": int(r[0]), "rate": float(r[2])}
            except (ValueError, IndexError):
                continue
    lo, hi = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    return [got[k] for k in sorted(got) if lo <= k <= hi]


async def open_interest(session, sym: str, a: datetime, b: datetime) -> List[Dict]:
    """OI на 4h-сетке: последний замер НЕ ПОЗЖЕ каждой границы.

    Ближайший по времени брать нельзя — ближайшим может оказаться замер из
    будущего. Дампы metrics только дневные.
    """
    samples: List[tuple] = []
    urls = [f"{DUMP}/daily/metrics/{sym}/{sym}-metrics-{d}.zip"
            for d in _days(a, b)]
    for blob in await asyncio.gather(*[_get(session, u) for u in urls]):
        if not blob:
            continue
        for r in _rows(blob):
            try:
                t = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
                samples.append((int(t.timestamp() * 1000), float(r[2])))
            except (ValueError, IndexError):
                continue
    if not samples:
        return []
    samples.sort()
    out, i, last = [], 0, None
    lo = (int(a.timestamp() * 1000) // _H4_MS + 1) * _H4_MS
    hi = int(b.timestamp() * 1000)
    for edge in range(lo, hi + 1, _H4_MS):
        while i < len(samples) and samples[i][0] <= edge:
            last = samples[i][1]
            i += 1
        if last is not None:
            out.append({"ts": edge, "oi": last})
    return out


async def universe(session, n: int) -> List[tuple]:
    """(символ, launch_ms) — топ-N по базовому объёму среди торгуемых на Bybit."""
    await _pace()
    async with session.get(BYBIT_ARCHIVE,
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        on_bybit = set(re.findall(r'href="([A-Z0-9]+USDT)/"', await r.text()))
    print(f"символов на Bybit: {len(on_bybit)}", file=sys.stderr)

    tickers = await _get(session, f"{GATE}/tickers", as_json=True) or []
    contracts = await _get(session, f"{GATE}/contracts", as_json=True) or []
    meta = {c["name"]: c for c in contracts}
    cand = []
    for t in tickers:
        name = t.get("contract", "")
        sym = name.replace("_", "")
        if not name.endswith("_USDT") or sym not in on_bybit:
            continue
        m = meta.get(name)
        if not m or m.get("in_delisting"):
            continue
        # Базовый объём, как в боевом scan_all (у Bybit volume24h — база).
        cand.append((sym, float(t.get("volume_24h_base") or 0),
                     float(m.get("launch_time") or 0) * 1000))
    cand.sort(key=lambda x: -x[1])
    for s, v, _ in cand[:n]:
        print(f"  {s:<16} {v/1e6:>12.1f} млн единиц / 24ч", file=sys.stderr)
    return [(s, lm) for s, _, lm in cand[:n]]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=100)
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--out", default=HIST_DIR)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # Окно заканчивается последним ЗАКРЫТЫМ месяцем: месячных дампов
    # фандинга за текущий месяц ещё нет, дневных Binance не публикует.
    # Иначе моменты текущего месяца остались бы без фандинга — а
    # build_ticker их теперь честно пропускает, и мы бы просто потеряли
    # часть окна, не понимая почему.
    today = datetime.now(timezone.utc)
    end = today.replace(day=1, hour=0, minute=0, second=0,
                        microsecond=0) - timedelta(seconds=1)
    start = end - timedelta(days=args.days)
    print(f"окно: {start.date()} → {end.date()} (последний закрытый месяц)",
          file=sys.stderr)

    async with aiohttp.ClientSession(trust_env=True) as s:
        picked = await universe(s, args.symbols)
        if not picked:
            print("не удалось собрать вселенную", file=sys.stderr)
            return 1
        ok = []
        for i, (sym, lm) in enumerate(picked, 1):
            k4, k1, k15, oi, fund = await asyncio.gather(
                klines(s, sym, "4h", start, end),
                klines(s, sym, "1h", start, end),
                klines(s, sym, "15m", start, end),
                open_interest(s, sym, start, end),
                funding(s, sym, start, end),
            )
            if not k4 or not k15 or not fund or not oi:
                print(f"[{i}/{len(picked)}] {sym}: неполные данные "
                      f"(4h={len(k4)} 15m={len(k15)} oi={len(oi)} "
                      f"fund={len(fund)}) — пропуск", file=sys.stderr)
                continue
            with open(os.path.join(args.out, f"{sym}.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"symbol": sym, "launch_ms": lm, "k4": k4, "k1": k1,
                           "k15": k15, "oi": oi, "funding": fund}, f)
            ok.append(sym)
            print(f"[{i}/{len(picked)}] {sym}: 4h={len(k4)} 1h={len(k1)} "
                  f"15m={len(k15)} oi={len(oi)} fund={len(fund)}", file=sys.stderr)
        with open(os.path.join(args.out, "_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"days": args.days, "symbols": ok,
                       "start_ms": int(start.timestamp() * 1000),
                       "end_ms": int(end.timestamp() * 1000),
                       "source": "binance USDT-M public dumps (НЕ Bybit — "
                                 "см. шапку tools/fetch_history_binance.py)"},
                      f, indent=2)
    print(f"\nГотово: {len(ok)} символов", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
