"""Историческая вселенная монет — БЕЗ выжившего смещения.

Зачем отдельный загрузчик. tools/fetch_history_binance.py берёт вселенную по
СЕГОДНЯШНЕМУ объёму среди монет, торгуемых сейчас. Для замера событий на
одном символе это терпимо, для КРОСС-СЕКЦИОННОГО портфеля — нет: там весь
смысл в том, какие монеты были сильны и слабы ОТНОСИТЕЛЬНО ДРУГ ДРУГА на
каждую дату. Монеты, умершие за период, в такой вселенной отсутствуют, и
результат меряет не моментум, а свойство «дожить до сегодня».

Здесь вселенная строится ЗАНОВО на каждую дату ребалансировки: берутся
монеты, ликвидные НА ТУ ДАТУ, включая делистнутые позже. Проверено, что
дампы Binance отдают и мёртвые символы (SRMUSDT, FTTUSDT доступны).

Что качаем:
  * список ВСЕХ символов USDT-M за всю историю — через листинг S3;
  * дневные свечи (1d) помесячно: цена закрытия и оборот в USDT. Для
    недельной ребалансировки с окном 14 дней 4-часовая детализация не
    нужна, а объём запросов вчетверо меньше;
  * ставки фандинга помесячно: без них портфель меряет валовую доходность.

Запуск:
    python3 -m tools.fetch_universe_binance --years 3
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

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DUMPS = "https://data.binance.vision/data/futures/um/monthly"
_LIST = ("https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
         "?delimiter=/&prefix=data/futures/um/monthly/klines/")
_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "universe")

# Частота ограничивается ГЛОБАЛЬНО (см. tools/fetch_history_binance.py):
# пауза внутри семафора дала бы частоту «слоты / пауза», а не «1 / пауза».
_SEM = asyncio.Semaphore(6)
_MIN_GAP = 1.0 / 10.0
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


async def _get(session, url: str, as_text: bool = False) -> Any:
    """None означает «нет такого файла» — это норма, а не ошибка."""
    status: Any = None
    for delay in (2, 5, 12):
        await _pace()
        async with _SEM:
            try:
                async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=90)) as r:
                    if r.status == 404:
                        return None
                    if r.status == 200:
                        return await (r.text() if as_text else r.read())
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
    # Заголовок есть не во всех файлах: слепой пропуск первой строки терял бы
    # свечу. Отбрасываем только если первое поле не число.
    if out and out[0] and not out[0][0].replace(".", "", 1).isdigit():
        out = out[1:]
    return [r for r in out if r]


def _months(a: datetime, b: datetime) -> List[str]:
    out, cur = [], a.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= b:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=8)).replace(day=1)
    return out


async def all_symbols(session) -> List[str]:
    """ВСЕ символы USDT-M за всю историю, включая делистнутые.

    Именно это убирает выжившее смещение: списки «торгуется сейчас» дают
    вселенную, отобранную по факту выживания.

    Отбрасываются срочные контракты с датой поставки (BTCUSDT_240329) и
    пары не к USDT: смешивать USDC-пары с USDT-парами значило бы считать
    один и тот же актив дважды.
    """
    out: List[str] = []
    token = ""
    seen_markers = set()
    while True:
        url = _LIST + (f"&marker={token}" if token else "")
        body = await _get(session, url, as_text=True)
        if not body:
            break
        page = re.findall(
            r"<Prefix>data/futures/um/monthly/klines/([^/]+)/</Prefix>", body)
        if not page:
            break
        out.extend(page)
        if not re.search(r"<IsTruncated>true</IsTruncated>", body):
            break
        # Маркер — ПОЛНЫЙ ключ, а не имя символа. С коротким именем S3
        # начинал выдачу заново, и цикл не завершался никогда.
        nm = re.search(r"<NextMarker>([^<]+)</NextMarker>", body)
        token = nm.group(1) if nm else (
            "data/futures/um/monthly/klines/" + page[-1] + "/")
        # Страховка от зацикливания: если маркер повторился, выходим, а не
        # крутимся молча. Тихий бесконечный цикл уже стоил одного прогона.
        if token in seen_markers:
            print("листинг вернул повторный маркер — обрываю", file=sys.stderr)
            break
        seen_markers.add(token)
    keep = [s for s in out if s.endswith("USDT") and "_" not in s]
    return sorted(set(keep))


async def daily_series(session, sym: str, months: List[str]) -> List[Dict]:
    """Дневные свечи: время открытия, закрытие, оборот в USDT."""
    async def one(m: str):
        blob = await _get(
            session, f"{_DUMPS}/klines/{sym}/1d/{sym}-1d-{m}.zip")
        return _rows(blob) if blob else []
    got = await asyncio.gather(*[one(m) for m in months])
    seen: Dict[int, Dict] = {}
    for rows in got:
        for r in rows:
            try:
                ts = int(float(r[0]))
                # Оборот в котируемой валюте — 8-я колонка у фьючерсных
                # дампов. Ранжировать ликвидность по объёму в БАЗОВОЙ монете
                # нельзя: он не сравним между монетами разной цены.
                seen[ts] = {"ts": ts, "close": float(r[4]),
                            "quote": float(r[7])}
            except (ValueError, IndexError):
                continue
    return [seen[k] for k in sorted(seen)]


async def funding_series(session, sym: str, months: List[str]) -> List[Dict]:
    async def one(m: str):
        blob = await _get(
            session,
            f"{_DUMPS}/fundingRate/{sym}/{sym}-fundingRate-{m}.zip")
        return _rows(blob) if blob else []
    got = await asyncio.gather(*[one(m) for m in months])
    seen: Dict[int, Dict] = {}
    for rows in got:
        for r in rows:
            try:
                seen[int(float(r[0]))] = {"ts": int(float(r[0])),
                                          "rate": float(r[2])}
            except (ValueError, IndexError):
                continue
    return [seen[k] for k in sorted(seen)]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--out", default=_OUT)
    ap.add_argument("--limit-symbols", type=int, default=0,
                    help="ограничить число символов (для проверки механики)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    # Окно кончается на последнем ПОЛНОМ месяце: дампы за текущий месяц
    # неполны, а фандинг публикуется только помесячно.
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=int(args.years * 365))
    months = _months(start, end - timedelta(days=1))
    os.makedirs(args.out, exist_ok=True)

    async with aiohttp.ClientSession(trust_env=True) as s:
        syms = await all_symbols(s)
        if args.limit_symbols:
            syms = syms[:args.limit_symbols]
        print(f"символов всего (включая делистнутые): {len(syms)}")
        print(f"окно: {months[0]} .. {months[-1]}  ({len(months)} мес.)")
        kept: List[str] = []
        for i, sym in enumerate(syms, 1):
            path = os.path.join(args.out, f"{sym}.json")
            if os.path.isfile(path):
                kept.append(sym)
                continue
            k = await daily_series(s, sym, months)
            if len(k) < 60:
                # меньше двух месяцев данных в окне — ранжировать нечем
                print(f"[{i}/{len(syms)}] {sym}: {len(k)} дн. — пропуск")
                continue
            f = await funding_series(s, sym, months)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"symbol": sym, "daily": k, "funding": f}, fh)
            kept.append(sym)
            print(f"[{i}/{len(syms)}] {sym}: {len(k)} дн., {len(f)} выплат")

    meta = {
        "source": "binance USDT-M public dumps, ВСЕ символы включая делистнутые",
        "symbols": kept,
        "months": months,
        "start_ms": int(start.timestamp() * 1000),
        "end_ms": int(end.timestamp() * 1000),
        "note": ("вселенная НЕ отфильтрована по выживанию: содержит монеты, "
                 "делистнутые в течение окна"),
    }
    with open(os.path.join(args.out, "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    print(f"\nготово: {len(kept)} символов в {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
